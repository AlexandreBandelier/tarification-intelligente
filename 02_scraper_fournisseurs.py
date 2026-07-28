import os
import sys
import re
import pandas as pd
import requests
from bs4 import BeautifulSoup

# --- 1. CONFIGURATION ET CHARGEMENT DU FICHIER LOCAL ---
dossier_actuel = os.path.dirname(os.path.abspath(__file__))
chemin_matrice = os.path.join(dossier_actuel, "matrice_prix_marges.csv")

if not os.path.exists(chemin_matrice):
    print(f"Erreur : Le fichier '{chemin_matrice}' est introuvable. Exécutez le script 01 d'abord.")
    sys.exit(1)

df_matrice = pd.read_csv(chemin_matrice, low_memory=False)

# Headers pour simuler une navigation humaine et éviter les blocages 403
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7'
}

# --- 2. FONCTIONS DE SCRAPING ET EXTRACTION ---
def extraire_donnees_fournisseur(url):
    """
    Va chercher la page HTML et tente de repérer le prix et le statut du stock.
    """
    if not url or pd.isna(url) or not str(url).startswith('http'):
        return None, None, None

    try:
        response = requests.get(str(url).strip(), headers=HEADERS, timeout=10)
        if response.status_code != 200:
            print(f"  [!] Erreur HTTP {response.status_code} pour : {url}")
            return None, None, "Erreur_HTTP"

        soup = BeautifulSoup(response.content, 'html.parser')

        # --- A. RECHERCHE DU PRIX ---
        prix = None
        # Cherche les balises courantes de prix (Microdonnées Schema.org / Balises e-commerce classiques)
        balise_prix = soup.find(attrs={"property": "product:price:amount"}) or \
                      soup.find(attrs={"itemprop": "price"}) or \
                      soup.find(class_=re.compile(r'price|prix', re.I))

        if balise_prix:
            valeur_texte = balise_prix.get('content') or balise_prix.text
            # Nettoyage pour récupérer un nombre flottant (ex: "45,90 €" -> 45.90)
            valeur_clean = re.sub(r'[^\d,. ]', '', valeur_texte).replace(',', '.').strip()
            # Si espace millier ou doublons
            match = re.search(r'\d+(\.\d{1,2})?', valeur_clean)
            if match:
                prix = float(match.group(0))

        # --- B. RECHERCHE DE LA DISPONIBILITÉ (STOCK) ---
        dispo = "Rupture"
        texte_page = soup.get_text().lower()
        
        # Détecteurs génériques de stock
        mots_stock = ["en stock", "in stock", "disponible", "en reapprovisionnement"]
        mots_rupture = ["ecomprime", "hors stock", "out of stock", "rupture"]

        if any(m in texte_page for m in mots_stock) and not any(r in texte_page for r in mots_rupture):
            dispo = "En stock"

        # Frais de port par défaut (peut être ajusté ou extrait si présent)
        port = 0.0

        return prix, port, dispo

    except Exception as e:
        print(f"  [!] Échec du scraping pour {url} : {e}")
        return None, None, "Erreur"

# --- 3. EXÉCUTION DU SCRAPING SUR LA MATRICE ---
print("Étape : Lancement du Scraping Fournisseurs / Concurrents...")

if 'URL_Fournisseur_1' not in df_matrice.columns:
    print("Avertissement : La colonne 'URL_Fournisseur_1' n'existe pas dans le Google Sheet.")
    sys.exit(0)

compteur_succes = 0

for idx, row in df_matrice.iterrows():
    url_f1 = row.get('URL_Fournisseur_1')

    if pd.notna(url_f1) and str(url_f1).startswith('http'):
        sku = row.get('SKU', f'Ligne {idx+1}')
        print(f"-> Scraping pour SKU {sku} ({url_f1[:40]}...)...")
        
        prix, port, dispo = extraire_donnees_fournisseur(url_f1)

        if prix is not None:
            df_matrice.loc[idx, 'Prix_Concurrent_1'] = prix
            df_matrice.loc[idx, 'Port_Concurrent_1'] = port
            df_matrice.loc[idx, 'Dispo_Concurrent_1'] = dispo
            compteur_succes += 1
            print(f"Trouvé : Prix = {prix}€ | Stock = {dispo}")
        else:
            print(f"Impossible d'extraire automatiquement le prix.")

# Sauvegarde des résultats
df_matrice.to_csv(chemin_matrice, index=False, encoding='utf-8')
print(f"\n Scraping terminé. {compteur_succes} prix fournisseurs mis à jour dans la matrice.")
