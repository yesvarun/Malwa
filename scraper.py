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
KEY            = os.environ["ANTHROPIC_API_KEY"]
MODEL          = "claude-haiku-4-5-20251001"   # cheapest, plenty for summaries
HOURS_BACK     = 48
MAX_NEW_PER_RUN= 60          # articles Claude reads per run (cost guard)
BATCH          = 8           # articles per Claude call
OUT            = "news.json"
CACHE          = "claude_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"}

def gn(q, hl, ceid):
    return (f"https://news.google.com/rss/search?q={quote(q + ' when:2d')}"
            f"&hl={hl}&gl=IN&ceid=IN:{ceid}")
def bing(q):
    return f"https://www.bing.com/news/search?q={quote(q)}&format=rss"

FEEDS = [
    # ਪੰਜਾਬੀ
    (gn('"ਰਾਮਪੁਰਾ ਫੂਲ"', "pa", "pa"), "pa", "rampura"),
    (gn("ਬਠਿੰਡਾ", "pa", "pa"), "pa", "bathinda"),
    (gn("ਬਰਨਾਲਾ OR ਤਪਾ ਮੰਡੀ", "pa", "pa"), "pa", "nearby"),
    (gn("ਤਲਵੰਡੀ ਸਾਬੋ OR ਮੌੜ ਮੰਡੀ OR ਗੋਨਿਆਣਾ OR ਭੁੱਚੋ OR ਰਮਾਣ", "pa", "pa"), "pa", "bathinda"),
    (gn("ਪੰਜਾਬ", "pa", "pa"), "pa", "punjab"),
    # हिन्दी
    (gn('"रामपुरा फूल"', "hi", "hi"), "hi", "rampura"),
    (gn("बठिंडा", "hi", "hi"), "hi", "bathinda"),
    (gn("बरनाला OR तपा", "hi", "hi"), "hi", "nearby"),
    (gn("पंजाब", "hi", "hi"), "hi", "punjab"),
    # English
    (gn('"Rampura Phul"', "en-IN", "en"), "en", "rampura"),
    (gn("Bathinda", "en-IN", "en"), "en", "bathinda"),
    (gn('Barnala OR "Tapa Mandi" Punjab', "en-IN", "en"), "en", "nearby"),
    (gn('"Talwandi Sabo" OR "Maur Mandi" OR Goniana OR "Bhucho Mandi" Bathinda',
        "en-IN", "en"), "en", "bathinda"),
    (gn("Punjab", "en-IN", "en"), "en", "punjab"),
    (gn("Punjab editorial OR opinion", "en-IN", "en"), "en", "opinion"),
    # Bing (carries images + snippets)
    (bing("Bathinda"), "en", "bathinda"),
    (bing('"Rampura Phul"'), "en", "rampura"),
    (bing("Barnala Punjab"), "en", "nearby"),
    (bing("बठिंडा"), "hi", "bathinda"),
    (bing("ਬਠਿੰਡਾ"), "pa", "bathinda"),
]

REGION_W = {"rampura": 40, "bathinda": 30, "nearby": 20, "punjab": 6, "opinion": 6}

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

def parse_feed(xml_text, lang, region):
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
                          date=pub.isoformat()))
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
    return "\n".join(paras)[:4000], (og.get("content", "") if og else ""), url

# ───────── Claude: read & summarize ─────────
def claude_read(batch):
    """batch: list of dicts with title/text/lang. Returns list of {summary, region, lang}."""
    numbered = "\n\n".join(
        f"### ARTICLE {i+1}\nTITLE: {a['title']}\nTEXT: {a['text'][:1800] or '(text unavailable — use title)'}"
        for i, a in enumerate(batch))
    prompt = f"""You are the desk editor of a local newspaper in Rampura Phul, Bathinda district, Punjab.
For EACH article below, return a JSON object with:
- "i": article number
- "summary": 50-80 words, factual, neutral, written in the SAME LANGUAGE as the article (Punjabi stays Punjabi in Gurmukhi, Hindi stays Hindi, English stays English). No opinions added, no hype.
- "region": exactly one of "rampura" (Rampura Phul / Phul town / Rampura tehsil villages), "bathinda" (Bathinda city/district incl. Talwandi Sabo, Maur, Goniana, Bhucho, Rama Mandi, Raman, Sangat), "nearby" (Barnala, Tapa, Dhanaula, Mehal Kalan), "opinion" (editorial/op-ed/magazine piece), or "punjab" (everything else).
- "lang": "pa", "hi" or "en".

Respond with ONLY a JSON array, no markdown fences, no preamble.

{numbered}"""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 4000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=180)
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"])
    text = re.sub(r"```(json)?", "", text).strip()
    return json.loads(text)

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
        try:
            for it in parse_feed(fetch(url).text, lang, region):
                ex = pool.get(it["id"])
                if ex:
                    if not ex["image"] and it["image"]:
                        ex["image"] = it["image"]
                    if not ex["snippet"] and it["snippet"]:
                        ex["snippet"] = it["snippet"]
                    if REGION_W[it["region"]] > REGION_W[ex["region"]]:
                        ex["region"] = it["region"]
                else:
                    pool[it["id"]] = it
            print(f"✓ {lang}/{region}")
        except Exception as e:
            print(f"✗ {lang}/{region}: {e}")

    # carry forward previously printed items still inside the window
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    for o in old_news:
        try:
            if datetime.fromisoformat(o["date"]) >= cutoff and o["id"] not in pool:
                pool[o["id"]] = o
        except Exception:
            pass

    # 2. which stories has Claude not read yet?
    fresh = [x for x in pool.values() if md5(x["title"]) not in cache][:MAX_NEW_PER_RUN]
    print(f"{len(pool)} in stock · {len(fresh)} new for Claude to read")

    # 3. fetch article text + image, then Claude reads in batches
    for x in fresh:
        text, og_img, real = grab_article(x)
        x["text"] = text
        if og_img and not x.get("image"):
            x["image"] = og_img
        if real and "news.google." not in real:
            x["link"] = real

    for i in range(0, len(fresh), BATCH):
        chunk = fresh[i:i + BATCH]
        try:
            for res in claude_read(chunk):
                a = chunk[res["i"] - 1]
                cache[md5(a["title"])] = {
                    "summary": res.get("summary", ""),
                    "region": res.get("region", a["region"]),
                    "lang": res.get("lang", a["lang"]),
                }
            print(f"  Claude read batch {i//BATCH + 1}")
        except Exception as e:
            print(f"  ✗ batch {i//BATCH + 1}: {e}")

    # 4. print the edition
    edition = []
    for x in pool.values():
        c = cache.get(md5(x["title"]), {})
        edition.append({
            "title": x["title"],
            "link": x["link"],
            "summary": c.get("summary") or x.get("snippet") or x.get("summary", ""),
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
