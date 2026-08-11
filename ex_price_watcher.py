"""
EX-Aera Preis-Watcher (Holo & Reverse Holo)
-------------------------------------------
Beobachtet ALLE Karten der Pokémon EX-Aera (2003-2007, 16 Sets) auf
auffaellige Preisrutsche und meldet sie per Telegram.

Datenquelle: pokemontcg.io (kostenlose API, liefert Cardmarket-Preise in EUR).
Cardmarket selbst hat seine API fuer neue Nutzer geschlossen und verbietet
Scraping - deshalb dieser Weg. Die Preise sind Marktaggregate (Trend, Tiefstpreis,
30-Tage-Schnitt), KEINE Einzelangebote. Der Bot sagt dir also "hier ist gerade
etwas deutlich unter Marktwert" - das konkrete Angebot pruefst du dann selbst
ueber den mitgeschickten Cardmarket-Link.

Zwei Signale:
  1) SCHNAEPPCHEN  - der guenstigste Anbieter liegt X % unter dem 30-Tage-Schnitt
  2) KURSSTURZ     - der Trendpreis selbst ist Y % unter den 30-Tage-Schnitt gefallen

Betriebsarten:
    python ex_price_watcher.py            # ein Ueberwachungslauf (fuer den Cron)
    python ex_price_watcher.py --report   # einmalige Uebersichtstabelle als CSV
    python ex_price_watcher.py --dry-run  # Lauf ohne Telegram, nur Konsole

Abhaengigkeiten:  pip install requests
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

STATE_FILE = "ex_price_state.json"
REPORT_FILE = "ex_price_report.csv"

# --- Telegram (EIGENER Bot, getrennt vom Restock-Watcher!) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_PRICE_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_PRICE_CHAT_ID", "")

# --- pokemontcg.io ---
API_BASE = "https://api.pokemontcg.io/v2/cards"
API_KEY = os.environ.get("POKEMONTCG_API_KEY", "")   # optional, erhoeht das Limit
PAGE_SIZE = 250
REQUEST_TIMEOUT = 30

# Welche Karten? Die EX-Aera ist bei pokemontcg.io die Serie "ex"
# (16 Sets: Ruby & Sapphire bis Power Keepers).
QUERY = "set.series:ex"

# Ersatzabfrage, falls die Serien-Abfrage nichts liefert (z.B. weil der
# Serienname anders geschrieben ist): die 16 Set-IDs direkt.
FALLBACK_QUERY = " OR ".join(f"set.id:ex{i}" for i in range(1, 17))

# --- Welche Varianten werden beobachtet? ---
# NORMAL-Seite: nur Holos. Commons/Uncommons ohne Reverse sind wertlos.
HOLO_RARITIES = [
    "rare holo",
    "rare holo ex",
]
# Gold Star ("Rare Holo Star") bleibt wie besprochen draussen.
# Zum Aktivieren einfach "rare holo star" in die Liste oben aufnehmen.

# REVERSE-Seite: automatisch jede Karte, fuer die Cardmarket Reverse-Preise
# fuehrt (das sind genau die Karten mit Reverse-Holo-Variante).
WATCH_REVERSE = True

# --- Ab wann wird gemeldet? ---
DEAL_DISCOUNT = 0.25      # Schnaeppchen: Tiefstpreis >= 25 % unter 30-Tage-Schnitt
TREND_DROP = 0.20         # Kurssturz:    Trendpreis  >= 20 % unter 30-Tage-Schnitt
MIN_AVG30 = 2.00          # Karten unter 2 EUR ignorieren (sonst Rauschen)
COOLDOWN_DAYS = 5         # gleiche Karte fruehestens nach X Tagen erneut melden
RE_ALERT_IF_LOWER = 0.10  # ...es sei denn, sie ist nochmal 10 % billiger geworden

MAX_ITEMS_PER_MESSAGE = 12
MESSAGE_PAUSE = 1.0       # Sekunden zwischen zwei Telegram-Nachrichten


# ---------------------------------------------------------------------------
# HILFSFUNKTIONEN
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def _today():
    return _now().strftime("%Y-%m-%d")


def _days_since(datestr):
    if not datestr:
        return 9999
    try:
        d = datetime.strptime(datestr, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return 9999
    return (_now() - d).days


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print("[WARN] State-Datei unlesbar - starte leer.")
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def notify(message, dry_run=False):
    if dry_run or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n[NACHRICHT]\n" + message + "\n")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "disable_web_page_preview": "true"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[WARN] Telegram {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        print(f"[WARN] Telegram fehlgeschlagen: {e}")


# ---------------------------------------------------------------------------
# DATEN HOLEN
# ---------------------------------------------------------------------------

def _fetch_page(query, page, headers):
    """
    Holt eine Seite. Rueckgabe: (liste, fehlertext).
    Bei Rate-Limit (429) oder Serverfehler (5xx) wird mehrfach mit
    wachsender Wartezeit erneut versucht.
    """
    params = {"q": query, "page": page, "pageSize": PAGE_SIZE,
              "orderBy": "set.releaseDate,number"}

    for versuch in range(1, 5):
        try:
            r = requests.get(API_BASE, headers=headers, params=params,
                             timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            print(f"    [WARN] Netzwerkfehler (Versuch {versuch}): {e}")
            time.sleep(5 * versuch)
            continue

        if r.status_code == 200:
            try:
                return r.json().get("data", []), None
            except ValueError:
                return None, "Antwort war kein gueltiges JSON"

        # Diese Faelle lohnen einen erneuten Versuch
        if r.status_code == 429 or r.status_code >= 500:
            wartezeit = 15 * versuch
            print(f"    [WARN] HTTP {r.status_code} (Versuch {versuch}) "
                  f"- warte {wartezeit}s …")
            time.sleep(wartezeit)
            continue

        # Alles andere ist dauerhaft (401/403 = Key-Problem, 400 = Abfragefehler)
        return None, f"HTTP {r.status_code}: {r.text[:300]}"

    return None, "Nach 4 Versuchen keine Antwort (Rate-Limit oder Serverproblem)"


def fetch_cards():
    """Laedt alle Karten der EX-Aera (paginiert). Gibt Liste von dicts zurueck."""
    headers = {"Accept": "application/json"}
    if API_KEY:
        headers["X-Api-Key"] = API_KEY
        print("API-Key: vorhanden")
    else:
        print("API-Key: KEINER gesetzt.")
        print("  Achtung: ohne Key sind die Limits sehr niedrig. Auf GitHub-Actions-")
        print("  Servern (geteilte IP-Adressen) reicht das haeufig nicht aus.")
        print("  Kostenlosen Key holen und als Secret POKEMONTCG_API_KEY hinterlegen.")

    # Hauptabfrage; falls die nichts liefert, Ersatzabfrage ueber die Set-IDs.
    abfragen = [
        (QUERY, "Serie 'ex'"),
        (FALLBACK_QUERY, "Set-IDs ex1-ex16"),
    ]

    for query, beschreibung in abfragen:
        print(f"\nAbfrage: {beschreibung}")
        print(f"  q = {query}")
        cards, page = [], 1

        while True:
            batch, fehler = _fetch_page(query, page, headers)

            if fehler:
                print(f"  [FEHLER] {fehler}")
                cards = []
                break

            if not batch:
                break

            cards.extend(batch)
            print(f"  Seite {page}: {len(batch)} Karten (gesamt {len(cards)})")

            if len(batch) < PAGE_SIZE:
                break
            page += 1
            time.sleep(0.5)

        if cards:
            return cards
        print(f"  -> {beschreibung} lieferte nichts.")

    return []


# ---------------------------------------------------------------------------
# KARTEN -> BEOBACHTUNGSPOSTEN
# ---------------------------------------------------------------------------

def _f(value):
    """Wandelt in float um; None/Unsinn -> None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def build_items(cards):
    """
    Erzeugt aus den Rohkarten die Beobachtungsposten.
    Pro Karte bis zu zwei: NORMAL (Holo) und REVERSE (Reverse Holo).
    """
    items = {}
    for c in cards:
        cm = c.get("cardmarket") or {}
        prices = cm.get("prices") or {}
        if not prices:
            continue

        base = {
            "name": c.get("name", "?"),
            "set": (c.get("set") or {}).get("name", "?"),
            "num": c.get("number", "?"),
            "rarity": c.get("rarity") or "",
            "url": cm.get("url", ""),
        }
        rarity_l = base["rarity"].lower()

        # ---- NORMAL / HOLO ----
        if rarity_l in HOLO_RARITIES:
            low = _f(prices.get("lowPriceExPlus")) or _f(prices.get("lowPrice"))
            items[f"{c['id']}::N"] = dict(
                base,
                variant="Holo",
                low=low,
                trend=_f(prices.get("trendPrice")),
                avg30=_f(prices.get("avg30")),
            )

        # ---- REVERSE HOLO ----
        if WATCH_REVERSE:
            r_low = _f(prices.get("reverseHoloLow"))
            r_trend = _f(prices.get("reverseHoloTrend"))
            r_avg30 = _f(prices.get("reverseHoloAvg30"))
            if r_low or r_trend or r_avg30:
                items[f"{c['id']}::R"] = dict(
                    base,
                    variant="Reverse Holo",
                    low=r_low,
                    trend=r_trend,
                    avg30=r_avg30,
                )

    return items


# ---------------------------------------------------------------------------
# SIGNAL-LOGIK
# ---------------------------------------------------------------------------

def evaluate(item):
    """
    Prueft die beiden Signale. Gibt (art, rabatt, referenzpreis) zurueck
    oder None, wenn nichts anliegt.
    Referenz ist immer der 30-Tage-Schnitt der API - also echter Marktverlauf,
    nicht unser eigener gespeicherter Wert.
    """
    avg30 = item.get("avg30")
    if not avg30 or avg30 < MIN_AVG30:
        return None

    low, trend = item.get("low"), item.get("trend")

    if low:
        disc = 1 - (low / avg30)
        if disc >= DEAL_DISCOUNT:
            return ("SCHNAEPPCHEN", disc, low)

    if trend:
        disc = 1 - (trend / avg30)
        if disc >= TREND_DROP:
            return ("KURSSTURZ", disc, trend)

    return None


def should_alert(key, price, state):
    """Cooldown: gleiche Karte nicht staendig wiederholen."""
    prev = state.get(key) or {}
    last_date = prev.get("alert_date")
    last_price = prev.get("alert_price")

    if _days_since(last_date) >= COOLDOWN_DAYS:
        return True
    # Innerhalb des Cooldowns nur, wenn es nochmal deutlich billiger wurde
    if last_price and price <= last_price * (1 - RE_ALERT_IF_LOWER):
        return True
    return False


# ---------------------------------------------------------------------------
# NACHRICHTEN
# ---------------------------------------------------------------------------

def format_block(kind, entries):
    kopf = ("💸 SCHNAEPPCHEN (unter Marktwert)" if kind == "SCHNAEPPCHEN"
            else "📉 KURSSTURZ (Trend faellt)")
    lines = [f"{kopf}: {len(entries)} Karten\n"]
    for it, disc, price, avg30 in entries[:MAX_ITEMS_PER_MESSAGE]:
        lines.append(
            f"• {it['name']} ({it['num']}) – {it['variant']}\n"
            f"  {it['set']}\n"
            f"  {price:.2f} € statt {avg30:.2f} € (30-Tage-Ø) = -{disc*100:.0f} %\n"
            f"  {it['url']}"
        )
    rest = len(entries) - MAX_ITEMS_PER_MESSAGE
    if rest > 0:
        lines.append(f"… und {rest} weitere.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# REPORT-MODUS
# ---------------------------------------------------------------------------

def write_report(items):
    rows = []
    for key, it in items.items():
        avg30, low, trend = it.get("avg30"), it.get("low"), it.get("trend")
        disc = (1 - low / avg30) if (low and avg30) else None
        rows.append({
            "Karte": it["name"],
            "Set": it["set"],
            "Nummer": it["num"],
            "Seltenheit": it["rarity"],
            "Variante": it["variant"],
            "Tiefstpreis_EUR": f"{low:.2f}" if low else "",
            "Trend_EUR": f"{trend:.2f}" if trend else "",
            "Schnitt30_EUR": f"{avg30:.2f}" if avg30 else "",
            "Abstand_zu_30T_Prozent": f"{disc*100:.1f}" if disc is not None else "",
            "Cardmarket": it["url"],
        })

    rows.sort(key=lambda r: float(r["Abstand_zu_30T_Prozent"] or -999), reverse=True)

    with open(REPORT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nReport geschrieben: {REPORT_FILE} ({len(rows)} Zeilen)")
    print("Sortiert nach groesstem Abstand zum 30-Tage-Schnitt.")


# ---------------------------------------------------------------------------
# HAUPTLAUF
# ---------------------------------------------------------------------------

def run(dry_run=False, report=False):
    print(f"[{_today()}] Lade EX-Aera Karten von pokemontcg.io …")
    cards = fetch_cards()
    if not cards:
        print("[FEHLER] Keine Karten geladen - Abbruch.")
        return 1
    print(f"{len(cards)} Karten geladen.")

    items = build_items(cards)
    print(f"{len(items)} Beobachtungsposten (Holo + Reverse Holo).")

    if report:
        write_report(items)
        return 0

    state = load_state()
    first_run = not state

    deals, crashes = [], []
    for key, it in items.items():
        result = evaluate(it)
        if result:
            kind, disc, price = result
            if not first_run and should_alert(key, price, state):
                entry = (it, disc, price, it["avg30"])
                (deals if kind == "SCHNAEPPCHEN" else crashes).append(entry)
                state.setdefault(key, {})["alert_date"] = _today()
                state.setdefault(key, {})["alert_price"] = price

        # Aktuellen Stand immer mitschreiben
        st = state.setdefault(key, {})
        st["low"] = it.get("low")
        st["trend"] = it.get("trend")
        st["avg30"] = it.get("avg30")
        st["seen"] = _today()

    if first_run:
        print("Erster Lauf: nur eingelernt, keine Benachrichtigungen.")
    else:
        deals.sort(key=lambda x: x[1], reverse=True)
        crashes.sort(key=lambda x: x[1], reverse=True)
        if deals:
            notify(format_block("SCHNAEPPCHEN", deals), dry_run)
            time.sleep(MESSAGE_PAUSE)
        if crashes:
            notify(format_block("KURSSTURZ", crashes), dry_run)
        if not deals and not crashes:
            print("Keine Auffaelligkeiten.")

    save_state(state)
    print(f"[{_today()}] Fertig. {len(deals)} Schnaeppchen, {len(crashes)} Kursstuerze.")
    return 0


def main():
    p = argparse.ArgumentParser(description="EX-Aera Preis-Watcher")
    p.add_argument("--report", action="store_true",
                   help="Einmalige Uebersichtstabelle als CSV, keine Alarme.")
    p.add_argument("--dry-run", action="store_true",
                   help="Lauf ohne Telegram-Versand (Ausgabe nur in der Konsole).")
    args = p.parse_args()
    sys.exit(run(dry_run=args.dry_run, report=args.report))


if __name__ == "__main__":
    main()
