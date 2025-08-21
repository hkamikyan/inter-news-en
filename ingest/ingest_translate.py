import os, json, time, hashlib, sys
from datetime import datetime, timezone
from typing import List, Dict
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

# ------------ CONFIG ------------
HOMEPAGE_URLS: List[str] = [
    "https://www.fcinternews.it/",
    "https://m.fcinternews.it/",
    "https://www.fcinternews.it/news/",
    "https://www.fcinternews.it/mercato/",
    "https://www.fcinternews.it/in-primo-piano/",
]

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "site", "data")
OUTPUT_FILE = os.path.join(OUTPUT_PATH, "articles.json")

LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")
SLEEP_BETWEEN_CALLS = float(os.getenv("TRANSLATE_SLEEP", "1.2"))
MAX_ITEMS_FROM_HTML = int(os.getenv("MAX_ITEMS_FROM_HTML", "30"))
TIMEOUT = 30

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; InterNewsFetcher/1.1; +https://github.com/your/repo)",
    "Accept-Language": "it,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

EXCLUDE_PATH_PARTS = {"tag", "topic", "categoria", "category", "video", "gallery"}

# ------------ HELPERS ------------
def ensure_dirs():
    os.makedirs(OUTPUT_PATH, exist_ok=True)

def md5(text: str) -> str:
    import hashlib as _h
    return _h.md5(text.encode("utf-8")).hexdigest()

def http_get(url: str) -> str:
    r = requests.get(url, headers=UA_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def is_fcinternews_article(url: str) -> bool:
    """Allow both absolute and relative urls; ensure domain is fcinternews.it and path isn't a tag/category."""
    p = urlparse(url)
    # Allow relative paths (no netloc)
    if not p.netloc:
        return True  # we'll join with base later
    host_ok = p.netloc.endswith("fcinternews.it")
    if not host_ok:
        return False
    parts = [seg for seg in p.path.split("/") if seg]
    if not parts:
        return False
    if any(seg in EXCLUDE_PATH_PARTS for seg in parts):
        return False
    # simple heuristic: likely article if path has at least 2 segments or a hyphen
    return ("-" in p.path) or (len(parts) >= 2)

def translate(text: str, source="it", target="en") -> str:
    """Try LibreTranslate; if it fails, try MyMemory; else return original."""
    if not text:
        return ""
    # 1) LibreTranslate
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
    # 2) MyMemory fallback
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
    # 3) Give up—return Italian so we still publish something
    return text

def collect_links_from_listing(base_url: str, html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    seen = set()
    items: List[Dict] = []

    for a in soup.find_all("a", href=True):
        href_raw = a["href"].strip()
        if not href_raw:
            continue

        # Make absolute
        href = urljoin(base_url, href_raw)

        # Filter to fcinternews and likely article pages
        if not is_fcinternews_article(href):
            continue
        p = urlparse(href)
        if not p.netloc.endswith("fcinternews.it"):
            continue

        # Use the anchor text as the title
        title_it = (a.get_text() or "").strip()
        if not title_it or len(title_it) < 6:  # skip very short labels
            continue

        # Dedupe by URL
        if href in seen:
            continue
        seen.add(href)

        items.append({"url": href, "title_it": title_it})

        if len(items) >= MAX_ITEMS_FROM_HTML:
            break

    return items

# ------------ MAIN ------------
def main():
    ensure_dirs()
    collected: List[Dict] = []

    for url in HOMEPAGE_URLS:
        try:
            html = http_get(url)
        except Exception as ex:
            print(f"[WARN] Listing fetch failed {url}: {ex}", file=sys.stderr)
            continue
        links = collect_links_from_listing(url, html)
        if links:
            collected.extend(links)

    # Dedupe across pages by URL, keep first occurrence
    unique_by_url = {}
    for it in collected:
        unique_by_url.setdefault(it["url"], it)
    unique = list(unique_by_url.values())

    # Translate TITLES ONLY (fast + robust)
    out_items: List[Dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for it in unique:
        try:
            title_en = translate(it["title_it"])
            time.sleep(SLEEP_BETWEEN_CALLS)
        except Exception:
            title_en = it["title_it"]
        out_items.append({
            "id": md5(it["url"]),
            "feed": "listing",
            "url": it["url"],
            "title_it": it["title_it"],
            "title_en": title_en,
            "summary_it": "",
            "summary_en": "",
            "published": now_iso,
        })

    payload = {
        "source": "FCInterNews (Italian)",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(out_items),
        "articles": out_items,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[INFO] Wrote {len(out_items)} items to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
