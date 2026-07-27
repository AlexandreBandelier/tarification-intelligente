import os
import sys
import pandas as pd
import gdown
from woocommerce import API

# --- 1. CONFIGURATION DES CHEMINS ET DRIVE ---
dossier_actuel = os.path.dirname(os.path.abspath(__file__))
chemin_matrice = os.path.join(dossier_actuel, "matrice_prix_marges.csv")

drive_id_matrice = os.environ.get("DRIVE_ID_MATRICE") or os.environ.get("DRIVE_ID_PROD")

if drive_id_matrice:
    print("Étape 1 : Téléchargement de la matrice de prix depuis Google Drive...")
    url_csv = f"https://docs.google.com/spreadsheets/d/{drive_id_matrice}/export?format=csv"
    try:
        gdown.download(url_csv, chemin_matrice, quiet=False)
    except Exception:
        url_standard = f"https://drive.google.com/uc?id={drive_id_matrice}"
        gdown.download(url_standard, chemin_matrice, quiet=False)

# --- 2. CONFIGURATION DE L'API WOOCOMMERCE ---
woo_url = os.environ.get("WOOCOMMERCE_URL") or os.environ.get("URL_SITE")
woo_ck = os.environ.get("WOO_CONSUMER_KEY") or os.environ.get("WC_CONSUMER_KEY")
woo_cs = os.environ.get("WOO_CONSUMER_SECRET") or os.environ.get("WC_CONSUMER_SECRET")

if not woo_url or not woo_ck or not woo_cs:
    print("Erreur : Secrets WooCommerce (URL, Key, Secret) manquants dans les variables d'environnement.")
    sys.exit(1)

wcapi = API(
    url=woo_url,
    consumer_key=woo_ck,
    consumer_secret=woo_cs,
    version="wc/v3",
    timeout=60
)

# --- 3. MOTEUR ALGORTIHMIQUE DE TARIFICATION DYNAMIQUE ---
def calculer_prix_dynamique(row):
    """
    Applique la matrice complète : Disjoncteur, Ruptures, Zones Geo,
    Protection Stock Faible, Corridor Asymétrique et Surstock.
    """
    prix_standard = float(row.get('Prix_Standard_TTC', 0) or 0)
    prix_plancher = float(row.get('Prix_Plancher_TTC', 0) or 0)
    
    if prix_standard <= 0:
        return float(row.get('Dernier_Prix_Applique', 0)), "PRIX_STANDARD_INVALID"

    # 1. Protection Disjoncteur
    if float(row.get('Nb_Baisses_48h', 0) or 0) >= 3:
        return round(prix_standard * 1.05, 2), "DISJONCTEUR_ACTIF"

    # 2. Détermination du concurrent cible (Gestion des ruptures)
    prix_comp, port_comp = None, None
    if str(row.get('Dispo_Concurrent_1')).strip().lower() in ["en stock", "in stock", "1", "true"]:
        prix_comp = float(row.get('Prix_Concurrent_1', 0) or 0)
        port_comp = float(row.get('Port_Concurrent_1', 0) or 0)
    elif str(row.get('Dispo_Concurrent_2')).strip().lower() in ["en stock", "in stock", "1", "true"]:
        prix_comp = float(row.get('Prix_Concurrent_2', 0) or 0)
        port_comp = float(row.get('Port_Concurrent_2', 0) or 0)
    
    # Cas de Rupture Globale des concurrents
    if prix_comp is None or prix_comp <= 0:
        if str(row.get('Statut_Stock')).strip().lower() == "stock_faible":
            return round(prix_standard * 1.05, 2), "REPLI_MONOPOLE_STOCK_FAIBLE"
        return prix_standard, "REPLI_MONOPOLE_STANDARD"

    # 3. Calcul du Prix Total Cible (Zones Nordiques vs Sud)
    if str(row.get('Zone_Geo')).strip().upper() == "NORD":
        cout_global_concurrent = prix_comp + port_comp
        prix_cible = (cout_global_concurrent * 0.90) - float(row.get('Frais_Port_Reels_Notre_Site', 0) or 0)
        prix_cible = max(prix_cible, float(row.get('Prix_FR_Brut', 0) or 0)) # Plancher Flottant
    else:
        prix_cible = prix_standard

    # 4. Protection Stock Faible
    if str(row.get('Statut_Stock')).strip().lower() == "stock_faible" and float(row.get('Ventes_30_Jours', 0) or 0) > 5:
        return max(prix_standard, float(row.get('Dernier_Prix_Applique', 0) or 0)), "GEL_STOCK_FAIBLE"

    # 5. Application du Corridor Asymétrique
    if prix_cible > prix_standard:
        nouveau_prix = prix_standard + ((prix_cible - prix_standard) * 0.66)
    else:
        delta = prix_cible - prix_standard
        if delta >= -0.10 * prix_standard:
            coeff = 0.50 if str(row.get('Is_Bestseller')).strip().lower() in ["oui", "yes", "true", "1"] else 1.0
            nouveau_prix = prix_standard + (delta * coeff)
        else:
            nouveau_prix = prix_standard + (delta * 0.33)

    if str(row.get('Statut_Stock')).strip().lower() == "surstock":
        nouveau_prix *= 0.95

    # 6. Sécurité Absolue : Prix Plancher Inviolable
    prix_final = max(nouveau_prix, prix_plancher)
    
    # Arrondi
    if str(row.get('Zone_Geo')).strip().upper() == "NORD":
        prix_final = round(prix_final) - 0.01
    else:
        prix_final = round(prix_final, 2)

    return prix_final, "OK"

# --- 4. MISE À JOUR WOOCOMMERCE ---
def mettre_a_jour_prix_woocommerce(df_matrice):
    batch_data = []
    
    print("Récupération des produits depuis WooCommerce via l'API REST...")
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

    df_matrice['SKU_Clean'] = df_matrice['SKU'].astype(str).str.strip()

    # Évaluation par produit WooCommerce
    for produit in tous_les_produits:
        sku = str(produit.get("sku", "")).strip()
        if not sku:
            continue
            
        prix_actuel = float(produit.get("regular_price") or 0)
        
        lignes = df_matrice[df_matrice['SKU_Clean'] == sku]
        if lignes.empty:
            continue
            
        row = lignes.iloc[0].to_dict()
        row['Dernier_Prix_Applique'] = prix_actuel

        # Calcul du nouveau prix
        nouveau_prix, statut_calcul = calculer_prix_dynamique(row)

        df_matrice.loc[df_matrice['SKU_Clean'] == sku, 'Dernier_Prix_Calcule'] = nouveau_prix
        df_matrice.loc[df_matrice['SKU_Clean'] == sku, 'Statut_Dernier_Calcul'] = statut_calcul

        if nouveau_prix > 0 and nouveau_prix != prix_actuel:
            payload_produit = {
                "id": produit["id"],
                "regular_price": str(nouveau_prix)
            }
            batch_data.append(payload_produit)

            if nouveau_prix < prix_actuel:
                current_baisses = df_matrice.loc[df_matrice['SKU_Clean'] == sku, 'Nb_Baisses_48h'].fillna(0).astype(int)
                df_matrice.loc[df_matrice['SKU_Clean'] == sku, 'Nb_Baisses_48h'] = current_baisses + 1

    # Envoi par paquets de 100
    if batch_data:
        print(f"Envoi des modifications pour {len(batch_data)} produits...")
        for i in range(0, len(batch_data), 100):
            paquet = batch_data[i:i + 100]
            resp = wcapi.post("products/batch", {"update": paquet})
            print(f"  -> Paquet {i//100 + 1} envoyé sur WooCommerce.")
        print(f"{len(batch_data)} prix mis à jour avec succès sur WooCommerce.")
    else:
        print("Tous les prix WooCommerce sont déjà parfaitement à jour.")

    if 'SKU_Clean' in df_matrice.columns:
        df_matrice.drop(columns=['SKU_Clean'], inplace=True)

    return df_matrice

# --- 5. POINT D'ENTRÉE ---
if __name__ == "__main__":
    print("Démarrage du pipeline Dynamic Pricing WooCommerce...")
    
    if os.path.exists(chemin_matrice):
        try:
            df_matrice = pd.read_csv(chemin_matrice, low_memory=False)
        except Exception:
            df_matrice = pd.read_excel(chemin_matrice)

        df_resultat = mettre_a_jour_prix_woocommerce(df_matrice)
        df_resultat.to_csv(chemin_matrice, index=False, encoding='utf-8')
        print("Fichier Matrice mis à jour et sauvegardé localement.")
    else:
        print(f"Erreur : Le fichier {chemin_matrice} est introuvable.")
        sys.exit(1)
