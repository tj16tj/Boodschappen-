#!/usr/bin/env python3
"""
Ververst de data voor de boodschappen-app.

1. Prijzen AH + Jumbo uit de open Checkjebon-dataset (dagelijks bijgewerkt, MIT).
2. Bonus/aanbiedingen via de (onofficiële) mobiele API's van AH en Jumbo.

Schrijft:
  data/prices.json  — alle producten per winkel, met prijs per kg/l/stuk
  data/bonus.json   — actuele aanbiedingen, gekoppeld op product-id
  data/status.json  — wat er gelukt/mislukt is (zichtbaar in de app)
  data/debug/*.json — kleine ruwe voorbeelden van API-antwoorden (om te repareren als iets verandert)

Gebruik:  python scripts/update_data.py [--prices-file sm.json] [--no-bonus]
"""
import argparse
import json
import re
import sys
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEBUG = DATA / "debug"
CHECKJEBON_URL = "https://raw.githubusercontent.com/supermarkt/checkjebon/main/data/supermarkets.json"
STORES = ("ah", "jumbo")

status = {"updated": None, "prices": {}, "bonus": {}, "errors": []}


def log(*a):
    print(*a, flush=True)


def save_debug(name, obj, limit=60_000):
    DEBUG.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, indent=1)
    (DEBUG / f"{name}.json").write_text(txt[:limit], encoding="utf-8")


# ---------------------------------------------------------------- eenheden

NUM = r"(\d+(?:[.,]\d+)?)"
UNIT_MAP = {
    "g": ("kg", 0.001), "gr": ("kg", 0.001), "gram": ("kg", 0.001), "grm": ("kg", 0.001), "gramm": ("kg", 0.001),
    "kg": ("kg", 1.0), "kilo": ("kg", 1.0),
    "ml": ("l", 0.001), "mililiters": ("l", 0.001), "milliliter": ("l", 0.001), "milliliters": ("l", 0.001),
    "cl": ("l", 0.01), "dl": ("l", 0.1),
    "l": ("l", 1.0), "lt": ("l", 1.0), "ltr": ("l", 1.0), "liter": ("l", 1.0), "liters": ("l", 1.0),
    "st": ("st", 1.0), "stk": ("st", 1.0), "stuk": ("st", 1.0), "stuks": ("st", 1.0),
    "rol": ("st", 1.0), "rollen": ("st", 1.0), "wasbeurten": ("wasb", 1.0), "tabletten": ("st", 1.0),
}
UNIT_RE = "|".join(sorted(map(re.escape, UNIT_MAP), key=len, reverse=True))


def fnum(s):
    return float(s.replace(",", "."))


def parse_size(text):
    """'3 x 200 ml' -> ('l', 0.6); '500 g' -> ('kg', 0.5); 'per stuk' -> ('st', 1)"""
    if not text:
        return None
    t = text.lower().replace("×", "x")
    t = re.sub(r"\bca\.?\s*", "", t)
    m = re.search(rf"{NUM}\s*x\s*{NUM}\s*({UNIT_RE})\b", t)
    if m:
        unit, f = UNIT_MAP[m.group(3)]
        return unit, fnum(m.group(1)) * fnum(m.group(2)) * f
    m = re.search(rf"{NUM}\s*({UNIT_RE})\b", t)
    if m:
        unit, f = UNIT_MAP[m.group(2)]
        return unit, fnum(m.group(1)) * f
    if "per stuk" in t or "los per" in t and "kilo" not in t:
        return "st", 1.0
    if "los per kilo" in t:
        return "kg", 1.0
    return None


def size_from_name(name):
    """Jumbo zet de inhoud vaak in de naam: 'Arla Bio Halfvolle Melk 1 L', 'Cola 6 x 1,5 L'."""
    m = re.search(rf"(\d+\s*x\s*)?{NUM}\s*({UNIT_RE})\s*$", name.lower().replace("×", "x"))
    if m:
        txt = m.group(0)
        return txt.strip(), parse_size(txt)
    m = re.search(rf"(\d+\s*x\s*)?{NUM}\s*({UNIT_RE})\b", name.lower().replace("×", "x"))
    if m:
        return m.group(0).strip(), parse_size(m.group(0))
    return "", None


# ---------------------------------------------------------------- prijzen

def ah_id(link):
    m = re.match(r"wi(\d+)", link or "")
    return m.group(1) if m else None


def jumbo_id(link):
    m = re.search(r"(\d+[A-Z]{2,4})$", link or "")
    return m.group(1) if m else None


def load_prices(prices_file=None):
    if prices_file:
        raw = json.loads(Path(prices_file).read_text(encoding="utf-8"))
    else:
        r = requests.get(CHECKJEBON_URL, timeout=120)
        r.raise_for_status()
        raw = r.json()
    by = {s["n"]: s for s in raw}
    out = {}
    for store in STORES:
        s = by.get(store)
        if not s:
            status["errors"].append(f"prijzen: {store} ontbreekt in Checkjebon")
            continue
        idf = ah_id if store == "ah" else jumbo_id
        prods = {}
        for p in s["d"]:
            pid = idf(p.get("l"))
            price = p.get("p")
            if not pid or not price:
                continue
            size_txt = (p.get("s") or "").strip()
            parsed = parse_size(size_txt)
            if not parsed and store == "jumbo":
                guess_txt, parsed = size_from_name(p["n"])
                if not size_txt:
                    size_txt = guess_txt
            unit, qty = parsed if parsed and parsed[1] > 0 else (None, None)
            up = round(price / qty, 2) if unit else None
            # [naam, prijs, inhoud, prijs-per-eenheid, eenheid, link]
            clean = lambda x: (x or "").replace("\ufffd", "e") if x else x  # kapotte tekens uit de bron
            prods[pid] = [clean(p["n"]), price, clean(size_txt), up, unit, clean(p.get("l"))]
        out[store] = {"base": s.get("u"), "products": prods}
        status["prices"][store] = len(prods)
        log(f"prijzen {store}: {len(prods)} producten")
    return out


# ---------------------------------------------------------------- bonus: mechanisme -> effectieve prijs

def effective(price, text, now_price=None):
    """
    Geeft (prijs per stuk bij de actie, minimaal aantal) terug.
    price = normale prijs per stuk, text = actietekst zoals '1+1 gratis', '2e halve prijs', '2 voor 5,00', '25% korting'.
    """
    t = (text or "").lower().replace("€", "").replace(",", ".")
    if price:
        m = re.search(r"(\d+)\s*\+\s*(\d+)\s*gratis", t)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            return round(price * a / (a + b), 2), a + b
        if re.search(r"2e\s*(artikel\s*)?gratis", t):
            return round(price / 2, 2), 2
        if re.search(r"2e\s*(artikel\s*)?halve\s*prijs", t):
            return round(price * 0.75, 2), 2
        m = re.search(r"(\d+)e\s*(?:artikel\s*)?(\d+)\s*%\s*korting", t)
        if m:
            n, pct = int(m.group(1)), int(m.group(2))
            return round(price * (n - pct / 100) / n, 2), n
        m = re.search(r"(\d+)\s*(?:stuks?|st\.?)\s+(\d+)\s*%\s*korting", t)
        if m:
            n, pct = int(m.group(1)), int(m.group(2))
            if pct < 100 and n < 10:
                return round(price * (1 - pct / 100), 2), n
        m = re.search(r"(\d+)\s*%\s*korting", t)
        if m:
            return round(price * (1 - int(m.group(1)) / 100), 2), 1
    m = re.search(r"(\d+)\s*(?:stuks\s*)?voor\s*(\d+(?:\.\d+)?)", t)
    if m:
        n, tot = int(m.group(1)), float(m.group(2))
        if 0 < n < 20:
            return round(tot / n, 2), n
    if now_price:
        return round(now_price, 2), 1
    m = re.search(r"(?:voor|nu)\s*(\d+(?:\.\d+)?)", t)
    if m:
        return float(m.group(1)), 1
    if price:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:euro\s*)?korting", t)
        if m and float(m.group(1)) < price:
            return round(price - float(m.group(1)), 2), 1
    return None, 1


# ---------------------------------------------------------------- bonus: AH

AH_HEADERS = {
    "x-application": "AHWEBSHOP",
    "user-agent": "Appie/9.12 Model/phone Android/14-API34",
    "content-type": "application/json; charset=UTF-8",
}


def ah_session():
    s = requests.Session()
    s.headers.update(AH_HEADERS)
    r = s.post("https://api.ah.nl/mobile-auth/v1/auth/token/anonymous", json={"clientId": "appie"}, timeout=30)
    r.raise_for_status()
    s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
    return s


def ah_product_to_bonus(p):
    pid = str(p.get("webshopId") or p.get("id") or "")
    if not pid:
        return None
    before = p.get("priceBeforeBonus")
    now = p.get("currentPrice")
    text = p.get("bonusMechanism") or ""
    labels = p.get("discountLabels") or []
    if not text and labels:
        text = labels[0].get("defaultDescription") or ""
    if not (p.get("isBonus") or text or (now and before and now < before)):
        return None
    # Geen permanente online-kortingen of Gall & Gall: alleen de echte bonus
    low = text.lower()
    if any(x in low for x in ("volume voordeel", "volumevoordeel", "pakketkorting", "gall & gall", "kaartkorting")):
        return None
    if (p.get("bonusEndDate") or "")[:4] >= "2099" or p.get("shopType") not in (None, "AH"):
        return None
    eff, qty = effective(before or now, text, now_price=now if (now and before and now < before) else None)
    return pid, {
        "t": text or "Bonus",
        "p": eff,
        "q": qty,
        "w": before,
        "from": p.get("bonusStartDate"),
        "until": p.get("bonusEndDate"),
        "n": p.get("title"),
        "s": p.get("salesUnitSize"),
    }


def fetch_ah_bonus():
    s = ah_session()
    out = {}
    # Route 1: bonuspagina (periodes -> secties -> producten/groepen)
    try:
        r = s.get("https://api.ah.nl/mobile-services/bonuspage/v1/metadata", timeout=30)
        r.raise_for_status()
        meta = r.json()
        save_debug("ah_bonus_metadata", meta)
        today = date.today().isoformat()
        for period in meta.get("periods", []):
            if not (period.get("bonusStartDate", "") <= today <= period.get("bonusEndDate", "9999")):
                continue
            for md in period.get("urlMetadataList", []):
                rr = s.get("https://api.ah.nl/mobile-services/" + md["url"].lstrip("/"), timeout=30)
                if not rr.ok:
                    continue
                sect = rr.json()
                if not out:
                    save_debug("ah_bonus_section", sect)
                for item in sect.get("bonusGroupOrProducts", []):
                    prods = []
                    if item.get("product"):
                        prods.append(item["product"])
                    grp = item.get("bonusGroup")
                    if grp:
                        g = s.get("https://api.ah.nl/mobile-services/bonuspage/v1/segment",
                                  params={"date": today, "segmentId": grp["id"], "includeActivatableDiscount": "false"},
                                  timeout=30)
                        if g.ok:
                            gj = g.json()
                            gprods = gj.get("products", [])
                            for gp in gprods:  # groepsactie: mechanisme staat soms alleen op de groep
                                gp.setdefault("bonusMechanism", grp.get("discountDescription") or grp.get("segmentDescription") or gj.get("discountDescription"))
                                gp.setdefault("bonusEndDate", period.get("bonusEndDate"))
                                gp.setdefault("isBonus", True)
                            prods += gprods
                        time.sleep(0.1)
                    for p in prods:
                        res = ah_product_to_bonus(p)
                        if res:
                            out[res[0]] = res[1]
    except Exception as e:
        status.setdefault("probe", {})["ah_bonuspage"] = str(e)[:200]  # oude route, valt terug op zoeken
        log(traceback.format_exc())

    # Route 2: het hele assortiment doorzoeken, per categorie (de zoek-API geeft max ~3000 per vraag)
    if len(out) < 50:
        try:
            out.update(ah_scan_catalog(s))
        except Exception as e:
            status["errors"].append(f"AH assortiment: {e}")
            log(traceback.format_exc())
    return out


AH_SEARCH = "https://api.ah.nl/mobile-services/product/search/v2"


AH_TERMS = """melk kaas vlees kip gehakt brood groente fruit aardappel tomaat appel banaan sla pasta rijst saus chips
koek chocolade snoep drop noten bier wijn frisdrank cola sap water koffie thee yoghurt kwark vla boter room eieren
ijs pizza diepvries friet shampoo douche deo tandpasta scheer wasmiddel afwas schoonmaak papier soep bouillon kruiden
olie mayonaise salade vis zalm worst ham salami beleg pindakaas jam hagelslag ontbijt muesli cracker baby luiers
kat hond vega tofu wraps noodles mexicaans italiaans aziatisch taart cake gebak vers maaltijd snack borrel nootjes""".split()


def ah_search_pages(s, base_params, sorts=("RELEVANCE",)):
    """Alle pagina's van één zoekvraag, eventueel met meerdere sorteringen om voorbij de 3000 te komen."""
    seen, total_el = {}, 0
    for sort in sorts:
        for page in range(0, 20):
            r = s.get(AH_SEARCH, params={**base_params, "page": page, "size": 750, "sortOn": sort}, timeout=60)
            if r.status_code == 400:   # voorbij het maximum
                break
            r.raise_for_status()
            j = r.json()
            total_el = j.get("page", {}).get("totalElements", 0)
            for p in j.get("products", []):
                seen[str(p.get("webshopId"))] = p
            if page + 1 >= j.get("page", {}).get("totalPages", 1):
                break
            time.sleep(0.05)
        if len(seen) >= total_el:
            break
    return seen, total_el


def ah_scan_catalog(s):
    probe = status.setdefault("probe", {})
    cats, variant = [], None
    r = s.get("https://api.ah.nl/mobile-services/v1/product-shelves/categories", timeout=30)
    probe["ah_categories_status"] = r.status_code
    if r.ok:
        cats = r.json()
        save_debug("ah_categories", cats)
    # Metadata van een zoekvraag (filters/facetten) bewaren, zonder de producten
    rm = s.get(AH_SEARCH, params={"query": "melk", "page": 0, "size": 5, "sortOn": "RELEVANCE"}, timeout=30)
    if rm.ok:
        save_debug("ah_search_meta", {k: v for k, v in rm.json().items() if k != "products"})
    c0 = cats[0] if cats else {}
    for key, val in (("taxonomyId", c0.get("id")), ("taxonomy", c0.get("id")),
                     ("taxonomySlug", c0.get("slugifiedName") or c0.get("slug"))):
        if val is None:
            continue
        rr = s.get(AH_SEARCH, params={"query": "", key: val, "page": 0, "size": 1, "sortOn": "RELEVANCE"}, timeout=30)
        tot = rr.json().get("page", {}).get("totalElements") if rr.ok else None
        probe[f"ah_{key}"] = [rr.status_code, tot]
        if rr.ok and tot and tot < 10000:
            variant = key
            break
    probe["ah_variant"] = variant
    allp, cover = {}, {}
    if variant:
        for c in cats:
            val = c.get("id") if variant != "taxonomySlug" else (c.get("slugifiedName") or c.get("slug"))
            got, tot = ah_search_pages(s, {"query": "", variant: val}, sorts=("RELEVANCE", "PRICELOWHIGH", "PRICEHIGHLOW"))
            allp.update(got)
            cover[c.get("name", str(val))] = [len(got), tot]
    else:  # terugval: zoeken op algemene woorden
        for term in AH_TERMS:
            got, tot = ah_search_pages(s, {"query": term}, sorts=("RELEVANCE", "PRICELOWHIGH", "PRICEHIGHLOW"))
            allp.update(got)
            cover[term] = [len(got), tot]
    probe["ah_cover"] = cover
    probe["ah_scanned"] = len(allp)
    out = {}
    for p in allp.values():
        if p.get("isBonus") or p.get("bonusMechanism"):
            res = ah_product_to_bonus(p)
            if res:
                out[res[0]] = res[1]
    return out


# ---------------------------------------------------------------- bonus: Jumbo

MONTHS = {"jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12}
WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
               "Accept-Language": "nl-NL,nl;q=0.9", "Accept": "text/html,application/xhtml+xml"}


def _text(html):
    t = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"').replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", t).strip()


def _until(subtitle):
    """'wo 11 t/m di 17 nov' -> '2026-11-17'"""
    m = re.search(r"t/m\s+\w+\s+(\d{1,2})\s+([a-z]{3})", subtitle or "")
    if not m or m.group(2) not in MONTHS:
        return None
    today = date.today()
    mon, day = MONTHS[m.group(2)], int(m.group(1))
    year = today.year + (1 if mon < today.month - 6 else 0)
    try:
        return date(year, mon, day).isoformat()
    except ValueError:
        return None


def parse_jumbo_cards(html):
    """Actiekaarten van jumbo.com/aanbiedingen: id, titel, actietekst, einddatum, link."""
    cards = {}
    for block in re.split(r'<(?:li|article) class="[^"]*card-promotion', html)[1:]:
        ends = [i for i in (block.find("</li>"), block.find("</article>")) if i > 0]
        block = block[: min(ends)] if ends else block[:6000]
        exp = re.search(r'expiration-date="(\d{4}-\d{2}-\d{2})', block)
        href = re.search(r'href="(/aanbiedingen/[^"]+)"', block)
        if not href:
            continue
        tags = [_text(t) for t in re.findall(r'data-testid="jum-tag"[^>]*>(.*?)</(?:span|div)>\s*</(?:div|span)>', block, flags=re.S)]
        tag = " ".join(t for t in tags if t).strip()
        title = re.search(r'class="title-link"[^>]*>(.*?)</a>', block, flags=re.S)
        sub = re.search(r'class="subtitle"[^>]*>(.*?)</p>', block, flags=re.S)
        sub_t = _text(sub.group(1)) if sub else ""
        cards[href.group(1)] = {"t": tag, "title": _text(title.group(1)) if title else "", "until": exp.group(1) if exp else _until(sub_t), "period": sub_t}
    return cards


def nuxt_payload(html):
    """Leest de __NUXT_DATA__-payload (devalue-formaat) van een Nuxt-pagina uit."""
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, flags=re.S)
    if not m:
        return None
    arr = json.loads(m.group(1))
    memo = {}
    wrappers = {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef"}

    def res(i):
        if not isinstance(i, int) or isinstance(i, bool) or i < 0 or i >= len(arr):
            return i
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in wrappers:
                memo[i] = None
                memo[i] = res(v[1]) if len(v) > 1 else None
                return memo[i]
            if v and isinstance(v[0], str) and v[0] in ("Set", "Map", "Date", "null", "undefined", "NaN", "Infinity", "BigInt", "RegExp"):
                memo[i] = None
                return None
            out = []
            memo[i] = out
            out.extend(res(x) for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            memo[i] = out
            for k, x in v.items():
                out[k] = res(x)
            return out
        memo[i] = v
        return v

    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 20000))
    try:
        return res(0)
    finally:
        sys.setrecursionlimit(old)


def parse_jumbo_nuxt_promotions(html):
    root = nuxt_payload(html)
    cards, stack, seen = {}, [root], set()
    while stack:
        o = stack.pop()
        if id(o) in seen:
            continue
        seen.add(id(o))
        if isinstance(o, dict):
            if o.get("__typename") == "Promotion" and o.get("url"):
                tags = " ".join(t.get("text", "") for t in (o.get("tags") or []) if isinstance(t, dict)).strip()
                end = ((o.get("end") or {}).get("iso") or "")[:10] or None
                cards[o["url"]] = {"t": tags, "title": o.get("title") or "", "until": end,
                                   "period": (o.get("durationTexts") or {}).get("shortTitle") or ""}
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return cards


def fetch_jumbo_bonus():
    probe = status.setdefault("probe", {})
    s = requests.Session()
    s.headers.update(WEB_HEADERS)
    r = s.get("https://www.jumbo.com/aanbiedingen/nu", timeout=60)
    r.raise_for_status()
    try:
        cards = parse_jumbo_nuxt_promotions(r.text)
    except Exception as e:
        probe["jumbo_nuxt_error"] = str(e)[:200]
        cards = {}
    probe["jumbo_nuxt_promotions"] = len(cards)
    for href, c in parse_jumbo_cards(r.text).items():
        cards.setdefault(href, c)
    # Acties die wel gelinkt zijn maar niet als kaart herkend: later de tekst van de actiepagina zelf halen
    for href in set(re.findall(r'href="(/aanbiedingen/[a-z0-9-]+/[0-9]+)"', r.text)):
        cards.setdefault(href, {"t": "", "title": "", "until": None, "period": "", "fromPage": True})
    probe["jumbo_cards"] = len(cards)
    out, pages_ok, first = {}, 0, True
    for href, c in list(cards.items())[:400]:
        if not c["t"] and not c.get("fromPage"):
            continue
        if re.search(r"alleen online|online spellen", c["t"], flags=re.I):  # je doet boodschappen in de winkel
            continue
        try:
            rr = s.get("https://www.jumbo.com" + href, timeout=60)
        except Exception:
            continue
        if not rr.ok:
            probe.setdefault("jumbo_page_errors", []).append([href, rr.status_code])
            continue
        if first:
            (DEBUG / "jumbo_promotion_page.txt").write_text(rr.text[:60_000], encoding="utf-8")
            first = False
        pages_ok += 1
        if not c["t"]:
            tags = [_text(t) for t in re.findall(r'data-testid="jum-tag"[^>]*>(.*?)</(?:span|div)>\s*</(?:div|span)>', rr.text, flags=re.S)]
            tags = [t for t in tags if t and "$" not in t]
            c["t"] = tags[0] if tags else ""
            m = re.search(r'loopt van ([^|"]+)', rr.text)
            c["until"] = _until(m.group(1)) if m else None
            if not c["t"]:
                continue
        ids = set(re.findall(r'href="/producten/[^"?#]*?-(\d+[A-Z]{2,4})"', rr.text))
        for pid in ids:
            out[pid] = {"t": c["t"], "p": None, "q": 1, "w": None, "until": c["until"], "title": c["title"]}
        time.sleep(0.4)
    probe["jumbo_pages"] = pages_ok
    probe["jumbo_sample_cards"] = list(cards.values())[:15]
    return out


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices-file")
    ap.add_argument("--no-bonus", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)

    prices = load_prices(args.prices_file)

    bonus = {"ah": {}, "jumbo": {}}
    old_bonus_path = DATA / "bonus.json"
    if not args.no_bonus:
        for store, fn in (("ah", fetch_ah_bonus), ("jumbo", fetch_jumbo_bonus)):
            try:
                bonus[store] = fn()
            except Exception as e:
                status["errors"].append(f"{store} bonus: {e}")
                log(traceback.format_exc())
            status["bonus"][store] = len(bonus[store])
            log(f"bonus {store}: {len(bonus[store])} aanbiedingen")
        # Mislukt? Houd de vorige bonus aan zolang die nog geldig is.
        if old_bonus_path.exists():
            old = json.loads(old_bonus_path.read_text(encoding="utf-8"))
            today = date.today().isoformat()
            for store in STORES:
                if not bonus[store]:
                    kept = {k: v for k, v in old.get(store, {}).items() if (v.get("until") or "9999") >= today}
                    if kept:
                        bonus[store] = kept
                        status["errors"].append(f"{store}: vorige bonus aangehouden ({len(kept)})")

    # Jumbo-acties: effectieve prijs uitrekenen met de normale prijs uit de prijslijst
    jp = prices.get("jumbo", {}).get("products", {})
    for pid, bb in bonus["jumbo"].items():
        if bb.get("p") is None and pid in jp:
            reg = jp[pid][1]
            eff, qty = effective(reg, bb["t"])
            bb.update({"p": eff, "q": qty, "w": reg})

    # Bonusproducten die niet in de prijslijst staan toch toevoegen
    for store in STORES:
        prods = prices.get(store, {}).get("products", {})
        for pid, b in bonus[store].items():
            if pid not in prods and b.get("n") and b.get("w"):
                parsed = parse_size(b.get("s") or "")
                unit, qty = parsed if parsed and parsed[1] > 0 else (None, None)
                prods[pid] = [b["n"], b["w"], b.get("s") or "", round(b["w"] / qty, 2) if unit else None, unit, None]
        for b in bonus[store].values():
            b.pop("n", None); b.pop("s", None); b.pop("title", None)

    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    status["updated"] = now
    (DATA / "prices.json").write_text(json.dumps({"updated": now, "stores": prices}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATA / "bonus.json").write_text(json.dumps({"updated": now, **bonus}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATA / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    log(json.dumps(status, ensure_ascii=False))
    if not prices:
        sys.exit(1)


if __name__ == "__main__":
    main()
