import os
import sys
import pandas as pd
import gdown
from woocommerce import API

# --- 1. CONFIGURATION DES CHEMINS ET DRIVE ---
dossier_actuel = os.path.dirname(os.path.abspath(__file__))
chemin_matrice = os.path.join(dossier_actuel, "matrice_prix_marges.csv")

drive_id_matrice = os.environ.get("DRIVE_ID_MATRICE") or os.environ.get("DRIVE_ID_PROD")

if not drive_id_matrice:
    print("Erreur : Variable 'DRIVE_ID_MATRICE' manquante.")
    sys.exit(1)

print("Étape 1 : Téléchargement de la matrice existante depuis Google Drive...")
url_csv = f"https://docs.google.com/spreadsheets/d/{drive_id_matrice}/export?format=csv"
try:
    gdown.download(url_csv, chemin_matrice, quiet=False)
except Exception:
    url_standard = f"https://drive.google.com/uc?id={drive_id_matrice}"
    gdown.download(url_standard, chemin_dest=chemin_matrice, quiet=False)

# Chargement de la matrice existante
if os.path.exists(chemin_matrice):
    try:
        df_matrice = pd.read_csv(chemin_matrice, low_memory=False)
    except Exception:
        df_matrice = pd.read_excel(chemin_matrice)
else:
    print("Erreur : Impossible de charger le fichier téléchargé.")
    sys.exit(1)

# Nettoyage des colonnes / Clé de jointure
if 'SKU' not in df_matrice.columns:
    print("Erreur : La colonne 'SKU' est introuvable dans le Google Sheet.")
    sys.exit(1)

df_matrice['SKU_Clean'] = df_matrice['SKU'].astype(str).str.strip()

# --- 2. CONFIGURATION DE L'API WOOCOMMERCE ---
woo_url = os.environ.get("URL_SITE") or os.environ.get("WOOCOMMERCE_URL")
woo_ck = os.environ.get("WOO_CONSUMER_KEY") or os.environ.get("WC_CONSUMER_KEY")
woo_cs = os.environ.get("WOO_CONSUMER_SECRET") or os.environ.get("WC_CONSUMER_SECRET")

if not woo_url or not woo_ck or not woo_cs:
    print("Erreur : Les identifiants API WooCommerce (URL_SITE, WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET) sont manquants.")
    sys.exit(1)

wcapi = API(
    url=woo_url,
    consumer_key=woo_ck,
    consumer_secret=woo_cs,
    version="wc/v3",
    timeout=60
)

# --- 3. EXTRACTION DU CATALOGUE WOOCOMMERCE ---
print("\nÉtape 2 : Extraction de tous les produits depuis WooCommerce...")
tous_les_produits = []
page = 1

while True:
    res = wcapi.get("products", params={"per_page": 100, "page": page}).json()
    if not res or (isinstance(res, dict) and "code" in res):
        break
    tous_les_produits.extend(res)
    print(f"  -> Page {page} récupérée ({len(res)} produits)")
    if len(res) < 100:
        break
    page += 1

print(f"Total récupéré : {len(tous_les_produits)} produits WooCommerce.")

# --- 4. SYNCHRONISATION / FUSION AVEC LA MATRICE ---
print("\nÉtape 3 : Mise à jour des colonnes WooCommerce dans la matrice...")

produits_mis_a_jour = 0
nouveaux_produits = []

for prod in tous_les_produits:
    sku = str(prod.get("sku", "")).strip()
    if not sku:
        continue

    id_woo = prod.get("id")
    nom = prod.get("name")
    prix_regulier = float(prod.get("regular_price") or 0)
    prix_actuel = float(prod.get("price") or 0)
    statut_stock = str(prod.get("stock_status", "instock")).lower()
    total_ventes = int(prod.get("total_sales") or 0)

    # Vérification si le produit existe déjà dans le Google Sheet
    idx = df_matrice.index[df_matrice['SKU_Clean'] == sku].tolist()

    if idx:
        row_i = idx[0]
        # Mise à jour des données dynamiques sans écraser les autres colonnes
        df_matrice.loc[row_i, 'ID_WooCommerce'] = id_woo
        df_matrice.loc[row_i, 'Nom_Produit'] = nom
        if 'Prix_Standard_TTC' not in df_matrice.columns or pd.isna(df_matrice.loc[row_i, 'Prix_Standard_TTC']):
            df_matrice.loc[row_i, 'Prix_Standard_TTC'] = prix_regulier
        df_matrice.loc[row_i, 'Dernier_Prix_Applique'] = prix_actuel
        df_matrice.loc[row_i, 'Statut_Stock'] = statut_stock
        df_matrice.loc[row_i, 'Ventes_30_Jours'] = total_ventes
        produits_mis_a_jour += 1
    else:
        # Si le produit est nouveau sur le site, on l'ajoute
        nouvelle_ligne = {
            'ID_WooCommerce': id_woo,
            'SKU': sku,
            'Nom_Produit': nom,
            'Prix_Standard_TTC': prix_regulier,
            'Dernier_Prix_Applique': prix_actuel,
            'Prix_Plancher_TTC': round(prix_regulier * 0.70, 2), # Valeur par défaut (30% de marge max)
            'Prix_FR_Brut': round(prix_regulier * 0.50, 2),
            'Statut_Stock': statut_stock,
            'Ventes_30_Jours': total_ventes,
            'Zone_Geo': 'SUD',
            'Is_Bestseller': 'Non',
            'Nb_Baisses_48h': 0
        }
        nouveaux_produits.append(nouvelle_ligne)

# Si de nouveaux produits ont été ajoutés sur le site
if nouveaux_produits:
    df_nouveaux = pd.DataFrame(nouveaux_produits)
    df_matrice = pd.concat([df_matrice, df_nouveaux], ignore_index=True)
    print(f"{len(nouveaux_produits)} nouveaux produits ajoutés à la matrice.")

# Nettoyage de la colonne temporaire
if 'SKU_Clean' in df_matrice.columns:
    df_matrice.drop(columns=['SKU_Clean'], inplace=True)

# Sauvegarde locale du fichier mis à jour
df_matrice.to_csv(chemin_matrice, index=False, encoding='utf-8')
print(f"{produits_mis_a_jour} produits WooCommerce synchronisés avec succès dans la matrice.")
