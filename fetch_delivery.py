#!/usr/bin/env python3
"""
Fetches delivery data from Gotom reporting pages and writes delivery.json.
Run by GitHub Actions daily. Requires: pip install requests beautifulsoup4
"""

import re
import json
import time
import logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Campaign registry ──────────────────────────────────────────────────────────
# market: "se" or "no"  |  id: Gotom task id  |  token: URL hash
CAMPAIGNS = [
    # Sweden
    {"id": "1176", "token": "6206f3e3dc2868875ea4b4bda3120732", "name": "Elgiganten SE – AON Juni",          "market": "se", "duration": True},
    {"id": "1180", "token": "c858e7512dbf170e0df906fbc582c9b7", "name": "Elgiganten SE – AON Juli",          "market": "se"},
    {"id": "1181", "token": "fdb610295414f686fdc0149ca8b3e05a", "name": "Elgiganten SE – AON Augusti",       "market": "se"},
    {"id": "1182", "token": "98856ddf26060fa0bd2da9b6cff4cd38", "name": "Elgiganten SE – AON September",     "market": "se"},
    {"id": "1183", "token": "b1032c78a2d18906a7b30aaa00bd448f", "name": "Elgiganten SE – AON Oktober",      "market": "se"},
    {"id": "1184", "token": "89a6db4b04b973253a295537435b5162", "name": "Elgiganten SE – AON November",     "market": "se"},
    {"id": "1093", "token": "99255fd10b5945724da5dc8db50ed338", "name": "Elgiganten SE – Black Week 2026",  "market": "se"},
    {"id": "1185", "token": "a35fc595e50c4fcd06ddb911bc17acc5", "name": "Elgiganten SE – AON December",     "market": "se"},
    {"id": "1186", "token": "673e94b2b05e7be4ea524add11ab43f8", "name": "Elgiganten SE – AON Januari 2027", "market": "se"},
    {"id": "1187", "token": "716daf04a86947e4982106727514b41e", "name": "Elgiganten SE – AON Februari 2027","market": "se"},
    {"id": "1188", "token": "8902ec6b2fcef5949ccf708953ec0c7e", "name": "Elgiganten SE – AON Mars 2027",    "market": "se"},
    {"id": "1189", "token": "4b04da17581e2fcbc8f122bbcf28b82c", "name": "Elgiganten SE – AON April 2027",   "market": "se"},
    # Norway
    {"id": "1175", "token": "bc8cd543dba15b0c206ac1087a6c1ab4", "name": "Elkjøp NO – AON Juni",             "market": "no", "duration": True},
    {"id": "1190", "token": "d6939225eb363f8b9a15abb36528a9fa", "name": "Elkjøp NO – AON Juli 2026",        "market": "no"},
    {"id": "1191", "token": "6d60ef49935cf118f24b95f3ca7adc25", "name": "Elkjøp NO – AON Aug 2026",         "market": "no"},
    {"id": "1192", "token": "6df5df0e131de1400471fe7a41e31355", "name": "Elkjøp NO – AON Sep 2026",         "market": "no"},
]

HEADERS = {"User-Agent": "Mozilla/5.0 (prisjakt delivery-checker/1.0)"}


def build_url(campaign):
    base = f"https://prisjakt.gotom.io/reporting-task-report/{campaign['id']}/{campaign['token']}"
    if campaign.get("duration"):
        base += "?reportingPeriod%5B1%5D=duration"
    return base


def parse_number(text):
    """'SEK 44\'688.90' or '0' or '' -> float"""
    text = re.sub(r"[A-Za-z]+", "", text).replace("'", "").replace(",", ".").strip()
    try:
        return round(float(text), 2)
    except ValueError:
        return 0.0


def parse_pct(text):
    """'87.50%' -> 87.5 | '' -> None"""
    text = text.replace("%", "").replace(",", ".").strip()
    if not text:
        return None
    try:
        return round(float(text), 1)
    except ValueError:
        return None


def identify_product(channel, ad_format, price_type):
    ch = channel.lower()
    af = ad_format.lower()
    pt = price_type.lower()
    if "audience extension" in ch or "- ae" in pt:
        return "A.E"
    if "roc" in af:
        return "ROC"
    if "welcome page" in af:
        return "Welcome Page"
    return None


def fetch_campaign(campaign):
    url = build_url(campaign)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Failed to fetch %s (%s): %s", campaign["name"], campaign["id"], e)
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    if len(tables) < 2:
        log.warning("No delivery table found for %s", campaign["name"])
        return {}

    delivery_table = tables[1]
    rows = delivery_table.find_all("tr")
    products = {}

    for row in rows[1:]:  # skip header row
        cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        if len(cells) < 17:
            continue

        platform = cells[1]
        if "total" in platform.lower():
            continue  # skip summary rows

        channel    = cells[2]
        ad_format  = cells[3]
        price_type = cells[8]
        units_booked_str      = cells[9]    # e.g. "457'967"
        costs_booked_str      = cells[10]   # e.g. "SEK 44'688.90"
        impressions_str       = cells[11]   # e.g. "302'179"
        value_delivered_str   = cells[16]   # e.g. "SEK 38'000.00"

        product = identify_product(channel, ad_format, price_type)
        if not product:
            log.debug("Unrecognised row: ch=%s af=%s pt=%s", channel, ad_format, price_type)
            continue

        units_booked      = parse_number(units_booked_str)
        impressions       = parse_number(impressions_str)
        booked            = parse_number(costs_booked_str)
        delivered         = parse_number(value_delivered_str)

        # Always compute pct from impressions_delivered / units_booked (stable, no HTML noise)
        if units_booked and units_booked > 0:
            pct = round(impressions / units_booked * 100, 1)
        else:
            pct = None

        products[product] = {
            "booked":    booked,
            "delivered": delivered,
            "pct":       pct,   # None if not yet started
        }

    return products


def main():
    result = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "campaigns": {}
    }

    for c in CAMPAIGNS:
        log.info("Fetching %s (id=%s)…", c["name"], c["id"])
        products = fetch_campaign(c)
        result["campaigns"][c["id"]] = {
            "name":     c["name"],
            "market":   c["market"],
            "products": products,
        }
        time.sleep(1)  # be polite

    with open("delivery.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log.info("Wrote delivery.json with %d campaigns.", len(result["campaigns"]))


if __name__ == "__main__":
    main()
