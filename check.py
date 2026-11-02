"""Preorder-Alarm: prüft deutsche/EU-Shops auf neue TCG-Displays und meldet sich per Discord.

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
# Was überwacht wird. Neue Sets einfach hier eintragen.
#   code:     Set-Code, wie er im Produktnamen steht (FB13, OP19, ...)
#   keywords: mindestens eines davon muss im Produktnamen vorkommen
# ---------------------------------------------------------------------------
WATCH = [
    {"code": "FB12", "keywords": ["dragon", "fusion"]},
    {"code": "FB13", "keywords": ["dragon", "fusion"]},
    {"code": "ST03", "keywords": ["dragon", "fusion"]},  # Story Booster 03
    # {"code": "OP19", "keywords": ["one piece"]},
]

# Nur echte Ware melden, keine Turnier-Tickets o. Ä.
PRODUCT_WORDS = ["display", "booster", "box", "case"]
EXCLUDE_WORDS = ["event", "turnier", "ticket", "sleeve", "playmat", "spielmatte"]

# Nach so vielen Fehlversuchen hintereinander kommt eine Warnung (bei 10-Min-Takt ≈ 1 Stunde).
ERROR_ALERT_AFTER = 6

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (preorder-alarm; privater Benachrichtigungs-Bot)"}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "replace")


def normalize(text):
    """'FB-13', 'FB 13' -> 'FB13', damit verschiedene Schreibweisen gefunden werden."""
    return re.sub(r"\b([A-Z]{2})[\s-]?(\d{2})\b", r"\1\2", text.upper())


def matches(title, watch):
    t = normalize(title)
    low = title.lower()
    return (
        re.search(rf"\b{watch['code']}\b", t) is not None
        and any(k in low for k in watch["keywords"])
        and any(w in low for w in PRODUCT_WORDS)
        and not any(w in low for w in EXCLUDE_WORDS)
    )


def search_terms(code):
    return [code, f"{code[:2]}-{code[2:]}"]


# ---------------------------------------------------------------------------
# Shops
# ---------------------------------------------------------------------------

def shopify(base):
    """Shopify-Shops haben eine öffentliche JSON-Suche inkl. Verfügbarkeit."""
    def search(term):
        url = f"{base}/search/suggest.json?" + urllib.parse.urlencode(
            {"q": term, "resources[type]": "product", "resources[limit]": 10}
        )
        data = json.loads(fetch(url))
        for p in data["resources"]["results"]["products"]:
            yield {
                "title": p["title"],
                "url": base + p["url"].split("?")[0],
                "price": f"{float(p['price']):.2f} €",
                "available": bool(p["available"]),
            }
    return search


def comicplanet(term):
    base = "https://www.comicplanet.de"
    html = fetch(f"{base}/search?" + urllib.parse.urlencode({"search": term}))
    for href, title in re.findall(
        r'<a href="([^"]+)"\s+title="([^"]+)"\s+class="product-name', html
    ):
        title = unescape(title)
        if not any(matches(title, w) for w in WATCH):
            continue
        page = fetch(href)
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


def goodgameguys(term):
    base = "https://www.goodgameguys.de"
    html = fetch(f"{base}/advanced_search_result.php?" + urllib.parse.urlencode(
        {"keywords": term, "inc_subcat": 1}
    ))
    links = sorted(set(re.findall(r'href="(https://www\.goodgameguys\.de/[^"]+\.html)"', html)))
    for href in links:
        if term.replace("-", "").lower() not in href.replace("-", "").lower():
            continue
        page = fetch(href)
        m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        title = unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1))).strip() if m else href
        if not any(matches(title + " " + href.replace("-", " "), w) for w in WATCH):
            continue
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
    "TCG Corner (JP)": shopify("https://tcg-corner.com"),
}


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------

def scan_shop(name, search):
    found = {}
    for watch in WATCH:
        for term in search_terms(watch["code"]):
            for item in search(term):
                if matches(item["title"], watch):
                    item["shop"] = name
                    found[f"{name}|{item['url']}"] = item
            time.sleep(1)  # Shops nicht unnötig belasten
    return found


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def send_discord(lines):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    text = "\n".join(lines)
    print(text)
    if not webhook:
        print("(Testmodus: DISCORD_WEBHOOK_URL nicht gesetzt, nichts gesendet)")
        return
    # Discord erlaubt max. 2000 Zeichen pro Nachricht.
    chunks, current = [], ""
    for line in lines:
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


def fmt(item):
    return f"**{item['title']}** – {item['price']} – {item['shop']}\n<{item['url']}>"


def main():
    old = load_state()
    first_run = old is None
    old = old or {"items": {}, "errors": {}}
    items = dict(old["items"])
    errors = dict(old.get("errors", {}))
    messages = []

    for name, search in SHOPS.items():
        try:
            found = scan_shop(name, search)
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
        lines = ["🤖 Preorder-Alarm ist aktiv. Aktueller Stand:"]
        for item in sorted(items.values(), key=lambda i: (i["title"], i["shop"])):
            lines.append(("🟢 " if item["available"] else "🔴 ") + fmt(item))
        if not items:
            lines.append("(noch nichts gefunden)")
        messages = ["\n".join(lines)]

    if messages:
        send_discord(messages)

    save_state({
        "items": items,
        "errors": errors,
        # Einmal täglich ändert sich die Datei, damit GitHub das Repo als aktiv ansieht.
        "heartbeat": datetime.date.today().isoformat(),
    })


if __name__ == "__main__":
    main()
