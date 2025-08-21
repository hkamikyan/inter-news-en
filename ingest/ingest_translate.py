import os, json, time, hashlib, sys
from datetime import datetime, timezone
from typing import List, Dict
import feedparser
import requests

# --- CONFIG ---
FEED_URLS = [
    # Try the main RSS; if it 404s later, we'll add section feeds you find on the site.
    "https://www.fcinternews.it/rss",
    # Example: add more once you locate them:
    # "https://www.fcinternews.it/rss/mercato",
    # "https://www.fcinternews.it/rss/news",
]
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "site", "data")
OUTPUT_FILE = os.path.join(OUTPUT_PATH, "articles.json")

# Free public LibreTranslate endpoint (no key). You can change later.
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")
SLEEP_BETWEEN_CALLS = float(os.getenv("TRANSLATE_SLEEP", "0.8"))  # be polite

MAX_ITEMS_PER_FEED = int(os.getenv("MAX_ITEMS_PER_FEED", "50"))

def ensure_dirs():
    os.makedirs(OUTPUT_PATH, exist_ok=True)

def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def translate(text: str, source="it", target="en") -> str:
    if not text:
        return ""
    r = requests.post(
        LIBRETRANSLATE_URL,
        data={"q": text, "source": source, "target": target, "format": "text"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "translatedText" in data:
        return data["translatedText"]
    if isinstance(data, list) and data and "translatedText" in data[0]:
        return data[0]["translatedText"]
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

        title_en = translate(title_it)
        time.sleep(SLEEP_BETWEEN_CALLS)
        summary_en = translate(summary_it) if summary_it else ""
        time.sleep(SLEEP_BETWEEN_CALLS)

        item = {
            "id": md5(link or title_it + published),
            "feed": url,
            "url": link,
            "title_it": title_it,
            "title_en": title_en,
            "summary_it": summary_it,
            "summary_en": summary_en,
            "published": published,
        }
        items.append(item)
    return items

def main():
    ensure_dirs()
    all_items = []
    for url in FEED_URLS:
        try:
            items = fetch_feed(url)
            all_items.extend(items)
        except Exception as ex:
            print(f"[WARN] Failed feed {url}: {ex}", file=sys.stderr)

    seen = set()
    unique = []
    for it in sorted(all_items, key=lambda x: x["published"], reverse=True):
        key = it["url"] or it["id"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)

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
