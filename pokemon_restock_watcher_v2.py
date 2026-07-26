"""
Pokemon Restock & Preorder Watcher v2
--------------------------------------
Ueberwacht (a) einzelne Produktseiten auf Lagerbestand und (b) Vorbestell-/
Neuheiten-Kategorieseiten auf neu erschienene Artikel - fuer den persoenlichen
Gebrauch, kostenlos, nur auf Basis oeffentlicher Shopseiten.

WICHTIG - bitte lesen:
- Nur oeffentliche Seiten ohne Login. Kein Auto-Checkout, keine Umgehung von
  Kauflimits oder Bot-Schutz (Cloudflare, Captchas etc.).
- robots.txt und AGB der jeweiligen Shops bitte selbst pruefen.
- Faire Intervalle (Default: alle 10 Minuten pro Shop via Scheduler) - das
  entspricht etwa dem, was ein Mensch macht, der ab und zu die Seite laedt.
- Manche Shops (v.a. grosse Ketten) setzen Bot-Schutz ein. Wenn ein Shop
  dauerhaft Fehler wirft, ist das ein Signal, ihn NICHT weiter zu bearbeiten,
  statt den Schutz zu umgehen.
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

NOTIFY_METHOD = "telegram"       # "telegram" oder "macos"
# Liest zuerst aus Umgebungsvariablen (wichtig fuer GitHub Actions Secrets),
# faellt lokal auf die Platzhalter zurueck, falls du sie direkt hier eintraegst.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "DEIN_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "DEINE_CHAT_ID")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersoenlicherRestockWatcher/1.0)"}

OUT_OF_STOCK_KEYWORDS = ["ausverkauft", "nicht verfügbar", "nicht auf lager", "sold out"]
IN_STOCK_KEYWORDS = ["in den warenkorb", "jetzt kaufen", "add to cart", "vorbestellen"]

# ---------------------------------------------------------------------------
# ERWEITERTE SHOP-LISTE
# ---------------------------------------------------------------------------
SHOPS = [
    {
        "shop": "God of Cards",
        "watch_pages": [
            {"name": "Neue Artikel", "url": "https://godofcards.com/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "FantasiaCards",
        "watch_pages": [
            {"name": "Neue Artikel", "url": "https://fantasiacards.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "Poke-Corner",
        "watch_pages": [
            {"name": "Neue Artikel", "url": "https://poke-corner.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "Kartenkrake",
        "watch_pages": [
            {"name": "Neue Artikel", "url": "https://kartenkrake.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "Taschenmonster",
        "watch_pages": [
            {"name": "Neue Artikel", "url": "https://taschenmonster.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "CardBuddies",
        "watch_pages": [
            {"name": "Neue Artikel", "url": "https://cardbuddies.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "TCG-Nord",
        "watch_pages": [
            {"name": "Neue Artikel", "url": "https://tcg-nord.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "Card-Panda",
        "watch_pages": [
            {"name": "Neue Artikel", "url": "https://card-panda.de/products.json", "method": "shopify"},
        ],
    },
    {
        "shop": "TCGViert",
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
    elif NOTIFY_METHOD == "macos":
        safe_message = message.replace('"', "'")
        os.system(f'osascript -e \'display notification "{safe_message}" with title "Pokemon Watcher"\'')


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
        if "pok" in title:
            handles.add(product.get("handle"))
    return handles


def check_watch_page_links(page):
    resp = requests.get(page["url"], headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return extract_product_links(resp.text, page["url"])


def check_product_stock(product):
    resp = requests.get(product["url"], headers=HEADERS, timeout=15)
    resp.raise_for_status()
    text = resp.text.lower()
    if any(k in text for k in OUT_OF_STOCK_KEYWORDS):
        return False
    if any(k in text for k in IN_STOCK_KEYWORDS):
        return True
    return None  # unklar -> lieber nichts melden als falscher Alarm


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
                notify(f"Neu bei {shop_name} ({page['name']}): {len(new_items)} neue(r) Artikel!\n{page['url']}")
            print(f"[{datetime.now()}] {shop_name} / {page['name']}: {len(current)} Artikel, {len(new_items)} neu")

            state[key] = list(current)
            time.sleep(random.uniform(2, 5))

        for product in shop.get("products", []):
            key = f"{shop_name}::{product['name']}"
            try:
                available = check_product_stock(product)
            except Exception as e:
                print(f"[{datetime.now()}] Fehler bei {shop_name} / {product['name']}: {e}")
                continue

            if available is None:
                continue

            was_available = state.get(key, False)
            if available and not was_available:
                notify(f"Restock! {shop_name}: {product['name']}\n{product['url']}")

            state[key] = available
            time.sleep(random.uniform(2, 5))

    save_state(state)


if __name__ == "__main__":
    run_once()
