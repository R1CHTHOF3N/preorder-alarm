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
import urllib.error
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

# Jede Discord-Nachricht pingt alle auf dem Server (auf dem eigenen Server also dich).
PING = "@everyone"

# Ein Shop gilt erst nach so vielen Fehlversuchen hintereinander als kaputt (bei 10-Min-Takt ≈ 30 Min).
# Einzelne Aussetzer (Timeout, kurz überlastet) lösen so keinen Fehlalarm aus.
ERROR_ALERT_AFTER = 3
# Danach wird alle so viele Fehlversuche erinnert (≈ 12 Stunden), solange es kaputt bleibt.
ERROR_REMIND_EVERY = 72

STATE_VERSION = 2  # erhöhen, wenn sich die Filter ändern -> nächster Lauf schickt neue Übersicht
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (preorder-alarm; privater Benachrichtigungs-Bot)"}

PREFIXES = sorted({s["prefix"] for s in SERIES}, key=len, reverse=True)
CODE_RE = re.compile(rf"\b({'|'.join(PREFIXES)})[\s-]?(\d{{1,2}})\b")


class LayoutError(Exception):
    """Die Seite sieht anders aus als erwartet – lieber Alarm als falsche Meldungen."""


# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------

def fetch(url, attempts=3):
    """Lädt eine Seite; bei Timeouts, 429 und Serverfehlern wird zweimal neu versucht."""
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == attempts:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts:
                raise
        time.sleep(5 * attempt)


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
# Kann ein Shop nicht sicher sagen, ob etwas bestellbar ist, wirft er LayoutError.
# ---------------------------------------------------------------------------

def shopify(base):
    """Shopify-Shops haben eine öffentliche JSON-Suche inkl. Verfügbarkeit."""
    def scan(codes):
        for code in codes:
            url = f"{base}/search/suggest.json?" + urllib.parse.urlencode(
                {"q": code, "resources[type]": "product", "resources[limit]": 10}
            )
            try:
                products = json.loads(fetch(url))["resources"]["results"]["products"]
            except (ValueError, KeyError, TypeError) as e:
                raise LayoutError(f"Suche liefert kein gültiges JSON mehr ({e})")
            for p in products:
                if not isinstance(p.get("available"), bool):
                    raise LayoutError("Verfügbarkeit fehlt in der Suche")
                try:
                    price = f"{float(p['price']):.2f} €"
                except (KeyError, TypeError, ValueError):
                    price = "?"
                yield {
                    "title": p["title"],
                    "url": base + p["url"].split("?")[0],
                    "price": price,
                    "available": p["available"],
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
            can_buy = "line-item/add" in buy_area
            can_notify = "stock-notification" in buy_area
            if can_buy == can_notify:
                raise LayoutError(f"Kaufbereich nicht erkennbar: {href}")
            price = re.search(r'itemprop="price"\s+content="([\d.]+)"', page)
            yield {
                "title": title,
                "url": href,
                "price": f"{float(price.group(1)):.2f} €" if price else "?",
                "available": can_buy,
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
        button = re.search(r'<input[^>]*name="btn-add-to-cart"[^>]*>', page)
        if not button:
            raise LayoutError(f"Warenkorb-Button nicht gefunden: {href}")
        m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        title = unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1))).strip() if m else href
        price = re.search(r'current-price-container">\s*(?:<[^>]+>\s*)*([\d.]+,\d{2})', page)
        yield {
            "title": title,
            "url": href,
            "price": price.group(1) + " €" if price else "?",
            "available": not re.search(r"\bdisabled\b|btn-inactive", button.group(0)),
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
    except ValueError:
        print("state.json ist beschädigt, starte neu.", file=sys.stderr)
        return None
    return state if state.get("version") == STATE_VERSION else None


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_FILE)  # nie eine halb geschriebene Datei hinterlassen


def post_discord(webhook, content):
    body = json.dumps({
        "content": content,
        "flags": 4,  # keine Link-Vorschau
        "allowed_mentions": {"parse": ["everyone"]},
    }).encode()
    for attempt in range(1, 6):
        req = urllib.request.Request(
            webhook, data=body, headers={**HEADERS, "Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=20).close()
            return
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == 5:
                raise
            try:
                wait = float(json.loads(e.read()).get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(min(wait, 30) + 0.5)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 5:
                raise
            time.sleep(3 * attempt)


def send_discord(messages, ping=True):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    print("\n\n".join(messages))
    if not webhook:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # Auf GitHub ohne Webhook würde der Alarm stumm laufen -> lieber Lauf fehlschlagen lassen.
            sys.exit("DISCORD_WEBHOOK_URL fehlt! Secret in GitHub anlegen (siehe README).")
        print("(Testmodus: DISCORD_WEBHOOK_URL nicht gesetzt, nichts gesendet)")
        return
    # Discord erlaubt max. 2000 Zeichen pro Nachricht. Titel und Link bleiben zusammen,
    # gepingt wird einmal pro Meldung (nicht pro Teilstück).
    entries = []
    for line in "\n\n".join(messages).split("\n"):
        if line.startswith("<http") and entries:
            entries[-1] += "\n" + line
        else:
            entries.append(line[:1800])
    chunks, current = [], PING + "\n" if ping else ""
    for entry in entries:
        if len(current) + len(entry) + 1 > 1900:
            chunks.append(current)
            current = ""
        current += entry + "\n"
    chunks.append(current)
    for chunk in chunks:
        post_discord(webhook, chunk)
        time.sleep(1)


def fmt(item):
    return f"**{item['title']}** – {item['price']} – {item['shop']}\n<{item['url']}>"


def main():
    old = load_state()
    first_run = old is None
    old = old or {}
    items = dict(old.get("items", {}))
    errors = dict(old.get("errors", {}))
    series_max = dict(old.get("series_max", {}))
    # Shops, die schon mal erfolgreich geprüft wurden. Ein neuer (oder beim ersten Lauf
    # ausgefallener) Shop wird beim ersten Erfolg still übernommen statt alles als 🆕 zu melden.
    known_shops = set(old.get("known_shops", []))
    codes = codes_to_search(series_max)
    print("Suche nach:", ", ".join(codes))
    messages = []

    for name, scan in SHOPS.items():
        try:
            found = {}
            recognized = 0
            for item in scan(codes):
                info = describe(item["title"], item["url"])
                if not info:
                    continue
                recognized += 1
                game, item_codes, english = info
                # Auch japanische Listungen schieben die Suche weiter (die erscheinen früher).
                for c in item_codes:
                    key = series_key(game, c)
                    series_max[key] = max(series_max.get(key, 0), code_number(c))
                if english and set(item_codes) & set(codes):
                    item.update(shop=name, game=game)
                    found[f"{name}|{item['url']}"] = item
            if recognized == 0:
                # Jeder dieser Shops führt Displays – null Treffer heißt fast sicher: Seite umgebaut.
                raise LayoutError("keine einzige Display-Seite erkannt")
        except Exception as e:  # ein kaputter Shop soll die anderen nicht aufhalten
            print(f"[{name}] Fehler: {type(e).__name__}: {e}", file=sys.stderr)
            count = errors.get(name, 0) + 1
            errors[name] = count
            if count == ERROR_ALERT_AFTER or (
                count > ERROR_ALERT_AFTER and (count - ERROR_ALERT_AFTER) % ERROR_REMIND_EVERY == 0
            ):
                messages.append(
                    f"⚠️ **{name}** lässt sich seit {count} Versuchen nicht prüfen – "
                    f"dort werden Preorders gerade NICHT erkannt!\n`{type(e).__name__}: {str(e)[:300]}`"
                )
            continue
        if errors.get(name, 0) >= ERROR_ALERT_AFTER:
            messages.append(f"✅ **{name}** wird wieder geprüft.")
        errors[name] = 0
        silent = first_run or name not in known_shops
        if silent and not first_run:
            available = [i for i in found.values() if i["available"]]
            messages.append(
                f"ℹ️ **{name}** wird ab jetzt überwacht ({len(found)} Displays)."
                + "".join("\n🟢 " + fmt(i) for i in available)
            )
        known_shops.add(name)

        for key, item in found.items():
            before = items.get(key)
            if not silent:
                if before is None and item["available"]:
                    messages.append("🟢 **JETZT BESTELLBAR (neu)**\n" + fmt(item))
                elif before is None:
                    messages.append("🆕 Neu gelistet, noch nicht bestellbar\n" + fmt(item))
                elif item["available"] and not before["available"]:
                    messages.append("🟢 **WIEDER BESTELLBAR**\n" + fmt(item))
            items[key] = item

    today = datetime.date.today()
    week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"

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
        broken = [n for n, c in errors.items() if c]
        if broken:
            lines.append(f"\n⚠️ Diesmal nicht erreichbar: {', '.join(broken)}")
        messages = ["\n".join(lines)]
    quiet = []
    if not first_run and old.get("status_week") != week:
        # Einmal pro Woche ein Lebenszeichen (ohne Ping): Bleibt es aus, läuft der Alarm nicht mehr.
        broken = [n for n, c in errors.items() if c >= ERROR_ALERT_AFTER]
        quiet.append(
            f"💓 Wochen-Check: Preorder-Alarm läuft, {len(items)} Displays in {len(SHOPS)} Shops überwacht."
            + (f" Gestört: {', '.join(broken)}" if broken else " Alle Shops ok.")
        )

    if messages:
        send_discord(messages)
    if quiet:
        send_discord(quiet, ping=False)

    save_state({
        "version": STATE_VERSION,
        "items": items,
        "errors": errors,
        "series_max": series_max,
        "known_shops": sorted(known_shops),
        "status_week": week,
        # Einmal täglich ändert sich die Datei, damit GitHub das Repo als aktiv ansieht.
        "heartbeat": today.isoformat(),
    })


if __name__ == "__main__":
    main()
