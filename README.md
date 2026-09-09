# Markez – számla-szinkronizáció

Ez a repó egyetlen dolgot csinál: óránként beolvassa a Markez **"Iktatás 2026"**
Google Sheets táblázatát, kigyűjti belőle a még ki nem fizetett, ismert
határidejű számlákat, és beküldi őket a markez.hu-n futó dashboard WordPress
REST végpontjára.

A dashboard front-end (projektek, feladatok, kontaktok, fotók) és a fogadó
REST végpont maga egy külön WPCode snippetben él a WordPress oldalon — az nem
ebben a repóban van, mert ott fut.

## Hogyan működik

`sync_invoices.py` végigmegy a táblázat összes fülén, megkeresi azokat a
sorokat, amik a `Iktatószám / Fizetési határidő / Fizetve` oszlopfejléceket
tartalmazzák, és az alattuk lévő adat-sorokat olvassa ki addig, amíg üres
Iktatószámba nem fut. Ez azért van így, nem fül- vagy oszlop-pozícióra
hardkódolva, hogy ha új projekt-fül kerül a táblázatba, a script módosítás
nélkül felismerje.

Csak azok a tételek kerülnek beküldésre, ahol `Fizetve = FALSE` és van
kitöltött `Fizetési határidő` — ez felel meg a dashboard lejárt/ma
esedékes/közelgő számla-listájának.

## Beállítandó GitHub Secrets

*Settings → Secrets and variables → Actions → New repository secret*

| Név | Érték |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_KEY` | A `markez-dashboard-sync` service account letöltött JSON kulcsfájljának **teljes tartalma** (nyisd meg szövegszerkesztővel, másold be mindet) |
| `GOOGLE_SHEET_ID` | `1bhLmreoJ-2QaFXSoU5v6e1z2lC10o8opjVX1Y36-bcE` (az "Iktatás 2026" Sheets URL-jéből) |
| `WP_SYNC_URL` | A WordPress REST végpont teljes URL-je, pl. `https://markez.hu/wp-json/markez/v1/invoices` |
| `WP_SYNC_API_KEY` | A WPCode snippetben beállított titkos kulcs — ezt akkor kapod meg, amikor a WordPress-oldali snippet elkészül |

A service accountnak (`markez-dashboard-sync@markez-dashboard.iam.gserviceaccount.com`)
**Megtekintő** joggal meg kell lennie osztva az "Iktatás 2026" táblázaton — ez
már megtörtént.

## Tesztelés

Miután a secretek be vannak állítva: *Actions* fül → *Számlák szinkronizálása*
workflow → *Run workflow* — ezzel manuálisan is elindítható, nem kell megvárni
a következő órás futást.

## Ismert kockázat: mhosting.hu (korábban tárhely.eu) WAF

A tárhelyen futó Imunify360 WAF korábban (a hetedhétország-projektnél) csendben
blokkolta a GitHub Actionsből érkező REST-hívásokat: HTTP 200-at adott vissza,
de a válasz tartalma "Access Denied" volt, nem a tényleges API-válasz. A script
ezért:

- valódi böngésző User-Agent-et küld a kérésben,
- és validálja a válasz *tartalmát* (JSON, benne `updated` mező), nem csak a
  HTTP-státuszkódot.

Ha a szinkronizálás mégis "HIBA: a válasz nem érvényes JSON" üzenettel hal el,
az szinte biztosan ez a WAF-probléma — ekkor a `/wp-json/markez/v1/invoices`
útvonalra kell egy kivételt kérni az mhosting.hu ügyfélszolgálattól.
