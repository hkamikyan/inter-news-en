import os, json, time, sys, re
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Try to import trafilatura for robust article extraction (optional)
try:
    import trafilatura  # type: ignore
    HAS_TRAFILATURA = True
except Exception:
    HAS_TRAFILATURA = False

# ==============================
# CONFIG
# ==============================
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
POSTS_DIR   = os.path.join(os.path.dirname(__file__), "..", "docs", "posts")

#LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.de/translate")
#USE_LIBRETRANSLATE = os.getenv("USE_LIBRETRANSLATE", "1") == "1"

LIBRETRANSLATE_URLS = [u.strip() for u in os.getenv("LIBRETRANSLATE_URLS", "").split(",") if u.strip()]
USE_LIBRETRANSLATE = os.getenv("USE_LIBRETRANSLATE", "1") == "1"


SLEEP_BETWEEN_CALLS = float(os.getenv("TRANSLATE_SLEEP", "1.0"))
TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))

MAX_LINKS_FROM_LISTINGS = int(os.getenv("MAX_LINKS_FROM_LISTINGS", "50"))
MAX_ARTICLE_ENRICH      = int(os.getenv("MAX_ARTICLE_ENRICH", "25"))
TRANSLATE_CHARS_PER_CHUNK = int(os.getenv("TRANSLATE_CHARS_PER_CHUNK", "420"))  # conservative

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; InterNewsFetcher/3.1; +https://github.com/your/repo)",
    "Accept-Language": "it,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

ARTICLE_TAIL = re.compile(r"-\d{5,}/?$")
EXCLUDE_FIRST_SEGMENTS = {"web-tv", "sondaggi", "calendario_classifica", "tag", "topic", "categoria", "category", "gallery"}
ALLOW_FIRST_SEGMENTS   = {"news", "mercato", "in-primo-piano", "focus"}

# ==============================
# HELPERS
# ==============================


def dbg(msg: str):
    print(f"[DBG] {msg}", file=sys.stderr)

def ensure_dirs():
    os.makedirs(OUTPUT_PATH, exist_ok=True)
    os.makedirs(POSTS_DIR,   exist_ok=True)

def http_get(url: str) -> str:
    r = requests.get(url, headers=UA_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

CACHE_PATH = os.path.join(os.path.dirname(__file__), ".trans_cache.json")
_translate_cache = None

def _load_cache():
    global _translate_cache
    if _translate_cache is not None:
        return _translate_cache
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            _translate_cache = json.load(f)
    except Exception:
        _translate_cache = {}
    return _translate_cache

def _save_cache():
    try:
        if _translate_cache is not None:
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(_translate_cache, f, ensure_ascii=False)
    except Exception as e:
        dbg(f"[cache] save error: {e}")

def _cache_key(text: str, src: str, tgt: str) -> str:
    import hashlib
    h = hashlib.sha1()
    h.update((src+"|"+tgt+"|"+text).encode("utf-8"))
    return h.hexdigest()

def translate_cached(text: str, source="it", target="en") -> str:
    if not text:
        return ""
    c = _load_cache()
    key = _cache_key(text, source, target)
    if key in c:
        return c[key]
    out = translate_once(text, source=source, target=target)
    # Only cache if it looks translated (not basically identical)
    if out and not _looks_unchanged(text, out):
        c[key] = out
        _save_cache()
    return out



def _lt_post(text: str, source="it", target="en") -> Optional[str]:
    """Try each LT endpoint; return translated text or None."""
    if not (USE_LIBRETRANSLATE and LIBRETRANSLATE_URLS):
        return None
    for url in LIBRETRANSLATE_URLS:
        try:
            r = requests.post(
                url,
                data={"q": text, "source": source, "target": target, "format": "text"},
                timeout=TIMEOUT,
            )
            # Some instances send text/plain or HTML on throttle; guard parse
            ct = r.headers.get("content-type", "")
            if r.ok and "json" in ct.lower():
                data = r.json()
                if isinstance(data, dict):
                    t = data.get("translatedText") or ""
                elif isinstance(data, list) and data and isinstance(data[0], dict):
                    t = data[0].get("translatedText") or ""
                else:
                    t = ""
                if t:
                    return t
            else:
                # Not JSON; log snippet and try next endpoint
                dbg(f"LibreTranslate non-JSON or error from {url}: {r.status_code} {r.text[:120]}")
        except Exception as e:
            dbg(f"LibreTranslate error @ {url}: {e}")
    return None




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
    """Return (title_it, teaser_it, published_iso) from OG/meta with fallbacks."""
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

def extract_fulltext(article_html: str, url: Optional[str] = None) -> str:
    """Extract main story; scrub boilerplate, social embeds, tickers."""
    text = ""

    # A) Use trafilatura if available (URL hint helps)
    if HAS_TRAFILATURA:
        try:
            text = trafilatura.extract(
                article_html,
                include_comments=False,
                include_tables=False,
                url=url,
            ) or ""
        except Exception:
            text = ""

    # B) Heuristic fallback
    if not text:
        soup = BeautifulSoup(article_html, "lxml")

        # drop obvious non-article blocks
        for sel in [
            "script", "style", "nav", "footer", "header", "aside",
            ".share", ".social", ".related", ".sidebar", ".widget", ".tags",
            ".gallery", ".video", ".player", ".breadcrumbs", ".author-box"
        ]:
            for el in soup.select(sel):
                el.decompose()

        main = (
            soup.find("article")
            or soup.select_one(
                "[itemprop='articleBody'], .article, .article__content, "
                ".post, .entry-content, .content, .news-content, .content-article"
            )
            or soup.body
        )
        paras = []
        if main:
            for el in main.find_all(["p", "h2", "h3", "li"]):
                t = el.get_text(" ", strip=True)
                if len(t) >= 5:
                    paras.append(t)
        text = "\n\n".join(paras)

    if not text:
        return ""

    # C) Scrub boilerplate / tickers / social
    STOP_MARKERS = [
        "Altre notizie", "Altre notizie -", "ALTRE NOTIZIE",
        "Leggi anche", "Potrebbe interessarti", "Articoli correlati"
    ]
    lowered = text.lower()
    cutoff = len(text)
    for m in STOP_MARKERS:
        i = lowered.find(m.lower())
        if i != -1:
            cutoff = min(cutoff, i)
    text = text[:cutoff].strip()

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    META_PREFIXES = ("Sezione:", "Data:", "Autore:", "Twitter:", "Redazione:",
                     "Fonte:", "Foto:", "Credit:", "Credits:", "Copyright")
    TICKER_RE = re.compile(r"^(?:-?\s*)?\d{1,2}:\d{2}\b")  # "21:45 ..."
    URL_RE = re.compile(r"https?://\S+")
    HANDLE_RE = re.compile(r"(^|[\s(])@[\w_]+")
    YT_TW_HINTS = ("youtube.com", "youtu.be", "twitter.com", "x.com", "pic.twitter.com")

    CLEAN = []
    for ln in lines:
        if any(ln.startswith(p) for p in META_PREFIXES):
            continue
        if TICKER_RE.match(ln):
            continue
        if any(h in ln for h in YT_TW_HINTS):
            continue
        ln = URL_RE.sub("", ln)
        ln = HANDLE_RE.sub(" ", ln)
        ln = re.sub(r"\s{2,}", " ", ln)
        if ln:
            CLEAN.append(ln)

    text = "\n\n".join(CLEAN).strip()

    if len(text) > 12000:
        text = text[:4000].rsplit("\n\n", 1)[0].strip()

    return text

def hashlib_md5(s: str) -> str:
    import hashlib as _h
    return _h.md5(s.encode("utf-8")).hexdigest()

def _normalize_for_compare(s: str) -> str:
    # strip all non-letters/digits, lowercase
    return re.sub(r"\W+", "", (s or "")).lower()

def _looks_unchanged(src: str, out: str) -> bool:
    """Heuristic: output still basically Italian (unchanged)."""
    if not out:
        return True
    a = _normalize_for_compare(src)
    b = _normalize_for_compare(out)
    if not a or not b:
        return True
    # identical or near-identical => unchanged
    if a == b:
        return True
    # if the first 24 chars match tightly, also treat as unchanged
    return _normalize_for_compare(src[:24]) == _normalize_for_compare(out[:24])

TOTAL_TRANSLATED_CHARS = 0
MAX_TRANSLATE_CHARS_BUDGET = int(os.getenv("MAX_TRANSLATE_CHARS_BUDGET", "30000"))

# put this near your other globals
MYMEMORY_DISABLED = False

def _normalize_for_compare(s: str) -> str:
    import re
    return re.sub(r"\W+", "", (s or "")).lower()

def _looks_unchanged(src: str, out: str) -> bool:
    """Heuristic to detect when the 'translation' is basically the original text."""
    if not out:
        return True
    a = _normalize_for_compare(src)
    b = _normalize_for_compare(out)
    if not a or not b:
        return True
    if a == b:
        return True
    # also treat as unchanged if the starts look the same
    return _normalize_for_compare(src[:24]) == _normalize_for_compare(out[:24])

def translate_once(text: str, source="it", target="en") -> str:
    """
    Try MyMemory first (free). If rate-limited (429), disable it for the rest of the run.
    Then try LibreTranslate, rotating through any endpoints provided.
    Returns the original text only if both services fail or echo the source.
    """
    import os, requests

    global MYMEMORY_DISABLED

    if not text:
        return ""

    # ---------- 1) MyMemory (skip after first 429 this run) ----------
    if not MYMEMORY_DISABLED:
        try:
            mm = requests.get(
                "https://api.mymemory.translated.net/get",
                params={
                    "q": text,
                    "langpair": f"{source}|{target}",
                    "mt": 1,
                    "mtonly": 1,  # hint to prefer MT result
                },
                timeout=TIMEOUT,
            )
            if mm.status_code == 429:
                MYMEMORY_DISABLED = True
                dbg("MyMemory disabled for remainder of run (429).")
            elif mm.ok:
                j = mm.json()
                t = (j.get("responseData", {}) or {}).get("translatedText", "") or ""
                if t and "QUERY LENGTH LIMIT EXCEEDED" not in t.upper() and not _looks_unchanged(text, t):
                    return t
            else:
                dbg(f"MyMemory HTTP {mm.status_code}: {mm.text[:200]}")
        except Exception as e:
            dbg(f"MyMemory error: {e}")

    # ---------- 2) LibreTranslate fallback (rotate endpoints) ----------
    try:
        # Prefer CSV list from env; fallback to single URL var for backward compatibility
        urls_csv = os.getenv("LIBRETRANSLATE_URLS", "").strip()
        endpoints = [u.strip() for u in urls_csv.split(",") if u.strip()]
        if not endpoints:
            single = os.getenv("LIBRETRANSLATE_URL", "")
            if single:
                endpoints = [single]

        if USE_LIBRETRANSLATE and endpoints:
            import json
            for url in endpoints:
                try:
                    r = requests.post(
                        url,
                        data={"q": text, "source": source, "target": target, "format": "text"},
                        timeout=TIMEOUT,
                    )
                    ct = r.headers.get("content-type", "")
                    if r.ok and "json" in ct.lower():
                        data = r.json()
                        if isinstance(data, dict):
                            t = data.get("translatedText") or ""
                        elif isinstance(data, list) and data and isinstance(data[0], dict):
                            t = data[0].get("translatedText") or ""
                        else:
                            t = ""
                        if t and not _looks_unchanged(text, t):
                            return t
                    else:
                        dbg(f"LibreTranslate non-JSON or error from {url}: {r.status_code} {r.text[:120]}")
                except Exception as e:
                    dbg(f"LibreTranslate error @ {url}: {e}")
    except Exception as e:
        dbg(f"LT fallback setup error: {e}")

    # ---------- 3) Give up: return original (do NOT cache originals) ----------
    return text


def translate_long_text(text: str, source="it", target="en") -> str:
    """Chunk + sub-chunk with caching and budget awareness."""
    if not text:
        return ""

    max_chars = min(TRANSLATE_CHARS_PER_CHUNK, 360)
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out_paras = []

    def split_sentences(s: str) -> list[str]:
        parts = re.split(r"(?<=[\.\?!])\s+", s)
        out, buf = [], ""
        for part in parts:
            if len((buf + " " + part).strip()) <= max_chars:
                buf = (buf + " " + part).strip() if buf else part
            else:
                if buf:
                    out.append(buf)
                if len(part) <= max_chars:
                    buf = part
                else:
                    for i in range(0, len(part), max_chars):
                        out.append(part[i:i+max_chars])
                    buf = ""
        if buf:
            out.append(buf)
        return out

    for p in paras:
        chunks = []
        for s in split_sentences(p):
            if len(s) <= max_chars:
                chunks.append(s)
            else:
                for i in range(0, len(s), max_chars):
                    chunks.append(s[i:i+max_chars])

        translated_chunks = []
        for ctext in chunks:
            t = translate_cached(ctext, source=source, target=target)
            if _looks_unchanged(ctext, t):
                tiny_parts = re.split(r"(?<=[,;:])\s+", ctext)
                tiny_out = []
                for tp in tiny_parts:
                    tt = translate_cached(tp, source=source, target=target)
                    tiny_out.append(tt if not _looks_unchanged(tp, tt) else tp)
                    time.sleep(SLEEP_BETWEEN_CALLS)
                t = " ".join(tiny_out)
            translated_chunks.append(t)
            time.sleep(SLEEP_BETWEEN_CALLS)
        out_paras.append("\n".join(translated_chunks))

    return "\n\n".join(out_paras)

def translate_chunked(long_text: str, source="it", target="en", chunk_chars=TRANSLATE_CHARS_PER_CHUNK) -> str:
    """Translate long text in small chunks; guard against API error echoes."""
    if not long_text:
        return ""
    max_chars = min(chunk_chars, 420)  # extra safe

    paras = [p.strip() for p in long_text.split("\n\n") if p.strip()]
    chunks = []
    for p in paras:
        if len(p) <= max_chars:
            chunks.append(p)
            continue
        parts = re.split(r"(?<=[\.\?!])\s+", p)
        buf = ""
        for part in parts:
            if len(buf) + len(part) + 1 <= max_chars:
                buf = f"{buf} {part}".strip() if buf else part
            else:
                if buf:
                    chunks.append(buf)
                if len(part) > max_chars:
                    for i in range(0, len(part), max_chars):
                        chunks.append(part[i:i+max_chars])
                    buf = ""
                else:
                    buf = part
        if buf:
            chunks.append(buf)

    out_parts = []
    for c in chunks:
        t = translate_once(c, source=source, target=target)
        if not t or "QUERY LENGTH LIMIT EXCEEDED" in t.upper():
            t = c  # fallback to Italian chunk
        out_parts.append(t)
        time.sleep(SLEEP_BETWEEN_CALLS)

    return "\n\n".join(out_parts)

# Title niceness
ACRONYMS = {"psg", "uefa", "fifa", "var", "usa", "uk", "napoli", "milan", "roma", "inter"}

def nice_en_title(s: str) -> str:
    if not s:
        return s
    # Remove leading "VIDEO —", "FOTO –", "Video:" etc.
    s = re.sub(r"^\s*(?:video|foto)\s*[\-–—:]\s*", "", s, flags=re.IGNORECASE)  # hyphen escaped/placed safely
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

    return " ".join(out).replace(" - ", " — ")


def html_paragraphs(text: str) -> str:
    if not text:
        return ""
    return "\n    ".join(f"<p>{x}</p>" for x in text.split("\n\n") if x.strip())

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
    full_en_html = html_paragraphs(full_en) if full_en else ""
    full_it_html = html_paragraphs(full_it) if full_it else ""

    has_en = bool(full_en_html)
    has_it = bool(full_it_html)

    # Build the main article block once
    if has_en:
        article_block = f"<h3>Article</h3>\n{full_en_html}"
    elif has_it:
        article_block = (
            '<div class="src-note">Auto-translation temporarily unavailable — showing original (Italian) below.</div>\n'
            "<h3>Articolo (Italiano)</h3>\n"
            f"{full_it_html}"
        )
    else:
        article_block = ""  # nothing available

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

    {article_block}

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

    # 1) Collect links
    seen_urls = set()
    links: List[str] = []
    per_page_cap = max(10, MAX_LINKS_FROM_LISTINGS // max(1, len(LISTING_URLS)))

    for url in LISTING_URLS:
        try:
            html = http_get(url)
            links.extend(collect_article_links(url, html, cap=per_page_cap, seen=seen_urls))
        except Exception as ex:
            print(f"[WARN] Listing fetch failed {url}: {ex}", file=sys.stderr)

    links = list(dict.fromkeys(links))[:MAX_LINKS_FROM_LISTINGS]
    items: List[Dict] = []

    # 2) Enrich subset: meta + full text + translation
    for href in links[:MAX_ARTICLE_ENRICH]:
        try:
            article_html = http_get(href)
            title_it, teaser_it, published = extract_meta(article_html)
            full_it = extract_fulltext(article_html, url=href)
    
            dbg(f"URL: {href}")
            dbg(f"  title_it: {bool(title_it)} teaser_it: {bool(teaser_it)} full_it_len: {len(full_it)}")
    
            # --- translate ---
            title_en = nice_en_title(translate_once(title_it))
            time.sleep(SLEEP_BETWEEN_CALLS)
    
            teaser_en = translate_once(teaser_it) if teaser_it else ""
            if teaser_it:
                time.sleep(SLEEP_BETWEEN_CALLS)
    
            # Use our new long-text translator for the body
            full_en = translate_long_text(full_it) if full_it else ""
            
            # Warn if it looks untranslated
            if full_it and full_en and _looks_unchanged(full_it[:180], full_en[:180]):
                dbg("  WARN: body first chunk still looks Italian after both services.")

    
            dbg(f"  title_en: {bool(title_en)} teaser_en: {bool(teaser_en)} full_en_len: {len(full_en)}")
    
        except Exception as ex:
            print(f"[WARN] Article fetch failed {href}: {ex}", file=sys.stderr)
            # minimal placeholders if fetch/extract failed
            title_it, teaser_it, published, full_it = "", "", datetime.now(timezone.utc).isoformat(), ""
            title_en, teaser_en, full_en = "", "", ""
    
        post_id = hashlib_md5(href)
        post_path = os.path.join(POSTS_DIR, f"{post_id}.html")
        with open(post_path, "w", encoding="utf-8") as f:
            f.write(render_post_html(title_en, title_it, teaser_en, teaser_it, full_en, full_it, href, published))
        dbg(f"  wrote: {post_path}")
    
        items.append({
            "id": post_id,
            "feed": "article",
            "url": href,
            "local_url": f"posts/{post_id}.html",
            "title_it": title_it,
            "title_en": title_en,
            "summary_it": teaser_it,
            "summary_en": teaser_en,
            "published": published,
        })

    # 3) Lightweight pages for the rest
    now_iso = datetime.now(timezone.utc).isoformat()
    for href in links[MAX_ARTICLE_ENRICH:]:
        slug = urlparse(href).path.rstrip("/").split("/")[-1].replace("-", " ").strip()
        title_it = slug if slug else href
        title_en = nice_en_title(translate_once(title_it))
        time.sleep(SLEEP_BETWEEN_CALLS)

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

    # 4) Write JSON
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
