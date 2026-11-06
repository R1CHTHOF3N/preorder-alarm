"""PS5-Pro-Alarm: meldet per Discord, sobald die PS5 Pro irgendwo zum UVP (899,99 €) bestellbar ist.

Teurere Angebote (Wiederverkäufer, Marktplatz-Händler) werden ignoriert.
Läuft ohne zusätzliche Pakete (nur Python-Standardbibliothek).
Ohne DISCORD_WEBHOOK_PS5PRO werden die Meldungen nur in der Konsole ausgegeben (Testmodus).
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
from concurrent.futures import ThreadPoolExecutor
from html import unescape

# UVP in Deutschland seit 2. April 2026 (vorher 799,99 €). Gemeldet wird nur bis zu diesem Preis.
UVP = 899.99
MAX_PRICE = UVP

PING = "@everyone"
ERROR_ALERT_AFTER = 4      # bei 5-Min-Takt ≈ 20 Min. Fehler am Stück, bevor gewarnt wird
ERROR_REMIND_EVERY = 144   # danach Erinnerung alle ≈ 12 Std.

STATE_VERSION = 2  # erhöhen -> nächster Lauf schickt neue Übersicht
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ps5pro_state.json")
HEADERS = {
    # Ein normaler Browser-Kennung, sonst blocken einige Shops schon die erste Anfrage.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "de-DE,de;q=0.9",
}

# Produktseiten der normalen PS5 Pro (2 TB, ohne Laufwerk). Alle liefern innerhalb Deutschlands.
SHOPS = {
    "PlayStation Direct": "https://direct.playstation.com/de-de/buy-consoles/playstation5-pro-console",
    "MediaMarkt": "https://www.mediamarkt.de/de/product/_sony-playstationr5-pro-2-tb-konsole-3018022.html",
    "Saturn": "https://www.saturn.de/de/product/_sony-playstationr5-pro-2-tb-konsole-3018022.html",
    "Alternate": "https://www.alternate.de/Sony-Interactive-Entertainment/"
                 "PlayStation-5-Pro-2-TB-E-Chassis-Spielkonsole/html/product/100160951",
    "Müller": "https://www.mueller.de/p/playstation5-pro-konsole-2-tb-IPN3068364/",
}


class LayoutError(Exception):
    """Die Seite sieht anders aus als erwartet – lieber Warnung als falsche Meldung."""


def fetch(url, attempts=3):
    """Lädt eine Seite; liefert (Text, End-Adresse nach Weiterleitungen)."""
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", "replace"), resp.geturl()
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == attempts:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts:
                raise
        time.sleep(5 * attempt)


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def main_offer(html):
    """Preis und Verfügbarkeit des Hauptprodukts aus den schema.org-Daten der Seite.

    Das erste Produkt auf der Seite ist das Hauptprodukt; weitere sind Empfehlungen darunter.
    """
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(unescape(block.strip()))
        except ValueError:
            # Manche Shops (Müller) haben kaputtes JSON in der Beschreibung -> Angebot direkt suchen.
            m = re.search(r'"offers"\s*:\s*\[?\s*\{[^{}]*?"price"\s*:\s*"?([\d.]+)"?[^{}]*?'
                          r'"availability"\s*:\s*"([^"]+)"', block)
            if m:
                return float(m.group(1)), m.group(2).rsplit("/", 1)[-1]
            continue
        for obj in walk(data):
            if obj.get("@type") not in ("Product", "ProductGroup") or "offers" not in obj:
                continue
            offers = obj["offers"] if isinstance(obj["offers"], list) else [obj["offers"]]
            for offer in offers:
                if isinstance(offer, dict) and "availability" in offer:
                    try:
                        price = float(offer.get("price") or offer.get("lowPrice"))
                    except (TypeError, ValueError):
                        price = None
                    return price, str(offer["availability"]).rsplit("/", 1)[-1]
    raise LayoutError("Preis/Verfügbarkeit nicht auf der Seite gefunden")


def check(name, url):
    html, final_url = fetch(url)
    if "queue" in urllib.parse.urlparse(final_url).netloc:
        # PlayStation Direct schaltet bei Drops eine Warteschlange vor -> sofort melden.
        return {"price": UVP, "available": True, "queue": True}
    price, availability = main_offer(html)
    if name in ("MediaMarkt", "Saturn"):
        # Deren schema.org-Daten sagen oft "InStock", obwohl die Seite "bald wieder verfügbar" zeigt.
        # Maßgeblich ist der Online-Status des Produkts selbst (nicht der Marktplatz-Angebote).
        product_id = re.search(r"-(\d+)\.html", url).group(1)
        m = re.search(
            rf'CofrOnlineStatusFeature:\w+:de:{product_id}\\?":\{{[^{{}}]*?'
            r'"isAvailableAndBuyable\\?":(true|false)[^{}]*?"onlineStatus\\?":\\?"(\w+)',
            html.replace('\\"', '"'))
        if not m:
            raise LayoutError("Online-Status des Produkts nicht gefunden")
        buyable = m.group(1) == "true" and m.group(2) == "AVAILABLE"
        availability = "InStock" if buyable else m.group(2)
    return {
        "price": price,
        "available": availability in ("InStock", "PreOrder", "LimitedAvailability", "OnlineOnly"),
        "availability": availability,
    }


def price_de(price):
    return "Preis unbekannt" if price is None else f"{price:.2f} €".replace(".", ",")


def at_uvp(item):
    return item["available"] and item["price"] is not None and item["price"] <= MAX_PRICE + 0.005


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def card(title, description="", color=0x95A5A6, url=None, fields=None):
    embed = {"title": title[:256], "description": description, "color": color, "timestamp": now_iso()}
    if url:
        embed["url"] = url
    if fields:
        embed["fields"] = fields
    return embed


def send_discord(embeds, ping=True):
    webhook = os.environ.get("DISCORD_WEBHOOK_PS5PRO", "").strip()
    for e in embeds:
        print(e["title"], "-", e.get("description", ""))
    if not webhook:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            sys.exit("DISCORD_WEBHOOK_PS5PRO fehlt! Secret in GitHub anlegen (siehe README).")
        print("(Testmodus: DISCORD_WEBHOOK_PS5PRO nicht gesetzt, nichts gesendet)")
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
                "User-Agent": "ps5pro-alarm", "Content-Type": "application/json"})
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


def status_line(name, item):
    if item is None:
        return f"⚠️ [{name}]({SHOPS[name]}) · nicht erreichbar"
    if at_uvp(item):
        icon = "🟢"
    elif item["available"]:
        icon = "💸"  # verfügbar, aber über UVP
    else:
        icon = "🔴"
    return f"{icon} [{name}]({SHOPS[name]}) · {price_de(item['price'])}"


def main():
    old = load_state()
    first_run = old is None
    old = old or {}
    items = dict(old.get("items", {}))
    errors = dict(old.get("errors", {}))
    alerts, quiet = [], []

    with ThreadPoolExecutor(max_workers=len(SHOPS)) as pool:
        futures = {name: pool.submit(check, name, url) for name, url in SHOPS.items()}

    current = {}
    for name, url in SHOPS.items():
        try:
            item = futures[name].result()
        except Exception as e:  # ein kaputter Shop soll die anderen nicht aufhalten
            print(f"[{name}] Fehler: {type(e).__name__}: {e}", file=sys.stderr)
            count = errors.get(name, 0) + 1
            errors[name] = count
            if count == ERROR_ALERT_AFTER or (
                count > ERROR_ALERT_AFTER and (count - ERROR_ALERT_AFTER) % ERROR_REMIND_EVERY == 0
            ):
                quiet.append(card(
                    f"⚠️ {name} lässt sich nicht prüfen",
                    f"Seit {count} Versuchen Fehler – dort wird die PS5 Pro gerade **nicht** überwacht.\n"
                    f"```{type(e).__name__}: {str(e)[:300]}```", 0xE74C3C, url))
            continue
        if errors.get(name, 0) >= ERROR_ALERT_AFTER:
            quiet.append(card(f"✅ {name} wird wieder geprüft", color=0x2ECC71))
        errors[name] = 0
        current[name] = item
        print(f"[{name}] {item}")

        before = items.get(name)
        if not first_run and at_uvp(item) and not (before and at_uvp(before)):
            if item.get("queue"):
                title, text = "🚦 PlayStation Direct: Warteschlange aktiv!", \
                    "Sony hat die Warteschlange eingeschaltet – meistens heißt das: **PS5 Pro Drop zum UVP**. Sofort rein!"
            else:
                title, text = f"🟢 PS5 Pro zum UVP bei {name}!", "Jetzt bestellbar – schnell sein!"
            alerts.append(card(title, text, 0x2ECC71, url, [
                {"name": "Shop", "value": name, "inline": True},
                {"name": "Preis", "value": price_de(item["price"]), "inline": True},
            ]))
        elif not first_run and before and at_uvp(before) and not at_uvp(item):
            reason = "ausverkauft" if not item["available"] else f"jetzt {price_de(item['price'])}"
            quiet.append(card(f"🔴 {name}: PS5 Pro wieder weg ({reason})", color=0x95A5A6, url=url))
        items[name] = item

    today = datetime.date.today()
    week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
    lines = "\n".join(status_line(n, current.get(n)) for n in SHOPS)
    legend = "🟢 zum UVP bestellbar · 💸 nur teurer · 🔴 ausverkauft"
    if first_run:
        quiet.append(card(
            "🎮 PS5-Pro-Alarm ist aktiv",
            f"Gemeldet wird nur zum UVP von **{price_de(UVP)}** oder günstiger.\n\n{lines}\n\n{legend}",
            0x003791))
    elif old.get("status_week") != week:
        quiet.append(card("💓 Wochen-Check", f"PS5-Pro-Alarm läuft.\n\n{lines}", 0x95A5A6))

    if alerts:
        send_discord(alerts, ping=True)
    if quiet:
        send_discord(quiet, ping=False)

    save_state({
        "version": STATE_VERSION,
        "items": items,
        "errors": errors,
        "status_week": week,
        "heartbeat": today.isoformat(),
    })


if __name__ == "__main__":
    main()
