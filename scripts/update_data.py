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
        status["errors"].append(f"AH bonuspagina: {e}")
        log(traceback.format_exc())

    # Route 2 (aanvulling/terugval): zoeken met bonusfilter
    if len(out) < 50:
        try:
            page, total = 0, 1
            while page < total and page < 40:
                r = s.get("https://api.ah.nl/mobile-services/product/search/v2",
                          params={"query": "", "page": page, "size": 750, "sortOn": "RELEVANCE", "bonus": "BONUS"},
                          timeout=60)
                r.raise_for_status()
                j = r.json()
                if page == 0:
                    save_debug("ah_search_bonus", j)
                total = j.get("page", {}).get("totalPages", 1)
                for p in j.get("products", []):
                    if p.get("isBonus"):
                        res = ah_product_to_bonus(p)
                        if res:
                            out.setdefault(res[0], res[1])
                page += 1
        except Exception as e:
            status["errors"].append(f"AH zoeken: {e}")
            log(traceback.format_exc())
    return out


# ---------------------------------------------------------------- bonus: Jumbo

JUMBO_HEADERS = {"User-Agent": "Jumbo/12.0 (Android 14)", "Accept": "application/json"}
JUMBO_ID = re.compile(r"^\d+[A-Z]{2,4}$")


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _jumbo_price(prod):
    pr = prod.get("prices") or {}
    reg = (pr.get("price") or {}).get("amount")
    promo = (pr.get("promotionalPrice") or {}).get("amount")
    f = lambda c: round(c / 100, 2) if isinstance(c, (int, float)) and c > 20 else c
    return f(reg), f(promo)


def _jumbo_promo_text(prod):
    for key in ("promotion", "promotions"):
        v = prod.get(key)
        if isinstance(v, list) and v:
            v = v[0]
        if isinstance(v, dict):
            tags = v.get("tags") or []
            txt = " ".join(t.get("text", "") for t in tags if isinstance(t, dict)) or v.get("name") or v.get("title") or ""
            return txt.strip(), v.get("toDate") or v.get("endDate") or v.get("validUntil")
    return "", None


def fetch_jumbo_bonus():
    out = {}
    s = requests.Session()
    s.headers.update(JUMBO_HEADERS)
    last_err = None
    for ver in ("v17", "v16", "v15"):
        base = f"https://mobileapi.jumbo.com/{ver}"
        try:
            r = s.get(base + "/promotion-overview", timeout=30)
            r.raise_for_status()
            ov = r.json()
            save_debug("jumbo_promotion_overview", ov)
            # Productobjecten die direct in het overzicht staan
            promo_ids = set()
            for d in _walk(ov):
                pid = d.get("id")
                if isinstance(pid, str) and JUMBO_ID.match(pid) and "title" in d:
                    reg, promo = _jumbo_price(d)
                    txt, until = _jumbo_promo_text(d)
                    eff, qty = effective(reg, txt, now_price=promo)
                    out[pid] = {"t": txt or "Aanbieding", "p": eff, "q": qty, "w": reg, "until": until,
                                "n": d.get("title"), "s": d.get("quantity")}
                elif isinstance(pid, (str, int)) and ("promotionId" in d or d.get("type") == "promotion"):
                    promo_ids.add(str(d.get("promotionId") or pid))
            # Per actie de producten ophalen
            for i, prid in enumerate(sorted(promo_ids)[:400]):
                rr = s.get(base + "/search", params={"promotionId": prid, "limit": 30}, timeout=30)
                if not rr.ok:
                    continue
                j = rr.json()
                if i == 0:
                    save_debug("jumbo_promotion_products", j)
                for d in (j.get("products") or {}).get("data", []):
                    reg, promo = _jumbo_price(d)
                    txt, until = _jumbo_promo_text(d)
                    eff, qty = effective(reg, txt, now_price=promo)
                    out[d["id"]] = {"t": txt or "Aanbieding", "p": eff, "q": qty, "w": reg, "until": until,
                                    "n": d.get("title"), "s": d.get("quantity")}
                time.sleep(0.1)
            if out:
                return out
        except Exception as e:
            last_err = e
            log(f"Jumbo {ver}: {e}")
    if last_err and not out:
        status["errors"].append(f"Jumbo aanbiedingen: {last_err}")
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

    # Bonusproducten die niet in de prijslijst staan toch toevoegen
    for store in STORES:
        prods = prices.get(store, {}).get("products", {})
        for pid, b in bonus[store].items():
            if pid not in prods and b.get("n") and b.get("w"):
                parsed = parse_size(b.get("s") or "")
                unit, qty = parsed if parsed and parsed[1] > 0 else (None, None)
                prods[pid] = [b["n"], b["w"], b.get("s") or "", round(b["w"] / qty, 2) if unit else None, unit, None]
        for b in bonus[store].values():
            b.pop("n", None); b.pop("s", None)

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
