#!/usr/bin/env python3
"""
Immo-Wächter Wien
Durchsucht mehrere Immobilienportale nach neuen Inseraten, prüft sie gegen die
Suchprofile in config.yaml und schickt Treffer per Telegram und/oder ntfy.

Unterstützt: willhaben, ImmoScout24, immodirekt, derStandard, immowelt, WG-Gesucht
sowie beliebige weitere Seiten mit eigenem "link_pattern" in der config.

Aufruf:
  python immo_watch.py                 einmal prüfen (für GitHub Actions / Aufgabenplanung)
  python immo_watch.py --loop 1200     dauerhaft laufen, alle ~20 Minuten prüfen
  python immo_watch.py --dry-run       Treffer nur anzeigen, nichts senden, nichts speichern
  python immo_watch.py --check         jede Suche einmal abrufen und zeigen, was erkannt wird
  python immo_watch.py --test-notify   Testnachricht schicken
"""
import argparse
import html as html_lib
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse, parse_qsl, urlunparse

import requests
import yaml
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "seen.json"
CONFIG_FILE = BASE / "config.yaml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
}

# Wiener Bezirksnamen -> PLZ (für Inserate, die nur den Namen nennen)
BEZIRKE = {
    "innere stadt": "1010", "leopoldstadt": "1020", "landstraße": "1030", "landstrasse": "1030",
    "wieden": "1040", "margareten": "1050", "mariahilf": "1060", "neubau": "1070",
    "josefstadt": "1080", "alsergrund": "1090", "favoriten": "1100", "simmering": "1110",
    "meidling": "1120", "hietzing": "1130", "penzing": "1140", "rudolfsheim": "1150",
    "fünfhaus": "1150", "ottakring": "1160", "hernals": "1170", "währing": "1180",
    "döbling": "1190", "brigittenau": "1200", "floridsdorf": "1210", "donaustadt": "1220",
    "liesing": "1230",
}
VALID_PLZ = {f"1{n:02d}0" for n in range(1, 24)}


# ================================================================ Hilfsfunktionen

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def to_number(val):
    """Liest Zahlen im österreichischen und im Maschinenformat:
    '1.250,50' -> 1250.5 | '1.850' -> 1850 | '51.68' -> 51.68 | '1250.5' -> 1250.5"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace("\xa0", "").replace(" ", "").replace("'", "")
    m = re.search(r"\d[\d.,]*", s)
    if not m:
        return None
    t = m.group(0).rstrip(".,")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".") if re.search(r",\d{1,2}$", t) else t.replace(",", "")
    elif "." in t:
        # 1.850 / 12.500 = Tausenderpunkt, 51.68 / 1250.5 = Dezimalpunkt
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
            t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


def set_query(url, key, value):
    parts = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    q.append((key, str(value)))
    return urlunparse(parts._replace(query=urlencode(q)))


def fmt(n, suffix=""):
    if n is None:
        return "?"
    if float(n).is_integer():
        return f"{int(n):,}".replace(",", ".") + suffix
    return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + suffix


def find_plz(*texts):
    patterns = [
        r"\b(1[0-2]\d0)\s*Wien",
        r"Wien\s*[,(\-]?\s*(1[0-2]\d0)\b",
        r"\((1[0-2]\d0)\)",
        r"/(1[0-2]\d0)-wien",
        r"wien-(1[0-2]\d0)\b",
    ]
    for text in texts:
        if not text:
            continue
        for p in patterns:
            for m in re.finditer(p, text, re.I):
                if m.group(1) in VALID_PLZ:
                    return m.group(1)
    for text in texts:
        if not text:
            continue
        low = text.lower()
        for name, plz in BEZIRKE.items():
            if name in low:
                return plz
    return None


# ================================================================ Werte aus Kartentext lesen

# Tausendergruppen (1.850, oder 1 850 mit geschütztem Leerzeichen) oder Zahl mit Dezimalstellen
NUM = r"\d{1,3}(?:[.'\u00a0\u202f]\d{3})+(?:,\d{1,2})?(?!\d)|\d+(?:[.,]\d{1,2})?(?!\d)"


COLD_RE = re.compile(r"netto\s*-?\s*kalt|kaltmiete|netto\s*-?\s*miete|hauptmietzins|\bhmz\b|\bkalt\b|"
                     r"(?:exkl|zzgl|ohne)\.?\s*(?:bk|betriebskosten|nebenkosten)", re.I)
GROSS_RE = re.compile(r"gesamtmiete|bruttomiete|warmmiete|\bwarm\b|gesamtbelastung|"
                      r"inkl\.?\s*(?:bk|betriebskosten|heizung)", re.I)


def extract_price_info(text):
    """(Preis, Art). Art ist 'kalt', wenn direkt beim Preis Kaltmiete/Nettomiete o.ä. steht,
    'gesamt' bei Gesamt-/Brutto-/Warmmiete, sonst None (unbekannt)."""
    for m in re.finditer(rf"(?:(?:€|EUR)\s*({NUM})|({NUM})\s*(?:€|EUR\b|Euro\b))", text):
        rest = text[m.end():m.end() + 6]
        if re.match(r"\s*/\s*m", rest):
            continue  # Preis pro m²
        before = text[max(0, m.start() - 18):m.start()].lower()
        if re.search(r"kaution|provision|ablöse|betriebskosten|pro m", before):
            continue
        v = to_number(m.group(1) or m.group(2))
        if v and 100 <= v <= 50000:
            around = text[max(0, m.start() - 22):m.start()] + " | " + text[m.end():m.end() + 30]
            kind = "gesamt" if GROSS_RE.search(around) else "kalt" if COLD_RE.search(around) else None
            return v, kind
    return None, None


def extract_price(text):
    return extract_price_info(text)[0]


def extract_area(text):
    for m in re.finditer(rf"({NUM})\s*m(?:²|2\b|\s*²)", text):
        if text[max(0, m.start() - 1):m.start()] == "/":
            continue
        v = to_number(m.group(1))
        if v and 8 <= v <= 1000:
            return v
    return None


def extract_rooms(text):
    m = re.search(r"(\d+(?:[.,]5)?)\s*[- ]?\s*(?:Zimmer|Zi\.|Zi\b|Räume)", text, re.I)
    if m:
        v = to_number(m.group(1))
        if v and 0.5 <= v <= 20:
            return v
    if re.search(r"garçonni[eè]re|garconniere|garsoniere|einzimmer", text, re.I):
        return 1.0
    return None


# ================================================================ Portale

PORTALS = {
    "willhaben": {
        "label": "willhaben",
        "domain": "willhaben.at",
        "link": r"willhaben\.at/iad/immobilien/d/[^?#]*?-(?P<id>\d{6,})/?(?:[?#]|$)",
        "paging": "query:page",
    },
    "immoscout24": {
        "label": "ImmoScout24",
        "domain": "immobilienscout24.at",
        "link": r"immobilienscout24\.at/expose/(?P<id>[0-9a-f]{24})",
        "paging": "path:seite-{n}",
    },
    "immodirekt": {
        "label": "immodirekt",
        "domain": "immodirekt.at",
        "link": r"immodirekt\.at/immobilie/[^?#]*?-(?P<id>[0-9a-f]{24})/?(?:[?#]|$)",
        "paging": "none",   # weitere Treffer lädt die Seite nur per JavaScript nach
    },
    "derstandard": {
        "label": "derStandard",
        "domain": "derstandard.at",
        "link": r"immobilien\.derstandard\.at/(?:detail|gruppe)/(?P<id>\d{5,})",
        "paging": "next",
    },
    "immowelt": {
        "label": "immowelt",
        "domain": "immowelt.at",
        "link": r"immowelt\.at/expose/(?P<id>[0-9a-zA-Z-]{6,})",
        "paging": "query:page",
    },
    "wg-gesucht": {
        "label": "WG-Gesucht",
        "domain": "wg-gesucht.de",
        "link": r"wg-gesucht\.de/(?:wohnungen|wg-zimmer|1-zimmer-wohnungen|haeuser)-in-[^/?#]*?\.(?P<id>\d{5,})\.html",
        "paging": "wgg",
    },
}

CAPTCHA_HINTS = [
    "bitte bestätigen sie, dass sie ein mensch sind",
    "are you a human", "cf-challenge", "captcha-delivery", "px-captcha",
    "ich bin kein roboter", "access denied",
]


def portal_for(search):
    if search.get("link_pattern"):
        return {
            "label": search.get("name") or urlparse(search["url"]).netloc,
            "link": search["link_pattern"],
            "paging": search.get("paging", "next"),
        }
    host = urlparse(search["url"]).netloc.lower()
    for key, p in PORTALS.items():
        if p["domain"] in host:
            return dict(p, key=key)
    raise RuntimeError(f"Unbekanntes Portal ({host}). Für andere Seiten 'link_pattern' angeben.")


def page_url(url, n, paging, html=None):
    """URL für Seite n (n >= 2)."""
    if paging.startswith("query:"):
        return set_query(url, paging.split(":", 1)[1], n)
    if paging.startswith("path:"):
        seg = paging.split(":", 1)[1].format(n=n)
        parts = urlparse(url)
        path = re.sub(r"/seite-\d+/?$", "", parts.path.rstrip("/"))
        return urlunparse(parts._replace(path=f"{path}/{seg}"))
    if paging == "wgg":
        return re.sub(r"\.(\d+)\.html", lambda m: f".{n - 1}.html", url, count=1) if re.search(r"\.\d+\.html", url) else None
    if paging == "next" and html is not None:
        return find_next_link(html, url)
    return None


def find_next_link(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("link", rel="next") or soup.find("a", rel="next")
    if link and link.get("href"):
        return urljoin(base_url, link["href"])
    for a in soup.find_all("a", href=True):
        label = " ".join([a.get_text(" ", strip=True), a.get("aria-label", ""), a.get("title", "")]).lower()
        if re.search(r"nächste|naechste|weiter|next", label) or label.strip() in ("›", "»", ">"):
            return urljoin(base_url, a["href"])
    return None


# ---------------------------------------------------------------- allgemeiner Karten-Leser

def parse_cards(html, base_url, portal):
    """Findet alle Links auf Inserat-Detailseiten, sucht für jeden die kleinste
    umgebende 'Karte' und liest aus deren Text Preis, Fläche, Zimmer und PLZ."""
    link_re = re.compile(portal["link"], re.I)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    def lid(a):
        m = link_re.search(urljoin(base_url, a["href"]))
        return m.group("id") if m else None

    anchors = {}
    for a in soup.find_all("a", href=True):
        i = lid(a)
        if i:
            anchors.setdefault(i, []).append(a)

    listings = []
    for i, links in anchors.items():
        node = links[0]
        while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
            parent = node.parent
            ids = {lid(x) for x in parent.find_all("a", href=True)} - {None}
            if len(ids) > 1 or len(parent.get_text(" ", strip=True)) > 2500:
                break
            node = parent
        text = node.get_text(" ", strip=True)
        head = node.find(["h1", "h2", "h3", "h4"])
        title = (head.get_text(" ", strip=True) if head else "") or \
            max((a.get_text(" ", strip=True) for a in links), key=len, default="") or \
            links[0].get("title", "") or text[:80]
        url = urljoin(base_url, links[0]["href"]).split("#")[0]
        listings.append({
            "id": f"{portal.get('key', portal['label'])}-{i}",
            "source": portal["label"],
            "title": title[:140],
            "price": extract_price_info(text)[0],
            "price_kind": extract_price_info(text)[1],
            "area": extract_area(text),
            "rooms": extract_rooms(title + " " + text),
            "postcode": find_plz(text, url),
            "location": "Wien",
            "text": text[:1500],
            "url": url,
        })
    fill_from_jsonld(html, base_url, link_re, listings)
    return listings


def fill_from_jsonld(html, base_url, link_re, listings):
    """Ergänzt fehlende Werte aus schema.org-Daten, falls die Seite welche hat."""
    by_id = {l["id"].split("-", 1)[1]: l for l in listings}
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack.extend(o)
                continue
            if not isinstance(o, dict):
                continue
            stack.extend(v for v in o.values() if isinstance(v, (dict, list)))
            url = o.get("url") or o.get("@id")
            if not isinstance(url, str):
                continue
            mm = link_re.search(urljoin(base_url, url))
            if not mm or mm.group("id") not in by_id:
                continue
            l = by_id[mm.group("id")]
            offers = o.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if l["price"] is None and isinstance(offers, dict):
                l["price"] = to_number(offers.get("price"))
            fs = o.get("floorSize")
            if l["area"] is None and fs:
                l["area"] = to_number(fs.get("value") if isinstance(fs, dict) else fs)
            if l["rooms"] is None and o.get("numberOfRooms"):
                l["rooms"] = to_number(o["numberOfRooms"])
            addr = o.get("address") or {}
            if not l["postcode"] and isinstance(addr, dict) and str(addr.get("postalCode", "")) in VALID_PLZ:
                l["postcode"] = str(addr["postalCode"])


# ---------------------------------------------------------------- willhaben (eigene Datenquelle)

NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def parse_willhaben(html, base_url, portal):
    m = NEXT_DATA_RE.search(html)
    try:
        data = json.loads(m.group(1))
        ads = data["props"]["pageProps"]["searchResult"]["advertSummaryList"]["advertSummary"]
    except (AttributeError, KeyError, TypeError, ValueError):
        return parse_cards(html, base_url, portal)  # Ersatzweg, falls sich die Seite ändert

    out = []
    for ad in ads:
        attrs = {}
        for a in (ad.get("attributes") or {}).get("attribute", []) or []:
            vals = a.get("values") or []
            attrs[a.get("name")] = vals[0] if vals else None

        def first(*names):
            for n in names:
                if attrs.get(n) not in (None, ""):
                    return attrs[n]
            return None

        location = first("LOCATION", "ADDRESS") or ""
        postcode = first("POSTCODE")
        if str(postcode) not in VALID_PLZ:
            postcode = find_plz(location)
        seo = first("SEO_URL")
        url = ("https://www.willhaben.at/iad/" + seo.lstrip("/")) if seo else \
            f"https://www.willhaben.at/iad/object?adId={ad.get('id')}"
        title = first("HEADING") or ad.get("description") or "(ohne Titel)"
        out.append({
            "id": f"willhaben-{ad.get('id')}",
            "source": "willhaben",
            "title": title,
            "price": to_number(first("PRICE", "PRICE/AMOUNT", "PRICE_FOR_DISPLAY")),
            "area": to_number(first("ESTATE_SIZE/LIVING_AREA", "ESTATE_SIZE", "ESTATE_SIZE/USEABLE_AREA")),
            "rooms": to_number(first("NUMBER_OF_ROOMS", "ROOMS")),
            "postcode": str(postcode) if postcode else None,
            "location": location,
            "text": " ".join(str(v) for v in [title, ad.get("description"), first("BODY_DYN")] if v),
            "url": url,
        })
    return out


PARSERS = {"willhaben": parse_willhaben}


# ---------------------------------------------------------------- Abruf

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def fetch_search(search):
    portal = portal_for(search)
    parser = PARSERS.get(portal.get("key"), parse_cards)
    pages = int(search.get("pages", 2))
    url = search["url"]
    listings, seen_ids = [], set()

    for n in range(1, pages + 1):
        r = SESSION.get(url, timeout=30)
        if r.status_code in (403, 429):
            raise RuntimeError(f"Zugriff verweigert (HTTP {r.status_code}), vermutlich Bot-Schutz")
        r.raise_for_status()
        html = r.text
        low = html[:20000].lower()
        if any(h in low for h in CAPTCHA_HINTS):
            raise RuntimeError("Captcha / Bot-Schutz statt Suchergebnissen")
        found = [l for l in parser(html, r.url, portal) if l["id"] not in seen_ids]
        if n == 1 and not found:
            raise RuntimeError("Keine Inserate erkannt (Seitenaufbau geändert oder falsche Such-URL?)")
        seen_ids.update(l["id"] for l in found)
        listings.extend(found)
        if not found or n == pages:
            break
        nxt = page_url(search["url"], n + 1, portal["paging"], html)
        if not nxt or nxt == url:
            break
        url = nxt
        time.sleep(random.uniform(2, 5))
    return portal["label"], listings


# ================================================================ Filter

def matches(listing, p):
    """(True, '') oder (False, Grund). Fehlende Fläche/Zimmer/PLZ lassen ein Inserat
    durch, damit nichts übersehen wird; ein fehlender Preis nur mit allow_missing_price."""
    src = [s.lower() for s in (p.get("sources") or [])]
    if src and listing["source"].lower() not in src:
        return False, "Portal nicht gewählt"
    if listing["price"] is None and not p.get("allow_missing_price", False):
        return False, "kein Preis"

    def too_small(val, limit):
        return limit is not None and val is not None and val < limit

    def too_big(val, limit):
        return limit is not None and val is not None and val > limit

    checks = [
        (too_big(listing["price"], p.get("max_price")), "Preis zu hoch"),
        (too_small(listing["price"], p.get("min_price")), "Preis zu niedrig"),
        (too_small(listing["area"], p.get("min_area")), "Fläche zu klein"),
        (too_big(listing["area"], p.get("max_area")), "Fläche zu groß"),
        (too_small(listing["rooms"], p.get("min_rooms")), "zu wenig Zimmer"),
        (too_big(listing["rooms"], p.get("max_rooms")), "zu viele Zimmer"),
    ]
    for failed, reason in checks:
        if failed:
            return False, reason

    plz = [str(x) for x in (p.get("postcodes") or [])]
    if plz and listing["postcode"] and listing["postcode"] not in plz:
        return False, "Bezirk"

    text = (listing["title"] + " " + listing["text"]).lower()
    for kw in p.get("exclude_keywords") or []:
        if kw.lower() in text:
            return False, f"Ausschlusswort '{kw}'"
    inc = p.get("include_keywords") or []
    if inc and not any(kw.lower() in text for kw in inc):
        return False, "kein Pflichtwort"
    return True, ""


def apply_warm_estimate(listings, cfg):
    """Ist im Inserat nur die Kaltmiete angegeben, wird die Warmmiete geschätzt und als
    Preis verwendet (für Filter, Preis pro m² und Aufteilung). Die Kaltmiete bleibt in
    price_cold erhalten."""
    w = cfg.get("warmmiete") or {}
    if w.get("aktiv", True) is False:
        return
    ust = float(w.get("ust_prozent", 10))
    bk = float(w.get("betriebskosten_pro_m2", 2.6))
    hz = float(w.get("heizkosten_pro_m2", 1.25))
    for l in listings:
        if l.get("price_kind") != "kalt" or not l["price"] or "price_cold" in l:
            continue
        if not l["area"]:
            l["cold_only"] = True      # ohne Fläche keine Schätzung möglich
            continue
        l["price_cold"] = l["price"]
        l["price"] = float(round(l["price"] * (1 + ust / 100) + l["area"] * (bk + hz)))
        l["warm_parts"] = (ust, bk, hz)


def fingerprint(l):
    """Gleiche Wohnung auf mehreren Portalen erkennen (PLZ + Preis + Fläche)."""
    if l["price"] is None or l["area"] is None or not l["postcode"]:
        return None
    return f"fp|{l['postcode']}|{round(l['price'])}|{round(l['area'])}"


# ================================================================ Benachrichtigung

def notify(cfg, text, click_url=None, title="Neue Wohnung", html=None):
    """text = reiner Text (ntfy), html = formatierte Fassung für Telegram (optional)."""
    n = cfg.get("notify") or {}
    token = os.getenv("TELEGRAM_BOT_TOKEN") or n.get("telegram_bot_token")
    chat = os.getenv("TELEGRAM_CHAT_ID") or n.get("telegram_chat_id")
    topic = os.getenv("NTFY_TOPIC") or n.get("ntfy_topic")
    sent = False

    if token and chat:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": html or text,
                                    **({"parse_mode": "HTML"} if html else {})}, timeout=20)
            if html and r.status_code == 400:   # Formatierung abgelehnt -> als reinen Text senden
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat, "text": text}, timeout=20)
            r.raise_for_status()
            sent = True
        except Exception as e:
            log(f"Telegram-Fehler: {e}")

    if topic:
        try:
            payload = {"topic": topic, "title": title, "message": text, "tags": ["house"]}
            if click_url:
                payload["click"] = click_url
            r = requests.post("https://ntfy.sh/", json=payload, timeout=20)
            r.raise_for_status()
            sent = True
        except Exception as e:
            log(f"ntfy-Fehler: {e}")

    if not sent:
        log("Keine Benachrichtigung verschickt (Telegram/ntfy nicht eingerichtet?)")
    return sent


WG_YES = re.compile(r"wg[- ]?(?:geeignet|tauglich|fähig|möglich|freundlich)|für\s+wgs?\b|"
                    r"wohngemeinschaft(?:en)?\s+(?:möglich|willkommen|geeignet)|studenten-?wg", re.I)
WG_NO = re.compile(r"(?:keine?|nicht)\s+(?:für\s+)?(?:wgs?\b|wohngemeinschaft)|wg[- ]?ungeeignet", re.I)


def size_class(area):
    return "klein" if area < 40 else "mittel" if area < 75 else "groß"


def market_average(listings):
    """Durchschnittlicher Preis pro m² aus allen in diesem Lauf geladenen Inseraten,
    getrennt nach Größenklasse (kleine Wohnungen sind pro m² immer teurer)."""
    seen, vals = set(), {"alle": []}
    for l in listings:
        if not l["price"] or not l["area"]:
            continue
        key = fingerprint(l) or l["id"]
        if key in seen:
            continue
        seen.add(key)
        v = l["price"] / l["area"]
        if 5 <= v <= 60:           # Ausreißer und Lesefehler raus
            vals["alle"].append(v)
            vals.setdefault(size_class(l["area"]), []).append(v)
    return {k: sum(v) / len(v) for k, v in vals.items() if len(v) >= (30 if k == "alle" else 15)}


def average_for(avg, area):
    """Schnitt der passenden Größenklasse, sonst Gesamtschnitt, sonst None."""
    return (avg or {}).get(size_class(area)) or (avg or {}).get("alle")


def price_rating(ppm, avg):
    d = (ppm - avg) / avg
    if d <= -0.25:
        return "🟢🟢 stark unter Marktpreis"
    if d <= -0.10:
        return "🟢 unter Marktpreis"
    if d < 0.10:
        return "🟡 im Marktschnitt"
    if d < 0.25:
        return "🟠 über Marktpreis"
    return "🔴 deutlich über Marktpreis"


def message_for(l, profile, avg=None):
    """Gibt (reiner Text, HTML für Telegram) zurück."""
    esc = html_lib.escape
    head = " · ".join(x for x in [
        l["postcode"] or "Wien",
        f"{l['area']:g} m²".replace(".", ",") if l["area"] else None,
        f"{fmt(l['rooms'])} Zi" if l["rooms"] else None,
    ] if x)
    lines = [f"<b>{esc(head)}</b>", esc(l["title"]), ""]

    if l.get("price_cold"):
        ust, bk, hz = l["warm_parts"]
        parts = ([f"{ust:g} % USt"] if ust else []) + \
            [f"{fmt(bk, ' €/m²')} Betriebskosten", f"{fmt(hz, ' €/m²')} Heizung"]
        lines.append(f"Miete: <b>ca. {fmt(l['price'], ' €')} warm</b> (geschätzt)")
        lines.append(f"Im Inserat: {fmt(l['price_cold'], ' €')} kalt, dazu " + ", ".join(parts))
    elif l.get("cold_only"):
        lines.append(f"Miete: <b>{fmt(l['price'], ' €')} kalt</b>")
        lines.append("Betriebskosten und Heizung kommen dazu")
    elif l["price"]:
        lines.append(f"Miete: <b>{fmt(l['price'], ' €')}</b>")
    else:
        lines.append("Miete: keine Angabe")
    if l["price"] and l["area"]:
        ppm = l["price"] / l["area"]
        lines.append(f"Pro m²: {fmt(round(ppm, 2), ' €')}")
        ref = average_for(avg, l["area"])
        if ref:
            lines.append(f"{price_rating(ppm, ref)} (Schnitt {fmt(round(ref, 2), ' €/m²')})")

    persons = profile.get("split_persons") or []
    text = l["title"] + " " + l["text"]
    if persons and WG_YES.search(text) and not WG_NO.search(text):
        lines.append("✅ ausdrücklich WG-geeignet")
    if persons and l["price"]:
        lines += ["", " · ".join(f"{n} Pers.: {fmt(round(l['price'] / n), ' €')}" for n in persons)]

    lines += ["", f"{esc(l['source'])}: {esc(l['url'])}", f"<i>Suchprofil: {esc(profile['name'])}</i>"]
    html = "\n".join(lines)
    plain = html_lib.unescape(re.sub(r"</?[bi]>", "", html))
    return plain, html


# ================================================================ Zustand

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8")), False
    return {}, True


def save_state(state, keep_days=60):
    cutoff = time.time() - keep_days * 86400
    state = {k: v for k, v in state.items() if k.startswith("fail|") or v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=0, sort_keys=True), encoding="utf-8")


# ================================================================ Ablauf

def active_searches(cfg):
    return [s for s in cfg.get("searches", []) if s.get("active", True) is not False]


def active_profiles(cfg):
    return [p for p in cfg.get("profiles", []) if p.get("active", True) is not False]


def run_once(cfg, dry_run=False):
    state, first_run = load_state()
    now = time.time()
    max_msgs = cfg.get("max_messages_per_run", 10)
    warn_after = cfg.get("warn_after_failures", 6)
    profiles = active_profiles(cfg)
    searches = active_searches(cfg)
    hits, failed, loaded = [], 0, []

    for search in searches:
        skey = f"fail|{search['url']}"
        try:
            label, listings = fetch_search(search)
        except Exception as e:
            failed += 1
            label = search.get("name") or urlparse(search["url"]).netloc
            log(f"Fehler bei {label}: {e}")
            if not dry_run:
                state[skey] = state.get(skey, 0) + 1
                if state[skey] == warn_after:
                    notify(cfg, f"⚠️ {label} liefert seit {warn_after} Durchläufen nichts.\n"
                                f"Grund: {e}\nDie anderen Portale laufen weiter.",
                           title="Immo-Wächter: Problem")
            continue
        state.pop(skey, None)
        log(f"{label}: {len(listings)} Inserate erkannt")
        loaded.extend(listings)

    apply_warm_estimate(loaded, cfg)
    avg = market_average(loaded)
    if avg:
        log("Marktschnitt aus diesem Lauf: " + ", ".join(f"{k} {v:.2f} €/m²" for k, v in avg.items()))

    for l in loaded:
        for p in profiles:
            key = f"{p['name']}|{l['id']}"
            if key in state:
                continue
            ok, _ = matches(l, p)
            if ok:
                hits.append((p, l))
            if not dry_run:
                state[key] = now

    # Duplikate über Portale hinweg aussortieren
    fresh = []
    for p, l in hits:
        fp = fingerprint(l)
        k = f"{fp}|{p['name']}" if fp else None
        if k and k in state:
            continue
        if k:
            state[k] = now
        fresh.append((p, l))

    if dry_run:
        for p, l in fresh:
            print("-" * 60)
            print(message_for(l, p, avg)[0])
        log(f"Dry-Run: {len(fresh)} Treffer, nichts gesendet")
        return

    if first_run:
        notify(cfg, f"✅ Immo-Wächter läuft ({len(searches) - failed} von {len(searches)} Suchen ok). "
                    f"{len(fresh)} bereits vorhandene Treffer wurden übersprungen, "
                    f"ab jetzt kommen nur neue Inserate.", title="Immo-Wächter")
    else:
        for p, l in fresh[:max_msgs]:
            plain, html = message_for(l, p, avg)
            notify(cfg, plain, l["url"], html=html)
            time.sleep(1)
        if len(fresh) > max_msgs:
            notify(cfg, f"… und {len(fresh) - max_msgs} weitere Treffer. Filter evtl. enger stellen.")
        log(f"{len(fresh)} neue Treffer gemeldet")

    save_state(state)
    if searches and failed == len(searches):
        sys.exit(1)  # alles fehlgeschlagen -> GitHub Actions zeigt den Lauf rot an


def check_searches(cfg):
    """Ruft jede Suche einmal ab und zeigt, was erkannt wird. Zum Einrichten gedacht."""
    for search in active_searches(cfg):
        s = dict(search, pages=1)
        try:
            label, listings = fetch_search(s)
        except Exception as e:
            print(f"\n✗ {search['url']}\n  {e}")
            continue
        cold = sum(1 for l in listings if l.get("price_kind") == "kalt")
        apply_warm_estimate(listings, cfg)
        full = sum(1 for l in listings if l["price"] and l["area"])
        print(f"\n✓ {label}: {len(listings)} Inserate, davon {full} mit Preis und Fläche, {cold} mit Kaltmiete")
        for l in listings[:3]:
            print(f"  · {l['title'][:60]} | {fmt(l['price'], ' €')} | {fmt(l['area'], ' m²')} | "
                  f"{fmt(l['rooms'])} Zi | {l['postcode'] or '?'}\n    {l['url']}")
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser(description="Immo-Wächter Wien")
    ap.add_argument("--config", default=str(CONFIG_FILE))
    ap.add_argument("--loop", type=int, metavar="SEKUNDEN", help="dauerhaft laufen, alle X Sekunden prüfen")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    if args.test_notify:
        ok = notify(cfg, "🔔 Test vom Immo-Wächter: Benachrichtigungen funktionieren.", title="Immo-Wächter")
        sys.exit(0 if ok else 1)
    if args.check:
        check_searches(cfg)
        return
    if not args.loop:
        run_once(cfg, args.dry_run)
        return

    while True:
        try:
            run_once(cfg, args.dry_run)
        except SystemExit:
            pass
        except Exception as e:
            log(f"Unerwarteter Fehler: {e}")
        wait = args.loop + random.randint(-60, 60)
        log(f"Nächste Prüfung in {wait // 60} Minuten")
        time.sleep(max(60, wait))


if __name__ == "__main__":
    main()
