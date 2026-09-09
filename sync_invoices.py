#!/usr/bin/env python3
"""
Markez dashboard – számla-szinkronizáló script.

Óránként (GitHub Actions cron) lefut, beolvassa az "Iktatás 2026" Google
Sheets fájlt egy service accounttal, kigyűjti az összes projekt-fülön
található, még ki nem fizetett, fizetési határidővel rendelkező tételt,
és POST-olja egy WordPress REST végpontra.

A tábla szerkezete fülenként ismétlődő blokkokból áll: valahol a fülön
van egy fejléc-sor, ami tartalmazza az "Iktatószám", "Fizetési határidő"
és "Fizetve" oszlopokat, alatta pedig adat-sorok, amíg üres Iktatószám
nem következik. A script ezt a mintát keresi meg minden fülön, nem
hardkódolt fül- vagy oszlop-pozíciókra épít – így ha új projekt-fül
kerül a táblázatba, külön kódmódosítás nélkül is felismeri.

Szükséges környezeti változók (GitHub Secrets-ből):
  GOOGLE_SERVICE_ACCOUNT_KEY  – a service account JSON kulcsának teljes tartalma
  GOOGLE_SHEET_ID             – az "Iktatás 2026" táblázat ID-ja (a Sheets URL-jéből)
  WP_SYNC_URL                 – a WordPress REST végpont teljes URL-je
                                 (pl. https://markez.hu/wp-json/markez/v1/invoices)
  WP_SYNC_API_KEY             – a WPCode snippetben beállított titkos kulcs
"""

import json
import os
import re
import sys
from datetime import date

import requests
from google.oauth2.service_account import Credentials
import gspread

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

REQUIRED_HEADERS = {"Iktatószám", "Fizetési határidő", "Fizetve"}


def get_client():
    key_json = os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"]
    info = json.loads(key_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def parse_amount(raw):
    """'10 398 Ft' -> 10398 (int). Üres/hibás érték esetén 0."""
    if raw is None:
        return 0
    digits = re.sub(r"[^\d]", "", str(raw))
    return int(digits) if digits else 0


def parse_hu_date(raw):
    """'2026.07.20.' -> '2026-07-20'. Üres/hibás érték esetén None."""
    if not raw:
        return None
    m = re.match(r"^\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\s*$", str(raw))
    if not m:
        return None
    y, mo, d = m.groups()
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def parse_bool(raw):
    return str(raw).strip().upper() in ("TRUE", "IGAZ", "1", "X")


def find_header_rows(values):
    """Egy fül összes sora közül visszaadja azoknak a sor-indexeit
    (0-alapú), amik a szükséges oszlopfejléceket tartalmazzák."""
    out = []
    for i, row in enumerate(values):
        cells = {c.strip() for c in row if isinstance(c, str)}
        if REQUIRED_HEADERS.issubset(cells):
            out.append(i)
    return out


def extract_block(values, header_row_idx):
    """A header_row_idx alatti adat-sorokat olvassa ki, amíg üres
    Iktatószámmal rendelkező sorba nem fut (vagy a fül végéig)."""
    header = values[header_row_idx]
    col = {name.strip(): idx for idx, name in enumerate(header) if isinstance(name, str) and name.strip()}

    def cell(row, name):
        idx = col.get(name)
        if idx is None or idx >= len(row):
            return ""
        return row[idx]

    rows = []
    for row in values[header_row_idx + 1:]:
        iktatoszam = cell(row, "Iktatószám").strip()
        if not iktatoszam:
            break
        rows.append({
            "id": iktatoszam,
            "megnevezes": cell(row, "Megnevezés"),
            "szamlaszam": cell(row, "Számlaszám"),
            "partner": cell(row, "Partner"),
            "osszeg": parse_amount(cell(row, "Összeg")),
            "hatarido": parse_hu_date(cell(row, "Fizetési határidő")),
            "fizetve": parse_bool(cell(row, "Fizetve")),
            "megjegyzes": cell(row, "Megjegyzés"),
        })
    return rows


def collect_invoices(spreadsheet):
    all_rows = []
    for ws in spreadsheet.worksheets():
        values = ws.get_all_values()
        for header_idx in find_header_rows(values):
            for item in extract_block(values, header_idx):
                item["projekt_fül"] = ws.title
                all_rows.append(item)
    return all_rows


def main():
    gc = get_client()
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    spreadsheet = gc.open_by_key(sheet_id)

    all_invoices = collect_invoices(spreadsheet)

    # Csak a ki nem fizetett, ismert határidejű tételek érdekesek a dashboard számára.
    open_invoices = [
        inv for inv in all_invoices
        if not inv["fizetve"] and inv["hatarido"]
    ]

    payload = {
        "generated_at": date.today().isoformat(),
        "invoices": open_invoices,
    }

    wp_url = os.environ["WP_SYNC_URL"]
    api_key = os.environ["WP_SYNC_API_KEY"]

    resp = requests.post(
        wp_url,
        json=payload,
        headers={
            "X-Markez-Api-Key": api_key,
            "Content-Type": "application/json",
            # Az mhosting.hu-s WAF (Imunify360) csendben blokkolja a
            # nyilvánvalóan script-szerű User-Agent-eket (200-as
            # státusszal, de "Access Denied" tartalommal) — ezért
            # valódi böngésző UA-t küldünk.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        },
        timeout=30,
    )

    print(f"POST {wp_url} -> HTTP {resp.status_code}")
    print(resp.text[:2000])

    try:
        data = resp.json()
    except ValueError:
        print("HIBA: a válasz nem érvényes JSON — valószínűleg a WAF vagy egy "
              "gyorsítótár/cache-plugin szólt közbe, nem a valódi endpoint válaszolt.")
        sys.exit(1)

    if not resp.ok or "updated" not in data:
        print(f"HIBA: váratlan válaszformátum: {data}")
        sys.exit(1)

    print(f"Sikeres szinkronizálás: {data.get('updated')} nyitott számla mentve.")


if __name__ == "__main__":
    main()
