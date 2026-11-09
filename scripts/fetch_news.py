#!/usr/bin/env python3
"""Hourly news pulse.

Pulls RSS feeds from many outlets, groups headlines that describe the same
story, ranks stories by how many different outlets are covering them (plus a
small recency bonus), and writes the top 5 for this hour to data/history.json.

Standard library only, so the GitHub Action needs no installs.
"""
import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

IST = timezone(timedelta(hours=5, minutes=30))
TOP_N = 5
KEEP_HOURS = 168          # one week of hourly snapshots
WINDOW_HOURS = 6          # only consider stories published in the last 6 hours
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "history.json")

GN = "https://news.google.com/rss{path}?hl=en-IN&gl=IN&ceid=IN:en"
FEEDS = [
    # (url, label used when an item has no <source>, is_google_news)
    (GN.format(path=""), "Google News", True),
    (GN.format(path="/headlines/section/topic/WORLD"), "Google News", True),
    (GN.format(path="/headlines/section/topic/NATION"), "Google News", True),
    (GN.format(path="/headlines/section/topic/BUSINESS"), "Google News", True),
    (GN.format(path="/headlines/section/topic/TECHNOLOGY"), "Google News", True),
    (GN.format(path="/headlines/section/topic/SPORTS"), "Google News", True),
    (GN.format(path="/headlines/section/topic/ENTERTAINMENT"), "Google News", True),
    ("https://timesofindia.indiatimes.com/rssfeedstopstories.cms", "Times of India", False),
    ("https://feeds.feedburner.com/ndtvnews-top-stories", "NDTV", False),
    ("https://www.thehindu.com/news/national/feeder/default.rss", "The Hindu", False),
    ("https://indianexpress.com/feed/", "The Indian Express", False),
    ("https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml", "Hindustan Times", False),
    ("https://economictimes.indiatimes.com/rssfeedstopstories.cms", "The Economic Times", False),
    ("https://www.livemint.com/rss/news", "Mint", False),
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC News", False),
    ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera", False),
    ("https://www.theguardian.com/world/rss", "The Guardian", False),
]

STOP = set("""
a an the and or but if of to in on at by for with from as is are was were be been being
this that these those it its into over after before about than then there their they them
he she his her you your we our us i me my not no yes will would can could should may might
has have had do does did says said say new news live update updates latest today amid
how why what when where who which more most just also up out off top big first one two
watch video photos report reports here all per via vs against under amidst during while
""".split())

TAG_RE = re.compile(r"<[^>]+>")
WORD_RE = re.compile(r"[a-z0-9]+")


def clean(text):
    return html.unescape(TAG_RE.sub(" ", text or "")).replace("\xa0", " ").strip()


def tokens(title):
    words = WORD_RE.findall(title.lower())
    return {w for w in words if len(w) >= 3 and w not in STOP}


MEDIA = "{http://search.yahoo.com/mrss/}"


def summarize(text, limit=420):
    text = re.sub(r"\s+", " ", clean(text)).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:-") + "…"


def find_image(it):
    for tag in (MEDIA + "content", MEDIA + "thumbnail"):
        for m in it.iter(tag):
            url = m.get("url") or ""
            if url.startswith("http") and (m.get("medium") in (None, "image") or tag.endswith("thumbnail")):
                return url
    enc = it.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image") and (enc.get("url") or "").startswith("http"):
        return enc.get("url")
    m = re.search(r'<img[^>]+src="(https?://[^"]+)"', it.findtext("description", "") or "")
    return m.group(1) if m else None


def parse_date(text):
    if not text:
        return None
    try:
        d = parsedate_to_datetime(text)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (NewsPulse hourly bot)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def parse_feed(raw, label, is_gn):
    """Yield dicts: title, link, source, published, extra_sources, gn_rank."""
    root = ET.fromstring(raw)
    items = root.findall(".//item")
    if not items:  # Atom fallback
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for i, e in enumerate(root.findall(".//a:entry", ns)):
            link = e.find("a:link", ns)
            yield {
                "title": clean(e.findtext("a:title", "", ns)),
                "link": link.get("href") if link is not None else "",
                "source": label,
                "published": parse_date(e.findtext("a:updated", "", ns)),
                "extra_sources": set(),
                "gn_rank": None,
                "summary": summarize(e.findtext("a:summary", "", ns)),
                "image": None,
            }
        return
    for i, it in enumerate(items):
        title = clean(it.findtext("title", ""))
        source = label
        src_el = it.find("source")
        if src_el is not None and (src_el.text or "").strip():
            source = src_el.text.strip()
        if is_gn and source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)].strip()
        extra = set()
        if is_gn:
            # Google News lists other outlets covering the same story in the description.
            desc = it.findtext("description", "") or ""
            for name in re.findall(r"<font[^>]*>(.*?)</font>", html.unescape(desc)):
                name = clean(name)
                if name:
                    extra.add(name)
        yield {
            "title": title,
            "link": (it.findtext("link", "") or "").strip(),
            "source": source,
            "published": parse_date(it.findtext("pubDate", "")),
            "extra_sources": extra,
            "gn_rank": i if is_gn else None,
            # Google News descriptions are just a list of links, so no summary from them.
            "summary": "" if is_gn else summarize(it.findtext("description", "")),
            "image": None if is_gn else find_image(it),
        }


def similar(a, b):
    shared = len(a & b)
    small = min(len(a), len(b))
    if small == 0:
        return False
    need = 2 if small <= 4 else 3
    return shared >= need and shared / small >= 0.5


def cluster(items):
    clusters = []
    for it in items:
        t = it["tokens"]
        best = None
        for c in clusters:
            if similar(t, c["core"]):
                best = c
                break
        if best is None:
            clusters.append({"core": set(t), "items": [it]})
        else:
            best["items"].append(it)
    return clusters


def score(c, now):
    sources = set()
    for it in c["items"]:
        sources.add(it["source"])
        sources |= it["extra_sources"]
    sources.discard("Google News")
    newest = max((it["published"] for it in c["items"] if it["published"]), default=None)
    age_h = (now - newest).total_seconds() / 3600 if newest else WINDOW_HOURS
    recency = max(0.0, 1 - age_h / WINDOW_HOURS)
    gn_ranks = [it["gn_rank"] for it in c["items"] if it["gn_rank"] is not None]
    gn_bonus = 1.5 / (1 + min(gn_ranks)) if gn_ranks else 0
    return len(sources) + recency + gn_bonus, sources, newest


def pick_headline(c):
    # Prefer a direct outlet link over a Google News redirect; then the newest.
    def key(it):
        direct = 0 if "news.google.com" in it["link"] else 1
        ts = it["published"].timestamp() if it["published"] else 0
        return (direct, ts)
    return max(c["items"], key=key)


def build(feeds_raw, now):
    items = []
    for raw, label, is_gn in feeds_raw:
        try:
            for it in parse_feed(raw, label, is_gn):
                if not it["title"] or not it["link"]:
                    continue
                if it["published"] and now - it["published"] > timedelta(hours=WINDOW_HOURS):
                    continue
                it["tokens"] = tokens(it["title"])
                if len(it["tokens"]) < 2:
                    continue
                items.append(it)
        except ET.ParseError as e:
            print(f"skip {label}: bad XML ({e})", file=sys.stderr)
    items.sort(key=lambda x: x["published"] or now, reverse=True)
    ranked = []
    for c in cluster(items):
        s, sources, newest = score(c, now)
        ranked.append((s, sources, newest, c))
    ranked.sort(key=lambda r: r[0], reverse=True)
    stories = []
    for s, sources, newest, c in ranked[:TOP_N]:
        h = pick_headline(c)
        ordered = sorted(c["items"], key=lambda it: ("news.google.com" in it["link"], -(it["published"].timestamp() if it["published"] else 0)))
        summary = next((it["summary"] for it in ordered if len(it.get("summary") or "") > 40 and it["summary"] != it["title"]), "")
        image = next((it["image"] for it in ordered if it.get("image")), None)
        articles, seen = [], set()
        for it in ordered:
            if it["source"] in seen:
                continue
            seen.add(it["source"])
            articles.append({"source": it["source"], "title": it["title"], "link": it["link"],
                             "publishedAt": it["published"].astimezone(IST).isoformat(timespec="minutes") if it["published"] else None})
        stories.append({
            "title": h["title"],
            "link": h["link"],
            "source": h["source"],
            "sources": sorted(sources)[:12],
            "sourceCount": len(sources),
            "publishedAt": newest.astimezone(IST).isoformat(timespec="minutes") if newest else None,
            "score": round(s, 2),
            "summary": summary,
            "image": image,
            "articles": articles[:8],
        })
    return stories, len(items)


def main():
    now = datetime.now(timezone.utc)
    feeds_raw, ok = [], 0
    for url, label, is_gn in FEEDS:
        try:
            feeds_raw.append((fetch(url), label, is_gn))
            ok += 1
        except Exception as e:
            print(f"skip {label}: {e}", file=sys.stderr)
    if ok == 0:
        print("No feeds reachable; leaving history unchanged.", file=sys.stderr)
        return 1
    stories, n_items = build(feeds_raw, now)
    write(stories, now, ok, n_items)
    print(f"{ok}/{len(FEEDS)} feeds, {n_items} headlines -> top {len(stories)}")
    for s in stories:
        print(f"  [{s['sourceCount']:>2}] {s['title']}")
    return 0


def write(stories, now, feeds_ok, n_items):
    hour = now.astimezone(IST).replace(minute=0, second=0, microsecond=0)
    try:
        with open(OUT) as f:
            hist = json.load(f)
    except Exception:
        hist = {"hours": []}
    key = hour.isoformat(timespec="minutes")
    hours = [h for h in hist.get("hours", []) if h.get("hour") != key]
    hours.insert(0, {
        "hour": key,
        "generatedAt": now.astimezone(IST).isoformat(timespec="minutes"),
        "feeds": feeds_ok,
        "headlines": n_items,
        "stories": stories,
    })
    hours.sort(key=lambda h: h["hour"], reverse=True)
    out = {"updated": now.astimezone(IST).isoformat(timespec="minutes"), "hours": hours[:KEEP_HOURS]}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.exit(main())
