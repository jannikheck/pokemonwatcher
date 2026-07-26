"""
Pokemon Restock & Preorder Watcher v5 (DACH Master Edition)
-----------------------------------------------------------
Ueberwacht deutsche und oesterreichische TCG-Shops auf

  * NEUE Pokémon-Produkte (inkl. Vorbestellungen), sobald sie im Shop auftauchen
  * WIEDER VERFUEGBARE Pokémon-Produkte (echter Restock: war ausverkauft -> jetzt kaufbar)

und schickt dir dazu eine Telegram-Nachricht MIT Produktname und Direktlink.

Funktionsweise
--------------
Fast alle hier gelisteten Shops laufen auf Shopify. Shopify stellt unter
    https://<shop>/products.json
das komplette Sortiment als JSON bereit (oeffentlich, kein Login noetig).
Der Watcher liest dieses JSON, filtert nach Pokémon und vergleicht den Stand
mit dem letzten Lauf (watcher_state.json).

WICHTIG: Nicht jeder Shop nutzt Shopify (viele deutsche Shops laufen auf
Shopware / JTL / Gambio / WooCommerce -> dort gibt es KEIN products.json).
Solche Shops werden automatisch erkannt und sauber uebersprungen. Du kannst
also bedenkenlos neue Domains in die Liste werfen - was nicht passt, faellt
beim Selbsttest einfach raus. Fuer den schnellen Check gibt es den
--verify-Modus (siehe unten).

Aufruf
------
    python pokemon_restock_watcher.py            # ein Ueberwachungslauf
    python pokemon_restock_watcher.py --verify   # nur pruefen, welche Shops Shopify sind
    python pokemon_restock_watcher.py --loop      # dauerhaft laufen (Intervall unten)

Abhaengigkeiten:  pip install requests beautifulsoup4
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

STATE_FILE = "watcher_state.json"

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "DEIN_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "DEINE_CHAT_ID")

# --- Verhalten ---
LOOP_INTERVAL_SECONDS = 300          # Pause zwischen zwei Komplettlaeufen im --loop-Modus
REQUEST_TIMEOUT = 20                 # Sekunden pro HTTP-Request
MAX_PAGES = 10                       # max. Shopify-Seiten je Shop (250 Produkte/Seite -> 2500)
PER_SHOP_DELAY = (3, 6)              # zufaellige Pause (Sek.) zwischen Shops -> hoeflich bleiben
PER_PAGE_DELAY = (1, 2)             # zufaellige Pause (Sek.) zwischen Katalogseiten
MAX_ITEMS_PER_MESSAGE = 15           # so viele Artikel werden einzeln in der Nachricht gelistet
NOTIFY_ON_RESTOCK = True             # auch bei "wieder verfuegbar" benachrichtigen
NOTIFY_ON_NEW = True                 # bei neuen Produkten / Vorbestellungen benachrichtigen

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersoenlicherRestockWatcher/1.0)",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9",
}

# Woran erkennt der Watcher "Pokémon"? (Kleinbuchstaben; "pok" faengt Pokemon/Pokémon)
POKEMON_KEYWORDS = ["pok"]

# Woran erkennt der Watcher eine Vorbestellung?
PREORDER_KEYWORDS = ["vorbestell", "preorder", "pre-order", "pre order", "vorverkauf", "coming soon"]

# ---------------------------------------------------------------------------
# SHOP-LISTE (Deutschland & Oesterreich)
#
#   base            : Shop-Basis-URL ohne abschliessenden Slash
#   country         : DE / AT
#   verified        : True  -> als Shopify bestaetigt (Recherche 07/2026)
#                     None  -> echter Shop, Plattform wird zur Laufzeit geprueft
#   collections     : optional; nur diese Shopify-Collection(s) statt Gesamtsortiment
#                     ueberwachen, z.B. ["vorbestellungen"] -> spart Traffic
#   method          : "shopify" (Standard) oder "links" (HTML-Fallback fuer Nicht-Shopify)
#
# Du kannst jederzeit Eintraege ergaenzen. Was nicht auf Shopify laeuft oder
# nicht erreichbar ist, wird beim Lauf automatisch uebersprungen.
# ---------------------------------------------------------------------------

SHOPS = [
    # ===================== DEUTSCHLAND - Shopify bestaetigt =====================
    {"shop": "TCGViert",         "country": "DE", "base": "https://tcgviert.com",         "verified": True},
    {"shop": "Feenturm",         "country": "DE", "base": "https://feenturm.de",          "verified": True},
    {"shop": "CardCosmos",       "country": "DE", "base": "https://cardcosmos.de",        "verified": True},
    {"shop": "TradingToys",      "country": "DE", "base": "https://www.tradingtoys.de",   "verified": True},
    {"shop": "KEEPSEVEN",        "country": "DE", "base": "https://keepseven.de",         "verified": True},
    {"shop": "BulkParadise TCG", "country": "DE", "base": "https://bulkparadise-tcg.de",  "verified": True},
    {"shop": "CrispyCards",      "country": "DE", "base": "https://crispycards.de",       "verified": True},

    # ===================== DEUTSCHLAND - Kandidaten (Laufzeit-Check) =====================
    {"shop": "Pokitrio",         "country": "DE", "base": "https://www.pokitrio.de",      "verified": None},
    {"shop": "Major Cards TCG",  "country": "DE", "base": "https://majorcardstcg.com",    "verified": None},
    {"shop": "YONKO TCG",        "country": "DE", "base": "https://yonko-tcg.de",         "verified": None},
    {"shop": "KTCards",          "country": "DE", "base": "https://ktcards.de",           "verified": None},
    {"shop": "TcG Love",         "country": "DE", "base": "https://tcg-love.de",          "verified": None},
    {"shop": "LottiCards",       "country": "DE", "base": "https://www.lotticards.de",    "verified": None},
    {"shop": "Collect-It",       "country": "DE", "base": "https://www.collect-it.de",    "verified": None},
    {"shop": "God of Cards",     "country": "DE", "base": "https://godofcards.com",       "verified": None},

    # ===================== OESTERREICH - Shopify bestaetigt =====================
    {"shop": "Vinticards",       "country": "AT", "base": "https://vinticards.at",        "verified": True},
    {"shop": "Cardstore.at",     "country": "AT", "base": "https://cardstore.at",         "verified": True},

    # ===================== OESTERREICH - Kandidaten (Laufzeit-Check) =====================
    {"shop": "Butti Cards",         "country": "AT", "base": "https://www.butticards.at",       "verified": None},
    {"shop": "Sammelkarten-Shop.at","country": "AT", "base": "https://www.sammelkarten-shop.at","verified": None},
    {"shop": "TCG-Shop.at",         "country": "AT", "base": "https://www.tcg-shop.at",         "verified": None},
    {"shop": "PokeVend",            "country": "AT", "base": "https://pokevend.at",             "verified": None},
    {"shop": "Cardcorner",          "country": "AT", "base": "https://cardcorner.at",           "verified": None},
    {"shop": "Grubi & Co",          "country": "AT", "base": "https://www.grubi-co.at",         "verified": None},
    {"shop": "SpielRaum",           "country": "AT", "base": "https://www.spielraum.co.at",     "verified": None},

    # ===================== LEGACY / UNBESTAETIGT (aus deiner alten Liste) =====================
    # Diese Domains tauchten in keiner Shop-Recherche auf - moeglicherweise nicht mehr
    # aktiv oder falsch geschrieben. Werden geprueft und bei Fehler uebersprungen.
    {"shop": "FantasiaCards", "country": "DE", "base": "https://fantasiacards.de", "verified": None},
    {"shop": "Poke-Corner",   "country": "DE", "base": "https://poke-corner.de",   "verified": None},
    {"shop": "Kartenkrake",   "country": "DE", "base": "https://kartenkrake.de",   "verified": None},
    {"shop": "Taschenmonster","country": "DE", "base": "https://taschenmonster.de","verified": None},
    {"shop": "CardBuddies",   "country": "DE", "base": "https://cardbuddies.de",   "verified": None},
    {"shop": "TCG-Nord",      "country": "DE", "base": "https://tcg-nord.de",      "verified": None},
    {"shop": "Card-Panda",    "country": "DE", "base": "https://card-panda.de",    "verified": None},
    {"shop": "UltraCards",    "country": "DE", "base": "https://ultracards.de",    "verified": None},
    {"shop": "Card Collector","country": "DE", "base": "https://cardcollector.de", "verified": None},
]

# ---------------------------------------------------------------------------
# BEKANNT NICHT-SHOPIFY (nur zur Info - werden NICHT ueberwacht)
# ---------------------------------------------------------------------------
#   tabletop-dragon.de      -> JTL-Shop
#   gate-to-the-games.de    -> JTL-Shop
#   comicplanet.de          -> Gambio
#   sapphire-cards.de       -> WooCommerce
#   packsandco (wixsite)    -> Wix
#   business.cardsandtoys.de-> Grosshandels-/Shopware-Setup
# Fuer diese braeuchtest du HTML-Scraping (method="links") mit shopspezifischen
# Selektoren - aufwaendiger und fragiler. Bei Bedarf nachruestbar.
# ---------------------------------------------------------------------------


# ===========================================================================
# ZUSTAND LADEN / SPEICHERN
# ===========================================================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print("[WARN] watcher_state.json unlesbar - starte mit leerem Zustand.")
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)  # atomar -> kein kaputter State bei Absturz


# ===========================================================================
# BENACHRICHTIGUNG
# ===========================================================================

def notify(message):
    """Schickt eine Telegram-Nachricht. Faellt bei fehlender Konfig auf Konsole zurueck."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "DEIN_BOT_TOKEN":
        print("[NOTIFY] (kein Telegram-Token gesetzt)\n" + message + "\n")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": "true",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            print(f"[WARN] Telegram-Antwort {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as e:
        print(f"[WARN] Telegram fehlgeschlagen: {e}")


# ===========================================================================
# HILFSFUNKTIONEN
# ===========================================================================

def _text_blob(product):
    """Baut aus Titel, Typ, Vendor und Tags einen durchsuchbaren Kleinbuchstaben-String."""
    parts = [
        str(product.get("title", "")),
        str(product.get("product_type", "")),
        str(product.get("vendor", "")),
    ]
    tags = product.get("tags", "")
    if isinstance(tags, list):
        parts.extend(str(t) for t in tags)
    else:
        parts.append(str(tags))
    return " ".join(parts).lower()


def is_pokemon(product):
    blob = _text_blob(product)
    return any(kw in blob for kw in POKEMON_KEYWORDS)


def is_preorder(product):
    blob = _text_blob(product)
    return any(kw in blob for kw in PREORDER_KEYWORDS)


def is_available(product):
    """Verfuegbar = mindestens eine Variante ist kaufbar."""
    for variant in product.get("variants", []):
        if variant.get("available"):
            return True
    return False


def product_url(base, handle):
    return f"{base}/products/{handle}"


def extract_product_links(html, base_url):
    """HTML-Fallback fuer Nicht-Shopify-Shops (method='links')."""
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(m in href for m in ["/products/", "/produkt", "/artikel", "-p-", "/item"]):
            if href.startswith("/"):
                href = urljoin(base_url, href)
            links.add(href)
    return links


# ===========================================================================
# SHOPIFY-ABRUF
# ===========================================================================

def fetch_shopify_products(base, collection=None, max_pages=MAX_PAGES):
    """
    Laedt Produkte via Shopify products.json (mit Paginierung).

    Rueckgabe:
        Liste[dict]  bei Erfolg (Shop ist Shopify)
        None         wenn der Shop kein gueltiges Shopify-JSON liefert
                     (falsche Plattform, 404, Redirect auf HTML, ...)
    """
    if collection:
        endpoint = f"{base}/collections/{collection}/products.json"
    else:
        endpoint = f"{base}/products.json"

    products = []
    seen_ids = set()

    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(
                endpoint,
                headers=HEADERS,
                params={"limit": 250, "page": page},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"    [WARN] Netzwerkfehler ({e.__class__.__name__}) bei {endpoint}")
            return None if page == 1 else products

        # Kein Shopify? Dann ist die Antwort meist HTML oder ein 404.
        if resp.status_code != 200:
            return None if page == 1 else products
        ctype = resp.headers.get("Content-Type", "")
        if "json" not in ctype and not resp.text.lstrip().startswith("{"):
            return None if page == 1 else products
        try:
            data = resp.json()
        except ValueError:
            return None if page == 1 else products
        if "products" not in data:
            return None if page == 1 else products

        batch = data["products"]
        if not batch:
            break  # letzte Seite erreicht

        new_on_page = 0
        for p in batch:
            pid = p.get("id")
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            products.append(p)
            new_on_page += 1

        # Manche Shops ignorieren den page-Parameter und liefern immer Seite 1
        # -> dann kommen keine neuen IDs -> abbrechen.
        if new_on_page == 0:
            break
        if len(batch) < 250:
            break  # weniger als eine volle Seite -> fertig

        time.sleep(random.uniform(*PER_PAGE_DELAY))

    return products


def build_current_map(base, products):
    """Erzeugt {handle: {a, p, title, url}} fuer alle Pokémon-Produkte."""
    current = {}
    for p in products:
        if not is_pokemon(p):
            continue
        handle = p.get("handle")
        if not handle:
            continue
        current[handle] = {
            "a": is_available(p),
            "p": is_preorder(p),
            "title": p.get("title", handle),
            "url": product_url(base, handle),
        }
    return current


# ===========================================================================
# NACHRICHTEN-AUFBAU
# ===========================================================================

def _format_items(items):
    """items = Liste[(titel, url, ist_vorbestellung)] -> Textzeilen (gekuerzt)."""
    lines = []
    for title, url, pre in items[:MAX_ITEMS_PER_MESSAGE]:
        tag = " (Vorbestellung)" if pre else ""
        lines.append(f"• {title}{tag}\n  {url}")
    rest = len(items) - MAX_ITEMS_PER_MESSAGE
    if rest > 0:
        lines.append(f"… und {rest} weitere.")
    return "\n".join(lines)


def build_messages(shop_name, page_label, new_items, restock_items):
    """Erzeugt 0-2 Telegram-Nachrichten (neu / restock)."""
    msgs = []
    where = f"{shop_name}" + (f" — {page_label}" if page_label else "")

    if NOTIFY_ON_NEW and new_items:
        pre = sum(1 for _, _, p in new_items if p)
        kopf = f"🚨 NEU bei {where}: {len(new_items)} Artikel"
        if pre:
            kopf += f" (davon {pre} Vorbestellung)"
        msgs.append(kopf + "\n\n" + _format_items(new_items))

    if NOTIFY_ON_RESTOCK and restock_items:
        kopf = f"♻️ WIEDER VERFUEGBAR bei {where}: {len(restock_items)} Artikel"
        msgs.append(kopf + "\n\n" + _format_items(restock_items))

    return msgs


# ===========================================================================
# EIN SHOP PRUEFEN
# ===========================================================================

def check_shop(shop, state):
    shop_name = shop["shop"]
    base = shop["base"].rstrip("/")
    method = shop.get("method", "shopify")
    collections = shop.get("collections") or [None]  # None = Gesamtsortiment

    for collection in collections:
        page_label = collection if collection else ""
        key = f"{shop_name}::{collection or 'ALL'}"
        prev = state.get(key)
        # Alt-Format (Liste von Handles) tolerieren -> als Seed behandeln
        if isinstance(prev, list):
            prev = {h: {} for h in prev}
        prev = prev or {}

        if method == "shopify":
            products = fetch_shopify_products(base, collection)
            if products is None:
                flag = "nicht erreichbar / kein Shopify"
                print(f"[{_now()}] {shop_name}: uebersprungen ({flag})")
                return
            current = build_current_map(base, products)
        else:
            # HTML-Fallback: nur Links, keine Verfuegbarkeit/Titel
            try:
                resp = requests.get(base if collection is None else f"{base}/{collection}",
                                    headers=HEADERS, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
            except requests.RequestException as e:
                print(f"[{_now()}] {shop_name}: uebersprungen ({e.__class__.__name__})")
                return
            current = {
                url: {"a": True, "p": False, "title": url.rsplit("/", 1)[-1], "url": url}
                for url in extract_product_links(resp.text, base)
            }

        # ---- Diff bilden ----
        new_handles = current.keys() - prev.keys()
        restock_handles = {
            h for h in (current.keys() & prev.keys())
            if current[h]["a"] and not prev[h].get("a", False)
        }

        new_items = [(current[h]["title"], current[h]["url"], current[h]["p"]) for h in new_handles]
        restock_items = [(current[h]["title"], current[h]["url"], current[h]["p"]) for h in restock_handles]

        # Beim allerersten Lauf nur "einlernen", nicht spammen
        if prev:
            for msg in build_messages(shop_name, page_label, new_items, restock_items):
                notify(msg)

        n_pok = len(current)
        print(f"[{_now()}] {shop_name} ({shop['country']}): "
              f"{n_pok} Pokémon-Artikel, {len(new_items)} neu, {len(restock_items)} Restock")

        # ---- Zustand aktualisieren (nur a/p speichern) ----
        state[key] = {h: {"a": current[h]["a"], "p": current[h]["p"]} for h in current}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# HAUPTLAUF
# ===========================================================================

def run_once():
    state = load_state()
    for shop in SHOPS:
        try:
            check_shop(shop, state)
        except Exception as e:  # ein Shop darf nie den ganzen Lauf killen
            print(f"[{_now()}] FEHLER bei {shop['shop']}: {e.__class__.__name__}: {e}")
        time.sleep(random.uniform(*PER_SHOP_DELAY))
    save_state(state)
    print(f"[{_now()}] Lauf beendet.\n")


# ===========================================================================
# VERIFY-MODUS  (schneller Plattform-Check ohne Benachrichtigungen/State)
# ===========================================================================

def verify_shops():
    print("Pruefe Shops auf Shopify + Pokémon-Sortiment …\n")
    ok, fail = [], []
    for shop in SHOPS:
        base = shop["base"].rstrip("/")
        products = fetch_shopify_products(base, max_pages=1)
        if products is None:
            fail.append(shop)
            print(f"  ✗  {shop['shop']:<22} {base}   (kein Shopify / nicht erreichbar)")
        else:
            n_pok = sum(1 for p in products if is_pokemon(p))
            ok.append(shop)
            mark = "✓" if shop.get("verified") else "≈"
            print(f"  {mark}  {shop['shop']:<22} {base}   Shopify OK "
                  f"(Seite 1: {len(products)} Produkte, {n_pok} Pokémon)")
        time.sleep(random.uniform(*PER_PAGE_DELAY))

    print(f"\nErgebnis: {len(ok)} Shopify-Shops nutzbar, {len(fail)} uebersprungen.")
    print("Legende:  ✓ vorab bestaetigt   ≈ zur Laufzeit als Shopify erkannt   ✗ nicht nutzbar")


# ===========================================================================
# EINSTIEG
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="Pokémon Restock & Preorder Watcher (DACH)")
    parser.add_argument("--verify", action="store_true",
                        help="Nur pruefen, welche Shops Shopify sind (kein State, keine Nachrichten).")
    parser.add_argument("--loop", action="store_true",
                        help=f"Dauerbetrieb: alle {LOOP_INTERVAL_SECONDS}s ein Lauf.")
    args = parser.parse_args()

    if args.verify:
        verify_shops()
        return

    if args.loop:
        print(f"Dauerbetrieb gestartet (Intervall {LOOP_INTERVAL_SECONDS}s). Abbruch mit Strg+C.")
        try:
            while True:
                run_once()
                time.sleep(LOOP_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("\nBeendet.")
            sys.exit(0)
    else:
        run_once()


if __name__ == "__main__":
    main()
