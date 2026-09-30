# Markez – számla-szinkronizáció

Ez a repó óránként két irányban szinkronizál a Markez **"Iktatás 2026"**
Google Sheets táblázata és a markez.hu-n futó dashboard között, és naponta
egyszer egy összesítő emailt is küld a nyitott számlákról.

1. beolvassa a táblázatot, kigyűjti belőle a még ki nem fizetett, ismert
   határidejű számlákat, és beküldi őket a WordPress REST végpontjára;
2. visszaírja a Sheetbe azokat a számlákat, amiket valaki a dashboardon
   fizetettnek jelölt (a "fizetve" checkboxszal) — ezért a service accountnak
   most már **Szerkesztő** jogosultság kell a táblázaton, nem csak Megtekintő;
3. beküldi az ÖSSZES számlát (fizetettet is, határidő nélkülit is) egy
   karcsúbb listában — ez a dashboard "Pénzügyi összesítő" paneljéhez kell,
   ami a kártyán lévő projektekhez rendeli hozzá a hozzájuk tartozó
   kiadásokat a számla fül-neve vagy Megjegyzés-mezője alapján.

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

## A "fizetve" jelölés visszaírása (checkbox a dashboardon)

Amikor valaki a dashboardon bepipálja egy számla "fizetve" jelölőnégyzetét,
az a WordPress-en egy "szinkronra vár" listára kerül (a felületen halványan,
kipipálva, de nem tűnik el) — nem íródik azonnal a Sheetbe. A következő órás
futáskor a script:

1. lekéri a WordPress-től a szinkronra váró tételeket (`/pending-paid`),
2. beírja a táblázat megfelelő sorába, hogy `Fizetve = TRUE`,
3. jelzi a WordPress-nek, hogy ezek már rendben vannak (`/clear-paid`) — ekkor
   tűnik csak el ténylegesen a tétel a dashboard listájáról, mert a normál
   szinkron (lásd fent) ettől kezdve már nem küldi be újra nyitottként.

Ha a Sheet-be írás sikertelen (pl. WAF-probléma), a tétel a következő órában
újra megpróbálja — addig halványan, kipipálva marad a felületen.

## Napi számla-összesítő email

Minden nap 06:12-kor (magyar idő) a script egy összesítő emailt küld a
`szamla@markez.hu` postafiókból (a markez.hu tárhelyének saját SMTP
szerverén, `we005.tarhely.com:465`, SSL-lel — nem Gmailen keresztül) az
`EMAIL_TO` secretben megadott címzett(ek)nek. A levél három csoportba
rendezve mutatja a még ki nem fizetett, határidővel rendelkező számlákat:

- **Lejárt** — a határidejük már elmúlt,
- **Ma esedékes** — pontosan aznap jár le a határidejük,
- **Közelgő** — minden további nyitott, határidős számla (nincs felső korlát).

A küldés az óránkénti szinkrontól függetlenül, külön ütemezésen fut (lásd a
workflow fájl `schedule` részét). Mivel a GitHub Actions cron UTC-ben fut és
nem ismeri a nyári/téli időszámítás-váltást, két, egy órával eltolt cron van
beállítva (`12 5 * * *` és `12 4 * * *`); a script a tényleges budapesti óra
alapján (a `zoneinfo` modullal) dönti el, hogy melyik a "helyes" — csak az
küld emailt, aminél épp 6 az aktuális magyar óra, a másik némán kihagyja.
Ha az SMTP-adatok vagy az `EMAIL_TO` secret hiányzik, a script ezt csak
naplózza és a normál szinkron egyébként zavartalanul lefut.

## Beállítandó GitHub Secrets

*Settings → Secrets and variables → Actions → New repository secret*

| Név | Érték |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_KEY` | A `markez-dashboard-sync` service account letöltött JSON kulcsfájljának **teljes tartalma** (nyisd meg szövegszerkesztővel, másold be mindet) |
| `GOOGLE_SHEET_ID` | `1bhLmreoJ-2QaFXSoU5v6e1z2lC10o8opjVX1Y36-bcE` (az "Iktatás 2026" Sheets URL-jéből) |
| `WP_SYNC_URL` | A WordPress REST végpont teljes URL-je, pl. `https://markez.hu/wp-json/markez/v1/invoices` |
| `WP_SYNC_API_KEY` | A WPCode snippetben beállított titkos kulcs — ezt akkor kapod meg, amikor a WordPress-oldali snippet elkészül |
| `SMTP_PASSWORD` | A `szamla@markez.hu` postafiók jelszava (az mhosting.hu vezérlőpultjáról) |
| `EMAIL_TO` | A napi összesítő címzett email-címe(i), több cím esetén vesszővel elválasztva |

A service accountnak (`markez-dashboard-sync@markez-dashboard.iam.gserviceaccount.com`)
**Szerkesztő** joggal meg kell lennie osztva az "Iktatás 2026" táblázaton
(mivel a script már ír is vissza, nem csak olvas) — ez már megtörtént.

Az SMTP szerver címe (`we005.tarhely.com`), portja (`465`) és a feladó
címe (`szamla@markez.hu`) a workflow fájlban van rögzítve — ezek nem
titkosak, nem kell hozzájuk secret.

## Tesztelés

Miután a secretek be vannak állítva: *Actions* fül → *Számlák szinkronizálása*
workflow → *Run workflow* — ezzel manuálisan is elindítható, nem kell megvárni
a következő órás futást.

A *Run workflow* gomb megnyomásakor megjelenik egy **"Napi összesítő
teszt-email küldése is ennél a futásnál"** jelölőnégyzet — ha ezt bepipálod,
a szokásos szinkron mellett azonnal, a napszaktól függetlenül kimegy a napi
összesítő email is az `EMAIL_TO` címzett(ek)nek, így nem kell megvárni a
következő 06:12-t ahhoz, hogy lásd, hogyan néz ki a levél.

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
