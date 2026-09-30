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

import html
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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
    # A "Számla" oszlop (B) jelöli, hogy a sor valódi számla-e — az
    # árajánlatok, szerződések stb. sorai ezt nem jelölik be, ezeket nem
    # szabad se a nyitott számlák közé, se a kiadás-összesítőbe beszámítani.
    # Ha egy fülön nincs ilyen oszlop, biztonságból mindent számlának
    # tekintünk (visszafelé kompatibilitás).
    szamla_col_present = "Számla" in col

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
        is_szamla = parse_bool(cell(row, "Számla")["value"]) if szamla_col_present else True
        rows.append({
            "id": iktatoszam,
            "szamla": is_szamla,
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


# ---------------------------------------------------------------------
# Napi összesítő email
# ---------------------------------------------------------------------
#
# A workflow két, egy órával eltolt cronnal fut (06:12 CET és 06:12 CEST),
# hogy a nyári/téli időszámítás-váltás ne csússza el a küldés idejét — csak
# az az egy fusson le ténylegesen levélküldéssel, amelyiknél a tényleges
# aktuális budapesti óra épp 6, a másik némán kihagyja (lásd main()).

def budapest_now():
    if ZoneInfo is None:
        return None
    try:
        return datetime.now(ZoneInfo("Europe/Budapest"))
    except Exception:
        return None


def fmt_amount_hu(n):
    return f"{int(n):,}".replace(",", " ") + " Ft"


def fmt_date_hu(iso):
    """'ÉÉÉÉ-HH-NN' -> 'ÉÉÉÉ.HH.NN.'"""
    try:
        y, m, d = iso.split("-")
        return f"{y}.{m}.{d}."
    except Exception:
        return iso or "—"


def _id_cell_html(inv):
    """Az iktatószám cellája — ha van hozzá kép-link (ugyanaz, mint a
    dashboardon), akkor linkelve nyílik meg új fülön."""
    inv_id = html.escape(str(inv.get("id") or "—"))
    link = inv.get("kep_url")
    if link:
        return f'<a href="{html.escape(str(link), quote=True)}" target="_blank" rel="noopener" style="color:#F99A1C;text-decoration:none;">{inv_id}</a>'
    return inv_id


def _summary_table_html(title, items, accent, empty_text, row_bg=None, row_border=None):
    """row_bg/row_border: ha meg van adva, minden sor (és a fejléc alja)
    ezzel a háttér-/keretszínnel emelődik ki — ugyanúgy, mint a dashboardon
    a "ma esedékes" számlacsoport kiemelése."""
    if not items:
        return (
            f'<h3 style="margin:22px 0 4px;color:#2a2013;font-size:15px;">{title} (0)</h3>'
            f'<p style="color:#8a7a63;font-size:13px;margin:0 0 4px;">{empty_text}</p>'
        )
    cell_bg = f'background:{row_bg};' if row_bg else ''
    border_style = f'1px solid {row_border}' if row_border else '1px solid #e9dcc3'
    rows = "".join(
        f'<tr style="{cell_bg}">'
        f'<td style="padding:5px 8px;border-bottom:{border_style};white-space:nowrap;">{_id_cell_html(inv)}</td>'
        f'<td style="padding:5px 8px;border-bottom:{border_style};white-space:nowrap;">{fmt_date_hu(inv["hatarido"])}</td>'
        f'<td style="padding:5px 8px;border-bottom:{border_style};">{html.escape(str(inv.get("tab") or "—"))}</td>'
        f'<td style="padding:5px 8px;border-bottom:{border_style};">{html.escape(str(inv.get("partner") or "—"))}</td>'
        f'<td style="padding:5px 8px;border-bottom:{border_style};">{html.escape(str(inv.get("megnevezes") or "—"))}</td>'
        f'<td style="padding:5px 8px;border-bottom:{border_style};text-align:right;white-space:nowrap;">{fmt_amount_hu(inv.get("osszeg") or 0)}</td>'
        '</tr>'
        for inv in items
    )
    table_border = f'border:1px solid {row_border};border-radius:8px;overflow:hidden;' if row_border else ''
    return f'''
    <h3 style="margin:22px 0 6px;color:{accent};font-size:15px;">{title} ({len(items)})</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;font-family:-apple-system,Segoe UI,sans-serif;{table_border}">
      <thead>
        <tr style="text-align:left;color:#8a7a63;font-size:11px;text-transform:uppercase;{cell_bg}">
          <th style="padding:4px 8px;">Iktatószám</th>
          <th style="padding:4px 8px;">Határidő</th>
          <th style="padding:4px 8px;">Projekt</th>
          <th style="padding:4px 8px;">Partner</th>
          <th style="padding:4px 8px;">Megnevezés</th>
          <th style="padding:4px 8px;text-align:right;">Összeg</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    '''


def build_daily_summary_email(open_invoices, today_iso):
    """open_invoices: a main() által számolt, még nyitott, határidős
    számlák listája. Visszaadja a (subject, html_body, n_overdue, n_today,
    n_upcoming) tuple-t."""
    overdue = sorted((i for i in open_invoices if i["hatarido"] < today_iso), key=lambda i: i["hatarido"])
    due_today = sorted((i for i in open_invoices if i["hatarido"] == today_iso), key=lambda i: (i.get("partner") or ""))
    upcoming = sorted((i for i in open_invoices if i["hatarido"] > today_iso), key=lambda i: i["hatarido"])

    # A "ma esedékes" csoport ugyanazt a világos, piros-narancsos kiemelést
    # kapja, mint a dashboardon (--overdue / --overdue-bg tokenek).
    body = f'''
    <div style="font-family:-apple-system,Segoe UI,sans-serif;color:#2a2013;max-width:760px;margin:0 auto;">
      <h2 style="margin:0 0 4px;font-size:19px;">Markez – napi számla-összesítő</h2>
      <p style="color:#8a7a63;font-size:13px;margin:0 0 14px;">{fmt_date_hu(today_iso)}</p>
      {_summary_table_html("Ma esedékes", due_today, "#c0392b", "Ma nincs esedékes számla.", row_bg="#fbe6e2", row_border="#c0392b")}
      {_summary_table_html("Lejárt", overdue, "#c0392b", "Nincs lejárt, nyitott számla.")}
      {_summary_table_html("Közelgő", upcoming, "#1f7a5c", "Nincs közelgő, nyitott számla.")}
    </div>
    '''
    subject = f"Markez – napi számla-összesítő – {len(due_today)} ma esedékes, {len(overdue)} lejárt"
    return subject, body, len(overdue), len(due_today), len(upcoming)


def send_daily_summary_email(open_invoices):
    smtp_server = os.environ.get("SMTP_SERVER")
    smtp_port = os.environ.get("SMTP_PORT") or "465"
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    email_to = os.environ.get("EMAIL_TO", "")
    recipients = [addr.strip() for addr in email_to.split(",") if addr.strip()]

    if not (smtp_server and smtp_user and smtp_password and recipients):
        print("FIGYELEM: napi összesítő email kihagyva — hiányzó SMTP/EMAIL_TO beállítás.")
        return

    now = budapest_now()
    today_iso = (now or datetime.utcnow()).date().isoformat()

    subject, html, n_overdue, n_today, n_upcoming = build_daily_summary_email(open_invoices, today_iso)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_server, int(smtp_port), context=context) as server:
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipients, msg.as_string())
        print(
            f"Napi összesítő email elküldve ide: {email_to} "
            f"({n_overdue} lejárt, {n_today} ma esedékes, {n_upcoming} közelgő)."
        )
    except Exception as exc:
        print(f"HIBA: nem sikerült elküldeni a napi összesítő emailt: {exc}")


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
    all_rows = collect_invoices(grids)
    all_invoices = [r for r in all_rows if r.get("szamla")]
    skipped = len(all_rows) - len(all_invoices)
    if skipped:
        print(f"{skipped} sor kimaradt, mert a \"Számla\" oszlop nincs bepipálva (árajánlat/szerződés/egyéb).")
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

    # 2b) Napi összesítő email — csak azon a futáson, amit erre a workflow
    # kifejezetten kijelöl (lásd SEND_DAILY_SUMMARY a workflow fájlban), és
    # csak akkor, ha a tényleges budapesti óra épp 6 — a másik, egy órával
    # eltolt cron (nyári/téli tartalék) ilyenkor némán kihagyja a küldést.
    # FORCE_DAILY_SUMMARY-vel (a kézi "Run workflow" teszt-kapcsolójával)
    # az óra-ellenőrzés megkerülhető, hogy bármikor tesztelhető legyen.
    force_summary = os.environ.get("FORCE_DAILY_SUMMARY", "").strip().lower() == "true"
    if os.environ.get("SEND_DAILY_SUMMARY", "").strip().lower() == "true":
        now = budapest_now()
        if force_summary or (now is not None and now.hour == 6):
            send_daily_summary_email(open_invoices)
        else:
            print(
                f"Napi email kihagyva — ez a nyári/téli tartalék cron futás "
                f"(a jelenlegi budapesti óra: {now.hour if now else 'ismeretlen'}, nem 6)."
            )

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
