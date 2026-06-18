#!/usr/bin/env python3
"""
Eurosport Labdarúgás -> RSS 2.0 feed generátor.

Adatforrás
----------
Az Eurosport (https://www.eurosport.hu/labdarugas/) egy Next.js (App Router)
alkalmazás. A cikkek strukturált adata a szerver által beágyazott
RSC (React Server Components) payloadban érkezik, az alábbi formában:

    self.__next_f.push([1,"<json-string-fragment>"])

A fragmentumok JSON-stringként vannak kódolva; összefűzve és dekódolva egy
normalizált objektum-gráfot kapunk, amelyben az `_type:"Article"` objektumok
tartalmazzák a címet, URL-t, publikálási időt és a kép-referenciákat
(`pictureFormatIds`). A képek `_type:"PictureFormat"` objektumokként vannak
jelen (url + width + height), így a legnagyobb felbontású verziót tudjuk
választani.

Robusztusság (a feladat szerinti sorrendben):
1. requests + a teljes böngésző-fejléckészlet (az Akamai 403-at ad enélkül).
2. Elsődlegesen a strukturált RSC JSON-ból olvassuk az adatokat.
3. Ha az RSC-parse nem ad cikket, BeautifulSoup-fallback a HTML kártyákra
   (`data-testid="card-title"` + a tartalmazó `<a href>`), og:image képpel.
4. A vizuális HTML helyett mindig a strukturált JSON-t részesítjük előnyben.
"""

from __future__ import annotations

import re
import sys
import time
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

# --- Konfiguráció ----------------------------------------------------------

SOURCE_URL = "https://www.eurosport.hu/labdarugas/"
OUTPUT_PATH = "docs/rss.xml"
MAX_ITEMS = 30

FEED_TITLE = "Eurosport Labdarúgás"
FEED_DESCRIPTION = "Latest football articles from Eurosport Hungary"
FEED_LANGUAGE = "hu-HU"

# Teljes böngésző-fejlécek – az Akamai WAF enélkül 403-at ad vissza.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Csak valódi cikkeket fogadunk el (story / video), live-meccsközvetítést nem.
ARTICLE_URL_RE = re.compile(r"/labdarugas/.*_(?:sto|vid)\d+/(?:story|video)\.shtml$")


# --- Letöltés --------------------------------------------------------------

def fetch_page(url: str, retries: int = 4) -> str:
    """
    Letöltés egy session-nel, exponenciális backoff-fal.

    Az oldalt Akamai Bot Manager védi: nagy kérésszám / gyanús IP esetén 403-at
    adhat. Egy óránkénti, alacsony volumenű kérés jellemzően átmegy; a retry a
    múló jellegű 403/5xx hibákat hidalja át. Először a főoldalt kérjük le, hogy
    megkapjuk a cookie-kat, majd same-origin Referer-rel a szekciót.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    last_status = None
    for attempt in range(retries):
        try:
            session.get("https://www.eurosport.hu/", timeout=30)
            resp = session.get(
                url,
                timeout=30,
                headers={
                    "Referer": "https://www.eurosport.hu/",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            last_status = resp.status_code
            if resp.status_code == 200 and resp.text:
                resp.encoding = "utf-8"
                return resp.text
        except requests.RequestException as exc:
            last_status = repr(exc)

        if attempt < retries - 1:
            time.sleep(2 ** attempt * 5)  # 5s, 10s, 20s

    raise RuntimeError(f"Nem sikerült letölteni az oldalt (utolsó állapot: {last_status})")


# --- 1. forrás: RSC payload (strukturált JSON) -----------------------------

def _decode_rsc(html: str) -> str:
    """A self.__next_f.push([1,"..."]) fragmentumok összefűzve, dekódolva."""
    fragments = re.findall(
        r'self\.__next_f\.push\(\[1,(".*?")\]\)', html, re.S
    )
    buf = []
    for frag in fragments:
        try:
            buf.append(json.loads(frag))  # JSON-string -> nyers szöveg
        except json.JSONDecodeError:
            continue
    return "".join(buf)


def _iter_json_objects(blob: str, start_token: str):
    """
    A normalizált RSC nem egyetlen érvényes JSON, ezért zárójel-párosítással
    vágjuk ki azokat az objektumokat, amelyek a start_token-t tartalmazzák.
    """
    idx = 0
    while True:
        hit = blob.find(start_token, idx)
        if hit == -1:
            return
        # Visszalépünk az objektum nyitó kapcsos zárójeléig.
        obj_start = blob.rfind("{", 0, hit)
        if obj_start == -1:
            idx = hit + len(start_token)
            continue
        depth = 0
        in_str = False
        esc = False
        end = None
        for i in range(obj_start, len(blob)):
            c = blob[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        if end is None:
            return
        yield blob[obj_start:end]
        idx = end


def _build_picture_map(blob: str) -> dict:
    """id -> (url, pixelszám) a PictureFormat objektumokból."""
    pics = {}
    for raw in _iter_json_objects(blob, '"_type":"PictureFormat"'):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        pid = obj.get("id")
        url = obj.get("url")
        if pid and url:
            w = obj.get("width") or 0
            h = obj.get("height") or 0
            pics[pid] = (url, int(w) * int(h))
    return pics


def parse_from_rsc(html: str) -> list[dict]:
    """Cikkek kinyerése a strukturált RSC payloadból."""
    blob = _decode_rsc(html)
    if not blob:
        return []

    pictures = _build_picture_map(blob)
    articles = []

    for raw in _iter_json_objects(blob, '"_type":"Article"'):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        url = obj.get("url")
        title = obj.get("title")
        if not url or not title or not ARTICLE_URL_RE.search(url):
            continue

        # Legnagyobb felbontású kép a referenciák közül.
        image = None
        best = -1
        for pid in obj.get("pictureFormatIds") or []:
            if pid in pictures:
                purl, pixels = pictures[pid]
                if pixels > best:
                    best, image = pixels, purl

        summary = obj.get("teaser") or obj.get("seoTeaser") or ""
        pub = obj.get("publicationTime") or obj.get("lastUpdatedTime")

        articles.append(
            {
                "title": title.strip(),
                "url": url.strip(),
                "image": image,
                "summary": summary.strip(),
                "published": _parse_iso(pub),
            }
        )

    return articles


# --- 2. forrás: HTML fallback ---------------------------------------------

def parse_from_html(html: str) -> list[dict]:
    """Tartalék: a látható HTML kártyákból olvasunk, ha az RSC nem elérhető."""
    soup = BeautifulSoup(html, "lxml")

    # Oldalszintű og:image tartalék kép.
    og = soup.find("meta", property="og:image")
    og_image = og["content"] if og and og.get("content") else None

    articles = []
    for title_el in soup.select('[data-testid="card-title"]'):
        anchor = title_el.find_parent("a", href=True)
        if not anchor:
            continue
        url = urljoin(SOURCE_URL, anchor["href"])
        if not ARTICLE_URL_RE.search(url):
            continue

        img = anchor.find("img")
        image = urljoin(SOURCE_URL, img["src"]) if img and img.get("src") else og_image

        articles.append(
            {
                "title": title_el.get_text(strip=True),
                "url": url,
                "image": image,
                "summary": "",
                "published": None,
            }
        )

    return articles


# --- Segédek ---------------------------------------------------------------

def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def dedupe(articles: list[dict]) -> list[dict]:
    """URL alapú deduplikáció, az első előfordulást megtartva."""
    seen = set()
    out = []
    for a in articles:
        if a["url"] in seen:
            continue
        seen.add(a["url"])
        out.append(a)
    return out


def clip_summary(text: str, lo: int = 150, hi: int = 300) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= hi:
        return text
    cut = text[:hi]
    # Szóhatáron vágunk, ha az nem túl korai.
    sp = cut.rfind(" ")
    if sp >= lo:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def guess_mime(url: str) -> str:
    u = url.lower()
    if ".png" in u:
        return "image/png"
    if ".webp" in u:
        return "image/webp"
    if ".gif" in u:
        return "image/gif"
    return "image/jpeg"


# --- RSS előállítás --------------------------------------------------------

def build_feed(articles: list[dict]) -> bytes:
    fg = FeedGenerator()
    fg.load_extension("media")  # media:content támogatás (Media RSS)

    fg.title(FEED_TITLE)
    fg.link(href=SOURCE_URL, rel="alternate")
    fg.description(FEED_DESCRIPTION)
    fg.language(FEED_LANGUAGE)
    fg.lastBuildDate(datetime.now(timezone.utc))
    fg.generator("eurosport-labdarugas-rss")

    # A feedgen az elemeket fordított sorrendben írja ki, ezért visszafelé adjuk hozzá.
    for a in reversed(articles):
        fe = fg.add_entry()
        fe.id(a["url"])
        fe.guid(a["url"], permalink=True)
        fe.title(a["title"])
        fe.link(href=a["url"])

        if a.get("published"):
            fe.pubDate(a["published"])

        summary = clip_summary(a.get("summary", ""))
        image = a.get("image")

        # HTML leírás: kép + összefoglaló, hogy az olvasók előnézetet mutassanak.
        desc_parts = []
        if image:
            desc_parts.append(f'<img src="{image}" />')
        if summary:
            desc_parts.append(f"<p>{summary}</p>")
        fe.description("\n".join(desc_parts) if desc_parts else a["title"])

        if image:
            mime = guess_mime(image)
            # enclosure (a length kötelező az RSS 2.0 szerint; 0-t adunk, mert ismeretlen)
            fe.enclosure(url=image, length="0", type=mime)
            # Media RSS – Feedly / NewsBlur / Inoreader előnézethez
            fe.media.content({"url": image, "medium": "image", "type": mime})

    return fg.rss_str(pretty=True)


# --- Belépési pont ---------------------------------------------------------

def main() -> int:
    html = fetch_page(SOURCE_URL)

    # Elsődlegesen a strukturált JSON-ból, tartalékként a HTML-ből.
    articles = parse_from_rsc(html)
    source = "RSC JSON"
    if not articles:
        articles = parse_from_html(html)
        source = "HTML fallback"

    articles = dedupe(articles)

    if not articles:
        print("HIBA: egyetlen cikket sem sikerült kinyerni.", file=sys.stderr)
        return 1

    # A legfrissebb 30 elem. Ahol nincs dátum, az a lista végére kerül.
    articles.sort(
        key=lambda a: a.get("published") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    articles = articles[:MAX_ITEMS]

    rss = build_feed(articles)
    with open(OUTPUT_PATH, "wb") as fh:
        fh.write(rss)

    print(f"OK: {len(articles)} cikk kiírva ({source}) -> {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
