import os, json, time, hashlib, sys, re
from datetime import datetime, timezone
from typing import List, Dict
import feedparser
import requests
from bs4 import BeautifulSoup

# --- CONFIG ---
FEED_URLS = [
     #"https://www.fcinternews.it/rss/"
]

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
SLEEP_BETWEEN_CALLS = float(os.getenv("TRANSLATE_SLEEP", "0.8"))
MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "50"))
MAX_ITEMS_FROM_HTML = int(os.getenv("MAX_ITEMS_FROM_HTML", "30"))

A_PATTERN = re.compile(r"^https://www\.fcinternews\.it/.+?-\d{5,}$")

def ensure_dirs():
    os.makedirs(OUTPUT_PATH, exist_ok=True)

def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def translate(text: str, source="it", target="en") -> str:
    """Try LibreTranslate; on any error, fall back to MyMemory; else return original text."""
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
        # If 4xx/5xx or unexpected payload, fall through to MyMemory
    except Exception:
        pass

    # Fallback: MyMemory (free, no key). Rate limits are loose; we only translate titles.
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

    # Last resort: return the original Italian so we still publish something.
    return text


def fetch_feed(url: str) -> List[Dict]:
    feed = feedparser.parse(url)
    items = []
    for e in feed.entries[:MAX_ITEMS_PER_FEED]:
        link = getattr(e, "link", "")
        title_it = getattr(e, "title", "") or ""
        summary_it = getattr(e, "summary", "") or getattr(e, "description", "") or ""
        published = getattr(e, "published", "") or getattr(e, "updated", "")
        if not published:
            published = datetime.now(timezone.utc).isoformat()

        title_en = translate(title_it); time.sleep(SLEEP_BETWEEN_CALLS)
        summary_en = translate(summary_it) if summary_it else ""; time.sleep(SLEEP_BETWEEN_CALLS)

        items.append({
            "id": md5(link or title_it + published),
            "feed": url,
            "url": link,
            "title_it": title_it,
            "title_en": title_en,
            "summary_it": summary_it,
            "summary_en": summary_en,
            "published": published,
        })
    return items

def fetch_html(url: str) -> List[Dict]:
    out = []
    html = requests.get(url, timeout=30).text
    soup = BeautifulSoup(html, "lxml")

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if A_PATTERN.match(href):
            title = (a.get_text() or "").strip()
            if title and (href, title) not in links:
                links.append((href, title))

    for href, title in links[:MAX_ITEMS_FROM_HTML]:
        teaser = ""
        a = soup.find("a", href=href)
        parent = a.parent if a else None
        if parent:
            sibling_texts = []
            for sib in list(parent.next_siblings)[:2] + list(parent.previous_siblings)[:2]:
                if getattr(sib, "name", "") in ("p", "span", "div"):
                    txt = (sib.get_text() or "").strip()
                    if 30 <= len(txt) <= 240:
                        sibling_texts.append(txt)
            if sibling_texts:
                teaser = sibling_texts[0]

        published = datetime.now(timezone.utc).isoformat()
        title_en = translate(title); time.sleep(SLEEP_BETWEEN_CALLS)
        teaser_en = translate(teaser) if teaser else ""; time.sleep(SLEEP_BETWEEN_CALLS)

        out.append({
            "id": md5(href),
            "feed": url,
            "url": href,
            "title_it": title,
            "title_en": title_en,
            "summary_it": teaser,
            "summary_en": teaser_en,
            "published": published,
        })
    return out

def main():
    ensure_dirs()
    all_items = []



    # 0) HTML first
    for url in HOMEPAGE_URLS:
         all_items.extend(fetch_html(url))


    # 1) Try RSS feeds (if any)
    for url in FEED_URLS:
        try:
            all_items.extend(fetch_feed(url))
        except Exception as ex:
            print(f"[WARN] RSS failed {url}: {ex}", file=sys.stderr)

    # 2) Fallback to HTML if nothing from RSS
    if not all_items:
        for url in HOMEPAGE_URLS:
            try:
                all_items.extend(fetch_html(url))
            except Exception as ex:
                print(f"[WARN] HTML failed {url}: {ex}", file=sys.stderr)

    # dedupe by URL
    seen = set(); unique = []
    for it in sorted(all_items, key=lambda x: x["published"], reverse=True):
        key = it["url"] or it["id"]
        if key in seen: continue
        seen.add(key); unique.append(it)

    payload = {
        "source": "FCInterNews (Italian)",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(unique),
        "articles": unique,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(unique)} items to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
