"""Pokémon-Alarm: meldet per Discord, wenn Pokémon-Displays, Booster, Top-Trainer-/Elite-Trainer-Boxen
oder Booster-Bundles (deutsch oder englisch) zu einem normalen Preis bestellbar werden.

Neue Sets brauchen keine Pflege: Jeder Lauf liest die neuesten Produkte jedes Shops. Ein neues Set
(z. B. nach "30 Jahre" und "Delta-Herrschaft") taucht dort automatisch auf, sobald ein Shop es anlegt.

Wucherpreise werden ignoriert: Pro Produktart gibt es eine Preisgrenze (UVP + kleiner Spielraum).
Läuft ohne zusätzliche Pakete (nur Python-Standardbibliothek).
Ohne DISCORD_WEBHOOK_POKEMON werden die Meldungen nur in der Konsole ausgegeben (Testmodus).
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

# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

# Produktarten: Erkennungswörter und Preisgrenze in Euro (UVP + wenig Spielraum für Shopaufschlag).
# Alles darüber gilt als Wiederverkäufer-/Hype-Preis und wird nicht gemeldet.
KINDS = {
    "Display": {
        "words": ["display", "booster box", "boosterbox"],
        "max": 189.99,   # 36 Booster
    },
    "Top-Trainer-Box": {
        "words": ["top trainer box", "top-trainer-box", "toptrainerbox", "elite trainer box",
                  "elite-trainer-box", "etb"],
        "max": 69.99,
    },
    "Booster-Bundle": {
        "words": ["booster bundle", "booster-bundle", "boosterbundle"],
        "max": 44.99,    # 6 Booster
    },
    "Booster": {
        "words": ["booster"],
        "max": 6.99,     # ein einzelnes Booster-Pack
    },
}
KIND_ORDER = list(KINDS)  # bei mehreren Treffern gewinnt die erste (spezifischere) Art

# Alles mit diesen Wörtern ist kein gesuchtes Produkt (Zubehör, Sammelboxen, ganze Kartons …).
EXCLUDE_WORDS = [
    "schlüsselanhänger", "schluesselanhaenger", "keychain", "anhänger", "acryl", "acrylic", "case",
    "sleeve", "hülle", "huelle", "binder", "ordner", "album", "portfolio", "deck box", "deckbox",
    "playmat", "spielmatte", "plüsch", "plush", "figur", "figure", "statue", "tin", "kollektion",
    "collection", "premium", "mystery", "blind", "lot", "sammelmappe", "pin", "tasche", "rucksack",
    "toploader", "protector", "schutz", "magnet", "build & battle", "build and battle", "stadium",
    "blister", "checklane", "3-pack", "3er", "gift", "geschenk", "special box", "pokemon center",
    "pokémon center", "half booster", "mini", "costco", "bundle of", "x booster bundle", "leer", "empty",
    "psa", "bgs", "cgc", "graded", "gem mint",  # bewertete Einzelkarten
]
GAME_WORDS = ["pokémon", "pokemon"]

# Sprachen: nur Deutsch und Englisch. Japanisch, Chinesisch, Koreanisch usw. werden ignoriert.
LANG_PATTERNS = {
    "DE": r"(?:^|[\s\[\(\-–/|])(?:de|deutsch|german|dt\.?)(?=$|[\s\]\)\-–/|,.])",
    "EN": r"(?:^|[\s\[\(\-–/|])(?:en|eng|englisch|english)(?=$|[\s\]\)\-–/|,.])",
}
FOREIGN_WORDS = ["japan", "[jp]", "(jp)", " jp ", "chines", "chn", "korea", "[kr]", "thai", "indones"]

PING = "@everyone"
ERROR_ALERT_AFTER = 3     # bei 10-Min-Takt ≈ 30 Min. Fehler am Stück, bevor gewarnt wird
ERROR_REMIND_EVERY = 72   # danach Erinnerung alle ≈ 12 Std.

STATE_VERSION = 1
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pokemon_state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (pokemon-alarm; privater Benachrichtigungs-Bot)"}

# Shopify-Shops mit deutschem Impressum, die Pokémon führen.
# Nicht dabei: Pokémon Center (harte Bot-Sperre), Amazon, Smyths, Thalia, Kaufland (blocken ebenfalls).
SHOPS = {
    "Cardcosmos": "https://cardcosmos.de",
    "FantasiaCards": "https://fantasiacards.de",
    "Cardlantis": "https://cardlantis.de",
    "BreakTheCase": "https://www.breakthecase.de",
    "Feenturm": "https://feenturm.de",
    "Play-Maniac": "https://www.play-maniac.de",
    "Yonko TCG": "https://yonko-tcg.de",
    "Card Collector": "https://card-collector.net",
}

# Pro Lauf und Shop: 1x neueste Produkte + 1 Suche im Wechsel (Shopify drosselt sonst).
# So fallen neue Produkte sofort auf, Nachlieferungen älterer Produkte nach spätestens ~50 Min.
SEARCHES = ["pokemon display", "pokemon top trainer box", "pokemon elite trainer box",
            "pokemon booster bundle", "pokemon booster"]


class LayoutError(Exception):
    """Die Antwort sieht anders aus als erwartet – lieber Warnung als falsche Meldungen."""


# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------

def describe(title):
    """Liefert (Produktart, Sprache) oder None, wenn es kein gesuchtes Pokémon-Produkt ist."""
    low = " " + title.lower().replace("_", " ") + " "
    if not any(w in low for w in GAME_WORDS):
        return None
    if any(re.search(rf"(?<![a-zäöü]){re.escape(w)}(?![a-zäöü])", low) for w in EXCLUDE_WORDS):
        return None
    if any(w in low for w in FOREIGN_WORDS):
        return None
    kind = next((k for k in KIND_ORDER if any(
        re.search(rf"(?<![a-zäöü]){re.escape(w)}(?![a-zäöü])", low) for w in KINDS[k]["words"])), None)
    if kind is None:
        return None
    if kind == "Booster-Bundle" and "," in title:
        return None  # vom Shop gemischte Booster mehrerer Sets ("Set A, Set B & Set C"), kein offizielles Bundle
    langs = [lang for lang, pat in LANG_PATTERNS.items() if re.search(pat, low)]
    if len(langs) != 1:
        return None  # Sprache unklar oder gemischt -> lieber nicht melden
    return kind, langs[0]


def price_ok(item):
    return item["price"] is not None and item["price"] <= KINDS[item["kind"]]["max"] + 0.005


def wanted(item):
    return item["available"] and price_ok(item)


# ---------------------------------------------------------------------------
# Abruf
# ---------------------------------------------------------------------------

def fetch_json(url, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == attempts:
                raise
            if e.code == 429:
                try:
                    wait = float(e.headers.get("Retry-After", 0))
                except ValueError:
                    wait = 0
                time.sleep(min(max(wait, 10 * attempt), 60))
                continue
        except ValueError as e:
            raise LayoutError(f"liefert kein gültiges JSON mehr ({e})")
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts:
                raise
        time.sleep(5 * attempt)


def to_price(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None  # 0,00 = Platzhalter ("Preis folgt")


def scan_shop(base, slot, full=False):
    found = {}
    try:
        for p in fetch_json(f"{base}/products.json?limit=250")["products"]:
            variants = p["variants"]
            found[p["handle"]] = {
                "title": p["title"],
                "url": f"{base}/products/{p['handle']}",
                "price": to_price(variants[0]["price"] if variants else None),
                "available": any(v["available"] for v in variants),
            }
    except (KeyError, TypeError) as e:
        raise LayoutError(f"Produktliste hat unerwartetes Format ({e})")
    time.sleep(1)

    for query in (SEARCHES if full else [SEARCHES[slot % len(SEARCHES)]]):
        data = fetch_json(f"{base}/search/suggest.json?" + urllib.parse.urlencode(
            {"q": query, "resources[type]": "product", "resources[limit]": 10}))
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
                "price": to_price(p.get("price")),
                "available": p["available"],
            }
        time.sleep(1)

    result = {}
    for item in found.values():
        info = describe(item["title"])
        if info:
            item["kind"], item["lang"] = info
            result[item["url"]] = item
    return result


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

COLOR = {"buy": 0xFFCB05, "error": 0xE74C3C, "ok": 0x2ECC71, "info": 0x3B4CCA}
FLAG = {"DE": "🇩🇪", "EN": "🇬🇧"}


def price_de(price):
    return "Preis folgt" if price is None else f"{price:.2f} €".replace(".", ",")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def card(kind, title, description="", url=None, fields=None, footer=None):
    embed = {"title": title[:256], "description": description[:4000], "color": COLOR[kind],
             "timestamp": now_iso()}
    if url:
        embed["url"] = url
    if fields:
        embed["fields"] = fields
    if footer:
        embed["footer"] = {"text": footer}
    return embed


def item_card(headline, item):
    return card("buy", f"{headline} {item['kind']}", item["title"], item["url"], [
        {"name": "Shop", "value": item["shop"], "inline": True},
        {"name": "Preis", "value": price_de(item["price"]), "inline": True},
        {"name": "Sprache", "value": f"{FLAG[item['lang']]} {item['lang']}", "inline": True},
    ], f"Gemeldet bis {price_de(KINDS[item['kind']]['max'])} ({item['kind']})")


def item_line(item):
    return f"[{item['title'][:70]}]({item['url']}) · {item['shop']} · {price_de(item['price'])}"


def send_discord(embeds, ping=True):
    webhook = os.environ.get("DISCORD_WEBHOOK_POKEMON", "").strip()
    for e in embeds:
        print(e["title"], "-", e.get("description", "")[:300])
    if not webhook:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            sys.exit("DISCORD_WEBHOOK_POKEMON fehlt! Secret in GitHub anlegen (siehe README).")
        print("(Testmodus: DISCORD_WEBHOOK_POKEMON nicht gesetzt, nichts gesendet)")
        return
    for start in range(0, len(embeds), 10):
        payload = {"embeds": embeds[start:start + 10],
                   "allowed_mentions": {"parse": ["everyone"] if ping else []}}
        if ping:
            payload["content"] = PING
            ping = False
        body = json.dumps(payload).encode()
        for attempt in range(1, 6):
            req = urllib.request.Request(webhook, data=body, headers={
                **HEADERS, "Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=20).close()
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 and e.code < 500 or attempt == 5:
                    raise
                time.sleep(3 * attempt)
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 5:
                    raise
                time.sleep(3 * attempt)
        time.sleep(1)


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        return None
    return state if state.get("version") == STATE_VERSION else None


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


def main():
    old = load_state()
    first_run = old is None
    old = old or {}
    items = dict(old.get("items", {}))
    errors = dict(old.get("errors", {}))
    known_shops = set(old.get("known_shops", []))
    slot = int(time.time() // 600)
    alerts, quiet = [], []

    # Nacheinander statt gleichzeitig: Shopify drosselt pro Adresse über alle Shops hinweg.
    for name, base in SHOPS.items():
        try:
            found = scan_shop(base, slot, full=first_run or name not in known_shops)
        except Exception as e:  # ein kaputter Shop soll die anderen nicht aufhalten
            print(f"[{name}] Fehler: {type(e).__name__}: {e}", file=sys.stderr)
            count = errors.get(name, 0) + 1
            errors[name] = count
            if count == ERROR_ALERT_AFTER or (
                count > ERROR_ALERT_AFTER and (count - ERROR_ALERT_AFTER) % ERROR_REMIND_EVERY == 0
            ):
                quiet.append(card("error", f"⚠️ {name} lässt sich nicht prüfen",
                                  f"Seit {count} Versuchen Fehler – dort wird gerade **nichts** erkannt.\n"
                                  f"```{type(e).__name__}: {str(e)[:300]}```"))
            continue
        if errors.get(name, 0) >= ERROR_ALERT_AFTER:
            quiet.append(card("ok", f"✅ {name} wird wieder geprüft"))
        errors[name] = 0
        silent = first_run or name not in known_shops
        known_shops.add(name)
        print(f"[{name}] {len(found)} passende Produkte, {sum(map(wanted, found.values()))} zum Normalpreis bestellbar")

        for url, item in found.items():
            item["shop"] = name
            key = f"{name}|{url}"
            before = items.get(key)
            if not silent and wanted(item):
                if before is None:
                    alerts.append(item_card("🆕 Neu & bestellbar:", item))
                elif not wanted(before):
                    alerts.append(item_card("🟢 Wieder bestellbar:", item))
            items[key] = item

    today = datetime.date.today()
    week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
    if first_run or old.get("status_week") != week:
        now_ok = sorted((i for i in items.values() if wanted(i)),
                        key=lambda i: (KIND_ORDER.index(i["kind"]), i["price"] or 0))
        lines = []
        for kind in KIND_ORDER:
            group = [i for i in now_ok if i["kind"] == kind]
            if group:
                lines.append(f"\n**{kind}** (bis {price_de(KINDS[kind]['max'])})")
                lines += [f"{FLAG[i['lang']]} " + item_line(i) for i in group[:8]]
                if len(group) > 8:
                    lines.append(f"… und {len(group) - 8} weitere")
        text = "\n".join(lines) or "Gerade nichts zum Normalpreis bestellbar."
        title = "⚡ Pokémon-Alarm ist aktiv" if first_run else "💓 Wochen-Check"
        quiet.append(card("info", title,
                          "Gemeldet werden Displays, Booster, Top-Trainer-/Elite-Trainer-Boxen und "
                          "Booster-Bundles (🇩🇪/🇬🇧) – nur zum normalen Preis.\n"
                          "**Gerade zum Normalpreis bestellbar:**" + text,
                          footer=f"{len(SHOPS)} Shops: " + ", ".join(SHOPS)))

    if alerts:
        send_discord(alerts, ping=True)
    if quiet:
        send_discord(quiet, ping=False)

    save_state({
        "version": STATE_VERSION,
        "items": items,
        "errors": errors,
        "known_shops": sorted(known_shops),
        "status_week": week,
        "heartbeat": today.isoformat(),
    })


if __name__ == "__main__":
    main()
