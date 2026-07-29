import os
import sys
import json
import time
import re
import pandas as pd
import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from woocommerce import API
from playwright.sync_api import sync_playwright

# --- 0. PARAMÈTRES DE SÉCURITÉ ET FONCTIONS UTILITAIRES ---
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

def extraire_infos_url(url):
    """
    Extrait le prix et le stock via Playwright (rendu JS complet).
    Compatible avec Shopware, Shopify, Magento, WooCommerce, React/Vue stores.
    """
    if not url or pd.isna(url) or not str(url).strip().startswith("http"):
        return None, "hors stock"

    clean_url = str(url).strip()
    prix = None
    dispo = "hors stock"

    try:
        with sync_playwright() as p:
            # Lancement d'un navigateur headless
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                locale="fr-FR"
            )
            page = context.new_page()
            
            # Chargement de la page et attente du rendu
            page.goto(clean_url, timeout=20000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500) # Laisse 2.5s au JS pour charger le prix

            # 1. Extraction JSON-LD post-rendu
            scripts_json_ld = page.locator('script[type="application/ld+json"]').all_inner_texts()
            for script_text in scripts_json_ld:
                try:
                    data = json.loads(script_text)
                    items = data.get("@graph", data) if isinstance(data, dict) else data
                    if not isinstance(items, list):
                        items = [items]

                    for item in items:
                        if isinstance(item, dict) and item.get("@type") in ["Product", "IndividualProduct"]:
                            offers = item.get("offers", {})
                            if isinstance(offers, list) and offers:
                                offers = offers[0]
                            if isinstance(offers, dict):
                                prix_val = offers.get("price") or offers.get("lowPrice")
                                if prix_val:
                                    prix = to_float(prix_val)
                                
                                availability = str(offers.get("availability", "")).lower()
                                if any(k in availability for k in ["instock", "in_stock", "limitedavailability"]):
                                    dispo = "en stock"
                except Exception:
                    continue

            # 2. Si le JSON-LD n'a pas suffi, inspection du DOM rendu
            if prix is None or prix <= 0:
                sélecteurs_prix = [
                    'meta[itemprop="price"]',
                    '.product-detail-price',
                    '.price-unit',
                    '.product-price',
                    '.price',
                    '.amount'
                ]
                for sel in sélecteurs_prix:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        content = loc.get_attribute("content") or loc.inner_text()
                        match = re.search(r'(\d+[\.,]?\d{0,2})', content)
                        if match:
                            prix_pot = to_float(match.group(1))
                            if prix_pot > 0:
                                prix = prix_pot
                                break

            # 3. Détection du stock dans le DOM
            if dispo == "hors stock":
                body_text = page.locator("body").inner_text().lower()
                mots_stock = ["in stock", "en stock", "disponible", "add to cart", "ajouter au panier"]
                mots_rupture = ["out of stock", "rupture de stock", "sold out", "épuisé"]

                if any(m in body_text for m in mots_stock):
                    if not any(r in body_text for r in mots_rupture):
                        dispo = "en stock"

            browser.close()

        if prix and prix > 0:
            print(f"✅ Scraping Playwright réussi [{clean_url}] -> Prix: {prix}€ | Stock: {dispo}")
        else:
            print(f"❌ Échec extraction Playwright [{clean_url}]")

        return prix, dispo

    except Exception as e:
        print(f"⚠️ Erreur Playwright sur l'URL {clean_url} : {e}")
        return None, "hors stock"


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

    # 2. Détermination du concurrent / FRS cible
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
        
        if dernier_prix < prix_standard:
            if str(row.get("Statut_Stock")).strip().lower() == "stock_faible":
                nouveau_prix = round(prix_standard * 1.05, 2)
                statut = "REPLI_MONOPOLE_STOCK_FAIBLE"
            else:
                nouveau_prix = prix_standard
                statut = "REPLI_MONOPOLE_STANDARD"
            
            return max(nouveau_prix, prix_plancher), statut
        else:
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
    if (
        str(row.get("Statut_Stock")).strip().lower() == "stock_faible"
        and str(row.get("Is_Bestseller")).strip().lower() == "oui"
    ):
        return max(prix_standard, dernier_prix), "GEL_STOCK_FAIBLE"

    # 5. Application du Corridor Asymétrique
    statut = "OK"
    
    ecart_fournisseur = (prix_standard - prix_comp) / prix_standard if prix_standard > 0 else 0
    if 0 < ecart_fournisseur <= 0.03:
        nouveau_prix = prix_comp * 0.99
        statut = "ALIGNEMENT_PROCHE_COMPETITEUR_-1%"
    else:
        if prix_cible > prix_standard:
            coeff_hausse = 0.95 if is_monopole else 0.66
            nouveau_prix = prix_standard + ((prix_cible - prix_standard) * coeff_hausse)
        else:
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

    # 7. Barrière de Sécurité Écart Max
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
    per_page = 50

    while True:
        try:
            params = {
                "per_page": per_page,
                "page": page,
                "_fields": "id,sku,name,regular_price,total_sales"
            }
            res = wcapi.get("products", params=params).json()
            
            if not res or (isinstance(res, dict) and "code" in res):
                break
                
            tous_les_produits.extend(res)
            
            if len(res) < per_page:
                break
                
            page += 1
            
        except Exception as e:
            print(f"Erreur temporaire page {page} ({e}). Nouvelle tentative dans 2s...")
            time.sleep(2)
            res = wcapi.get("products", params=params).json()
            if not res or (isinstance(res, dict) and "code" in res):
                break
            tous_les_produits.extend(res)
            if len(res) < per_page:
                break
            page += 1

    print(f"{len(tous_les_produits)} produits récupérés depuis WooCommerce.")

    # --- CALCUL DYNAMIQUE DU TOP 3% (BESTSELLERS) ---
    ventes_totales = [int(p.get("total_sales") or 0) for p in tous_les_produits]
    
    if ventes_totales:
        seuil_top_3_percent = pd.Series(ventes_totales).quantile(0.97)
        print(f"Seuil Bestseller (Top 3%) calculé à : {seuil_top_3_percent} ventes.")
    else:
        seuil_top_3_percent = float('inf')

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

        # --- EXTRACTION EN DIRECT DE L'URL DU FOURNISSEUR / CONCURRENT ---
        for num in ["1", "2"]:
            url_col = f"URL_Concurrent_{num}"
            if url_col in row and str(row.get(url_col)).strip().startswith("http"):
                scraped_prix, scraped_dispo = extraire_infos_url(row.get(url_col))
                
                # Mise à jour dans la ligne locale et globale
                val_prix_final = scraped_prix if (scraped_prix and scraped_prix > 0) else ""
                row[f"Prix_Concurrent_{num}"] = val_prix_final
                row[f"Dispo_Concurrent_{num}"] = scraped_dispo

                df_matrice.loc[df_matrice["Clave_Unique"] == cle_recherche, f"Prix_Concurrent_{num}"] = val_prix_final
                df_matrice.loc[df_matrice["Clave_Unique"] == cle_recherche, f"Dispo_Concurrent_{num}"] = scraped_dispo

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

    # Synchronisation WooCommerce
    if batch_data:
        print(f"Envoi des modifications pour {len(batch_data)} produits sur WooCommerce...")
        for i in range(0, len(batch_data), 100):
            paquet = batch_data[i : i + 100]
            wcapi.post("products/batch", {"update": paquet})
        print(f"{len(batch_data)} prix mis à jour avec succès sur WooCommerce.")
    else:
        print("Tous les prix WooCommerce autorisés sont déjà à jour.")

    for col_temp in ["SKU_Clean", "Clave_Unique"]:
        if col_temp in df_matrice.columns:
            df_matrice.drop(columns=[col_temp], inplace=True)

    # --- 5. ÉCRITURE ET MISE À JOUR DANS GOOGLE SHEETS ---
    print("Mise à jour du Google Sheet en ligne (Prix calculés + Données extraites)...")
    try:
        headers = worksheet.row_values(1)
        
        colonnes_a_mettre_a_jour = [
            "Prix_Concurrent_1", "Dispo_Concurrent_1",
            "Prix_Concurrent_2", "Dispo_Concurrent_2",
            "Dernier_Prix_Calcule", "Statut_Dernier_Calcul"
        ]

        for col_name in colonnes_a_mettre_a_jour:
            if col_name in headers and col_name in df_matrice.columns:
                col_idx = headers.index(col_name) + 1
                vals = [[val] for val in df_matrice[col_name].fillna("").tolist()]
                worksheet.update(f"{gspread.utils.rowcol_to_a1(2, col_idx)}", vals)
        
        print("Données extraites et calculs mis à jour avec succès dans le Google Sheet !")
    except Exception as e:
        print(f"Erreur lors de l'écriture dans le Google Sheet : {e}")


# --- 6. POINT D'ENTRÉE ---
if __name__ == "__main__":
    print("Démarrage du pipeline Dynamic Pricing WooCommerce...")
    mettre_a_jour_prix()
