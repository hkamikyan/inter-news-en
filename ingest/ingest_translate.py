import os, json, time, sys, re
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Try to import trafilatura for robust article extraction.
# If unavailable, we'll fall back to BeautifulSoup heuristics.
try:
    import trafilatura  # type: ignore
    HAS_TRAFILATURA = True
except Exception:
    HAS_TRAFILATURA = False

# ==============================
# CONFIG (tweak via env if needed)
# ==============================
LISTING_URLS: List[str] = [
    "https://www.fcinternews.it/",
    "https://m.fcinternews.it/",
    "https://www.fcinternews.it/news/",
    "https://www.fcinternews.it/mercato/",
    "https://www.fcinternews.it/in-primo-piano/",
    "https://www.fcinternews.it/focus/",
]

# Output lives in /docs so GitHub Pages can serve it
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
OUTPUT_FILE = os.path.join(OUTPUT_PATH, "articles.json")
POSTS_DIR   = os.path.join(os.path.dirname(__file__), "..", "docs", "posts")

LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com/translate")
SLEEP_BETWEEN_CALLS = float(os.getenv("TRANSLATE_SLEEP", "1.0"))
TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))

# How many links to consider vs. fully enrich (open article page)
MAX_LINKS_FROM_LISTINGS = int(os.getenv("MAX_LINKS_FROM_LISTINGS", "50"))
MAX_ARTICLE_ENRICH      = int(os.getenv("MAX_ARTICLE_ENRICH", "25"))  # we'll translate full text for these

# Translation chunk size (avoid API payload limits)
TRANSLATE_CHARS_PER_CHUNK = int(os.getenv("TRANSLATE_CHARS_PER_CHUNK", "900"))

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; InterNewsFetcher/3.0; +https://github.com/your/repo)",
    "Accept-Language": "it,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# -------- URL filters ----------
ARTICLE_TAIL = re.compile(r"-\d{5,}/?$")  # ends with -<id>
EXCLUDE_FIRST_SEGMENTS = {"web-tv", "sondaggi", "calendario_classifica", "tag", "topic", "categoria", "category", "gallery"}
ALLOW_FIRST_SEGMENTS   = {"news", "mercato", "in-primo-piano", "focus"}  # common sections

# ==============================
# HELPERS
# ==============================
def ensure_dirs():
    os.makedirs(OUTPUT_PATH, exist_ok=True)
    os.makedirs(POSTS_DIR,   exist_ok=True)

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
    if not ARTICLE_TAIL.search(p.path):
        return False
    if parts[0] in ALLOW_FIRST_SEGMENTS or len(parts) >= 1:
        return True
    return False

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

    # prefer og:description, else meta[name=description], else first paragraph
    ogd = soup.find("meta", property="og:description")
    if ogd and ogd.get("content"):
        teaser = ogd["content"].strip()
    else:
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            teaser = md["content"].strip()
        else:
            main = soup.find("article") or soup.select_one(".article, .post, .entry-content, .content, .news-content")
            if main:
                p = main.find("p")
                if p:
                    teaser = p.get_text(" ", strip=True)

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

    if not published_iso:
        published_iso = datetime.now(timezone.utc).isoformat()

    if teaser and len(teaser) > 300:
        teaser = teaser[:297] + "…"

    return title, teaser, published_iso

def extract_fulltext(article_html: str) -> str:
    """Use trafilatura if available; otherwise heuristic extraction."""
    if HAS_TRAFILATURA:
        try:
            txt = trafilatura.extract(article_html, include_comments=False, include_tables=False) or ""
            return txt.strip()
        except Exception:
            pass
    # Heuristic fallback
    soup = BeautifulSoup(article_html, "lxml")
    main = soup.find("article") or soup.select_one(".article, .post, .entry-content, .content, .news-content, .content-article")
    if not main:
        main = soup.body
    if not main:
        return ""
    # collect paragraphs
    paras = []
    for p in main.find_all(["p", "h2", "h3", "li"]):
        text = p.get_text(" ", strip=True)
        if len(text) >= 5:
            paras.append(text)
    # remove very short/boilerplate tails
    if paras and len(paras[-1]) < 20:
        paras = paras[:-1]
    return "\n\n".join(paras).strip()

def hashlib_md5(s: str) -> str:
    import hashlib as _h
    return _h.md5(s.encode("utf-8")).hexdigest()

def translate_once(text: str, source="it", target="en") -> str:
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
    # 3) Give up—return original
    return text

def translate_chunked(long_text: str, source="it", target="en", chunk_chars=TRANSLATE_CHARS_PER_CHUNK) -> str:
    """Split long text into chunks to avoid API limits, translate piecewise, then join."""
    if not long_text:
        return ""
    chunks = []
    current = []
    current_len = 0
    for para in long_text.split("\n\n"):
        p = para.strip()
        if not p:
            continue
        if current_len + len(p) + 2 <= chunk_chars:
            current.append(p)
            current_len += len(p) + 2
        else:
            chunks.append("\n\n".join(current))
            current = [p]
            current_len = len(p)
    if current:
        chunks.append("\n\n".join(current))

    out_parts = []
    for c in chunks:
        out_parts.append(translate_once(c, source=source, target=target))
        time.sleep(SLEEP_BETWEEN_CALLS)
    return "\n\n".join(out_parts)

# Optional: make English titles look nicer
ACRONYMS = {"psg", "uefa", "fifa", "var", "usa", "uk", "napoli", "milan", "roma", "inter"}
def nice_en_title(s: str) -> str:
    if not s:
        return s
    s = re.sub(r"\s+", " ", s).strip()
    words = s.split(" ")
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in ACRONYMS:
            out.append(lw.upper())
        elif i == 0:
            out.append(w[:1].upper() + w[1:])
        else:
            out.append(w[:1].upper() + w[1:] if len(w) > 3 else lw)
    s2 = " ".join(out).replace(" - ", " — ")
    return s2

def html_paragraphs(text: str) -> str:
    """Turn plain text with blank-line paragraphs into <p> blocks."""
    if not text:
        return ""
    parts = [f"<p>{x}</p>" for x in text.split("\n\n") if x.strip()]
    return "\n    ".join(parts)

def render_post_html(
    title_en: str,
    title_it: str,
    teaser_en: str,
    teaser_it: str,
    full_en: str,
    full_it: str,
    source_url: str,
    published_iso: str
) -> str:
    # Simple static page. We link back to source for full text reference.
    full_en_html = html_paragraphs(full_en) if full_en else ""
    full_it_html = html_paragraphs(full_it) if full_it else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title_en}</title>
  <link rel="stylesheet" href="../styles.css"/>
  <style>
    .btn {{
      display:inline-block; padding:10px 14px; border:1px solid #2a3240; border-radius:8px;
      background:#0e1218; color:#87b4ff; text-decoration:none; font-weight:600;
    }}
    .src-note {{ color:#9fb0c5; font-size:0.9rem; margin-top: 8px; }}
    .date {{ color:#9fb0c5; font-size:.95rem; margin:6px 0 14px; }}
    .muted {{ color:#9fb0c5; }}
  </style>
</head>
<body>
  <main style="max-width: 920px; margin: 24px auto; padding: 0 16px;">
    <h1 style="margin-bottom:4px;">{title_en}</h1>
    <p class="date">Published: {published_iso}</p>

    {"<p>"+teaser_en+"</p>" if teaser_en else ""}

    {"<h3>Article</h3>" if full_en_html else ""}
    {full_en_html}

    <p class="src-note">Source (Italian): <a href="{source_url}" target="_blank" rel="noopener noreferrer">{source_url}</a></p>
    <p><a class="btn" href="{source_url}" target="_blank" rel="noopener noreferrer">Read original on FCInterNews</a></p>

    <hr style="border:0;border-top:1px solid #1f2630;margin:24px 0;">
    <details>
      <summary>Show original (Italian) teaser/body</summary>
      {"<p>"+teaser_it+"</p>" if teaser_it else "<p class='muted'>(no teaser available)</p>"}
      {("<h4>Testo originale</h4>"+full_it_html) if full_it_html else ""}
    </details>
  </main>
</body>
</html>"""

# ==============================
# MAIN
# ==============================
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

    # Deduplicate and clamp
    links = list(dict.fromkeys(links))[:MAX_LINKS_FROM_LISTINGS]

    items: List[Dict] = []

    # 2) Enrich a subset by fetching article pages (OG meta + full text)
    for href in links[:MAX_ARTICLE_ENRICH]:
        try:
            article_html = http_get(href)
            title_it, teaser_it, published = extract_meta(article_html)
            full_it = extract_fulltext(article_html)
        except Exception as ex:
            print(f"[WARN] Article fetch failed {href}: {ex}", file=sys.stderr)
            title_it, teaser_it, published, full_it = "", "", datetime.now(timezone.utc).isoformat(), ""

        # translate + tidy
        title_en = nice_en_title(translate_once(title_it))
        time.sleep(SLEEP_BETWEEN_CALLS)

        teaser_en = translate_once(teaser_it) if teaser_it else ""
        if teaser_it:
            time.sleep(SLEEP_BETWEEN_CALLS)

        full_en = translate_chunked(full_it) if full_it else ""
        # no extra sleep: translate_chunked already sleeps per chunk

        post_id = hashlib_md5(href)
        post_path = os.path.join(POSTS_DIR, f"{post_id}.html")
        with open(post_path, "w", encoding="utf-8") as f:
            f.write(render_post_html(title_en, title_it, teaser_en, teaser_it, full_en, full_it, href, published))

        items.append({
            "id": post_id,
            "feed": "article",
            "url": href,                           # original (Italian)
            "local_url": f"posts/{post_id}.html",  # your page
            "title_it": title_it,
            "title_en": title_en,
            "summary_it": teaser_it,
            "summary_en": teaser_en,
            "published": published,
        })

    # 3) For remaining links (not enriched), create minimal local pages with just titles
    now_iso = datetime.now(timezone.utc).isoformat()
    for href in links[MAX_ARTICLE_ENRICH:]:
        slug = urlparse(href).path.rstrip("/").split("/")[-1].replace("-", " ").strip()
        title_it = slug if slug else href
        title_en = nice_en_title(translate_once(title_it))
        time.sleep(SLEEP_BETWEEN_CALLS)

        # No full text for these lightweight pages
        post_id = hashlib_md5(href)
        post_path = os.path.join(POSTS_DIR, f"{post_id}.html")
        with open(post_path, "w", encoding="utf-8") as f:
            f.write(render_post_html(title_en, title_it, "", "", "", "", href, now_iso))

        items.append({
            "id": post_id,
            "feed": "listing",
            "url": href,
            "local_url": f"posts/{post_id}.html",
            "title_it": title_it,
            "title_en": title_en,
            "summary_it": "",
            "summary_en": "",
            "published": now_iso,
        })

    # 4) Sort newest first and write payload
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

if __name__ == "__main__":
    main()
