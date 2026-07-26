"""
Pokemon Restock & Preorder Watcher v3 (Extended Europe)
------------------------------------------------------
Ueberwacht Shops in Deutschland, Oesterreich und Europa auf neue
Pokemon-Artikel und Vorbestellungen.
"""

import json
import os
import random
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

STATE_FILE = "watcher_state.json"
NOTIFY_METHOD = "telegram"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "DEIN_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "DEINE_CHAT_ID")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersoenlicherRestockWatcher/1.0)"}

OUT_OF_STOCK_KEYWORDS = ["ausverkauft", "nicht verfügbar", "nicht auf lager", "sold out"]
IN_STOCK_KEYWORDS = ["in den warenkorb", "jetzt kaufen", "add to cart", "vorbestellen"]

# ---------------------------------------------------------------------------
# GROSSE SHOP-LISTE (DEUTSCHLAND, OESTERREICH & INTERNATIONALE IMPORTE)
# ---------------------------------------------------------------------------
SHOPS = [
    {
        "shop": "God of Cards",
        "watch_pages": [
            {"name": "Alle neuen Produkte", "url": "https://godofcards.com/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "FantasiaCards",
        "watch_pages": [
            {"name": "Alle neuen Produkte", "url": "https://fantasiacards.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "Poke-Corner",
        "watch_pages": [
            {"name": "Alle neuen Produkte", "url": "https://poke-corner.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "Kartenkrake",
        "watch_pages": [
            {"name": "Alle neuen Produkte", "url": "https://kartenkrake.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "Taschenmonster",
        "watch_pages": [
            {"name": "Alle neuen Produkte", "url": "https://taschenmonster.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "CardBuddies",
        "watch_pages": [
            {"name": "Alle neuen Produkte", "url": "https://cardbuddies.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "TCG-Nord",
        "watch_pages": [
            {"name": "Alle neuen Produkte", "url": "https://tcg-nord.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "Card-Panda",
        "watch_pages": [
            {"name": "Alle neuen Produkte", "url": "https://card-panda.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "TCGViert (Vorbestellungen)",
        "watch_pages": [
            {"name": "Vorbestellungen", "url": "https://tcgviert.com/collections/vorbestellungen", "method": "links"},
        ],
    }
]

# ---------------------------------------------------------------------------
# HILFSFUNKTIONEN
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def notify(message):
    if NOTIFY_METHOD == "telegram":
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        except requests.RequestException as e:
            print(f"[WARN] Telegram fehlgeschlagen: {e}")

def extract_product_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(marker in href for marker in ["/products/", "/produkt", "/artikel", "-p-", "/item"]):
            if href.startswith("/"):
                href = urljoin(base_url, href)
            links.add(href)
    return links

def check_watch_page_shopify(page):
    resp = requests.get(page["url"], headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    handles = set()
    for product in data.get("products", []):
        title = product.get("title", "").lower()
        # Filtert gezielt nach Pokemon im Titel
        if "pok" in title:
            handles.add(product.get("handle"))
    return handles

def check_watch_page_links(page):
    resp = requests.get(page["url"], headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return extract_product_links(resp.text, page["url"])

# ---------------------------------------------------------------------------
# HAUPTLOGIK
# ---------------------------------------------------------------------------

def run_once():
    state = load_state()

    for shop in SHOPS:
        shop_name = shop["shop"]

        for page in shop.get("watch_pages", []):
            key = f"{shop_name}::{page['name']}"
            try:
                if page["method"] == "shopify":
                    current = check_watch_page_shopify(page)
                else:
                    current = check_watch_page_links(page)
            except Exception as e:
                print(f"[{datetime.now()}] Fehler bei {shop_name} / {page['name']}: {e}")
                continue

            previous = set(state.get(key, []))
            new_items = current - previous

            if previous and new_items:
                notify(f"🚨 NEU / VORBESTELLUNG bei {shop_name} ({page['name']}): {len(new_items)} neue Artikel!\n🔗 {page['url']}")
            print(f"[{datetime.now()}] {shop_name} / {page['name']}: {len(current)} Artikel, {len(new_items)} neu")

            state[key] = list(current)
            time.sleep(random.uniform(2, 5))

    save_state(state)

if __name__ == "__main__":
    run_once()
