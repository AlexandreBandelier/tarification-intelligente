```python
import os
import pandas as pd
from woocommerce import API
from dotenv import load_dotenv

# Chargement des variables d'environnement (.env en local, Render en production)
load_dotenv()

# 1. CONFIGURATION DE L'API WOOCOMMERCE
wcapi = API(
    url=os.getenv("WOOCOMMERCE_URL"),
    consumer_key=os.getenv("WC_CONSUMER_KEY"),
    consumer_secret=os.getenv("WC_CONSUMER_SECRET"),
    version="wc/v3"
)


# 2. MOTEUR ALGORTIHMIQUE DE TARIFICATION DYNAMIQUE
def calculer_prix_dynamique(row):
    """
    Applique la matrice complète : Disjoncteur, Ruptures, Zones Geo,
    Protection Stock Faible, Corridor Asymétrique et Surstock.
    """
    # 1. Protection Disjoncteur
    if row.get('Nb_Baisses_48h', 0) >= 3:
        return round(row['Prix_Standard_TTC'] * 1.05, 2), "DISJONCTEUR_ACTIF"

    # 2. Détermination du concurrent cible (Gestion des ruptures)
    prix_comp, port_comp = None, None
    if row.get('Dispo_Concurrent_1') == "En Stock":
        prix_comp, port_comp = row.get('Prix_Concurrent_1'), row.get('Port_Concurrent_1')
    elif row.get('Dispo_Concurrent_2') == "En Stock":
        prix_comp, port_comp = row.get('Prix_Concurrent_2'), row.get('Port_Concurrent_2')
    
    # Cas de Rupture Globale des concurrents
    if prix_comp is None:
        if row.get('Statut_Stock') == "Stock_Faible":
            return round(row['Prix_Standard_TTC'] * 1.05, 2), "REPLI_MONOPOLE_STOCK_FAIBLE"
        return row['Prix_Standard_TTC'], "REPLI_MONOPOLE_STANDARD"

    # 3. Calcul du Prix Total Cible (Zones Nordiques vs Sud)
    if row.get('Zone_Geo') == "Nord":
        cout_global_concurrent = prix_comp + port_comp
        prix_cible = (cout_global_concurrent * 0.90) - row.get('Frais_Port_Reels_Notre_Site', 0)
        prix_cible = max(prix_cible, row.get('Prix_FR_Brut', 0)) # Plancher Flottant
    else:
        prix_cible = row['Prix_Standard_TTC']

    # 4. Protection Stock Faible
    if row.get('Statut_Stock') == "Stock_Faible" and row.get('Ventes_30_Jours', 0) > 5:
        return max(row['Prix_Standard_TTC'], row.get('Dernier_Prix_Applique', 0)), "GEL_STOCK_FAIBLE"

    # 5. Application du Corridor Asymétrique
    prix_actuel = row.get('Dernier_Prix_Applique', 0)
    prix_standard = row['Prix_Standard_TTC']
    prix_plancher = row['Prix_Plancher_TTC']

    if prix_cible > prix_standard:
        nouveau_prix = prix_standard + ((prix_cible - prix_standard) * 0.66)
    else:
        delta = prix_cible - prix_standard
        if delta >= -0.10 * prix_standard:
            coeff = 0.50 if row.get('Is_Bestseller') == "Oui" else 1.0
            nouveau_prix = prix_standard + (delta * coeff)
        else:
            nouveau_prix = prix_standard + (delta * 0.33)

    if row.get('Statut_Stock') == "Surstock":
        nouveau_prix *= 0.95

    # 6. Sécurité Absolue : Prix Plancher Inviolable
    prix_final = max(nouveau_prix, prix_plancher)
    
    # Arrondi
    if row.get('Zone_Geo') == "Nord":
        prix_final = round(prix_final) - 0.01
    else:
        prix_final = round(prix_final, 2)

    return prix_final, "OK"


# 3. FONCTION DE MISE À JOUR BATCH WOOCOMMERCE
def mettre_a_jour_prix_woocommerce(df_matrice):
    batch_data = []
    
    # Récupération de TOUS les produits WooCommerce (Pagination)
    tous_les_produits = []
    page = 1
    while True:
        res = wcapi.get("products", params={"per_page": 100, "page": page}).json()
        if not res or (isinstance(res, dict) and "code" in res):
            break
        tous_les_produits.extend(res)
        if len(res) < 100:
            break
        page += 1

    print(f"{len(tous_les_produits)} produits récupérés depuis WooCommerce.")

    # Évaluation par produit
    for produit in tous_les_produits:
        sku = produit.get("sku")
        prix_actuel = float(produit.get("regular_price") or 0)
        
        lignes = df_matrice[df_matrice['SKU'] == sku]
        if lignes.empty:
            continue
            
        row = lignes.iloc[0].to_dict()
        row['Dernier_Prix_Applique'] = prix_actuel

        # Calcul du nouveau prix
        nouveau_prix, statut_calcul = calculer_prix_dynamique(row)

        df_matrice.loc[df_matrice['SKU'] == sku, 'Dernier_Prix_Calcule'] = nouveau_prix
        df_matrice.loc[df_matrice['SKU'] == sku, 'Statut_Dernier_Calcul'] = statut_calcul

        if nouveau_prix != prix_actuel:
            payload_produit = {
                "id": produit["id"],
                "regular_price": str(nouveau_prix)
            }
            batch_data.append(payload_produit)

            if nouveau_prix < prix_actuel:
                df_matrice.loc[df_matrice['SKU'] == sku, 'Nb_Baisses_48h'] = (
                    df_matrice.loc[df_matrice['SKU'] == sku, 'Nb_Baisses_48h'].fillna(0) + 1
                )

    # Envoi par paquets de 100
    if batch_data:
        for i in range(0, len(batch_data), 100):
            paquet = batch_data[i:i + 100]
            wcapi.post("products/batch", {"update": paquet})
        print(f"{len(batch_data)} prix mis à jour avec succès sur WooCommerce.")
    else:
        print("Tous les prix WooCommerce sont déjà parfaitement optimisés.")

    return df_matrice


# 4. POINT D'ENTRÉE DU SCRIPT (EXÉCUTION)
if __name__ == "__main__":
    print("Démarrage du pipeline Dynamic Pricing...")
    
    # 1. Charger le fichier Google Drive / CSV Matrice
    # (On remplace 'matrice_prix_marges.csv' par le chemin du fichier ou l'API Google Drive)
    chemin_matrice = "matrice_prix_marges.csv"
    
    if os.path.exists(chemin_matrice):
        df_matrice = pd.read_csv(chemin_matrice)
        
        # 2. Exécuter la mise à jour
        df_resultat = mettre_a_jour_prix_woocommerce(df_matrice)
        
        # 3. Sauvegarder l'état mis à jour (Compteurs disjoncteur, statuts)
        df_resultat.to_csv(chemin_matrice, index=False)
        print("Fichier Matrice mis à jour et sauvegardé.")
    else:
        print(f"Erreur : Le fichier {chemin_matrice} est introuvable.")

```
