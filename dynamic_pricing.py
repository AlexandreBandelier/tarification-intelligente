import os
import sys
import json
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from woocommerce import API

# --- 0. PARAMÈTRES DE SÉCURITÉ ---
SEUIL_VARIATION_MAX = 0.25 


def to_float(val, default=0.0):
    """Convertit une valeur (texte avec virgule ou nombre) en float proprement."""
    if pd.isna(val) or val is None or str(val).strip() == "":
        return float(default)
    try:
        clean_val = str(val).replace(",", ".").replace("\xa0", "").strip()
        return float(clean_val)
    except (ValueError, TypeError):
        return float(default)


# --- 1. CONNEXION ET LECTURE GOOGLE SHEETS VIA API ---
print("Étape 1 : Connexion à Google Sheets...")

google_credentials_json = os.environ.get("GOOGLE_CREDENTIALS")
drive_id_matrice = os.environ.get("DRIVE_ID_MATRICE") or os.environ.get("DRIVE_ID_PROD")

if not google_credentials_json or not drive_id_matrice:
    print("Erreur : Les secrets GOOGLE_CREDENTIALS ou DRIVE_ID_MATRICE sont manquants.")
    sys.exit(1)

# Authentification gspread
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds_dict = json.loads(google_credentials_json)
credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(credentials)

# Ouverture de la feuille de calcul
try:
    sh = gc.open_by_key(drive_id_matrice)
    worksheet = sh.sheet1
    data = worksheet.get_all_records()
    df_matrice = pd.DataFrame(data)
    print("Matrice de prix chargée avec succès depuis Google Sheets !")
except Exception as e:
    print(f"Erreur lors de l'accès au Google Sheet : {e}")
    sys.exit(1)


# --- 2. CONFIGURATION DE L'API WOOCOMMERCE ---
woo_url = os.environ.get("URL_SITE") or os.environ.get("WOOCOMMERCE_URL")
woo_ck = os.environ.get("WOO_CONSUMER_KEY") or os.environ.get("WC_CONSUMER_KEY")
woo_cs = os.environ.get("WOO_CONSUMER_SECRET") or os.environ.get("WC_CONSUMER_SECRET")

if not woo_url or not woo_ck or not woo_cs:
    print("Erreur : Secrets WooCommerce manquants dans les variables d'environnement.")
    sys.exit(1)

wcapi = API(
    url=woo_url,
    consumer_key=woo_ck,
    consumer_secret=woo_cs,
    version="wc/v3",
    timeout=60,
)


# --- 3. MOTEUR ALGORITHMIQUE DE TARIFICATION DYNAMIQUE ---
def calculer_prix_dynamique(row):
    prix_standard = to_float(row.get("Prix_Standard_TTC"))
    prix_plancher = to_float(row.get("Prix_Plancher_TTC"))

    if prix_standard <= 0:
        return to_float(row.get("Dernier_Prix_Applique")), "PRIX_STANDARD_INVALID"

    # 1. Protection Disjoncteur
    if to_float(row.get("Nb_Baisses_48h")) >= 3:
        return round(prix_standard, 2), "DISJONCTEUR_ACTIF"

    # 2. Détermination du concurrent cible
    prix_comp, port_comp = None, 0.0
    
    dispo_1 = str(row.get("Dispo_Concurrent_1") or "").strip().lower()
    dispo_2 = str(row.get("Dispo_Concurrent_2") or "").strip().lower()

    if dispo_1 in ["en stock", "in stock", "1", "true", "oui"]:
        prix_comp = to_float(row.get("Prix_Concurrent_1"))
        port_comp = to_float(row.get("Port_Concurrent_1"))
    elif dispo_2 in ["en stock", "in stock", "1", "true", "oui"]:
        prix_comp = to_float(row.get("Prix_Concurrent_2"))
        port_comp = to_float(row.get("Port_Concurrent_2"))

    # Cas de Rupture Globale des concurrents
    if prix_comp is None or prix_comp <= 0:
        if str(row.get("Statut_Stock")).strip().lower() == "stock_faible":
            nouveau_prix = round(prix_standard * 1.05, 2)
            statut = "REPLI_MONOPOLE_STOCK_FAIBLE"
        else:
            nouveau_prix = prix_standard
            statut = "REPLI_MONOPOLE_STANDARD"
    else:
        # 3. Calcul du Prix Total Cible
        cout_global_concurrent = prix_comp + port_comp
        frais_port_notre_site = to_float(row.get("Frais_Port_Reels_Notre_Site"))
        prix_fr_brut = to_float(row.get("Prix_FR_Brut"))

        if str(row.get("Zone_Geo")).strip().upper() == "NORD":
            prix_cible = (cout_global_concurrent * 0.90) - frais_port_notre_site
            prix_cible = max(prix_cible, prix_fr_brut)
        else:
            prix_cible = cout_global_concurrent - frais_port_notre_site
            prix_cible = max(prix_cible, prix_plancher)

        # 4. Protection Stock Faible
        if (
            str(row.get("Statut_Stock")).strip().lower() == "stock_faible"
            and to_float(row.get("Ventes_30_Jours")) > 5
        ):
            return max(
                prix_standard, to_float(row.get("Dernier_Prix_Applique"))
            ), "GEL_STOCK_FAIBLE"

        # 5. Application du Corridor Asymétrique
        if prix_cible > prix_standard:
            nouveau_prix = prix_standard + ((prix_cible - prix_standard) * 0.66)
        else:
            delta = prix_cible - prix_standard
            if delta >= -0.10 * prix_standard:
                coeff = (
                    0.50
                    if str(row.get("Is_Bestseller")).strip().lower() in ["oui", "yes", "true", "1"]
                    else 1.0
                )
                nouveau_prix = prix_standard + (delta * coeff)
            else:
                nouveau_prix = prix_standard + (delta * 0.33)

        if str(row.get("Statut_Stock")).strip().lower() == "surstock":
            nouveau_prix *= 0.95

        statut = "OK"

    # 6. Prix Plancher Inviolable
    prix_final = max(nouveau_prix, prix_plancher)

    # Arrondi
    if str(row.get("Zone_Geo")).strip().upper() == "NORD":
        prix_final = round(prix_final) - 0.01
    else:
        prix_final = round(prix_final, 2)

    # 7. Barrière de Sécurité Écart Max
    variation = abs(prix_final - prix_standard) / prix_standard
    if variation > SEUIL_VARIATION_MAX:
        dernier_prix = to_float(row.get("Dernier_Prix_Applique"), prix_standard)
        return (
            dernier_prix,
            f"BLOCAGE_VARIATION_EXCESSIVE_({round(variation*100)}%)",
        )

    return prix_final, statut


# --- 4. TRAITEMENT ET SYNCHRONISATION ---
def mettre_a_jour_prix():
    batch_data = []
    produits_en_alerte = []

    print("Récupération des produits depuis WooCommerce...")
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

    if "Code_Site" not in df_matrice.columns:
        df_matrice["Code_Site"] = "FR"

    df_matrice["Code_Site"] = (
        df_matrice["Code_Site"].fillna("FR").astype(str).str.upper().str.strip()
    )
    df_matrice["SKU_Clean"] = df_matrice["SKU"].astype(str).str.strip()
    df_matrice["Clave_Unique"] = (
        df_matrice["SKU_Clean"] + "_" + df_matrice["Code_Site"]
    )

    code_site_courant = "FR"

    for produit in tous_les_produits:
        sku = str(produit.get("sku", "")).strip()
        if not sku:
            continue

        prix_actuel = to_float(produit.get("regular_price"))
        cle_recherche = sku + "_" + code_site_courant

        lignes = df_matrice[df_matrice["Clave_Unique"] == cle_recherche]
        if lignes.empty:
            continue

        row = lignes.iloc[0].to_dict()
        row["Dernier_Prix_Applique"] = prix_actuel

        nouveau_prix, statut_calcul = calculer_prix_dynamique(row)

        df_matrice.loc[
            df_matrice["Clave_Unique"] == cle_recherche, "Dernier_Prix_Calcule"
        ] = nouveau_prix
        df_matrice.loc[
            df_matrice["Clave_Unique"] == cle_recherche, "Statut_Dernier_Calcul"
        ] = statut_calcul

        if "BLOCAGE_VARIATION_EXCESSIVE" in statut_calcul:
            produits_en_alerte.append(
                {
                    "SKU": sku,
                    "Nom": produit.get("name"),
                    "Prix_Actuel": prix_actuel,
                    "Statut": statut_calcul,
                }
            )

        if (
            nouveau_prix > 0
            and nouveau_prix != prix_actuel
            and "BLOCAGE" not in statut_calcul
        ):
            payload_produit = {
                "id": produit["id"],
                "regular_price": str(nouveau_prix),
            }
            batch_data.append(payload_produit)

            if nouveau_prix < prix_actuel:
                current_baisses = (
                    df_matrice.loc[
                        df_matrice["Clave_Unique"] == cle_recherche,
                        "Nb_Baisses_48h",
                    ]
                    .fillna(0)
                    .astype(int)
                )
                df_matrice.loc[
                    df_matrice["Clave_Unique"] == cle_recherche,
                    "Nb_Baisses_48h",
                ] = (current_baisses + 1)

    # Mise à jour WooCommerce
    if batch_data:
        print(f"Envoi des modifications pour {len(batch_data)} produits sur WooCommerce...")
        for i in range(0, len(batch_data), 100):
            paquet = batch_data[i : i + 100]
            wcapi.post("products/batch", {"update": paquet})
        print(f"{len(batch_data)} prix mis à jour avec succès sur WooCommerce.")
    else:
        print("Tous les prix WooCommerce autorisés sont déjà à jour.")

    if produits_en_alerte:
        print("\n--- ALERTE : PRODUITS NÉCESSITANT UNE VÉRIFICATION MANUELLE ---")
        for p in produits_en_alerte:
            print(
                f"  • SKU {p['SKU']} ({p['Nom']}) | Prix actuel : {p['Prix_Actuel']}€ | Motif : {p['Statut']}"
            )
        print("--------------------------------------------------------------------\n")

    # Nettoyage des colonnes de calcul temporaires
    for col_temp in ["SKU_Clean", "Clave_Unique"]:
        if col_temp in df_matrice.columns:
            df_matrice.drop(columns=[col_temp], inplace=True)

    # --- 5. ÉCRITURE DANS LE GOOGLE SHEET EN LIGNE ---
    print("Mise à jour en cours du Google Sheet en ligne...")
    try:
        # Nettoyage des valeurs NaN pour éviter les erreurs d'envoi JSON
        df_clean = df_matrice.fillna("")
        
        # Envoi des données complètes vers le Google Sheet
        worksheet.clear()
        worksheet.update([df_clean.columns.values.tolist()] + df_clean.values.tolist())
        print("Le Google Sheet a été mis à jour directement en ligne avec succès !")
    except Exception as e:
        print(f"Erreur lors de l'écriture dans le Google Sheet : {e}")


# --- 6. POINT D'ENTRÉE ---
if __name__ == "__main__":
    print("Démarrage du pipeline Dynamic Pricing WooCommerce...")
    mettre_a_jour_prix()
