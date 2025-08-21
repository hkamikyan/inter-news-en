import os, json, time, sys, re
from datetime import datetime, timezone
from typing import List, Dict, Tuple
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

# ------------ CONFIG ------------
LISTING_URLS: List[str] = [
    "https://www.fcinternews.it/",
    "https://m.fcinternews.it/",
    "https://www.fcinternews.it/news/",
    "https://www.fcinternews.it/mercato/",
    "https://www.fcinternews.it/in-primo-piano/",
    "https://www.fcinternews.it/focus/",
]

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
OUTPUT_FILE = os.path.join(OUTPUT_PATH, "articles.json")

LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")
SLEEP_BETWEEN_CALLS = float(os.getenv("TRANSLATE_SLEEP", "1.0"))
TIMEOUT = 30

# How many links to pull from listings (deduped), and how many to "enrich" by opening the article page
MAX_LINKS_FROM_LISTINGS = int(os.getenv("MAX_LINKS_FROM_LISTINGS", "80"))
MAX_ARTICLE_ENRICH = int(os.getenv("MAX_ARTICLE_ENRICH", "25"))

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; InterNewsFetcher/2.0; +https://github.com/your/repo)",
    "Accept-Language": "it,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# -------- URL filters ----------
# Accept articles that end with -<5+ digits> (observed article pattern)
ARTICLE_TAIL = re.compile(r"-\d{5,}/?$")
EXCLUDE_FIRST_SEGMENTS = {"web-tv", "sondaggi", "calendario_classifica", "tag", "topic", "categoria", "category", "gallery"}
ALLOW_FIRST_SEGMENTS = {"news", "mercato", "in-primo-piano", "focus"}  # allow top-level too

def ensure_dirs():
    os.makedirs(OUTPUT_PATH, exist_ok=True)

def http_get(url: str) -> str:
    r = requests.get(url, headers=UA_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def is_article_url(resolved_url: str) -> bool:
    p = urlparse(resolved_url)
    if not (p.scheme and p.netloc and p.netloc.endswith("fcinternews.it")):
        return False
    parts = [seg for seg in p.path.split("/") if seg]
    if not parts:
        return False
    if parts[0] in EXCLUDE_FIRST_SEGMENTS:
        return False
    # Either it’s under an allowed section OR it’s a top-level article slug,
    # BUT must end with -digits to avoid hubs like /in-primo-piano/
    if not ARTICLE_TAIL.search(p.path):
        return False
    if parts[0] in ALLOW_FIRST_SEGMENTS or len(parts) >= 1:
        return True
    return False

def translate(text: str, source="it", target="en") -> str:
    """Try LibreTranslate; if it fails, try MyMemory; else return original."""
    if not text:
        return ""
    try:
        r = requests.post(
            LIBRETRANSLATE_URL,
            data={"q": text, "source": source, "target": target, "format": "text"},
            timeout=TIMEOUT,
        )
        if r.ok:
            data = r.json()
            if isinstance(data, dict) and "translatedText" in data:
                return data["translatedText"]
            if isinstance(data, list) and data and "translatedText" in data[0]:
                return data[0]["translatedText"]
    except Exception:
        pass
    try:
        mm = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": f"{source}|{target}"},
            timeout=TIMEOUT,
        )
        if mm.ok:
            j = mm.json()
            t = j.get("responseData", {}).get("translatedText")
            if t:
                return t
    except Exception:
        pass
    return text

def collect_article_links(base_url: str, html: str, cap: int, seen: set) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    out: List[str] = []
    for a in soup.find_all("a", href=True):
        href_raw = a["href"].strip()
        if not href_raw:
            continue
        href = urljoin(base_url, href_raw)
        if href in seen:
            continue
        if is_article_url(href):
            seen.add(href)
            out.append(href)
            if len(out) >= cap:
                break
    return out

def extract_meta(article_html: str) -> Tuple[str, str, str]:
    """Return (title_it, teaser_it, published_iso) using OG tags with fallbacks."""
    soup = BeautifulSoup(article_html, "lxml")
    title = ""
    teaser = ""
    published_iso = ""

    ogt = soup.find("meta", property="og:title")
    if ogt and ogt.get("content"):
        title = ogt["content"].strip()

    ogd = soup.find("meta", property="og:description")
    if ogd and ogd.get("content"):
        teaser = ogd["content"].strip()

    # Try article:published_time or time tags
    ogtime = soup.find("meta", property="article:published_time")
    if ogtime and ogtime.get("content"):
        published_iso = ogtime["content"].strip()

    if not title:
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        else:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)

    # If no published time found, use now (UTC)
    if not published_iso:
        published_iso = datetime.now(timezone.utc).isoformat()

    # Trim teaser
    if teaser and len(teaser) > 300:
        teaser = teaser[:297] + "…"

    return title, teaser, published_iso

def main():
    ensure_dirs()

    # 1) Collect candidate article links from listings
    seen_urls = set()
    links: List[str] = []
    per_page_cap = max(10, MAX_LINKS_FROM_LISTINGS // max(1, len(LISTING_URLS)))

    for url in LISTING_URLS:
        try:
            html = http_get(url)
            links.extend(collect_article_links(url, html, cap=per_page_cap, seen=seen_urls))
        except Exception as ex:
            print(f"[WARN] Listing fetch failed {url}: {ex}", file=sys.stderr)

    # Deduplicate and clamp to overall max
    links = list(dict.fromkeys(links))[:MAX_LINKS_FROM_LISTINGS]

    # 2) Enrich a subset by fetching article pages (for clean title/summary/date)
    items: List[Dict] = []
    for href in links[:MAX_ARTICLE_ENRICH]:
        try:
            article_html = http_get(href)
        except Exception as ex:
            print(f"[WARN] Article fetch failed {href}: {ex}", file=sys.stderr)
            # Fallback minimal item if page fetch fails
            items.append({
                "id": hashlib_md5(href),
                "feed": "listing",
                "url": href,
                "title_it": "",
                "title_en": "",
                "summary_it": "",
                "summary_en": "",
                "published": datetime.now(timezone.utc).isoformat(),
            })
            continue

        title_it, teaser_it, published = extract_meta(article_html)
        title_en = translate(title_it)
        time.sleep(SLEEP_BETWEEN_CALLS)
        # If you want translated summaries too, uncomment the next two lines:
        # teaser_en = translate(teaser_it) if teaser_it else ""
        # time.sleep(SLEEP_BETWEEN_CALLS)
        teaser_en = ""  # keep empty for speed/stability

        items.append({
            "id": hashlib_md5(href),
            "feed": "article",
            "url": href,
            "title_it": title_it,
            "title_en": title_en,
            "summary_it": teaser_it,
            "summary_en": teaser_en,
            "published": published,
        })

    # 3) If we gathered fewer than requested, also include some *lightweight* items (title = URL slug)
    if len(items) < len(links):
        for href in links[len(items):]:
            slug = urlparse(href).path.rstrip("/").split("/")[-1].replace("-", " ").strip()
            title_it = slug.title() if slug else href
            items.append({
                "id": hashlib_md5(href),
                "feed": "listing",
                "url": href,
                "title_it": title_it,
                "title_en": translate(title_it),
                "summary_it": "",
                "summary_en": "",
                "published": datetime.now(timezone.utc).isoformat(),
            })
            time.sleep(SLEEP_BETWEEN_CALLS)

    # 4) Sort newest first (ISO timestamps sort fine lexicographically if always ISO8601)
    items.sort(key=lambda x: x["published"], reverse=True)

    payload = {
        "source": "FCInterNews (Italian)",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "articles": items,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[INFO] Wrote {len(items)} items to {OUTPUT_FILE}")

def hashlib_md5(s: str) -> str:
    import hashlib as _h
    return _h.md5(s.encode("utf-8")).hexdigest()

if __name__ == "__main__":
    main()
