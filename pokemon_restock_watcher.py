#!/usr/bin/env python3
"""
Pokemon Restock & Preorder Watcher v6 (Europa-Edition)
------------------------------------------------------
Ueberwacht TCG-Shops in ganz Europa auf

  * NEUE Pokémon-Produkte (inkl. Vorbestellungen), sobald sie im Shop auftauchen
  * WIEDER VERFUEGBARE Pokémon-Produkte (echter Restock: war ausverkauft -> jetzt kaufbar)

und schickt dir dazu eine Telegram-Nachricht MIT Produktname und Direktlink.

Was ist neu gegenueber v5
-------------------------
1. NEBENLAEUFIG: alle Shops werden parallel geprueft (ThreadPool) statt nacheinander.
   -> ein kompletter Lauf dauert jetzt Sekunden statt Minuten.
2. WOOCOMMERCE: zusaetzlich zu Shopify (products.json) werden jetzt auch
   WooCommerce-Shops ueber die oeffentliche Store-API gelesen
   (/wp-json/wc/store/v1/products). Damit sind viele europaeische Shops
   erreichbar, die NICHT auf Shopify laufen (z. B. viele GR/CZ/DE-Shops).
3. FIX gegen Falschmeldungen: neue Artikel werden nur noch gemeldet, wenn man
   sie auch kaufen oder vorbestellen kann. "Neu, aber schon ausverkauft" wird
   nicht mehr als Fund gemeldet (war die Ursache fuer deine unsicheren Treffer).
4. ROBUSTER: kurze Timeouts (tote Domains blockieren nicht mehr), automatisches
   Ueberspringen chronisch toter Shops, versioniertes State-Format.
5. DIAGNOSE: --test (Telegram testen), --check-url (beliebigen Shop pruefen),
   --selftest (Melde-Logik offline testen), --max-runtime (enges Intervall in CI).

Aufruf
------
    python pokemon_restock_watcher.py                 # ein Ueberwachungslauf
    python pokemon_restock_watcher.py --test          # Telegram-Testnachricht senden
    python pokemon_restock_watcher.py --check-url URL # pruefen, ob ein Shop lesbar ist
    python pokemon_restock_watcher.py --verify        # alle Shops in der Liste pruefen
    python pokemon_restock_watcher.py --selftest      # Melde-Logik ohne Netz testen
    python pokemon_restock_watcher.py --loop          # dauerhaft laufen (lokal)
    python pokemon_restock_watcher.py --max-runtime 13  # ~13 Min lang wiederholt pruefen (CI)

Abhaengigkeiten:  pip install requests
"""

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

STATE_FILE = "watcher_state.json"
STATE_SCHEMA = 6  # bei Formataenderung erhoehen -> alter State wird neu eingelernt

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "DEIN_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "DEINE_CHAT_ID")

# --- Verhalten ---
MAX_WORKERS = 8                      # wie viele Shops parallel geprueft werden
REQUEST_TIMEOUT = (5, 10)            # (Verbindungs-, Lese-Timeout) in Sekunden
MAX_PAGES = 10                       # max. Katalogseiten je Shop
SHOPIFY_PER_PAGE = 250
WOO_PER_PAGE = 100
PER_PAGE_DELAY = (0.3, 0.8)          # kleine Pause zwischen Seiten DESSELBEN Shops
LOOP_INTERVAL_SECONDS = 180          # Pause zwischen Laeufen im --loop / --max-runtime
MAX_ITEMS_PER_MESSAGE = 15           # so viele Artikel werden einzeln gelistet
TELEGRAM_MSG_DELAY = 0.5             # Pause zwischen Telegram-Nachrichten
TELEGRAM_MAX_CHARS = 3900            # unter dem Telegram-Limit (4096) bleiben

NOTIFY_ON_NEW = True                 # neue Produkte / Vorbestellungen melden
NOTIFY_ON_RESTOCK = True             # "wieder verfuegbar" melden
# FIX: neue Artikel nur melden, wenn kaufbar ODER Vorbestellung.
# So kommt kein "NEU"-Alarm mehr fuer Produkte, die schon ausverkauft sind.
NOTIFY_NEW_REQUIRES_ACTIONABLE = True

SKIP_AFTER_FAILS = 6                 # Shop nach so vielen Fehl-Laeufen in Folge ueberspringen
REPROBE_EVERY = 20                   # ... aber alle N Laeufe trotzdem nochmal testen

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersoenlicherRestockWatcher/1.0)",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# Woran erkennt der Watcher "Pokémon"? (Kleinbuchstaben; "pok" faengt Pokemon/Pokémon)
POKEMON_KEYWORDS = ["pok"]

# Woran erkennt der Watcher eine Vorbestellung?
PREORDER_KEYWORDS = [
    "vorbestell", "preorder", "pre-order", "pre order", "vorverkauf",
    "coming soon", "προπαραγγελ", "predobjedn", "předobjedn",
]

# ---------------------------------------------------------------------------
# SHOP-LISTE
#
#   shop      : Anzeigename
#   country   : DE / AT / GR / CZ / ...
#   base      : Shop-Basis-URL OHNE abschliessenden Slash
#   platform  : optional "shopify" oder "woocommerce" als Hinweis.
#               Fehlt der Hinweis, wird die Plattform zur Laufzeit erkannt
#               (erst Shopify, dann WooCommerce). Erkanntes Ergebnis wird
#               gemerkt, damit spaetere Laeufe nur 1 Request pro Shop brauchen.
#   collections: optional; nur diese Shopify-Collection(s) statt Gesamtsortiment.
#   verified  : rein informativ.
#
# Neue Shops einfach ergaenzen. Was weder Shopify noch WooCommerce ist (oder
# nicht erreichbar), wird automatisch sauber uebersprungen. Zum Testen eines
# einzelnen Shops:  python pokemon_restock_watcher.py --check-url <url>
# ---------------------------------------------------------------------------

SHOPS = [
    # ===================== DEUTSCHLAND — Shopify bestaetigt =====================
    {"shop": "TCGViert",         "country": "DE", "base": "https://tcgviert.com",         "platform": "shopify", "verified": True},
    {"shop": "Feenturm",         "country": "DE", "base": "https://feenturm.de",          "platform": "shopify", "verified": True},
    {"shop": "CardCosmos",       "country": "DE", "base": "https://cardcosmos.de",        "platform": "shopify", "verified": True},
    {"shop": "TradingToys",      "country": "DE", "base": "https://www.tradingtoys.de",   "platform": "shopify", "verified": True},
    {"shop": "KEEPSEVEN",        "country": "DE", "base": "https://keepseven.de",         "platform": "shopify", "verified": True},
    {"shop": "BulkParadise TCG", "country": "DE", "base": "https://bulkparadise-tcg.de",  "platform": "shopify", "verified": True},
    {"shop": "CrispyCards",      "country": "DE", "base": "https://crispycards.de",       "platform": "shopify", "verified": True},

    # ===================== DEUTSCHLAND — Kandidaten (Laufzeit-Check) =====================
    {"shop": "Pokitrio",         "country": "DE", "base": "https://www.pokitrio.de",      "verified": None},
    {"shop": "Major Cards TCG",  "country": "DE", "base": "https://majorcardstcg.com",    "verified": None},
    {"shop": "YONKO TCG",        "country": "DE", "base": "https://yonko-tcg.de",         "verified": None},
    {"shop": "KTCards",          "country": "DE", "base": "https://ktcards.de",           "verified": None},
    {"shop": "TcG Love",         "country": "DE", "base": "https://tcg-love.de",          "verified": None},
    {"shop": "LottiCards",       "country": "DE", "base": "https://www.lotticards.de",    "verified": None},
    {"shop": "Collect-It",       "country": "DE", "base": "https://www.collect-it.de",    "verified": None},
    {"shop": "God of Cards",     "country": "DE", "base": "https://godofcards.com",       "verified": None},

    # ===================== OESTERREICH — Shopify bestaetigt =====================
    {"shop": "Vinticards",       "country": "AT", "base": "https://vinticards.at",        "platform": "shopify", "verified": True},
    {"shop": "Cardstore.at",     "country": "AT", "base": "https://cardstore.at",         "platform": "shopify", "verified": True},

    # ===================== OESTERREICH — Kandidaten (Laufzeit-Check) =====================
    {"shop": "Butti Cards",          "country": "AT", "base": "https://www.butticards.at",        "verified": None},
    {"shop": "Sammelkarten-Shop.at", "country": "AT", "base": "https://www.sammelkarten-shop.at", "verified": None},
    {"shop": "TCG-Shop.at",          "country": "AT", "base": "https://www.tcg-shop.at",          "verified": None},
    {"shop": "PokeVend",             "country": "AT", "base": "https://pokevend.at",              "verified": None},
    {"shop": "Cardcorner",           "country": "AT", "base": "https://cardcorner.at",            "verified": None},
    {"shop": "Grubi & Co",           "country": "AT", "base": "https://www.grubi-co.at",          "verified": None},
    {"shop": "SpielRaum",            "country": "AT", "base": "https://www.spielraum.co.at",      "verified": None},

    # ===================== EUROPA — Kandidaten (Plattform unbestaetigt) =====================
    # Bitte mit --verify oder --check-url pruefen. Viele nationale Shops laufen
    # leider auf OpenCart/PrestaShop und lassen sich (noch) NICHT auslesen.
    # ExtremePokeCorner ist bestaetigt WooCommerce (Store-API-Verfuegbarkeit offen).
    {"shop": "ExtremePokeCorner (GR)", "country": "GR", "base": "https://extremepokecorner.com", "platform": "woocommerce", "verified": None},
    {"shop": "Pokemon Center (GR)",    "country": "GR", "base": "https://www.pokemoncenter.gr",  "verified": None},
    {"shop": "Nerdom (GR)",            "country": "GR", "base": "https://www.nerdom.gr",          "verified": None},
    {"shop": "Cardstore.cz",           "country": "CZ", "base": "https://www.cardstore.cz",       "verified": None},
    {"shop": "TCGKarty.cz",            "country": "CZ", "base": "https://www.tcgkarty.cz",        "verified": None},

    # ===================== LEGACY / UNBESTAETIGT =====================
    {"shop": "FantasiaCards",  "country": "DE", "base": "https://fantasiacards.de", "verified": None},
    {"shop": "Poke-Corner",    "country": "DE", "base": "https://poke-corner.de",   "verified": None},
    {"shop": "Kartenkrake",    "country": "DE", "base": "https://kartenkrake.de",   "verified": None},
    {"shop": "Taschenmonster", "country": "DE", "base": "https://taschenmonster.de","verified": None},
    {"shop": "CardBuddies",    "country": "DE", "base": "https://cardbuddies.de",   "verified": None},
    {"shop": "TCG-Nord",       "country": "DE", "base": "https://tcg-nord.de",      "verified": None},
    {"shop": "Card-Panda",     "country": "DE", "base": "https://card-panda.de",    "verified": None},
    {"shop": "UltraCards",     "country": "DE", "base": "https://ultracards.de",    "verified": None},
    {"shop": "Card Collector", "country": "DE", "base": "https://cardcollector.de", "verified": None},
]


# ===========================================================================
# ZUSTAND LADEN / SPEICHERN
# ===========================================================================

def _empty_state():
    return {"_meta": {"schema": STATE_SCHEMA, "run_count": 0}, "shops": {}, "health": {}}


def load_state():
    if not os.path.exists(STATE_FILE):
        return _empty_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        print("[WARN] watcher_state.json unlesbar — starte mit leerem Zustand.")
        return _empty_state()
    if not isinstance(data, dict) or data.get("_meta", {}).get("schema") != STATE_SCHEMA:
        # Alt-/Fremdformat: nicht spammen, sondern sauber neu einlernen.
        print("[INFO] State-Format veraltet — lerne einmalig neu ein (keine Flut-Meldungen).")
        return _empty_state()
    data.setdefault("shops", {})
    data.setdefault("health", {})
    data.setdefault("_meta", {"schema": STATE_SCHEMA, "run_count": 0})
    return data


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)  # atomar -> kein kaputter State bei Absturz


# ===========================================================================
# HILFSFUNKTIONEN
# ===========================================================================

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _blob(*parts):
    """Baut aus beliebigen Teilen (Strings, Listen, {name:..}-Dicts) einen
    durchsuchbaren Kleinbuchstaben-String."""
    out = []
    for p in parts:
        if isinstance(p, (list, tuple)):
            for x in p:
                out.append(x.get("name", "") if isinstance(x, dict) else str(x))
        elif isinstance(p, dict):
            out.append(p.get("name", ""))
        else:
            out.append(str(p))
    return " ".join(out).lower()


def is_pokemon(item):
    return any(kw in item["blob"] for kw in POKEMON_KEYWORDS)


def is_preorder(item):
    return any(kw in item["blob"] for kw in PREORDER_KEYWORDS)


def build_current_map(items):
    """Erzeugt {id: {a, p, title, url}} fuer alle Pokémon-Produkte."""
    current = {}
    for it in items:
        if not is_pokemon(it):
            continue
        current[it["id"]] = {
            "a": it["available"],
            "p": is_preorder(it),
            "title": it["title"],
            "url": it["url"],
        }
    return current


def _get(url, params=None):
    return requests.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)


# ===========================================================================
# PLATTFORM-ABRUF  (jede Funktion gibt eine Liste normierter Items zurueck
# oder None, wenn der Shop diese Plattform nicht ist.)
#
# Normiertes Item:
#   {"id": str, "title": str, "url": str, "available": bool, "blob": str}
# ===========================================================================

def _shopify_items(base, products):
    items = []
    for p in products:
        handle = p.get("handle")
        if not handle:
            continue
        available = any(v.get("available") for v in p.get("variants", []))
        items.append({
            "id": handle,
            "title": p.get("title", handle),
            "url": f"{base}/products/{handle}",
            "available": available,
            "blob": _blob(p.get("title", ""), p.get("product_type", ""),
                          p.get("vendor", ""), p.get("tags", "")),
        })
    return items


def fetch_shopify(base, collection=None):
    """Shopify products.json (mit Paginierung). None, wenn kein Shopify."""
    endpoint = (f"{base}/collections/{collection}/products.json"
                if collection else f"{base}/products.json")
    products, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        try:
            r = _get(endpoint, {"limit": SHOPIFY_PER_PAGE, "page": page})
        except requests.RequestException:
            return None if page == 1 else products
        if r.status_code != 200:
            return None if page == 1 else products
        ctype = r.headers.get("Content-Type", "")
        if "json" not in ctype and not r.text.lstrip().startswith("{"):
            return None if page == 1 else products
        try:
            data = r.json()
        except ValueError:
            return None if page == 1 else products
        if not isinstance(data, dict) or "products" not in data:
            return None if page == 1 else products
        batch = data["products"]
        if not batch:
            break
        new = 0
        for p in batch:
            pid = p.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            products.append(p)
            new += 1
        if new == 0 or len(batch) < SHOPIFY_PER_PAGE:
            break
        time.sleep(random.uniform(*PER_PAGE_DELAY))
    return products


def _woo_items(base, products):
    items = []
    for p in products:
        slug = p.get("slug") or (str(p["id"]) if p.get("id") else None)
        if not slug:
            continue
        available = bool(p.get("is_in_stock", False)) and bool(p.get("is_purchasable", True))
        items.append({
            "id": slug,
            "title": p.get("name", slug),
            "url": p.get("permalink") or f"{base}/?p={p.get('id', '')}",
            "available": available,
            "blob": _blob(p.get("name", ""), p.get("categories", []), p.get("tags", [])),
        })
    return items


def fetch_woocommerce(base, collection=None):
    """WooCommerce Store-API. None, wenn kein (offenes) WooCommerce."""
    for path in ("/wp-json/wc/store/v1/products", "/wp-json/wc/store/products"):
        endpoint = base + path
        products, reached = [], False
        for page in range(1, MAX_PAGES + 1):
            try:
                r = _get(endpoint, {"per_page": WOO_PER_PAGE, "page": page})
            except requests.RequestException:
                break
            if r.status_code != 200:
                break
            try:
                data = r.json()
            except ValueError:
                break
            if not isinstance(data, list):
                break
            reached = True
            if not data:
                break
            products.extend(data)
            total_pages = r.headers.get("X-WP-TotalPages")
            if (total_pages and page >= int(total_pages)) or len(data) < WOO_PER_PAGE:
                break
            time.sleep(random.uniform(*PER_PAGE_DELAY))
        if reached:
            return products
    return None


def fetch_items(base, platform_hint=None, collection=None):
    """Erkennt die Plattform (oder nutzt den Hinweis) und gibt (platform, items)
    zurueck. (None, None), wenn der Shop nicht lesbar ist."""
    base = base.rstrip("/")
    if platform_hint == "shopify":
        order = [("shopify", fetch_shopify)]
    elif platform_hint in ("woocommerce", "woo"):
        order = [("woocommerce", fetch_woocommerce)]
    else:
        order = [("shopify", fetch_shopify), ("woocommerce", fetch_woocommerce)]

    for platform, fn in order:
        raw = fn(base, collection)
        if raw is not None:
            items = _shopify_items(base, raw) if platform == "shopify" else _woo_items(base, raw)
            return platform, items
    return None, None


# ===========================================================================
# EIN SHOP PRUEFEN  (laeuft im Worker-Thread; fasst KEINEN gemeinsamen Zustand
# an und sendet KEINE Nachrichten — das macht der Hauptthread.)
# ===========================================================================

def check_shop_worker(shop, prev_shops_state, hint):
    name = shop["shop"]
    base = shop["base"].rstrip("/")
    collections = shop.get("collections") or [None]
    res = {"shop": name, "ok": False, "platform": None, "error": None, "collections": []}

    for collection in collections:
        key = f"{name}::{collection or 'ALL'}"
        prev = prev_shops_state.get(key, {})
        prev_items = prev.get("items", {}) if isinstance(prev, dict) else {}

        platform, items = fetch_items(base, hint, collection)
        if items is None:
            res["error"] = "kein Shopify/WooCommerce erreichbar"
            continue

        res["ok"] = True
        res["platform"] = platform
        current = build_current_map(items)

        new_ids = current.keys() - prev_items.keys()
        restock_ids = {
            i for i in (current.keys() & prev_items.keys())
            if current[i]["a"] and not prev_items[i].get("a", False)
        }

        new_items = []
        for i in new_ids:
            c = current[i]
            if NOTIFY_NEW_REQUIRES_ACTIONABLE and not (c["a"] or c["p"]):
                continue  # neu, aber ausverkauft -> nicht melden
            new_items.append((c["title"], c["url"], c["p"], c["a"]))
        restock_items = [(current[i]["title"], current[i]["url"], current[i]["p"], True)
                         for i in restock_ids]

        res["collections"].append({
            "key": key,
            "platform": platform,
            "country": shop.get("country", ""),
            "current": {i: {"a": current[i]["a"], "p": current[i]["p"]} for i in current},
            "n_pokemon": len(current),
            "n_new_raw": len(new_ids),
            "new_items": new_items,
            "restock_items": restock_items,
            "had_prev": bool(prev_items),
        })
    return res


# ===========================================================================
# NACHRICHTEN
# ===========================================================================

def _fmt(items):
    lines = []
    for title, url, pre, avail in items[:MAX_ITEMS_PER_MESSAGE]:
        tags = []
        if pre:
            tags.append("Vorbestellung")
        if not avail and not pre:
            tags.append("ausverkauft")
        suffix = f" ({', '.join(tags)})" if tags else ""
        lines.append(f"• {title}{suffix}\n  {url}")
    rest = len(items) - MAX_ITEMS_PER_MESSAGE
    if rest > 0:
        lines.append(f"… und {rest} weitere.")
    return "\n".join(lines)


def build_messages(shop_name, col):
    msgs = []
    label = col["key"].split("::", 1)[1]
    where = shop_name if label == "ALL" else f"{shop_name} — {label}"
    if NOTIFY_ON_NEW and col["new_items"]:
        pre = sum(1 for _, _, p, _ in col["new_items"] if p)
        head = f"🚨 NEU bei {where}: {len(col['new_items'])} Artikel"
        if pre:
            head += f" (davon {pre} Vorbestellung)"
        msgs.append(head + "\n\n" + _fmt(col["new_items"]))
    if NOTIFY_ON_RESTOCK and col["restock_items"]:
        head = f"♻️ WIEDER VERFÜGBAR bei {where}: {len(col['restock_items'])} Artikel"
        msgs.append(head + "\n\n" + _fmt(col["restock_items"]))
    return msgs


def _chunk(text, limit):
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        parts.append(cur)
    return parts


def notify(message):
    """Schickt eine Telegram-Nachricht. True bei Erfolg."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "DEIN_BOT_TOKEN":
        print("[NOTIFY] (kein Telegram-Token gesetzt)\n" + message + "\n")
        return True
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "disable_web_page_preview": "true"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[WARN] Telegram-Antwort {r.status_code}: {r.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[WARN] Telegram fehlgeschlagen: {e}")
        return False


def send_all(messages):
    for m in messages:
        for chunk in _chunk(m, TELEGRAM_MAX_CHARS):
            notify(chunk)
            time.sleep(TELEGRAM_MSG_DELAY)


# ===========================================================================
# HAUPTLAUF
# ===========================================================================

def run_once():
    state = load_state()
    meta = state["_meta"]
    run_count = meta.get("run_count", 0) + 1
    meta["run_count"] = run_count
    shops_state = state["shops"]
    health = state["health"]

    # Welche Shops pruefen wir? Chronisch tote ueberspringen, aber ab und zu neu testen.
    active = []
    for shop in SHOPS:
        streak = health.get(shop["shop"], {}).get("fail_streak", 0)
        if streak >= SKIP_AFTER_FAILS and (run_count % REPROBE_EVERY) != 0:
            continue
        active.append(shop)

    # Parallel abrufen. Als Plattform-Hinweis nehmen wir den Eintrag aus der
    # Shop-Liste oder die zuletzt erkannte Plattform (spart Requests).
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {}
        for shop in active:
            hint = shop.get("platform") or health.get(shop["shop"], {}).get("platform")
            futures[ex.submit(check_shop_worker, shop, shops_state, hint)] = shop
        for fut in as_completed(futures):
            shop = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"shop": shop["shop"], "ok": False,
                                "error": f"{e.__class__.__name__}: {e}", "collections": []})

    # Ab hier wieder single-threaded: State schreiben + Nachrichten sammeln.
    outgoing = []
    for res in results:
        name = res["shop"]
        if not res.get("ok"):
            h = health.setdefault(name, {})
            h["fail_streak"] = h.get("fail_streak", 0) + 1
            print(f"[{_now()}] {name}: uebersprungen ({res.get('error', '?')}) "
                  f"[Fehlserie {h['fail_streak']}]")
            continue
        health[name] = {"fail_streak": 0, "last_ok": _now(), "platform": res.get("platform")}
        for col in res["collections"]:
            shops_state[col["key"]] = {"items": col["current"]}
            print(f"[{_now()}] {name} ({col['country']}, {col['platform']}): "
                  f"{col['n_pokemon']} Pokémon, {col['n_new_raw']} neu (roh), "
                  f"{len(col['new_items'])} meldbar, {len(col['restock_items'])} Restock")
            if col["had_prev"]:
                outgoing.extend(build_messages(name, col))
            # sonst: erster Lauf fuer diesen Shop -> nur einlernen, nicht melden

    save_state(state)
    if outgoing:
        send_all(outgoing)
    print(f"[{_now()}] Lauf #{run_count} beendet: {len(active)} Shops geprueft, "
          f"{len(outgoing)} Nachricht(en) gesendet.\n")


def run_bounded(minutes, interval):
    """Wiederholt Laeufe fuer ~'minutes' Minuten (fuer enge Intervalle in CI)."""
    end = time.time() + minutes * 60
    n = 0
    print(f"Bounded-Loop: pruefe ~{minutes} Min lang alle {interval}s.\n")
    while True:
        n += 1
        print(f"--- Durchlauf {n} ---")
        run_once()
        if time.time() + interval >= end:
            print(f"Zeitfenster erreicht — beende nach {n} Durchlaeufen.")
            break
        time.sleep(interval)


# ===========================================================================
# DIAGNOSE-MODI
# ===========================================================================

def test_telegram():
    print("Sende Test-Nachricht an Telegram …")
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "DEIN_BOT_TOKEN":
        print("✗ Kein TELEGRAM_BOT_TOKEN gesetzt (Secret/Umgebungsvariable fehlt).")
        return
    if not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == "DEINE_CHAT_ID":
        print("✗ Keine TELEGRAM_CHAT_ID gesetzt.")
        return
    ok = notify(f"✅ Test vom Pokémon-Watcher ({_now()}). Wenn du das liest, funktioniert Telegram.")
    if ok:
        print("✓ Nachricht abgeschickt — schau in deinen Telegram-Chat.")
    else:
        print("✗ Senden fehlgeschlagen. Haeufigste Ursachen:")
        print("   1. Du hast dem Bot noch nie '/start' geschickt (Bots duerfen erst dann schreiben).")
        print("   2. Falsche chat_id.")
        print("   3. Token nicht korrekt in den GitHub-Secrets hinterlegt.")


def check_url(url):
    base = url if url.startswith("http") else "https://" + url
    base = base.rstrip("/")
    print(f"Pruefe {base} …")
    platform, items = fetch_items(base)
    if items is None:
        print("  ✗ Weder Shopify (products.json) noch offenes WooCommerce (Store-API) gefunden.")
        print("    -> Dieser Shop laesst sich mit dem Watcher aktuell nicht ueberwachen.")
        return
    n = sum(1 for it in items if is_pokemon(it))
    avail = sum(1 for it in items if is_pokemon(it) and it["available"])
    host = base.split("//", 1)[-1]
    print(f"  ✓ {platform.upper()} erkannt.")
    print(f"    {len(items)} Produkte gelesen, davon {n} Pokémon ({avail} aktuell verfuegbar).")
    print("    Eintrag zum Einfuegen in die SHOPS-Liste:")
    print(f'      {{"shop": "{host}", "country": "??", "base": "{base}", '
          f'"platform": "{platform}", "verified": True}},')


def verify_shops():
    print("Pruefe alle Shops (Shopify + WooCommerce), parallel …\n")

    def probe(shop):
        platform, items = fetch_items(shop["base"].rstrip("/"), shop.get("platform"))
        return shop, platform, items

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for shop, platform, items in ex.map(probe, SHOPS):
            if items is None:
                fail += 1
                print(f"  ✗  {shop['shop']:<24} {shop['base']}  (nicht nutzbar)")
            else:
                ok += 1
                n = sum(1 for it in items if is_pokemon(it))
                print(f"  ✓  {shop['shop']:<24} {shop['base']}  "
                      f"[{platform}] {len(items)} Produkte, {n} Pokémon")
    print(f"\nErgebnis: {ok} nutzbar, {fail} nicht nutzbar.")


def selftest():
    """Prueft die Diff-/Melde-Logik mit Testdaten — ohne Netzwerk."""
    def item(pid, title, avail, extra=""):
        return {"id": pid, "title": title, "url": f"http://x/{pid}",
                "available": avail, "blob": (title + " " + extra).lower()}

    prev_items = {
        "charizard-etb": {"a": False, "p": False},  # war ausverkauft
        "pikachu-tin":   {"a": True,  "p": False},  # war verfuegbar
    }
    items = [
        item("charizard-etb", "Pokemon Charizard ETB", True),                  # RESTOCK
        item("pikachu-tin",   "Pokemon Pikachu Tin",   True),                  # unveraendert
        item("new-soldout",   "Pokemon New Set Booster", False),               # NEU, ausverkauft -> unterdruecken
        item("new-preorder",  "Pokemon New Set ETB", False, "vorbestellung"),  # NEU + Vorbestellung -> melden
        item("new-instock",   "Pokemon New Promo", True),                      # NEU + verfuegbar -> melden
        item("random-mtg",    "Magic Booster", True),                          # kein Pokémon -> ignorieren
    ]
    current = build_current_map(items)
    new_ids = current.keys() - prev_items.keys()
    restock_ids = {i for i in (current.keys() & prev_items.keys())
                   if current[i]["a"] and not prev_items[i].get("a", False)}
    meldbar_neu = {i for i in new_ids if (current[i]["a"] or current[i]["p"])}

    assert "random-mtg" not in current, "Nicht-Pokémon wurde nicht gefiltert"
    assert restock_ids == {"charizard-etb"}, f"Restock falsch: {restock_ids}"
    assert meldbar_neu == {"new-preorder", "new-instock"}, f"Meldbare Neu falsch: {meldbar_neu}"
    assert "new-soldout" in new_ids and "new-soldout" not in meldbar_neu, \
        "Ausverkauft-Neu wurde nicht unterdrueckt"

    print("✓ Selbsttest bestanden:")
    print("   • Nicht-Pokémon (Magic) korrekt ignoriert")
    print("   • Restock erkannt (ausverkauft -> verfuegbar): charizard-etb")
    print("   • Neu + kaufbar/Vorbestellung gemeldet: new-instock, new-preorder")
    print("   • Neu, aber ausverkauft korrekt NICHT gemeldet: new-soldout")


# ===========================================================================
# EINSTIEG
# ===========================================================================

def main():
    ap = argparse.ArgumentParser(description="Pokémon Restock & Preorder Watcher (Europa)")
    ap.add_argument("--verify", action="store_true",
                    help="Alle Shops pruefen (Shopify/WooCommerce), ohne State/Nachrichten.")
    ap.add_argument("--check-url", metavar="URL",
                    help="Einen beliebigen Shop pruefen und passenden SHOPS-Eintrag ausgeben.")
    ap.add_argument("--test", action="store_true",
                    help="Test-Nachricht an Telegram senden.")
    ap.add_argument("--selftest", action="store_true",
                    help="Melde-Logik offline testen (kein Netz noetig).")
    ap.add_argument("--loop", action="store_true",
                    help=f"Dauerbetrieb (lokal): alle {LOOP_INTERVAL_SECONDS}s ein Lauf.")
    ap.add_argument("--max-runtime", type=float, metavar="MIN",
                    help="~MIN Minuten lang wiederholt pruefen, dann beenden (fuer CI).")
    ap.add_argument("--interval", type=int, default=LOOP_INTERVAL_SECONDS,
                    help="Sekunden zwischen Laeufen bei --loop / --max-runtime.")
    args = ap.parse_args()

    if args.selftest:
        selftest()
    elif args.check_url:
        check_url(args.check_url)
    elif args.test:
        test_telegram()
    elif args.verify:
        verify_shops()
    elif args.max_runtime:
        run_bounded(args.max_runtime, args.interval)
    elif args.loop:
        print(f"Dauerbetrieb gestartet (Intervall {args.interval}s). Abbruch mit Strg+C.")
        try:
            while True:
                run_once()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nBeendet.")
            sys.exit(0)
    else:
        run_once()


if __name__ == "__main__":
    main()
