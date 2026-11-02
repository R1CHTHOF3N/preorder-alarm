"""Preorder-Alarm: prüft deutsche Shops auf englische Dragon-Ball- und One-Piece-Displays
und meldet sich per Discord.

Neue Sets (FB14, OP19, ...) werden automatisch gesucht: Für jede Serie wird die höchste bisher
gesehene Nummer gemerkt und immer auch nach den nächsten beiden Nummern gesucht.

Läuft ohne zusätzliche Pakete (nur Python-Standardbibliothek).
Ohne DISCORD_WEBHOOK_URL werden die Meldungen nur in der Konsole ausgegeben (Testmodus).
"""

import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from html import unescape

# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

# Spiele und woran man sie im Produktnamen erkennt.
GAMES = {
    "Dragon Ball": ["fusion world"],  # schließt das alte "Masters"-Spiel bewusst aus
    "One Piece": ["one piece"],
}

# Serien je Spiel. "start" = aktuelle Nummer; ab da wird automatisch hochgezählt.
SERIES = [
    {"game": "Dragon Ball", "prefix": "FB", "start": 13},   # Booster
    {"game": "Dragon Ball", "prefix": "ST", "start": 3},    # Story Booster
    {"game": "Dragon Ball", "prefix": "SB", "start": 1},    # Manga Booster
    {"game": "One Piece", "prefix": "OP", "start": 18},     # Booster
    {"game": "One Piece", "prefix": "EB", "start": 6},      # Extra Booster
    {"game": "One Piece", "prefix": "PRB", "start": 2},     # Premium Booster
    {"game": "One Piece", "prefix": "PEB", "start": 1},     # Premium Extra Booster
]

# Gesucht wird von (höchste Nummer - LOOK_BACK) bis (höchste Nummer + LOOK_AHEAD).
# LOOK_BACK ist größer, weil Shops Sets oft Monate vorher listen (z. B. OP20, während OP18 erst erscheint).
LOOK_BACK = 3
LOOK_AHEAD = 2

DISPLAY_WORDS = ["display", "booster box"]
EXCLUDE_WORDS = [
    "case", "starter", "event", "turnier", "ticket", "sleeve", "hülle", "playmat",
    "spielmatte", "illustration", "bundle", "double pack", "gift", "collection box",
]
ENGLISH_MARKERS = ["englisch", "english", "[eng]", "[en]", "(eng)", "(en)"]

# Nach so vielen Fehlversuchen hintereinander kommt eine Warnung (bei 10-Min-Takt ≈ 1 Stunde).
ERROR_ALERT_AFTER = 6

STATE_VERSION = 2  # erhöhen, wenn sich die Filter ändern -> nächster Lauf schickt neue Übersicht
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (preorder-alarm; privater Benachrichtigungs-Bot)"}

PREFIXES = sorted({s["prefix"] for s in SERIES}, key=len, reverse=True)
CODE_RE = re.compile(rf"\b({'|'.join(PREFIXES)})[\s-]?(\d{{1,2}})\b")


# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------

def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "replace")


def describe(title, url=""):
    """Liefert (Spiel, [Set-Codes], ist_englisch) oder None, wenn es kein Display ist."""
    text = f"{title} {urllib.parse.unquote(url).rsplit('/', 1)[-1]}".replace("-", " ").replace("_", " ")
    low = text.lower()
    if not any(w in low for w in DISPLAY_WORDS) or any(w in low for w in EXCLUDE_WORDS):
        return None
    game = next((g for g, words in GAMES.items() if any(w in low for w in words)), None)
    if game is None:
        return None
    prefixes = {s["prefix"] for s in SERIES if s["game"] == game}
    codes = sorted({
        f"{p}{int(n):02d}" for p, n in CODE_RE.findall(text.upper()) if p in prefixes
    })
    if not codes:
        return None
    english = any(m in low for m in ENGLISH_MARKERS)
    return game, codes, english


def series_key(game, code):
    return f"{game}|{re.match(r'[A-Z]+', code).group(0)}"


def code_number(code):
    return int(re.search(r"\d+", code).group(0))


# ---------------------------------------------------------------------------
# Shops: jede Funktion bekommt die gesuchten Codes und liefert Treffer
#   {"title", "url", "price", "available"}
# ---------------------------------------------------------------------------

def shopify(base):
    """Shopify-Shops haben eine öffentliche JSON-Suche inkl. Verfügbarkeit."""
    def scan(codes):
        for code in codes:
            url = f"{base}/search/suggest.json?" + urllib.parse.urlencode(
                {"q": code, "resources[type]": "product", "resources[limit]": 10}
            )
            data = json.loads(fetch(url))
            for p in data["resources"]["results"]["products"]:
                yield {
                    "title": p["title"],
                    "url": base + p["url"].split("?")[0],
                    "price": f"{float(p['price']):.2f} €",
                    "available": bool(p["available"]),
                }
            time.sleep(0.5)
    return scan


def comicplanet(codes):
    base = "https://www.comicplanet.de"
    seen = set()
    for code in codes:
        html = fetch(f"{base}/search?" + urllib.parse.urlencode({"search": code}))
        time.sleep(0.5)
        for href, title in re.findall(
            r'<a href="([^"]+)"\s+title="([^"]+)"\s+class="product-name', html
        ):
            title = unescape(title).strip()
            info = describe(title, href)
            if href in seen or not info or not info[2]:
                continue
            seen.add(href)
            page = fetch(href)
            time.sleep(0.5)
            # Nur den Kaufbereich des Produkts selbst ansehen, nicht die Empfehlungen darunter.
            start = page.find("product-detail-buy")
            end = page.find("product-detail-tabs", start)
            buy_area = page[start:end] if start != -1 and end != -1 else ""
            price = re.search(r'itemprop="price"\s+content="([\d.]+)"', page)
            yield {
                "title": title,
                "url": href,
                "price": f"{float(price.group(1)):.2f} €" if price else "?",
                "available": "line-item/add" in buy_area,
            }


def goodgameguys(codes):
    """Die Suche dort findet One Piece nicht per Set-Code, daher die Kategorieseiten."""
    base = "https://www.goodgameguys.de"
    wanted = set(codes)
    links = set()
    for category in ["dragonball-super-card-game", "dragonball-super-card-game-preorder", "one-piece-card-game"]:
        html = fetch(f"{base}/{category}/")
        links |= set(re.findall(r'href="(https://www\.goodgameguys\.de/[^"/]+\.html)"', html))
        time.sleep(0.5)
    for href in sorted(links):
        info = describe("", href)
        if not info or not info[2] or not wanted & set(info[1]):
            continue
        page = fetch(href)
        time.sleep(0.5)
        m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        title = unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1))).strip() if m else href
        price = re.search(r'current-price-container">\s*(?:<[^>]+>\s*)*([\d.]+,\d{2})', page)
        yield {
            "title": title,
            "url": href,
            "price": price.group(1) + " €" if price else "?",
            "available": re.search(r"leider vergriffen", page, re.I) is None,
        }


SHOPS = {
    "Comic Planet": comicplanet,
    "Card Collector": shopify("https://card-collector.net"),
    "FantasiaCards": shopify("https://fantasiacards.de"),
    "GoodGameGuys": goodgameguys,
}


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------

def codes_to_search(series_max):
    codes = []
    for s in SERIES:
        top = max(s["start"], series_max.get(f"{s['game']}|{s['prefix']}", 0))
        for n in range(max(1, top - LOOK_BACK), top + LOOK_AHEAD + 1):
            codes.append(f"{s['prefix']}{n:02d}")
    return sorted(set(codes))


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    return state if state.get("version") == STATE_VERSION else None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def send_discord(messages):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    print("\n".join(messages))
    if not webhook:
        print("(Testmodus: DISCORD_WEBHOOK_URL nicht gesetzt, nichts gesendet)")
        return
    # Discord erlaubt max. 2000 Zeichen pro Nachricht.
    chunks, current = [], ""
    for line in "\n".join(messages).split("\n"):
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = ""
        current += line + "\n"
    chunks.append(current)
    for chunk in chunks:
        body = json.dumps({"content": chunk, "flags": 4}).encode()  # flags 4 = keine Link-Vorschau
        req = urllib.request.Request(
            webhook, data=body, headers={**HEADERS, "Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=20).close()
        time.sleep(1)


def fmt(item):
    return f"**{item['title']}** – {item['price']} – {item['shop']}\n<{item['url']}>"


def main():
    old = load_state()
    first_run = old is None
    old = old or {"items": {}, "errors": {}, "series_max": {}}
    items = dict(old["items"])
    errors = dict(old.get("errors", {}))
    series_max = dict(old.get("series_max", {}))
    codes = codes_to_search(series_max)
    print("Suche nach:", ", ".join(codes))
    messages = []

    for name, scan in SHOPS.items():
        try:
            found = {}
            for item in scan(codes):
                info = describe(item["title"], item["url"])
                if not info:
                    continue
                game, item_codes, english = info
                # Auch japanische Listungen schieben die Suche weiter (die erscheinen früher).
                for c in item_codes:
                    key = series_key(game, c)
                    series_max[key] = max(series_max.get(key, 0), code_number(c))
                if english and set(item_codes) & set(codes):
                    item.update(shop=name, game=game)
                    found[f"{name}|{item['url']}"] = item
        except Exception as e:  # ein kaputter Shop soll die anderen nicht aufhalten
            print(f"[{name}] Fehler: {e}", file=sys.stderr)
            errors[name] = errors.get(name, 0) + 1
            if errors[name] == ERROR_ALERT_AFTER:
                messages.append(f"⚠️ {name} lässt sich seit {ERROR_ALERT_AFTER} Versuchen nicht abfragen ({e}).")
            continue
        if errors.get(name, 0) >= ERROR_ALERT_AFTER:
            messages.append(f"✅ {name} funktioniert wieder.")
        errors[name] = 0

        for key, item in found.items():
            before = items.get(key)
            if not first_run:
                if before is None and item["available"]:
                    messages.append("🟢 **JETZT BESTELLBAR (neu)**\n" + fmt(item))
                elif before is None:
                    messages.append("🆕 Neu gelistet, noch nicht bestellbar\n" + fmt(item))
                elif item["available"] and not before["available"]:
                    messages.append("🟢 **WIEDER BESTELLBAR**\n" + fmt(item))
            items[key] = item

    if first_run:
        lines = ["🤖 Preorder-Alarm ist aktiv (nur englische Displays). Aktueller Stand:"]
        for game in GAMES:
            game_items = sorted(
                (i for i in items.values() if i.get("game") == game),
                key=lambda i: (i["title"].lower(), i["shop"]),
            )
            lines.append(f"\n__**{game}**__")
            for item in game_items:
                lines.append(("🟢 " if item["available"] else "🔴 ") + fmt(item))
            if not game_items:
                lines.append("(noch nichts gefunden)")
        messages = ["\n".join(lines)]

    if messages:
        send_discord(messages)

    save_state({
        "version": STATE_VERSION,
        "items": items,
        "errors": errors,
        "series_max": series_max,
        # Einmal täglich ändert sich die Datei, damit GitHub das Repo als aktiv ansieht.
        "heartbeat": datetime.date.today().isoformat(),
    })


if __name__ == "__main__":
    main()
