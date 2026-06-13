#!/usr/bin/env python3
"""
ਮਾਲਵਾ ਗਜ਼ਟ press room
Scrapes Punjab newspapers (via Google News + Bing News, covering Jagbani, Ajit,
Tribune, Bhaskar, Punjab Kesari, Rozana Spokesman & more), pulls full article
text, has Claude read each story and write a clean summary + region tag,
then prints news.json for the Gazette app.

Same architecture as oshocamps: cache-first, cheap, runs on GitHub Actions.
"""
import os, re, json, base64, hashlib, html as htmllib
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import requests
from bs4 import BeautifulSoup

# ───────── config ─────────
KEY            = os.environ["ANTHROPIC_API_KEY"].strip()
MODEL          = "claude-haiku-4-5-20251001"   # cheapest, plenty for summaries
HOURS_BACK     = 24
MAX_NEW_PER_RUN= 120         # articles Claude reads per run (cost guard) — local stories prioritized
BATCH          = 5           # articles per Claude call (smaller = no truncation now we ask for digests)
OUT            = "news.json"
CACHE          = "claude_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"}

def gn(q, hl, ceid):
    return (f"https://news.google.com/rss/search?q={quote(q + ' when:1d')}"
            f"&hl={hl}&gl=IN&ceid=IN:{ceid}")
def bing(q):
    return f"https://www.bing.com/news/search?q={quote(q)}&format=rss"

FEEDS = [
    # ਪੰਜਾਬੀ — only your area
    (gn('"ਰਾਮਪੁਰਾ ਫੂਲ"', "pa", "pa"), "pa", "rampura"),
    (gn("ਬਠਿੰਡਾ", "pa", "pa"), "pa", "bathinda"),
    (gn("ਬਰਨਾਲਾ OR ਤਪਾ ਮੰਡੀ", "pa", "pa"), "pa", "nearby"),
    (gn("ਤਲਵੰਡੀ ਸਾਬੋ OR ਮੌੜ ਮੰਡੀ OR ਗੋਨਿਆਣਾ OR ਭੁੱਚੋ OR ਰਮਾਣ OR ਸੰਗਤ OR ਨਥਾਣਾ OR ਫੂਲ", "pa", "pa"), "pa", "bathinda"),
    # हिन्दी — only your area
    (gn('"रामपुरा फूल"', "hi", "hi"), "hi", "rampura"),
    (gn("बठिंडा", "hi", "hi"), "hi", "bathinda"),
    (gn("बरनाला OR तपा मंडी", "hi", "hi"), "hi", "nearby"),
    (gn('"तलवंडी साबो" OR "मौड़ मंडी" OR गोनियाना OR भुच्चो', "hi", "hi"), "hi", "bathinda"),
    # English — only your area
    (gn('"Rampura Phul"', "en-IN", "en"), "en", "rampura"),
    (gn('Bathinda Punjab', "en-IN", "en"), "en", "bathinda"),
    (gn('Barnala OR "Tapa Mandi" Punjab', "en-IN", "en"), "en", "nearby"),
    (gn('"Talwandi Sabo" OR "Maur Mandi" OR Goniana OR "Bhucho Mandi" OR Nathana OR Sangat Bathinda',
        "en-IN", "en"), "en", "bathinda"),
    # Bing (carries images + snippets) — area only
    (bing("Bathinda Punjab"), "en", "bathinda"),
    (bing('"Rampura Phul"'), "en", "rampura"),
    (bing("Barnala Punjab"), "en", "nearby"),
    (bing("बठिंडा"), "hi", "bathinda"),
    (bing("ਬਠਿੰਡਾ"), "pa", "bathinda"),
]

# ── hard geo-gate: a story is kept only if title/snippet mentions one of these ──
GEO_TERMS = [
    # English
    "rampura", "phul", "bathinda", "bhatinda", "barnala", "tapa", "talwandi sabo",
    "maur", "goniana", "bhucho", "rama mandi", "raman", "sangat", "nathana",
    "dhanaula", "mehal kalan", "malwa",
    # ਪੰਜਾਬੀ
    "ਰਾਮਪੁਰਾ", "ਫੂਲ", "ਬਠਿੰਡਾ", "ਬਰਨਾਲਾ", "ਤਪਾ", "ਤਲਵੰਡੀ ਸਾਬੋ", "ਮੌੜ", "ਗੋਨਿਆਣਾ",
    "ਭੁੱਚੋ", "ਰਮਾਣ", "ਸੰਗਤ", "ਨਥਾਣਾ", "ਧਨੌਲਾ", "ਮਹਿਲ ਕਲਾਂ", "ਮਾਲਵਾ",
    # हिन्दी
    "रामपुरा", "फूल", "बठिंडा", "भटिंडा", "बरनाला", "तपा", "तलवंडी साबो", "मौड़",
    "गोनियाना", "भुच्चो", "रामा मंडी", "रमन", "संगत", "नथाना", "धनौला", "मालवा",
]
def in_my_area(item):
    blob = (item["title"] + " " + item.get("snippet", "")).lower()
    return any(t.lower() in blob for t in GEO_TERMS)

REGION_W = {"rampura": 40, "bathinda": 30, "nearby": 20, "opinion": 6}

# ───────── helpers ─────────
def norm_key(t):
    t = re.sub(r"\s*[-–|]\s*[^-–|]+$", "", t.lower())
    t = re.sub(r"[^\w\s\u0900-\u0A7F]", "", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()[:64]

def md5(s):
    return hashlib.md5(s.encode("utf-8", "ignore")).hexdigest()

def decode_gn(link):
    """Google News article IDs usually carry the real URL base64-encoded."""
    m = re.search(r"articles/([^?]+)", link)
    if not m:
        return None
    s = m.group(1).replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        raw = base64.b64decode(s)
    except Exception:
        return None
    mm = re.search(rb"https?://[ -~]{8,}", raw)
    if not mm:
        return None
    return re.split(r"[\x00-\x1f]", mm.group(0).decode("utf-8", "ignore"))[0]

def fetch(url, timeout=12):
    r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r

def parse_feed(xml_text, lang, region, enrich=False):
    items = []
    soup = BeautifulSoup(xml_text, "xml")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    for it in soup.find_all("item"):
        title = (it.title.text if it.title else "").strip()
        if not title:
            continue
        link = (it.link.text if it.link else "").strip()
        try:
            from email.utils import parsedate_to_datetime
            pub = parsedate_to_datetime(it.pubDate.text)
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if pub < cutoff:
            continue
        source = it.source.text.strip() if it.source else ""
        sm = re.search(r"\s[-–]\s([^-–]{2,40})$", title)
        if sm:
            if not source:
                source = sm.group(1).strip()
            title = re.sub(r"\s[-–]\s[^-–]{2,40}$", "", title).strip()
        desc = it.description.text if it.description else ""
        snippet = re.sub(r"<[^>]+>", " ", desc)
        snippet = re.sub(r"\s+", " ", htmllib.unescape(snippet)).strip()
        if "href" in snippet or len(snippet) < 35 or snippet.startswith(title[:20]):
            snippet = ""
        image = ""
        for tag in ("News:Image", "Image"):
            n = it.find(tag)
            if n and n.text.strip().startswith("http"):
                image = n.text.strip()
                break
        if not image:
            n = it.find("media:content") or it.find("media:thumbnail") or it.find("enclosure")
            if n and n.get("url"):
                image = n["url"]
        items.append(dict(id=norm_key(title), title=title, link=link,
                          source=source or "Press wire", snippet=snippet,
                          image=image, lang=lang, region=region,
                          date=pub.isoformat(), enrich=enrich))
    return items

def grab_article(item):
    """Resolve real URL, return (text, og_image, real_url)."""
    url = item["link"]
    if "news.google." in url:
        url = decode_gn(url) or url
    try:
        html_doc = fetch(url).text
    except Exception:
        return "", "", item["link"]
    soup = BeautifulSoup(html_doc, "html.parser")
    for t in soup(["script", "style", "noscript", "iframe", "form",
                   "header", "footer", "nav", "aside"]):
        t.decompose()
    best, best_score = None, 0
    for c in soup.select('article, main, [class*="story"], [class*="article"], '
                         '[class*="content"], [itemprop="articleBody"]'):
        sc = sum(len(p.get_text(strip=True)) for p in c.find_all("p")
                 if len(p.get_text(strip=True)) > 60)
        if sc > best_score:
            best, best_score = c, sc
    root = best or soup.body or soup
    seen, paras = set(), []
    for p in root.find_all("p"):
        t = re.sub(r"\s+", " ", p.get_text(strip=True))
        if (len(t) > 50 and t not in seen and not re.search(
                r"cookie|subscrib|whatsapp channel|download.{0,12}app|advertis|epaper",
                t, re.I)):
            seen.add(t)
            paras.append(t)
    og = soup.find("meta", property="og:image")
    # the article's OWN published date — the ground truth
    pub_iso = ""
    dn = (soup.find("meta", attrs={"property": "article:published_time"})
          or soup.find("meta", attrs={"itemprop": "datePublished"})
          or soup.find("meta", attrs={"name": "publish-date"})
          or soup.find("time", attrs={"datetime": True}))
    if dn:
        raw = dn.get("content") or dn.get("datetime") or ""
        m = re.match(r"\d{4}-\d{2}-\d{2}[T ]?[\d:.+Z]*", raw.strip())
        if m:
            pub_iso = m.group(0)
    return "\n".join(paras)[:4000], (og.get("content", "") if og else ""), url, pub_iso

# ───────── Claude: read & summarize ─────────
def claude_read(batch):
    """batch: list of dicts with title/text/lang. Returns list of {summary, region, lang}."""
    numbered = "\n\n".join(
        f"### ARTICLE {i+1}\nTITLE: {a['title']}\nTEXT: {a['text'][:1800] or '(text unavailable — use title)'}"
        for i, a in enumerate(batch))
    prompt = f"""You are the desk editor of a local newspaper in Rampura Phul, Bathinda district, Punjab.
For EACH article below, return a JSON object with:
- "i": article number
- "summary": 50-80 words, factual, neutral, written in the SAME LANGUAGE as the article (Punjabi stays Punjabi in Gurmukhi, Hindi stays Hindi, English stays English). No opinions added, no hype. This is the short card preview.
- "digest": a richer 4-6 sentence account in the SAME LANGUAGE — the full who/what/where/when/why, every concrete fact, figure, name and place from the article, written cleanly as a proper news brief so the reader needs nothing else. Still neutral, no opinion, no padding.
- "region": exactly one of "rampura" (Rampura Phul / Phul town / Rampura tehsil villages), "bathinda" (Bathinda city/district incl. Talwandi Sabo, Maur, Goniana, Bhucho, Rama Mandi, Raman, Sangat, Nathana), "nearby" (Barnala, Tapa, Dhanaula, Mehal Kalan), or "opinion" (editorial/op-ed/magazine piece). If unclear, pick the closest of these — never invent other regions.
- "lang": "pa", "hi" or "en".
- "fresh": true normally; false ONLY if the text clearly reports events from more than 3 days ago (old dates, last year, anniversary retrospectives, recycled stories).

Respond with ONLY a JSON array, no markdown fences, no preamble.

{numbered}"""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 8000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=240)
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"])
    text = re.sub(r"```(json)?", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        # reply was cut off mid-JSON — salvage every complete {...} object
        objs = []
        depth = 0; start = None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        objs.append(json.loads(text[start:i+1]))
                    except Exception:
                        pass
                    start = None
        return objs

# ───────── main ─────────
def main():
    cache = {}
    if os.path.exists(CACHE):
        cache = json.load(open(CACHE, encoding="utf-8"))
    old_news = []
    if os.path.exists(OUT):
        old_news = json.load(open(OUT, encoding="utf-8"))

    # 1. collect from every press
    pool = {}
    for url, lang, region in FEEDS:
        is_bing = "bing.com" in url   # Bing fakes dates on old stories → enrich-only
        try:
            for it in parse_feed(fetch(url).text, lang, region, enrich=is_bing):
                ex = pool.get(it["id"])
                if ex:
                    if not ex["image"] and it["image"]:
                        ex["image"] = it["image"]
                    if not ex["snippet"] and it["snippet"]:
                        ex["snippet"] = it["snippet"]
                    if REGION_W.get(it["region"],0) > REGION_W.get(ex["region"],0):
                        ex["region"] = it["region"]
                elif not it["enrich"] and in_my_area(it):
                    pool[it["id"]] = it
            print(f"✓ {lang}/{region}")
        except Exception as e:
            print(f"✗ {lang}/{region}: {e}")

    # carry forward previously printed items still inside the window AND still in-area
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    for o in old_news:
        try:
            if (datetime.fromisoformat(o["date"]) >= cutoff and o["id"] not in pool
                    and in_my_area(o)):
                pool[o["id"]] = o
        except Exception:
            pass

    # 2. which stories has Claude not read yet? prioritize local + newest
    unread = [x for x in pool.values() if md5(x["title"]) not in cache]
    unread.sort(key=lambda x: x["date"], reverse=True)
    unread.sort(key=lambda x: -REGION_W.get(x["region"], 0))
    fresh = unread[:MAX_NEW_PER_RUN]
    print(f"{len(pool)} in stock · {len(fresh)} new for Claude to read")

    # 3. fetch article text + image, then Claude reads in batches
    for x in fresh:
        text, og_img, real, page_date = grab_article(x)
        x["text"] = text
        if og_img and not x.get("image"):
            x["image"] = og_img
        if real and "news.google." not in real:
            x["link"] = real
        if page_date:  # the article page itself knows its true date
            try:
                pd = datetime.fromisoformat(page_date.replace("Z", "+00:00").replace(" ", "T"))
                if pd.tzinfo is None:
                    pd = pd.replace(tzinfo=timezone.utc)
                if pd < cutoff:
                    x["drop"] = True   # feed lied — story is old
                else:
                    x["date"] = pd.isoformat()
            except Exception:
                pass
    fresh = [x for x in fresh if not x.get("drop")]

    for i in range(0, len(fresh), BATCH):
        chunk = fresh[i:i + BATCH]
        try:
            results = claude_read(chunk)
            for res in results:
                idx = res.get("i")
                if not isinstance(idx, int) or idx < 1 or idx > len(chunk):
                    continue
                a = chunk[idx - 1]
                if not res.get("summary"):
                    continue
                cache[md5(a["title"])] = {
                    "summary": res.get("summary", ""),
                    "digest": res.get("digest", ""),
                    "region": res.get("region", a["region"]),
                    "lang": res.get("lang", a["lang"]),
                    "fresh": res.get("fresh", True),
                }
            print(f"  Claude read batch {i//BATCH + 1} ({len(results)} ok)")
        except Exception as e:
            print(f"  ✗ batch {i//BATCH + 1}: {e}")

    # 4. print the edition
    edition = []
    for x in pool.values():
        c = cache.get(md5(x["title"]), {})
        if x.get("drop") or c.get("fresh") is False:
            continue   # old story — never print
        edition.append({
            "title": x["title"],
            "link": x["link"],
            "summary": c.get("summary") or x.get("snippet") or x.get("summary", ""),
            "digest": c.get("digest", ""),
            "image": x.get("image", ""),
            "lang": c.get("lang", x["lang"]),
            "region": c.get("region", x["region"]),
            "date": x["date"],
            "source": x["source"],
            "id": x["id"],
        })
    edition.sort(key=lambda e: (-REGION_W.get(e["region"], 0), e["date"]), reverse=False)
    edition.sort(key=lambda e: e["date"], reverse=True)
    edition.sort(key=lambda e: -REGION_W.get(e["region"], 0))

    json.dump(edition, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    # keep cache from growing forever
    if len(cache) > 3000:
        cache = dict(list(cache.items())[-2000:])
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"🗞️  printed {len(edition)} stories → {OUT}")

if __name__ == "__main__":
    main()
