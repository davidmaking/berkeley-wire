#!/usr/bin/env python3
"""
Berkeley Wire — a tiny local-news aggregator.

Pulls headlines from Berkeley news sources, filters out the non-news noise,
and writes a single static page of short headline + blurb cards that link out
to the originals. No database, no server.

  python3 berkeley_wire.py             # pull live feeds -> index.html
  python3 berkeley_wire.py --demo      # render sample data (no network)
  python3 berkeley_wire.py --out _site/index.html   # write somewhere specific

Config is loaded from a .env file if present (copy .env.example to .env and
fill in your keys), or from real environment variables. pip install python-dotenv.

Optional add-ons (each off unless configured):
  - Reddit (r/berkeley):  set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET, pip install praw
  - AI news filter + blurb tidy:  set GROQ_API_KEY (free tier at https://console.groq.com),
                                   pip install groq
                                   optionally set GROQ_MODEL (default: openai/gpt-oss-20b)

How "actual news" is enforced (see FILTERING below):
  1. Newsroom RSS feeds are trusted, with a light URL-path exclude (opinion, etc.).
  2. Reddit posts pass strict heuristics (score, not-a-question, not housing/advice).
  3. If a Groq key is present, a fast free-tier model classifies anything
     ambiguous as news / not-news and drops the rest.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import tempfile
import urllib.parse

import feedparser  # pip install feedparser

try:
    from dotenv import load_dotenv  # pip install python-dotenv
    load_dotenv()
except ImportError:
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _utcfromts(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).replace(tzinfo=None)


# ==========================================================================
# CONFIG
# ==========================================================================
# Each newsroom gets a colour so you can scan by source. type="news" feeds are
# trusted; type="social" feeds (Reddit) get heavier filtering. If a feed URL
# 404s or changes, the script skips it and keeps going.
SOURCES = [
    {"name": "Daily Cal",        "url": "https://www.dailycal.org/search/?f=rss&t=article&l=50&s=start_time&sd=desc", "color": "#1B3A6B", "type": "news"},
    {"name": "Berkeleyside",     "url": "https://www.berkeleyside.org/feed",    "color": "#2E7D6F", "type": "news"},
    {"name": "Berkeley Scanner", "url": "https://www.berkeleyscanner.com/rss",  "color": "#B23A2E", "type": "news"},
    {"name": "Berkeley News",    "url": "https://news.berkeley.edu/feed",       "color": "#4B2E83", "type": "news"},
    # More candidates — uncomment and verify the URL in a browser first:
    # {"name": "The Oaklandside", "url": "https://oaklandside.org/feed",        "color": "#0B7285", "type": "news"},
    # {"name": "KQED",            "url": "https://www.kqed.org/news/feed",       "color": "#0A7D8C", "type": "news"},
    # {"name": "City of Berkeley","url": "https://berkeleyca.gov/rss.xml",       "color": "#5A6470", "type": "news"},
]

REDDIT = {
    "subreddit": "berkeley",
    "listing": "hot",      # hot | new | rising | top
    "limit": 25,           # pull this many, then filter down
    "color": "#FF4500",
    "min_score": 25,       # ignore low-traction posts
    "min_comments": 4,
}

# ---- FILTERING ----------------------------------------------------------
# News feeds: drop items whose link path contains any of these. Tune freely —
# remove "/arts" if you want arts coverage, add "/sports" stays out, etc.
EXCLUDE_URL_PARTS = [
    "/opinion", "/sponsored", "sponsored-post", "/classified",
    "/sports", "/arts", "/podcast", "/comics", "/games/",
]

# Reddit: phrases that mark a post as a personal question / not news.
NON_NEWS_PAT = re.compile(
    r"\b(roommate|sublet|sublease|looking for|recommend|recommendation|advice|"
    r"where (can|do|to|should)|best (place|spot|coffee|food|restaurant|cafe|gym)|"
    r"lost|found|selling|for sale|\bwtb\b|\bwts\b|\biso\b|meme|shitpost|rant|vent|"
    r"moving (to|here)|transfer|admission|chance me|gpa|which (class|professor)|"
    r"waitlist|enroll|how do i|how to|any (recs|suggestions)|thoughts on)\b",
    re.I,
)
QUESTION_STARTS = (
    "who ", "what ", "when ", "where ", "why ", "how ", "is ", "are ", "do ",
    "does ", "can ", "should ", "could ", "would ", "anyone ", "any ", "need ",
)
# External links to these domains are treated as strong "this is news" signals.
NEWS_DOMAINS = (
    "berkeleyside.org", "berkeleyscanner.com", "dailycal.org", "news.berkeley.edu",
    "oaklandside.org", "kqed.org", "sfchronicle.com", "sfgate.com", "mercurynews.com",
    "eastbaytimes.com", "abc7news.com", "ktvu.com", "nbcbayarea.com", "apnews.com",
)

BLURB_WORDS = 28
MAX_ITEMS = 60          # hard ceiling on cards (safety cap)
MAX_AGE_HOURS = 24      # only show stories newer than this; override with --hours
ARCHIVE_LOOKBACK_HOURS = 48   # window used to gather archive candidates (wider than
                               # the display window, so a missed/delayed run can't
                               # silently create a permanent hole in the archive)
OUTPUT = "index.html"
GROQ_MODEL = os.getenv("GROQ_MODEL") or "openai/gpt-oss-20b"


# ==========================================================================
# Ingestion
# ==========================================================================
def clean_blurb(raw: str, max_words: int = BLURB_WORDS) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(".,;:") + "…"
    return text


def parse_date(entry) -> dt.datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.datetime(*t[:6])
    return _utcnow()


def url_is_news(link: str) -> bool:
    low = link.lower()
    return not any(part in low for part in EXCLUDE_URL_PARTS)


FEED_HEADERS = {"User-Agent": "BerkeleyWire/1.0 (+https://github.com/davidmaking/berkeley-wire)"}


def fetch_rss(source: dict) -> list:
    try:
        feed = feedparser.parse(source["url"], request_headers=FEED_HEADERS)
        status = feed.get("status")
        if status and status >= 400:
            print(f"  ! skipped {source['name']}: HTTP {status}", file=sys.stderr)
            return []
        items = []
        for e in feed.entries:
            title = clean_blurb(e.get("title", ""), max_words=20)
            link = e.get("link", "")
            if not title or not link or not url_is_news(link):
                continue
            items.append({
                "source": source["name"], "color": source["color"], "type": "news",
                "title": title, "blurb": clean_blurb(e.get("summary", "")),
                "link": link, "date": parse_date(e),
            })
        return items
    except Exception as err:  # noqa: BLE001 — one bad feed shouldn't kill the run
        print(f"  ! skipped {source['name']}: {err}", file=sys.stderr)
        return []


def reddit_is_newsish(post) -> bool:
    """Heuristic: keep posts that read like news, drop questions/advice/listings."""
    title = (post.title or "").strip()
    low = title.lower()
    flair = (getattr(post, "link_flair_text", "") or "").lower()
    domain = (getattr(post, "domain", "") or "").lower()

    # Strong keeps: a news flair, or a link to a known news outlet.
    if "news" in flair or any(d in domain for d in NEWS_DOMAINS):
        return True
    # Strong drops: questions and the non-news vocabulary.
    if low.endswith("?") or low.startswith(QUESTION_STARTS):
        return False
    if NON_NEWS_PAT.search(low):
        return False
    # Otherwise require some traction so randomness doesn't slip through.
    return post.score >= REDDIT["min_score"] and post.num_comments >= REDDIT["min_comments"]


def fetch_reddit() -> list:
    cid, secret = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return []
    try:
        import praw  # pip install praw
    except ImportError:
        print("  ! reddit configured but praw not installed (pip install praw)", file=sys.stderr)
        return []
    try:
        reddit = praw.Reddit(client_id=cid, client_secret=secret,
                             user_agent="berkeley-wire/0.2 (personal project)")
        sub = reddit.subreddit(REDDIT["subreddit"])
        listing = getattr(sub, REDDIT["listing"])(limit=REDDIT["limit"])
        items = []
        for post in listing:
            if post.stickied or not reddit_is_newsish(post):
                continue
            blurb = clean_blurb(getattr(post, "selftext", "") or f"Discussion · {post.domain}")
            items.append({
                "source": f"r/{REDDIT['subreddit']}", "color": REDDIT["color"], "type": "social",
                "title": clean_blurb(post.title, max_words=20), "blurb": blurb,
                "link": f"https://reddit.com{post.permalink}", "date": _utcfromts(post.created_utc),
            })
        return items
    except Exception as err:  # noqa: BLE001
        print(f"  ! skipped reddit: {err}", file=sys.stderr)
        return []


# ==========================================================================
# Quality passes
# ==========================================================================
def within_window(items: list, hours: float) -> list:
    """Keep only items published within the last `hours`. hours<=0 disables it."""
    if not hours or hours <= 0:
        return items
    cutoff = _utcnow() - dt.timedelta(hours=hours)
    return [it for it in items if it["date"] >= cutoff]


def dedupe(items: list) -> list:
    """Drop near-identical headlines (same wire story carried by two outlets)."""
    seen, out = set(), []
    for it in items:
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:55]
        if key and key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def ai_news_filter(items: list) -> list:
    """Optional, gated on GROQ_API_KEY. Classify items as news / not-news.

    Batched (one call per ~40 items) to keep it cheap. Fails open: if the call
    errors, items pass through unchanged rather than emptying the page.
    """
    if not os.getenv("GROQ_API_KEY") or not items:
        return items
    try:
        from groq import Groq  # pip install groq
    except ImportError:
        print("  ! AI filter configured but groq not installed", file=sys.stderr)
        return items

    client = Groq()
    kept = []
    for start in range(0, len(items), 40):
        chunk = items[start:start + 40]
        listing = "\n".join(f"{i}. [{it['source']}] {it['title']}" for i, it in enumerate(chunk))
        prompt = (
            "Below are items from Berkeley-area feeds. Return the indices that are "
            "ACTUAL NEWS of public interest — crime/safety, local government, the "
            "university as an institution (including its research findings and "
            "administrative decisions), development, public health, courts, public "
            "services and benefits, notable local events, or business/community "
            "happenings with real impact (openings, closures, major changes). "
            "EXCLUDE personal questions, advice or recommendation requests, "
            "opinion/editorial, memes, quizzes, listicles, lifestyle fluff, "
            "promotions, housing-wanted posts, and routine event listings.\n\n"
            f"{listing}\n\n"
            'Reply with ONLY a JSON object: {"keep": [list of integer indices]}'
        )
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=500,
                reasoning_effort="low",
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.choices[0].message.content.strip().strip("`")
            text = text[text.find("{"): text.rfind("}") + 1]
            keep_idx = set(json.loads(text).get("keep", []))
            kept += [chunk[i] for i in range(len(chunk)) if i in keep_idx]
        except Exception as err:  # noqa: BLE001 — fail open
            print(f"  ! AI filter chunk failed, keeping all: {err}", file=sys.stderr)
            kept += chunk
    dropped = len(items) - len(kept)
    if dropped:
        print(f"  AI filter removed {dropped} non-news item(s)")
    return kept


def maybe_tighten(items: list) -> list:
    """Optional. Rewrite blurbs to a uniform voice/length (mainly helps Reddit)."""
    if not os.getenv("GROQ_API_KEY"):
        return items
    try:
        from groq import Groq
    except ImportError:
        return items
    client = Groq()
    for it in items:
        if not it["blurb"]:
            continue
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=150,
                reasoning_effort="low",
                messages=[{"role": "user", "content": (
                    "Rewrite as one neutral news blurb, max 20 words, just the facts. "
                    f"Return only the blurb:\n\n{it['title']} — {it['blurb']}")}],
            )
            it["blurb"] = resp.choices[0].message.content.strip()
        except Exception:  # noqa: BLE001
            pass
    return items


# ==========================================================================
# Archive (persistent, searchable history)
# ==========================================================================
def normalize_link(url: str) -> str:
    """Canonical form of a link, used only as the cross-run dedupe key.

    Strips query string/fragment and a trailing slash so the same article
    re-served across hourly fetches (sometimes with drifting tracking
    params) is recognized as one item. The original, unmodified link is
    what's stored/displayed -- this is never shown to a reader.
    """
    parts = urllib.parse.urlsplit(url.strip().lower())
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def month_key(when: dt.datetime) -> str:
    return when.strftime("%Y-%m")


def load_archive_shards(shards_dir: str) -> dict:
    """Load every YYYY-MM.json shard file directly inside shards_dir into {month: [items]}."""
    shards = {}
    if not os.path.isdir(shards_dir):
        return shards
    for name in os.listdir(shards_dir):
        if not re.fullmatch(r"\d{4}-\d{2}\.json", name):
            continue
        month = name[:-5]
        with open(os.path.join(shards_dir, name), encoding="utf-8") as f:
            raw_items = json.load(f)
        for it in raw_items:
            it["date"] = dt.datetime.fromisoformat(it["date"].rstrip("Z"))
        shards[month] = raw_items
    return shards


def merge_into_archive(shards: dict, new_items: list) -> tuple:
    """Add new_items into shards, deduped by normalize_link across the WHOLE
    archive (not just this run). Returns (shards, number_actually_added)."""
    seen = {normalize_link(it["link"]) for month_items in shards.values() for it in month_items}
    added = 0
    for it in new_items:
        key = normalize_link(it["link"])
        if key in seen:
            continue
        seen.add(key)
        shards.setdefault(month_key(it["date"]), []).append(it)
        added += 1
    return shards, added


def write_archive_shards(shards_dir: str, shards: dict) -> None:
    """Write each month's shard back to disk and rebuild the index.json manifest."""
    os.makedirs(shards_dir, exist_ok=True)
    manifest = []
    for month in sorted(shards.keys()):
        items = sorted(shards[month], key=lambda x: x["date"], reverse=True)
        path = os.path.join(shards_dir, f"{month}.json")
        payload = [{**it, "date": it["date"].isoformat() + "Z"} for it in items]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        dates = [it["date"] for it in items]
        manifest.append({
            "month": month, "count": len(items),
            "first_date": min(dates).isoformat() + "Z",
            "last_date": max(dates).isoformat() + "Z",
        })
    manifest.sort(key=lambda m: m["month"], reverse=True)
    with open(os.path.join(shards_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, separators=(",", ":"))


# ==========================================================================
# Render
# ==========================================================================
PAGE_CSS = """
  :root {
    --paper:#F2ECDC; --ink:#1C1712; --muted:#6E6152; --line:#D9CDAF; --flag:#A32B20;
    --masthead:'Playfair Display', Georgia, serif;
    --serif:'Source Serif 4', Georgia, serif;
    --mono:'JetBrains Mono', ui-monospace, monospace;
  }
  * { box-sizing:border-box; min-width:0; }
  html, body { overflow-x:hidden; max-width:100%; }
  body {
    margin:0; background-color:var(--paper); color:var(--ink); font-family:var(--serif);
    -webkit-font-smoothing:antialiased;
    background-image:radial-gradient(rgba(28,23,18,.05) 0.6px, transparent 0.6px);
    background-size:3px 3px;
  }
  .wrap { max-width:760px; margin:0 auto; padding:0 20px 80px; }
  header { padding:34px 0 0; margin-bottom:10px; }
  .updated-tag {
    text-align:center; font-family:var(--mono); font-size:9.5px; letter-spacing:.08em;
    text-transform:uppercase; color:var(--muted); margin-bottom:14px;
  }
  h1.masthead {
    font-family:var(--masthead); font-weight:900; text-align:center;
    font-size:clamp(46px, 9vw, 84px); letter-spacing:-.01em; line-height:.95;
    margin:0 0 16px; text-transform:uppercase;
  }
  h1.masthead span { color:var(--flag); font-style:italic; }
  .rules { margin-bottom:16px; }
  .rule-thick { height:4px; background:var(--ink); margin-bottom:3px; }
  .rule-thin { height:1px; background:var(--ink); }
  nav.top-links { text-align:center; font-family:var(--mono); font-size:11px; letter-spacing:.08em; text-transform:uppercase; margin-bottom:20px; }
  nav.top-links a { color:var(--muted); text-decoration:none; border-bottom:1px solid var(--line); padding-bottom:1px; }
  nav.top-links a:hover { color:var(--flag); border-color:var(--flag); }
  .legend { display:flex; flex-wrap:wrap; justify-content:center; gap:8px 18px; margin-bottom:26px; }
  .tag { font-family:var(--mono); font-size:10.5px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); display:flex; align-items:center; gap:6px; }
  .tag i { width:8px; height:8px; display:inline-block; }
  .card { padding:22px 0 22px 18px; border-bottom:1px solid var(--line); border-left:3px solid var(--c); }
  .card:first-of-type { padding-top:6px; }
  .meta { display:flex; align-items:center; gap:8px; margin-bottom:9px; }
  .src {
    font-family:var(--mono); font-size:10.5px; font-weight:700; letter-spacing:.1em; text-transform:uppercase;
    color:var(--c); border:1.5px solid var(--c); padding:2px 7px;
  }
  .dot { color:var(--line); }
  .time { font-family:var(--mono); font-size:11px; color:var(--muted); }
  h2 { font-family:var(--serif); font-size:23px; font-weight:700; line-height:1.22; margin:0 0 8px; letter-spacing:-.01em; }
  h2 a { color:var(--ink); text-decoration:none; }
  h2 a:hover { color:var(--flag); text-decoration:underline solid var(--flag) 2px; text-underline-offset:3px; }
  .card p { margin:0; color:var(--muted); font-size:16px; line-height:1.55; }
  .empty { color:var(--muted); font-size:16px; padding:40px 18px; font-style:italic; }
  .empty code { font-family:var(--mono); font-style:normal; font-size:13px; background:#E7DCC0; padding:1px 5px; }
  footer { margin-top:34px; padding-top:16px; border-top:1px solid var(--ink); font-family:var(--mono); font-size:10.5px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); text-align:center; }
  .search-row { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:24px; }
  .search-row input[type="text"] {
    flex:1; min-width:180px; font-family:var(--serif); font-size:16px; padding:10px 14px;
    border:1.5px solid var(--ink); background:var(--paper); color:var(--ink); border-radius:0;
  }
  .search-row input[type="date"] {
    font-family:var(--mono); font-size:14px; padding:10px; border:1.5px solid var(--ink);
    background:var(--paper); color:var(--ink); border-radius:0;
  }
  .search-row input:focus { outline:2px solid var(--flag); outline-offset:-1px; }
  .clear-date {
    font-family:var(--mono); font-size:11px; letter-spacing:.06em; text-transform:uppercase;
    color:var(--muted); background:none; border:1.5px solid var(--line); padding:0 12px; cursor:pointer;
    display:none;
  }
  .clear-date.visible { display:inline-block; }
  .clear-date:hover { color:var(--flag); border-color:var(--flag); }
  .month-chips { display:flex; flex-wrap:wrap; gap:8px; justify-content:center; margin-bottom:26px; }
  .month-chip {
    font-family:var(--mono); font-size:10.5px; letter-spacing:.06em; text-transform:uppercase;
    color:var(--muted); border:1px solid var(--line); padding:4px 10px; background:none; cursor:pointer;
  }
  .month-chip:hover { color:var(--flag); border-color:var(--flag); }
  .month-chip.active { color:var(--paper); background:var(--ink); border-color:var(--ink); }
  .status-line { text-align:center; font-family:var(--mono); font-size:11px; color:var(--muted); margin-bottom:20px; }
"""


def relative_time(when: dt.datetime) -> str:
    secs = (_utcnow() - when).total_seconds()
    if secs < 3600:
        return f"{max(0, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def build_html(items: list) -> str:
    utc_now = _utcnow()
    updated_utc_iso = utc_now.isoformat() + "Z"
    updated_fallback = utc_now.strftime("%b %-d, %-I:%M %p UTC")
    sources, seen = [], set()
    for it in items:
        if it["source"] not in seen:
            seen.add(it["source"])
            sources.append((it["source"], it["color"]))
    legend = "".join(
        f'<span class="tag"><i style="background:{c}"></i>{html.escape(n)}</span>'
        for n, c in sources
    )
    cards = "".join(f"""
      <article class="card" style="--c:{it['color']}">
        <div class="meta">
          <span class="src">{html.escape(it['source'])}</span>
          <span class="dot">&middot;</span>
          <span class="time">{relative_time(it['date'])}</span>
        </div>
        <h2><a href="{html.escape(it['link'])}" target="_blank" rel="noopener">{html.escape(it['title'])}</a></h2>
        <p>{html.escape(it['blurb'])}</p>
      </article>""" for it in items)
    if not items:
        cards = ('<p class="empty">Nothing new in this edition. '
                 'Widen the window with <code>--hours</code> (e.g. <code>--hours 48</code>).</p>')

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Berkeley Wire</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>{PAGE_CSS}</style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="updated-tag" id="updated-tag" data-utc="{updated_utc_iso}">Updated {updated_fallback}</div>
      <h1 class="masthead">Berkeley <span>Wire</span></h1>
      <div class="rules"><div class="rule-thick"></div><div class="rule-thin"></div></div>
      <nav class="top-links"><a href="archive.html">Search the Archive</a></nav>
      <div class="legend">{legend}</div>
    </header>
    <main>{cards}</main>
    <footer>{len(items)} stories &middot; headlines link out to original sources</footer>
  </div>
  <script>
    (function () {{
      var el = document.getElementById('updated-tag');
      if (!el) return;
      var d = new Date(el.dataset.utc);
      if (isNaN(d.getTime())) return;
      var text = d.toLocaleString(undefined, {{
        month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'
      }});
      el.textContent = 'Updated ' + text;
    }})();
  </script>
</body>
</html>"""


ARCHIVE_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Berkeley Wire — Archive</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>__PAGE_CSS__</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1 class="masthead">Berkeley <span>Wire</span></h1>
      <div class="rules"><div class="rule-thick"></div><div class="rule-thin"></div></div>
      <nav class="top-links"><a href="index.html">&larr; Back to Today</a></nav>
      <div class="search-row">
        <input id="search-input" type="text" placeholder="Search headlines, sources, topics…" autocomplete="off">
        <input id="date-input" type="date">
        <button id="clear-date" class="clear-date" type="button">Clear date</button>
      </div>
      <div class="month-chips" id="month-chips"></div>
      <div class="status-line" id="status-line">Loading archive…</div>
    </header>
    <main id="results"></main>
    <footer>Berkeley Wire Archive &middot; headlines link out to original sources</footer>
  </div>
  <script>
    (function () {
      var mount = document.getElementById('results');
      var chipsEl = document.getElementById('month-chips');
      var searchEl = document.getElementById('search-input');
      var dateEl = document.getElementById('date-input');
      var clearDateEl = document.getElementById('clear-date');
      var statusEl = document.getElementById('status-line');
      var monthCache = {};
      var manifest = [];
      var activeMonth = null;

      function escapeHtml(s) {
        var d = document.createElement('div');
        d.textContent = s == null ? '' : s;
        return d.innerHTML;
      }

      function relTime(iso) {
        var secs = (Date.now() - new Date(iso).getTime()) / 1000;
        if (secs < 3600) return Math.max(0, Math.floor(secs / 60)) + 'm ago';
        if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
        return Math.floor(secs / 86400) + 'd ago';
      }

      // Local (browser-timezone) calendar date, to match <input type="date">'s
      // plain YYYY-MM-DD value and how relTime/the rest of the site reads as
      // local time to a visitor.
      function localDateKey(d) {
        var mm = String(d.getMonth() + 1).padStart(2, '0');
        var dd = String(d.getDate()).padStart(2, '0');
        return d.getFullYear() + '-' + mm + '-' + dd;
      }

      function render() {
        var q = searchEl.value.trim().toLowerCase();
        var dateFilter = dateEl.value;
        clearDateEl.classList.toggle('visible', !!dateFilter);
        var months = activeMonth ? [activeMonth] : Object.keys(monthCache);
        var items = [];
        months.forEach(function (m) { items = items.concat(monthCache[m] || []); });
        if (dateFilter) {
          items = items.filter(function (it) { return localDateKey(new Date(it.date)) === dateFilter; });
        }
        if (q) {
          items = items.filter(function (it) {
            return (it.title + ' ' + it.blurb + ' ' + it.source).toLowerCase().indexOf(q) !== -1;
          });
        }
        items.sort(function (a, b) { return new Date(b.date) - new Date(a.date); });
        var total = items.length;
        items = items.slice(0, 200);
        if (!items.length) {
          mount.innerHTML = '<p class="empty">No matching stories in what\\'s loaded so far. '
            + 'Try a different search, or load an older month below.</p>';
        } else {
          mount.innerHTML = items.map(function (it) {
            return '<article class="card" style="--c:' + it.color + '">'
              + '<div class="meta"><span class="src">' + escapeHtml(it.source) + '</span>'
              + '<span class="dot">&middot;</span><span class="time">' + relTime(it.date) + '</span></div>'
              + '<h2><a href="' + escapeHtml(it.link) + '" target="_blank" rel="noopener">'
              + escapeHtml(it.title) + '</a></h2>'
              + '<p>' + escapeHtml(it.blurb) + '</p></article>';
          }).join('');
        }
        var loadedCount = Object.keys(monthCache).length;
        var shown = total > 200 ? '200 of ' + total : String(total);
        statusEl.textContent = shown + ' shown · ' + loadedCount + ' of ' + manifest.length + ' months loaded';
      }

      function loadMonth(month) {
        if (monthCache[month]) return Promise.resolve();
        return fetch('data/' + month + '.json')
          .then(function (r) { return r.json(); })
          .then(function (data) { monthCache[month] = data; })
          .catch(function () { monthCache[month] = []; });
      }

      function renderChips() {
        var chips = manifest.map(function (m) {
          var cls = 'month-chip' + (activeMonth === m.month ? ' active' : '');
          return '<button class="' + cls + '" data-month="' + m.month + '">'
            + m.month + ' (' + m.count + ')</button>';
        });
        chips.push('<button class="month-chip' + (activeMonth === null ? ' active' : '')
          + '" data-month="">All loaded</button>');
        chipsEl.innerHTML = chips.join('');
      }

      chipsEl.addEventListener('click', function (e) {
        var btn = e.target.closest('.month-chip');
        if (!btn) return;
        dateEl.value = '';   // chips are a coarser nav mode; picking one drops any exact-date filter
        var month = btn.dataset.month || null;
        activeMonth = month;
        renderChips();
        if (month && !monthCache[month]) {
          statusEl.textContent = 'Loading ' + month + '…';
          loadMonth(month).then(render);
        } else {
          render();
        }
      });

      searchEl.addEventListener('input', render);

      dateEl.addEventListener('change', function () {
        var val = dateEl.value;
        if (!val) { render(); return; }
        var month = val.slice(0, 7);
        activeMonth = month;
        renderChips();
        if (!monthCache[month]) {
          statusEl.textContent = 'Loading ' + month + '…';
          loadMonth(month).then(render);
        } else {
          render();
        }
      });

      clearDateEl.addEventListener('click', function () {
        dateEl.value = '';
        render();
      });

      fetch('data/index.json')
        .then(function (r) { return r.json(); })
        .then(function (data) {
          manifest = data;
          renderChips();
          var toLoad = manifest.slice(0, 2).map(function (m) { return m.month; });
          Promise.all(toLoad.map(loadMonth)).then(render);
        })
        .catch(function () {
          statusEl.textContent = 'Could not load the archive index.';
        });
    })();
  </script>
</body>
</html>"""


def build_archive_html() -> str:
    """Static shell -- fetches data/index.json and month shards at runtime,
    so this page never needs to be regenerated differently run to run."""
    return ARCHIVE_HTML_TEMPLATE.replace("__PAGE_CSS__", PAGE_CSS)


def write_output(path: str, content: str) -> str:
    """Write the page, falling back to a writable dir if the target is read-only."""
    name = os.path.basename(path) or "index.html"
    parent = os.path.dirname(os.path.abspath(path))
    candidates = [path, os.path.join(os.path.expanduser("~"), name),
                  os.path.join(tempfile.gettempdir(), name)]
    last_err = None
    for i, cand in enumerate(candidates):
        try:
            d = os.path.dirname(os.path.abspath(cand))
            os.makedirs(d, exist_ok=True)   # create _site/ etc. if needed
            with open(cand, "w", encoding="utf-8") as f:
                f.write(content)
            if i > 0:
                print(f"  ! {os.path.abspath(path)} wasn't writable "
                      f"({getattr(last_err, 'strerror', last_err)}); wrote here instead:",
                      file=sys.stderr)
            return os.path.abspath(cand)
        except OSError as err:
            last_err = err
    raise last_err


# ==========================================================================
DEMO_ITEMS = [
    {"source": "Berkeley Scanner", "color": "#B23A2E", "type": "news",
     "title": "Two-alarm fire damages building on University Avenue",
     "blurb": "Crews knocked down a blaze at a mixed-use building Tuesday night; no injuries reported and the cause is under investigation, officials said.",
     "link": "https://www.berkeleyscanner.com/", "date": _utcnow() - dt.timedelta(minutes=42)},
    {"source": "Berkeley Scanner", "color": "#B23A2E", "type": "news",
     "title": "Power outage hits parts of South Berkeley; PG&E dispatches crews",
     "blurb": "About 1,200 customers lost power Wednesday afternoon; the utility estimated restoration by early evening.",
     "link": "https://www.berkeleyscanner.com/", "date": _utcnow() - dt.timedelta(hours=1, minutes=10)},
    {"source": "Berkeley News", "color": "#4B2E83", "type": "news",
     "title": "UC Berkeley researchers map a new region of the human genome",
     "blurb": "A campus team published findings that could reshape how scientists study certain inherited conditions, the university announced.",
     "link": "https://news.berkeley.edu/", "date": _utcnow() - dt.timedelta(hours=2)},
    {"source": "Daily Cal", "color": "#1B3A6B", "type": "news",
     "title": "ASUC Senate passes resolution on student housing affordability",
     "blurb": "The resolution urges the university to expand below-market units and freeze certain fees, citing rising rents in surrounding neighborhoods.",
     "link": "https://www.dailycal.org/", "date": _utcnow() - dt.timedelta(hours=4)},
    {"source": "Berkeley News", "color": "#4B2E83", "type": "news",
     "title": "Campus libraries extend overnight hours for finals week",
     "blurb": "Doe and Moffitt will stay open later through the end of the month to accommodate students during exams.",
     "link": "https://news.berkeley.edu/", "date": _utcnow() - dt.timedelta(hours=6)},
    {"source": "Berkeleyside", "color": "#2E7D6F", "type": "news",
     "title": "City Council weighs new bike-lane plan for Shattuck Avenue",
     "blurb": "A redesign would add protected lanes and cut a travel lane each direction; a public hearing is scheduled for later this month.",
     "link": "https://www.berkeleyside.org/", "date": _utcnow() - dt.timedelta(hours=8)},
    {"source": "Berkeleyside", "color": "#2E7D6F", "type": "news",
     "title": "BART service delayed at Downtown Berkeley after medical emergency",
     "blurb": "Trains single-tracked through the station for about 40 minutes during the Wednesday commute before normal service resumed.",
     "link": "https://www.berkeleyside.org/", "date": _utcnow() - dt.timedelta(hours=11)},
    {"source": "r/berkeley", "color": "#FF4500", "type": "social",
     "title": "Protest planned at Sproul Plaza Thursday over proposed tuition hikes",
     "blurb": "Organizers say they expect several hundred attendees; the university said it supports peaceful demonstration.",
     "link": "https://reddit.com/r/berkeley", "date": _utcnow() - dt.timedelta(hours=13)},
    {"source": "Daily Cal", "color": "#1B3A6B", "type": "news",
     "title": "City releases draft budget with cuts to several programs",
     "blurb": "The proposal trims funding for some community services amid a projected shortfall; council will take public comment next week.",
     "link": "https://www.dailycal.org/", "date": _utcnow() - dt.timedelta(hours=18)},
    # ---- older than 24h: gets filtered out by the default window ----
    {"source": "Berkeleyside", "color": "#2E7D6F", "type": "news",
     "title": "Council approved parklet ordinance at last month's meeting",
     "blurb": "This story is more than a day old and should be filtered out by the default 24-hour window.",
     "link": "https://www.berkeleyside.org/", "date": _utcnow() - dt.timedelta(hours=30)},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="render sample data, no network")
    ap.add_argument("--out", default=OUTPUT, help=f"output file path (default: {OUTPUT})")
    ap.add_argument("--hours", type=float, default=MAX_AGE_HOURS,
                    help=f"only show stories from the last N hours (default: {MAX_AGE_HOURS}; 0 = no limit)")
    ap.add_argument("--archive-dir", default=None,
                    help="persist stories into a searchable archive at this directory "
                         "(writes data/YYYY-MM.json shards + archive.html); off by default")
    args = ap.parse_args()

    if args.demo:
        archive_candidates = within_window(DEMO_ITEMS, args.hours)
        items = archive_candidates
    else:
        items = []
        print("Pulling feeds…")
        for src in SOURCES:
            got = fetch_rss(src)
            print(f"  {src['name']}: {len(got)}")
            items += got
        rd = fetch_reddit()
        if rd:
            print(f"  r/{REDDIT['subreddit']}: {len(rd)} (after filter)")
        items += rd

        # Gather archive candidates over a WIDER window than what's displayed
        # today, so a missed hourly run can't quietly erase stories from the
        # archive forever (RSS feeds don't offer historical backfill).
        lookback = max(args.hours, ARCHIVE_LOOKBACK_HOURS)
        archive_candidates = within_window(items, lookback)
        print(f"  within last {lookback:g}h: {len(archive_candidates)}")
        archive_candidates = dedupe(archive_candidates)
        archive_candidates = ai_news_filter(archive_candidates)   # optional, gated on GROQ_API_KEY
        archive_candidates = maybe_tighten(archive_candidates)    # optional, gated on GROQ_API_KEY

        items = within_window(archive_candidates, args.hours)   # narrow to today's display window

    items.sort(key=lambda x: x["date"], reverse=True)
    items = items[:MAX_ITEMS]

    written = write_output(args.out, build_html(items))
    print(f"Wrote {written} ({len(items)} stories)")

    if args.archive_dir:
        shards_dir = os.path.join(args.archive_dir, "data")
        shards = load_archive_shards(shards_dir)
        shards, added = merge_into_archive(shards, archive_candidates)
        write_archive_shards(shards_dir, shards)
        archive_html_path = write_output(
            os.path.join(args.archive_dir, "archive.html"), build_archive_html())
        print(f"Archived {added} new stories; wrote {archive_html_path}")


if __name__ == "__main__":
    main()