# Eurosport Labdarúgás – RSS feed

Ingyenes, óránként frissülő RSS 2.0 feed az [Eurosport Hungary labdarúgás
rovatából](https://www.eurosport.hu/labdarugas/). GitHub Actions generálja,
GitHub Pages szolgálja ki – **havi $0** költséggel.

A feed minden cikkhez tartalmaz címet, linket, publikálási dátumot, kiemelt
képet (`<enclosure>` + `media:content` + kép a leírásban) és rövid
összefoglalót. Optimalizálva **Inoreader**, **Feedly** és **NewsBlur**
megjelenítéshez.

## Feed URL

```
https://magi12345.github.io/eurosport-foci-rss/rss.xml
```

## Hogyan működik

A `scripts/generate_feed.py` letölti a labdarúgás oldalt, és elsődlegesen a
beágyazott **Next.js RSC JSON** payloadból olvassa ki a cikkeket (cím, URL,
publikálási idő, összefoglaló, kiemelt kép a legnagyobb felbontásban). Ha a
strukturált JSON nem elérhető, automatikusan a látható HTML kártyákra
(`data-testid="card-title"`) vált, og:image képpel. A script deduplikál, a
30 legfrissebb cikket tartja meg, és érvényes RSS 2.0 feedet ír a
`docs/rss.xml` fájlba.

## Telepítés / beüzemelés

1. **Forkold vagy másold** ezt a repót a saját GitHub fiókodba.
2. Engedélyezd a GitHub Actions írási jogát:
   *Settings → Actions → General → Workflow permissions* →
   **Read and write permissions**.
3. Indítsd el manuálisan az első futtatást:
   *Actions → „Update RSS feed" → Run workflow*.
   Ez legenerálja a `docs/rss.xml`-t és commitolja.

A workflow ezután **óránként** automatikusan fut
(`.github/workflows/update-feed.yml`), és csak akkor commitol, ha a feed
ténylegesen változott.

### Helyi futtatás (opcionális)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/generate_feed.py
# eredmény: docs/rss.xml
```

## GitHub Pages aktiválása

1. *Settings → Pages*.
2. **Source:** „Deploy from a branch".
3. **Branch:** `main`, **mappa:** `/docs`.
4. Mentés után pár perccel elérhető lesz a feed:
   `https://magi12345.github.io/eurosport-foci-rss/rss.xml`

## Feliratkozás Inoreaderben

1. Lépj be az [Inoreaderbe](https://www.inoreader.com/).
2. Bal felül **+ Add (Hozzáadás)** → **Subscribe / Feed hozzáadása**.
3. Illeszd be a feed URL-t:
   `https://magi12345.github.io/eurosport-foci-rss/rss.xml`
4. **Subscribe.** A cikkek képes előnézettel jelennek meg a lista- és
   olvasónézetben is.

Ugyanez a URL működik Feedlyben és NewsBlurben is.

## Megjegyzés (Akamai)

Az Eurosport oldalt Akamai Bot Manager védi, amely a sima `requests`
TLS-ujjlenyomatát (és így a GitHub Actions kéréseit) 403-mal blokkolja. Ezért
a letöltés a **`curl_cffi`** csomaggal történik, amely valódi Chrome
TLS/HTTP2-ujjlenyomatot imitál – így az Akamai böngészőnek látja a kérést. A
`requests` tartalékként marad, és retry/backoff logika hidalja át a múló
hibákat.

## Repó-struktúra

```
.
├── .github/workflows/update-feed.yml   # óránkénti GitHub Actions
├── docs/rss.xml                         # a generált feed (GitHub Pages)
├── scripts/generate_feed.py             # scraper + RSS generátor
├── requirements.txt
└── README.md
```
