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
    """Convertit une valeur (ex: '4,8', '4,80 €', 4.8) en float de façon robuste."""
    if pd.isna(val) or val is None:
        return float(default)
    
    if isinstance(val, (int, float)):
        return float(val)
    
    s = str(val).strip()
    if not s:
        return float(default)
    
    s = s.replace("€", "").replace("\xa0", "").replace(" ", "").strip()
    s = s.replace(",", ".")
    
    try:
        return float(s)
    except (ValueError, TypeError):
        return float(default)


# --- 1. CONNEXION ET LECTURE GOOGLE SHEETS VIA API ---
print("Étape 1 : Connexion à Google Sheets...")

google_credentials_json = os.environ.get("GOOGLE_CREDENTIALS")
drive_id_matrice = os.environ.get("DRIVE_ID_MATRICE") or os.environ.get("DRIVE_ID_PROD")

if not google_credentials_json or not drive_id_matrice:
    print("Erreur : Les secrets GOOGLE_CREDENTIALS ou DRIVE_ID_MATRICE sont manquants.")
    sys.exit(1)

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds_dict = json.loads(google_credentials_json)
credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(credentials)

try:
    sh = gc.open_by_key(drive_id_matrice)
    worksheet = sh.sheet1
    data = worksheet.get_all_records()
    df_matrice = pd.DataFrame(data)
    print("✅ Matrice de prix chargée avec succès depuis Google Sheets !")
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
    dernier_prix = to_float(row.get("Dernier_Prix_Applique"))

    if prix_standard <= 0:
        return dernier_prix, "PRIX_STANDARD_INVALID"

    # 1. Protection Disjoncteur
    if to_float(row.get("Nb_Baisses_48h")) >= 3:
        return round(prix_standard, 2), "DISJONCTEUR_ACTIF"

    # 2. Détermination du concurrent cible
    # Le port concurrent est assumé égal à notre port
    frais_port_notre_site = to_float(row.get("Frais_Port_Reels_Notre_Site"))
    prix_comp = None
    is_monopole = False
    
    dispo_1 = str(row.get("Dispo_Concurrent_1") or "").strip().lower()
    dispo_2 = str(row.get("Dispo_Concurrent_2") or "").strip().lower()

    if dispo_1 in ["en stock", "in stock", "1", "true", "oui"]:
        prix_comp = to_float(row.get("Prix_Concurrent_1"))
    elif dispo_2 in ["en stock", "in stock", "1", "true", "oui"]:
        prix_comp = to_float(row.get("Prix_Concurrent_2"))

    # Cas de Rupture Globale des concurrents
    if prix_comp is None or prix_comp <= 0:
        is_monopole = True
        
        # Repli sur le prix standard UNIQUEMENT SI on était en dessous
        if dernier_prix < prix_standard:
            if str(row.get("Statut_Stock")).strip().lower() == "stock_faible":
                nouveau_prix = round(prix_standard * 1.05, 2)
                statut = "REPLI_MONOPOLE_STOCK_FAIBLE"
            else:
                nouveau_prix = prix_standard
                statut = "REPLI_MONOPOLE_STANDARD"
            
            return max(nouveau_prix, prix_plancher), statut
        else:
            # SINON, calcul normal en récupérant le prix concurrent même hors stock
            prix_comp = to_float(row.get("Prix_Concurrent_1"))
            if prix_comp <= 0:
                prix_comp = to_float(row.get("Prix_Concurrent_2"))
            if prix_comp <= 0:
                prix_comp = dernier_prix

    # 3. Calcul du Prix Total Cible
    cout_global_concurrent = prix_comp + frais_port_notre_site
    prix_fr_brut = to_float(row.get("Prix_FR_Brut"))

    if str(row.get("Zone_Geo")).strip().upper() == "NORD":
        prix_cible = (cout_global_concurrent * 0.90) - frais_port_notre_site
        prix_cible = max(prix_cible, prix_fr_brut)
    else:
        prix_cible = cout_global_concurrent - frais_port_notre_site
        prix_cible = max(prix_cible, prix_plancher)

    # 4. Protection Stock Faible
    # Remplacement de "Ventes_30_Jours > 5" par la nouvelle condition "Bestseller (Top 3%)"
    if (
        str(row.get("Statut_Stock")).strip().lower() == "stock_faible"
        and str(row.get("Is_Bestseller")).strip().lower() == "oui"
    ):
        return max(prix_standard, dernier_prix), "GEL_STOCK_FAIBLE"

    # 5. Application du Corridor Asymétrique
    statut = "OK"
    
    # Alignement exact -1% si le fournisseur est à moins de 3% de notre prix
    ecart_fournisseur = (prix_standard - prix_comp) / prix_standard if prix_standard > 0 else 0
    if 0 < ecart_fournisseur <= 0.03:
        nouveau_prix = prix_comp * 0.99
        statut = "ALIGNEMENT_PROCHE_COMPETITEUR_-1%"
    else:
        if prix_cible > prix_standard:
            # Hausse à 95% si contexte monopole, 66% en temps normal
            coeff_hausse = 0.95 if is_monopole else 0.66
            nouveau_prix = prix_standard + ((prix_cible - prix_standard) * coeff_hausse)
        else:
            # Baisse à 33% pour tous, sauf bestsellers (Top 3%) à 15%
            delta = prix_cible - prix_standard
            if str(row.get("Is_Bestseller")).strip().lower() == "oui":
                nouveau_prix = prix_standard + (delta * 0.15)
            else:
                nouveau_prix = prix_standard + (delta * 0.33)

    if str(row.get("Statut_Stock")).strip().lower() == "surstock":
        nouveau_prix *= 0.95

    # 6. Prix Plancher Inviolable
    prix_final = max(nouveau_prix, prix_plancher)

    # Arrondi
    if str(row.get("Zone_Geo")).strip().upper() == "NORD":
        prix_final = round(prix_final) - 0.01
    else:
        prix_final = round(prix_final, 2)

    # 7. Barrière de Sécurité Écart Max (Avertissement visuel uniquement)
    variation = abs(prix_final - prix_standard) / prix_standard
    if variation > SEUIL_VARIATION_MAX:
        statut = f"{statut} (-25% attention)"

    return prix_final, statut


# --- 4. TRAITEMENT ET SYNCHRONISATION ---
def mettre_a_jour_prix():
    batch_data = []

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

    # --- CALCUL DYNAMIQUE DU TOP 3% (BESTSELLERS) ---
    ventes_totales = [int(p.get("total_sales") or 0) for p in tous_les_produits]
    
    if ventes_totales:
        seuil_top_3_percent = pd.Series(ventes_totales).quantile(0.97)
        print(f"🌟 Seuil Bestseller (Top 3%) calculé à : {seuil_top_3_percent} ventes.")
    else:
        seuil_top_3_percent = float('inf') # Sécurité si aucun produit n'a de ventes

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

        # Injection du statut Bestseller dynamique (remplace la donnée du Sheet)
        total_sales_produit = int(produit.get("total_sales") or 0)
        est_bestseller = (total_sales_produit >= seuil_top_3_percent) and (total_sales_produit > 0)
        row["Is_Bestseller"] = "oui" if est_bestseller else "non"

        nouveau_prix, statut_calcul = calculer_prix_dynamique(row)

        df_matrice.loc[
            df_matrice["Clave_Unique"] == cle_recherche, "Dernier_Prix_Calcule"
        ] = nouveau_prix
        df_matrice.loc[
            df_matrice["Clave_Unique"] == cle_recherche, "Statut_Dernier_Calcul"
        ] = statut_calcul

        if nouveau_prix > 0 and nouveau_prix != prix_actuel and "BLOCAGE" not in statut_calcul:
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
        print(f"✅ {len(batch_data)} prix mis à jour avec succès sur WooCommerce.")
    else:
        print("Tous les prix WooCommerce autorisés sont déjà à jour.")

    # Nettoyage des colonnes de calcul temporaires
    for col_temp in ["SKU_Clean", "Clave_Unique"]:
        if col_temp in df_matrice.columns:
            df_matrice.drop(columns=[col_temp], inplace=True)

    # --- 5. ÉCRITURE DANS LE GOOGLE SHEET EN LIGNE ---
    print("Mise à jour ciblée du Google Sheet en ligne...")
    try:
        headers = worksheet.row_values(1)
        
        col_prix = headers.index("Dernier_Prix_Calcule") + 1
        col_statut = headers.index("Statut_Dernier_Calcul") + 1
        
        vals_prix = [[val] for val in df_matrice["Dernier_Prix_Calcule"].fillna("").tolist()]
        vals_statut = [[val] for val in df_matrice["Statut_Dernier_Calcul"].fillna("").tolist()]
        
        worksheet.update(f"{gspread.utils.rowcol_to_a1(2, col_prix)}", vals_prix)
        worksheet.update(f"{gspread.utils.rowcol_to_a1(2, col_statut)}", vals_statut)
        
        print("Seules les colonnes de calcul ont été mises à jour (données sources préservées) !")
    except Exception as e:
        print(f"Erreur lors de l'écriture ciblée dans le Google Sheet : {e}")


# --- 6. POINT D'ENTRÉE ---
if __name__ == "__main__":
    print("Démarrage du pipeline Dynamic Pricing WooCommerce...")
    mettre_a_jour_prix()
