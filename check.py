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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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

# Jede Meldung pingt alle auf dem Server (außer dem Wochen-Check).
PING = "@everyone"

# Überteuerte Angebote (Wiederverkäufer-Preise) werden nicht gemeldet. Als "UVP" gilt der günstigste
# Preis, den irgendein Shop je für das Set verlangt hat; gemeldet wird bis UVP + 25 %.
PRICE_TOLERANCE = 1.25
# Liegt der günstigste Preis eines Sets über dem 2,5-Fachen des günstigsten Sets derselben Serie,
# ist das Set schon "gehypt" (alt, nur noch Marktpreise) -> dann zählt der Serien-Preis als UVP.
HYPE_FACTOR = 2.5
MIN_DISPLAY_PRICE = 40  # darunter ist es sicher kein Display-Preis (Tippfehler, Einzelbooster)

# Ein Shop gilt erst nach so vielen Fehlversuchen hintereinander als kaputt (bei 10-Min-Takt ≈ 30 Min).
# Einzelne Aussetzer (Timeout, kurz überlastet) lösen so keinen Fehlalarm aus.
ERROR_ALERT_AFTER = 3
# Danach wird alle so viele Fehlversuche erinnert (≈ 12 Stunden), solange es kaputt bleibt.
ERROR_REMIND_EVERY = 72

STATE_VERSION = 6  # erhöhen, wenn sich die Filter ändern -> nächster Lauf schickt neue Übersicht
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
            if e.code == 429:  # "zu viele Anfragen": so lange warten, wie der Shop verlangt
                try:
                    wait = float(e.headers.get("Retry-After", 0))
                except ValueError:
                    wait = 0
                time.sleep(min(max(wait, 10 * attempt), 60))
                continue
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

# Shopify drosselt viele Anfragen von derselben Adresse über alle Shops hinweg.
# Deshalb pro Lauf: 1x die neuesten Produkte (neue Preorders = neu angelegte Produkte, sofort erkannt)
# plus nur ein Drittel der Set-Suchen im Wechsel (Nachlieferungen fallen so nach spätestens ~30 Min. auf).
SHOPIFY_ROTATION = 3
# Die Drosselung gilt pro Adresse über ALLE Shopify-Shops – darum nie zwei Shopify-Shops gleichzeitig.
SHOPIFY_LOCK = threading.Lock()
# Beim ersten Lauf alle Set-Suchen auf einmal, damit die Übersicht vollständig ist.
FULL_SCAN = False


def shopify_price(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"{value:.2f} €" if value > 0 else "?"  # 0,00 = Platzhalter des Shops


def shopify(base, raffles=False):
    """Shopify-Shops haben öffentliche JSON-Schnittstellen inkl. Verfügbarkeit.
    raffles=True: Shop verlost manche Displays (Raffle) – die werden komplett ignoriert."""
    def json_get(url):
        try:
            return json.loads(fetch(url))
        except ValueError as e:
            raise LayoutError(f"liefert kein gültiges JSON mehr ({e})")

    def scan(codes):
        with SHOPIFY_LOCK:
            return list(scan_locked(codes))

    def scan_locked(codes):
        found = {}
        try:
            newest = json_get(f"{base}/products.json?limit=250")["products"]
            for p in newest:
                variants = p["variants"]
                found[p["handle"]] = {
                    "title": p["title"],
                    "url": f"{base}/products/{p['handle']}",
                    "price": shopify_price(variants[0]["price"] if variants else None),
                    "available": any(v["available"] for v in variants),
                }
        except (KeyError, TypeError) as e:
            raise LayoutError(f"Produktliste hat unerwartetes Format ({e})")
        time.sleep(1)

        step = 1 if FULL_SCAN else SHOPIFY_ROTATION
        for code in codes[int(time.time() // 600) % step::step]:
            data = json_get(f"{base}/search/suggest.json?" + urllib.parse.urlencode(
                {"q": code, "resources[type]": "product", "resources[limit]": 10}
            ))
            try:
                products = data["resources"]["results"]["products"]
            except (KeyError, TypeError) as e:
                raise LayoutError(f"Suche hat unerwartetes Format ({e})")
            for p in products:
                if not isinstance(p.get("available"), bool):
                    raise LayoutError("Verfügbarkeit fehlt in der Suche")
                handle = p["url"].split("?")[0].rsplit("/", 1)[-1]
                found[handle] = {
                    "title": p["title"],
                    "url": f"{base}/products/{handle}",
                    "price": shopify_price(p.get("price")),
                    "available": p["available"],
                }
            time.sleep(1)

        for handle, item in found.items():
            info = describe(item["title"], item["url"])
            if not info or not info[2]:
                continue
            if raffles:
                product = json_get(f"{base}/products/{handle}.js")
                time.sleep(1)
                if "raffle" in json.dumps(product.get("selling_plan_groups", [])).lower():
                    continue  # Verlosung statt normaler Bestellung -> uninteressant
            yield item
    scan.zero_ok = True  # gültiges JSON ohne Treffer ist hier normal (Shop führt die Sets nicht)
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


def cardcorner(codes):
    """JTL-Shop. Card Corner verkauft Displays oft als Drop ('Verkauf startet bald')."""
    base = "https://www.card-corner.de"
    seen = set()
    for code in codes:
        html = fetch(f"{base}/?" + urllib.parse.urlencode({"qs": f"{code} Display"}))
        time.sleep(0.5)
        for href, title in re.findall(
            r'class="[^"]*productbox-title[^"]*"[^>]*>\s*(?:<[^>]+>\s*)*<a[^>]*href="([^"]+)"[^>]*>\s*([^<]+)', html
        ):
            title = unescape(title).strip()
            info = describe(title, href)
            if href in seen or not info or not info[2]:
                continue
            seen.add(href)
            page = fetch(href)
            time.sleep(0.5)
            # Nur den Produktbereich lesen – darunter stehen empfohlene Artikel mit eigenen Preisen/Status.
            start, end = page.find('class="product-details'), page.find("side-right")
            if start < 0 or end < start:
                raise LayoutError(f"Produktbereich nicht gefunden: {href}")
            main = page[start:end]
            status = re.search(r'<span class="status status-(\d)"', main)
            sale_not_started = "price_on_application" in main  # "Verkauf startet bald"
            coming_soon = "coming_soon" in main  # "Versand am …", noch kein Warenkorb
            if not (status or sale_not_started or coming_soon):
                raise LayoutError(f"Lieferstatus nicht gefunden: {href}")
            price = re.search(r'itemprop="price"\s+content="([\d.]+)"', main)
            yield {
                "title": title,
                "url": href,
                "price": f"{float(price.group(1)):.2f} €" if price and float(price.group(1)) > 0 else "?",
                # status-0 = ausverkauft, 1/2 = Lieferzeit/auf Lager
                "available": bool(status) and status.group(1) != "0" and not sale_not_started and not coming_soon,
            }


SHOPS = {
    "Comic Planet": comicplanet,
    "Card Collector": shopify("https://card-collector.net"),
    "FantasiaCards": shopify("https://fantasiacards.de"),
    "GoodGameGuys": goodgameguys,
    "Card Corner": cardcorner,
    "Cardcosmos": shopify("https://cardcosmos.de"),
    "Cardlantis": shopify("https://cardlantis.de"),
    "Daesu Cards": shopify("https://daesu-cards.de"),
    "Yonko TCG": shopify("https://yonko-tcg.de"),
    "BreakTheCase": shopify("https://www.breakthecase.de"),
    "Feenturm": shopify("https://feenturm.de", raffles=True),
    "Play-Maniac": shopify("https://www.play-maniac.de"),
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


# ---------------------------------------------------------------------------
# Discord-Ausgabe: farbige Karten (Embeds) statt Fließtext
# ---------------------------------------------------------------------------

COLOR = {
    "buy": 0x2ECC71, "error": 0xE74C3C, "info": 0x95A5A6,
    "Dragon Ball": 0xF39C12, "One Piece": 0xC0392B,
}
GAME_ICON = {"Dragon Ball": "🐉", "One Piece": "🏴‍☠️"}


def post_discord(webhook, embeds, ping=False):
    payload = {"embeds": embeds, "allowed_mentions": {"parse": ["everyone"] if ping else []}}
    if ping:
        payload["content"] = PING
    body = json.dumps(payload).encode()
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


def embed_size(embed):
    """Discord zählt Titel, Text, Felder und Fußzeile zusammen (max. 6000 Zeichen pro Nachricht)."""
    return (len(embed.get("title", "")) + len(embed.get("description", ""))
            + len(embed.get("footer", {}).get("text", ""))
            + sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", [])))


def as_text(embed):
    parts = [embed.get("title", ""), embed.get("description", "")]
    parts += [f"{f['name']}: {f['value']}" for f in embed.get("fields", [])]
    return "\n".join(p for p in parts if p)


def send_discord(embeds, ping=True):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    print("\n\n".join(as_text(e) for e in embeds))
    if not webhook:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # Auf GitHub ohne Webhook würde der Alarm stumm laufen -> lieber Lauf fehlschlagen lassen.
            sys.exit("DISCORD_WEBHOOK_URL fehlt! Secret in GitHub anlegen (siehe README).")
        print("(Testmodus: DISCORD_WEBHOOK_URL nicht gesetzt, nichts gesendet)")
        return
    # Max. 10 Karten und 6000 Zeichen pro Nachricht -> bei Bedarf auf mehrere Nachrichten verteilen.
    batch, size = [], 0
    for embed in embeds:
        s = embed_size(embed)
        if batch and (size + s > 5500 or len(batch) == 10):
            post_discord(webhook, batch, ping)
            ping = False  # ein Ping pro Lauf reicht
            time.sleep(1)
            batch, size = [], 0
        batch.append(embed)
        size += s
    if batch:
        post_discord(webhook, batch, ping)


def main_code(item):
    """Set-Code, unter dem ein Display einsortiert wird (z. B. 'OP15 EB04 ...' -> EB04)."""
    info = describe(item["title"], item["url"])
    if not info:
        return "?"
    # Codes aus dem Titel haben Vorrang; im Link vertippen sich Shops manchmal.
    in_title = {f"{p}{int(n):02d}" for p, n in CODE_RE.findall(item["title"].upper().replace("-", " "))}
    codes = sorted(set(info[1]) & in_title) or info[1]
    special = [c for c in codes if not c.startswith(("OP", "FB"))]
    return (special or codes)[-1]


def sort_key(code):
    """Serien in der Reihenfolge von SERIES, darin aufsteigend (FB11, FB12, FB13 …)."""
    prefix = re.match(r"[A-Z]+", code).group(0)
    order = [s["prefix"] for s in SERIES]
    return (order.index(prefix) if prefix in order else 99, code_number(code))


def price_de(price):
    return "Preis folgt" if price == "?" else re.sub(r"^(\d+)\.(\d{2})", r"\1,\2", price)


def price_value(item):
    m = re.search(r"\d+(?:[.,]\d{1,2})?", item["price"])
    return float(m.group(0).replace(",", ".")) if m else float("inf")


def status_icon(item):
    return "🟢" if item["available"] else "🔴"


def shop_line(item):
    return f"{status_icon(item)} [{item['shop']}]({item['url']}) · {price_de(item['price'])}"


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def notice(kind, title, description=""):
    return {"title": title, "description": description, "color": COLOR[kind], "timestamp": now_iso()}


def number_de(value):
    return f"{value:.2f}".replace(".", ",")


def update_ref_prices(ref, items):
    """Merkt sich pro Set den günstigsten je gesehenen Preis (sinkt nur, steigt nie)."""
    for item in items.values():
        p = price_value(item)
        if MIN_DISPLAY_PRICE <= p < float("inf"):
            code = main_code(item)
            ref[code] = min(ref.get(code, p), p)


def uvp(code, ref):
    """Geschätzte UVP eines Sets; None, wenn noch kein Preis bekannt ist."""
    prefix = re.match(r"[A-Z]+", code).group(0)
    series = [v for c, v in ref.items() if re.match(r"[A-Z]+", c).group(0) == prefix]
    if code not in ref:
        return min(series) if series else None
    return ref[code] if ref[code] <= HYPE_FACTOR * min(series) else min(series)


def overpriced(item, ref):
    limit = uvp(main_code(item), ref)
    return limit is not None and price_value(item) < float("inf") and price_value(item) > limit * PRICE_TOLERANCE


def event(kind, headline, item, note="", ref=None):
    estimate = uvp(main_code(item), ref or {})
    return {
        "title": headline[:256],
        "url": item["url"],
        "color": COLOR[kind],
        "description": item["title"] + (f"\n\n{note}" if note else ""),
        "fields": [
            {"name": "Shop", "value": item["shop"], "inline": True},
            {"name": "Preis", "value": price_de(item["price"]), "inline": True},
            {"name": "Set", "value": f"{GAME_ICON.get(item['game'], '')} {main_code(item)}", "inline": True},
        ],
        "footer": {"text": f"UVP ca. {number_de(estimate)} €" if estimate else "UVP noch unbekannt"},
        "timestamp": now_iso(),
    }


def overview(items, broken=(), ref=None):
    embeds = [notice(
        "info", "📋 Preorder-Alarm – Übersicht",
        "🟢 bestellbar  ·  🔴 noch nicht / ausverkauft\n"
        "Ändert sich etwas, kommt eine eigene Nachricht."
        + (f"\n\n⚠️ Diesmal nicht erreichbar: {', '.join(broken)}" if broken else ""),
    )]
    for game in GAMES:
        by_code = {}
        for item in items.values():
            if item.get("game") == game:
                by_code.setdefault(main_code(item), []).append(item)
        fields = []
        for code in sorted(by_code, key=sort_key):
            shops = sorted(by_code[code], key=lambda i: (not i["available"], price_value(i), i["shop"]))
            # Feld-Limit 1024 Zeichen: lange Listen laufen in einem Feld ohne Überschrift weiter.
            chunks = [[]]
            for line in map(shop_line, shops):
                if chunks[-1] and sum(len(l) + 1 for l in chunks[-1]) + len(line) > 1000:
                    chunks.append([])
                chunks[-1].append(line)
            for n, chunk in enumerate(chunks):
                estimate = uvp(code, ref or {})
                name = f"**{code}**" + (f" · UVP ca. {estimate:.0f} €" if estimate else "")
                fields.append({"name": name if n == 0 else "\u200b", "value": "\n".join(chunk), "inline": True})
        title = f"{GAME_ICON[game]} {game}"
        if not fields:
            embeds.append(notice(game, title, "Noch nichts gefunden."))
            continue
        # Pro Karte max. 25 Felder und lieber deutlich unter 6000 Zeichen bleiben.
        part = {"title": title, "color": COLOR[game], "fields": []}
        for f in fields:
            if part["fields"] and (len(part["fields"]) == 24 or embed_size(part) + len(f["value"]) > 4500):
                embeds.append(part)
                part = {"title": f"{title} (Fortsetzung)", "color": COLOR[game], "fields": []}
            part["fields"].append(f)
        embeds.append(part)
    embeds[-1]["footer"] = {"text": f"{len(SHOPS)} Shops: " + ", ".join(SHOPS)}
    return embeds


def main():
    global FULL_SCAN
    old = load_state()
    first_run = old is None
    FULL_SCAN = first_run
    old = old or {}
    # Verlosungen aus älteren Versionen wegwerfen.
    items = {k: v for k, v in old.get("items", {}).items() if not v.get("raffle")}
    errors = dict(old.get("errors", {}))
    series_max = dict(old.get("series_max", {}))
    # Shops, die schon mal erfolgreich geprüft wurden. Ein neuer (oder beim ersten Lauf
    # ausgefallener) Shop wird beim ersten Erfolg still übernommen statt alles als neu zu melden.
    known_shops = set(old.get("known_shops", []))
    ref = dict(old.get("ref_price", {}))
    codes = codes_to_search(series_max)
    print("Suche nach:", ", ".join(codes))
    messages = []
    changes = []  # Statuswechsel; erst nach allen Shops melden, wenn die UVP-Schätzung aktuell ist

    # Alle Shops gleichzeitig abfragen (jeder Shop für sich nacheinander, also nicht mehr Last pro Shop).
    with ThreadPoolExecutor(max_workers=len(SHOPS)) as pool:
        futures = {name: pool.submit(lambda s=scan: list(s(codes))) for name, scan in SHOPS.items()}

    for name, scan in SHOPS.items():
        try:
            found = {}
            recognized = 0
            for item in futures[name].result():
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
            if recognized == 0 and not getattr(scan, "zero_ok", False):
                # Jeder dieser Shops führt Displays – null Treffer heißt fast sicher: Seite umgebaut.
                raise LayoutError("keine einzige Display-Seite erkannt")
        except Exception as e:  # ein kaputter Shop soll die anderen nicht aufhalten
            print(f"[{name}] Fehler: {type(e).__name__}: {e}", file=sys.stderr)
            count = errors.get(name, 0) + 1
            errors[name] = count
            if count == ERROR_ALERT_AFTER or (
                count > ERROR_ALERT_AFTER and (count - ERROR_ALERT_AFTER) % ERROR_REMIND_EVERY == 0
            ):
                messages.append(notice(
                    "error", f"⚠️ {name} lässt sich nicht prüfen",
                    f"Seit {count} Versuchen Fehler – dort werden Preorders gerade **nicht** erkannt!\n"
                    f"```{type(e).__name__}: {str(e)[:300]}```",
                ))
            continue
        if errors.get(name, 0) >= ERROR_ALERT_AFTER:
            messages.append(notice("buy", f"✅ {name} wird wieder geprüft"))
        errors[name] = 0
        silent = first_run or name not in known_shops
        if silent and not first_run:
            available = [i for i in found.values() if i["available"]]
            messages.append(notice(
                "info", f"ℹ️ {name} wird ab jetzt überwacht",
                f"{len(found)} Displays gefunden." + "".join("\n" + shop_line(i) for i in available)[:3500],
            ))
        known_shops.add(name)

        for key, item in found.items():
            before = items.get(key)
            # Gemeldet wird nur, was man wirklich kaufen kann (nicht vorrätig = keine Meldung).
            if not silent and item["available"]:
                if before is None:
                    changes.append(("jetzt bestellbar!", item))
                elif not before["available"]:
                    # "wieder" nur, wenn es schon mal bestellbar war (Nachschub)
                    changes.append(("wieder bestellbar!" if before.get("was_available") else "jetzt bestellbar!", item))
            item["was_available"] = item["available"] or bool((before or {}).get("was_available"))
            items[key] = item

    update_ref_prices(ref, items)
    for text, item in changes:
        if overpriced(item, ref):
            print(f"Überteuert, nicht gemeldet: {item['shop']} {item['title']} {item['price']}")
            continue
        messages.append(event("buy", f"🟢 {main_code(item)} {text}", item, "", ref))

    today = datetime.date.today()
    week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"

    if first_run:
        messages = overview(items, [n for n, c in errors.items() if c], ref)
    quiet = []
    if not first_run and old.get("status_week") != week:
        # Einmal pro Woche ein Lebenszeichen (ohne Ping): Bleibt es aus, läuft der Alarm nicht mehr.
        broken = [n for n, c in errors.items() if c >= ERROR_ALERT_AFTER]
        quiet.append(notice(
            "info", "💓 Wochen-Check",
            f"Preorder-Alarm läuft – {len(items)} Displays in {len(SHOPS)} Shops überwacht.\n"
            + (f"⚠️ Gestört: {', '.join(broken)}" if broken else "✅ Alle Shops ok."),
        ))

    if messages:
        send_discord(messages)
    if quiet:
        send_discord(quiet, ping=False)

    save_state({
        "version": STATE_VERSION,
        "items": items,
        "errors": errors,
        "series_max": series_max,
        "ref_price": ref,
        "known_shops": sorted(known_shops),
        "status_week": week,
        # Einmal täglich ändert sich die Datei, damit GitHub das Repo als aktiv ansieht.
        "heartbeat": today.isoformat(),
    })


if __name__ == "__main__":
    main()
