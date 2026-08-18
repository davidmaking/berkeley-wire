#!/usr/bin/env python3
"""
Berkeley Wire — a tiny local-news aggregator.

Pulls headlines from Berkeley news sources, filters out the non-news noise,
and writes a single static page of short headline + blurb cards that link out
to the originals. No database, no server.

  python3 berkeley_wire.py             # pull live feeds -> index.html
  python3 berkeley_wire.py --demo      # render sample data (no network)
  python3 berkeley_wire.py --out _site/index.html   # write somewhere specific

Optional add-ons (each off unless configured):
  - Reddit (r/berkeley):  set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET, pip install praw
  - AI news filter + blurb tidy:  run Ollama locally (https://ollama.com), pip install ollama
                                   optionally set OLLAMA_MODEL (default: llama3.2)

How "actual news" is enforced (see FILTERING below):
  1. Newsroom RSS feeds are trusted, with a light URL-path exclude (opinion, etc.).
  2. Reddit posts pass strict heuristics (score, not-a-question, not housing/advice).
  3. If a local Ollama server is reachable, a small model classifies anything
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

import feedparser  # pip install feedparser


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
    {"name": "Daily Cal",        "url": "https://www.dailycal.org/feed",        "color": "#1B3A6B", "type": "news"},
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
OUTPUT = "index.html"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")


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


def fetch_rss(source: dict) -> list:
    try:
        feed = feedparser.parse(source["url"])
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
    """Optional, uses a local Ollama model. Classify items as news / not-news.

    Batched (one call per ~40 items) to keep it cheap. Fails open: if the call
    errors (e.g. Ollama isn't running), items pass through unchanged rather
    than emptying the page.
    """
    if not items:
        return items
    try:
        import ollama  # pip install ollama
    except ImportError:
        print("  ! AI filter needs the ollama package (pip install ollama)", file=sys.stderr)
        return items

    kept = []
    for start in range(0, len(items), 40):
        chunk = items[start:start + 40]
        listing = "\n".join(f"{i}. [{it['source']}] {it['title']}" for i, it in enumerate(chunk))
        prompt = (
            "Below are items from Berkeley-area feeds. Return the indices that are "
            "ACTUAL NEWS of public interest — crime/safety, local government, the "
            "university as an institution, development, public health, courts, or "
            "notable local events. EXCLUDE personal questions, advice or "
            "recommendation requests, opinion/editorial, memes, promotions, "
            "housing-wanted posts, and routine event listings.\n\n"
            f"{listing}\n\n"
            'Reply with ONLY a JSON object: {"keep": [list of integer indices]}'
        )
        try:
            resp = ollama.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": prompt}],
                options={"num_predict": 300},
            )
            text = resp["message"]["content"].strip().strip("`")
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
    try:
        import ollama
    except ImportError:
        return items
    for it in items:
        if not it["blurb"]:
            continue
        try:
            resp = ollama.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": (
                    "Rewrite as one neutral news blurb, max 20 words, just the facts. "
                    f"Return only the blurb:\n\n{it['title']} — {it['blurb']}")}],
                options={"num_predict": 60},
            )
            it["blurb"] = resp["message"]["content"].strip()
        except Exception:  # noqa: BLE001
            pass
    return items


# ==========================================================================
# Render
# ==========================================================================
def relative_time(when: dt.datetime) -> str:
    secs = (_utcnow() - when).total_seconds()
    if secs < 3600:
        return f"{max(0, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def build_html(items: list) -> str:
    updated = dt.datetime.now().strftime("%b %-d, %-I:%M %p")
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
      <article class="card">
        <div class="meta">
          <span class="src" style="--c:{it['color']}">{html.escape(it['source'])}</span>
          <span class="time">{relative_time(it['date'])}</span>
        </div>
        <h2><a href="{html.escape(it['link'])}" target="_blank" rel="noopener">{html.escape(it['title'])}</a></h2>
        <p>{html.escape(it['blurb'])}</p>
      </article>""" for it in items)
    if not items:
        cards = ('<p class="empty">Nothing new in this window yet. '
                 'Widen it with <code>--hours</code> (e.g. <code>--hours 48</code>).</p>')

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Berkeley Wire</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --paper:#F4F5F2; --ink:#15181C; --muted:#6B7178; --line:#E2E3DD; --amber:#C8852C;
    --display:'Space Grotesk', system-ui, sans-serif; --mono:'JetBrains Mono', ui-monospace, monospace;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--paper); color:var(--ink); font-family:var(--display); -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:760px; margin:0 auto; padding:0 20px 80px; }}
  header {{ border-bottom:2px solid var(--ink); padding:34px 0 14px; margin-bottom:8px; }}
  .ticker {{ font-family:var(--mono); font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); display:flex; justify-content:space-between; gap:12px; margin-bottom:18px; }}
  .ticker .live::before {{ content:""; display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--amber); margin-right:7px; vertical-align:middle; animation:pulse 2s infinite; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.35}} }}
  h1 {{ font-size:40px; font-weight:700; letter-spacing:-.02em; margin:0; line-height:1; }}
  h1 span {{ color:var(--amber); }}
  .legend {{ display:flex; flex-wrap:wrap; gap:14px; margin-top:16px; }}
  .tag {{ font-family:var(--mono); font-size:11px; color:var(--muted); display:flex; align-items:center; gap:6px; }}
  .tag i {{ width:9px; height:9px; border-radius:2px; display:inline-block; }}
  .card {{ padding:22px 0; border-bottom:1px solid var(--line); }}
  .meta {{ display:flex; align-items:center; gap:12px; margin-bottom:8px; }}
  .src {{ font-family:var(--mono); font-size:10.5px; font-weight:500; letter-spacing:.08em; text-transform:uppercase; color:var(--c); border:1px solid var(--c); padding:2px 7px; border-radius:3px; }}
  .time {{ font-family:var(--mono); font-size:11px; color:var(--muted); }}
  h2 {{ font-size:20px; font-weight:500; line-height:1.25; margin:0 0 6px; letter-spacing:-.01em; }}
  h2 a {{ color:var(--ink); text-decoration:none; }}
  h2 a:hover {{ color:var(--amber); }}
  .card p {{ margin:0; color:var(--muted); font-size:15px; line-height:1.5; }}
  .empty {{ color:var(--muted); font-size:15px; padding:40px 0; }}
  .empty code {{ font-family:var(--mono); font-size:13px; background:#E9EAE4; padding:1px 5px; border-radius:3px; }}
  footer {{ font-family:var(--mono); font-size:11px; color:var(--muted); margin-top:30px; }}
  @media (prefers-reduced-motion:reduce) {{ .ticker .live::before {{ animation:none; }} }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="ticker"><span class="live">Berkeley local feed</span><span>Updated {updated}</span></div>
      <h1>Berkeley <span>Wire</span></h1>
      <div class="legend">{legend}</div>
    </header>
    <main>{cards}</main>
    <footer>{len(items)} stories · headlines link out to original sources</footer>
  </div>
</body>
</html>"""


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
    args = ap.parse_args()

    if args.demo:
        items = within_window(DEMO_ITEMS, args.hours)
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

        items = within_window(items, args.hours)   # drop stale items before the costly passes
        print(f"  within last {args.hours:g}h: {len(items)}")
        items = dedupe(items)
        items = ai_news_filter(items)   # optional, uses local Ollama if available
        items = maybe_tighten(items)    # optional, uses local Ollama if available

    items.sort(key=lambda x: x["date"], reverse=True)
    items = items[:MAX_ITEMS]

    written = write_output(args.out, build_html(items))
    print(f"Wrote {written} ({len(items)} stories)")


if __name__ == "__main__":
    main()