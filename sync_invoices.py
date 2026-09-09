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

Ezen kívül: a WordPress-en bejelölt "fizetve" számlákat (amiket a
dashboardon valaki bepipált) minden futáskor visszaírja a Sheetbe
(Fizetve = TRUE), és csak ez után törli őket a "szinkronra vár"
állapotból — így a checkbox a felületen csak a sikeres visszaírás
után tűnik el ténylegesen a listáról.

Szükséges környezeti változók (GitHub Secrets-ből):
  GOOGLE_SERVICE_ACCOUNT_KEY  – a service account JSON kulcsának teljes tartalma
                                 (a service accountnak Szerkesztő jogosultsággal
                                 kell rendelkeznie a táblázaton, mert most már ír is)
  GOOGLE_SHEET_ID             – az "Iktatás 2026" táblázat ID-ja (a Sheets URL-jéből)
  WP_SYNC_URL                 – a WordPress REST végpont teljes URL-je
                                 (pl. https://markez.hu/wp-json/markez/v1/invoices)
  WP_SYNC_API_KEY             – a WPCode snippetben beállított titkos kulcs
"""

import json
import os
import re
import sys
from datetime import date, datetime

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

REQUIRED_HEADERS = {"Iktatószám", "Fizetési határidő", "Fizetve"}

# Valódi böngésző User-Agent kell, mert az mhosting.hu-s WAF (Imunify360)
# csendben (HTTP 200, de "Access Denied" tartalommal) blokkolja a
# nyilvánvalóan script-szerű kéréseket.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------
# Google Sheets olvasás/írás
# ---------------------------------------------------------------------

def get_sheets_service():
    key_json = os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"]
    info = json.loads(key_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


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


def col_letter(idx0):
    """0-alapú oszlopindexből A1-stílusú oszlopbetű ('A', 'B', ..., 'AA', ...)."""
    idx = idx0 + 1
    out = ""
    while idx:
        idx, r = divmod(idx - 1, 26)
        out = chr(65 + r) + out
    return out


def fetch_sheets_grid(service, sheet_id):
    """Az egész táblázatot lekéri, fülenkénti cella-rácsban: minden cella
    {'value': megjelenített szöveg, 'link': hivatkozás vagy None}."""
    resp = service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields=(
            "sheets(properties.title,"
            "data.rowData.values(formattedValue,hyperlink,userEnteredValue.formulaValue))"
        ),
    ).execute()

    grids = {}
    for sheet in resp.get("sheets", []):
        title = sheet["properties"]["title"]
        row_data = (sheet.get("data") or [{}])[0].get("rowData", []) or []
        grid = []
        for row in row_data:
            out_row = []
            for cell in row.get("values", []) or []:
                value = cell.get("formattedValue", "") or ""
                link = cell.get("hyperlink")
                if not link:
                    formula = (cell.get("userEnteredValue") or {}).get("formulaValue")
                    if formula:
                        m = re.search(r'HYPERLINK\(\s*"([^"]+)"', formula, re.IGNORECASE)
                        if m:
                            link = m.group(1)
                out_row.append({"value": value, "link": link})
            grid.append(out_row)
        grids[title] = grid
    return grids


def find_header_rows(grid):
    out = []
    for i, row in enumerate(grid):
        cells = {c["value"].strip() for c in row if c["value"]}
        if REQUIRED_HEADERS.issubset(cells):
            out.append(i)
    return out


def extract_block(grid, header_row_idx, sheet_title):
    """A header_row_idx alatti adat-sorokat olvassa ki, amíg üres
    Iktatószámmal rendelkező sorba nem fut. Minden tételhez eltárolja a
    Sheet-beli abszolút sorszámát és a "Fizetve" oszlop indexét is, hogy
    később vissza tudjunk írni oda."""
    header = grid[header_row_idx]
    col = {
        c["value"].strip(): idx
        for idx, c in enumerate(header)
        if c["value"] and c["value"].strip()
    }
    fizetve_col = col.get("Fizetve")

    def cell(row, name):
        idx = col.get(name)
        if idx is None or idx >= len(row):
            return {"value": "", "link": None}
        return row[idx]

    rows = []
    blank_streak = 0
    for offset, row in enumerate(grid[header_row_idx + 1:]):
        row_number = header_row_idx + 2 + offset  # 1-alapú, valódi Sheet sorszám
        iktatoszam = cell(row, "Iktatószám")["value"].strip()
        if not iktatoszam:
            # Egy-egy üres sor (pl. elválasztó, vagy egy Drive-linket tartalmazó
            # jegyzet-sor közvetlenül a fejléc alatt) nem jelenti a blokk végét —
            # csak több egymást követő üres sor után adjuk fel a keresést.
            blank_streak += 1
            if blank_streak >= 5:
                break
            continue
        blank_streak = 0
        rows.append({
            "id": iktatoszam,
            "kep_url": cell(row, "Iktatószám")["link"] or "",
            "megnevezes": cell(row, "Megnevezés")["value"],
            "szamlaszam": cell(row, "Számlaszám")["value"],
            "partner": cell(row, "Partner")["value"],
            "osszeg": parse_amount(cell(row, "Összeg")["value"]),
            "hatarido": parse_hu_date(cell(row, "Fizetési határidő")["value"]),
            "fizetve": parse_bool(cell(row, "Fizetve")["value"]),
            "megjegyzes": cell(row, "Megjegyzés")["value"],
            "tab": sheet_title,
            "_sheet_title": sheet_title,
            "_row": row_number,
            "_fizetve_col": fizetve_col,
        })
    return rows


def collect_invoices(grids):
    all_rows = []
    print(f"{len(grids)} fül található a táblázatban: {', '.join(grids.keys())}")
    for title, grid in grids.items():
        header_rows = find_header_rows(grid)
        if not header_rows:
            print(f"  [{title}] NINCS felismert fejléc-sor (Iktatószám/Fizetési határidő/Fizetve egy sorban) — kihagyva")
            continue
        tab_rows = []
        for header_idx in header_rows:
            block = extract_block(grid, header_idx, title)
            print(f"  [{title}] fejléc a(z) {header_idx + 1}. sorban, {len(block)} adat-sor kiolvasva")
            tab_rows.extend(block)
        all_rows.extend(tab_rows)
    return all_rows


def write_paid_back(service, sheet_id, entries):
    """entries: az invoice-rekordok listája (a collect_invoices kimenetéből),
    amiket TRUE-ra kell írni a Fizetve oszlopukban. Egy batchUpdate hívással
    írja vissza mindet."""
    data = []
    for inv in entries:
        if inv["_fizetve_col"] is None:
            continue
        rng = "'{}'!{}{}".format(
            inv["_sheet_title"].replace("'", "''"),
            col_letter(inv["_fizetve_col"]),
            inv["_row"],
        )
        data.append({"range": rng, "values": [["TRUE"]]})
    if not data:
        return
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()


# ---------------------------------------------------------------------
# WordPress REST hívások
# ---------------------------------------------------------------------

def wp_request(method, url, api_key, json_body=None):
    """Egységes, WAF-tudatos kérés a markez/v1 API-kulcsos végpontokhoz.
    Visszaadja a dekódolt JSON választ, vagy None + hibaüzenet kiírás, ha
    valami hibás (nem JSON válasz, HTTP hiba, stb.)."""
    try:
        resp = requests.request(
            method, url,
            json=json_body,
            headers={
                "X-Markez-Api-Key": api_key,
                "Content-Type": "application/json",
                "User-Agent": BROWSER_UA,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"HIBA: {method} {url} kérés sikertelen: {exc}")
        return None

    print(f"{method} {url} -> HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        print(
            "HIBA: a válasz nem érvényes JSON — valószínűleg a WAF vagy egy "
            "gyorsítótár/cache-plugin szólt közbe, nem a valódi endpoint válaszolt."
        )
        print(resp.text[:500])
        return None

    if not resp.ok:
        print(f"HIBA: váratlan válaszformátum: {data}")
        return None

    return data


def now_hu_str():
    """Aktuális dátum+idő magyar (Europe/Budapest, CET/CEST) időzónában,
    'ÉÉÉÉ.HH.NN. óó:pp' formátumban."""
    if ZoneInfo is not None:
        try:
            now = datetime.now(ZoneInfo("Europe/Budapest"))
            return now.strftime("%Y.%m.%d. %H:%M")
        except Exception:
            pass
    # Ha a zoneinfo/tzdata valamiért nem elérhető, essünk vissza UTC-re,
    # inkább legyen pontatlan az óra, mint hogy elszálljon a script.
    return datetime.utcnow().strftime("%Y.%m.%d. %H:%M UTC")


def main():
    sheets_service = get_sheets_service()
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    wp_url = os.environ["WP_SYNC_URL"].rstrip("/")
    api_key = os.environ["WP_SYNC_API_KEY"]

    # A /wp-json/markez/v1/invoices URL-ből vezetjük le a testvér-végpontokat.
    base_url = wp_url.rsplit("/invoices", 1)[0]
    pending_url = f"{base_url}/pending-paid"
    clear_url = f"{base_url}/clear-paid"

    grids = fetch_sheets_grid(sheets_service, sheet_id)
    all_invoices = collect_invoices(grids)
    by_id = {}
    for inv in all_invoices:
        by_id.setdefault(inv["id"], []).append(inv)

    # 1) Amit a dashboardon bejelöltek fizetettnek, azt írjuk vissza a Sheetbe.
    pending_data = wp_request("GET", pending_url, api_key)
    cleared_ids = []
    if pending_data and isinstance(pending_data.get("pending"), list):
        to_write = []
        for p in pending_data["pending"]:
            pid = p.get("id")
            ptab = p.get("tab") or ""
            candidates = by_id.get(pid) or []
            match = None
            for c in candidates:
                if ptab and c["tab"] == ptab:
                    match = c
                    break
            if match is None and candidates:
                match = candidates[0]
            if match is None:
                print(f"FIGYELEM: a szinkronra váró {pid!r} iktatószám nem található a táblázatban, kihagyva.")
                continue
            to_write.append(match)

        if to_write:
            try:
                write_paid_back(sheets_service, sheet_id, to_write)
                for inv in to_write:
                    inv["fizetve"] = True  # ne kerüljön be a nyitott listába lent
                    cleared_ids.append(inv["id"])
                print(f"{len(to_write)} tétel visszaírva a Sheetbe (Fizetve = TRUE).")
            except Exception as exc:
                print(f"HIBA: nem sikerült visszaírni a Sheetbe: {exc}")

        if cleared_ids:
            clear_resp = wp_request("POST", clear_url, api_key, {"ids": cleared_ids})
            if clear_resp is None:
                print(
                    "FIGYELEM: a Sheet-be írás sikerült, de a WordPress-en nem sikerült "
                    "törölni a 'szinkronra vár' jelölést — a következő futás újra megpróbálja."
                )
    else:
        print("Nincs (elérhető) szinkronra váró tétel, vagy a pending-paid lekérés sikertelen volt — kihagyva.")

    # 2) A szokásos, teljes nyitott-számla lista frissítése.
    open_invoices = [
        {k: v for k, v in inv.items() if not k.startswith("_")}
        for inv in all_invoices
        if not inv["fizetve"] and inv["hatarido"]
    ]

    # 3) A pénzügyi összesítőhöz (dashboard "Pénzügyi összesítő" panelje) az
    # ÖSSZES tétel kell — fizetett is, határidő nélküli is —, hogy a
    # projektenkénti kiadás-összeg helyes legyen. Csak a szükséges, karcsú
    # mezőket küldjük.
    all_invoices_lean = [
        {
            "tab": inv["tab"],
            "megjegyzes": inv["megjegyzes"],
            "osszeg": inv["osszeg"],
            "fizetve": inv["fizetve"],
        }
        for inv in all_invoices
    ]

    payload = {
        "generated_at": now_hu_str(),
        "invoices": open_invoices,
        "all_invoices": all_invoices_lean,
    }

    result = wp_request("POST", wp_url, api_key, payload)
    if result is None or "updated" not in result:
        sys.exit(1)

    print(f"Sikeres szinkronizálás: {result.get('updated')} nyitott számla mentve.")


if __name__ == "__main__":
    main()
