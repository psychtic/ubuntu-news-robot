"""Step 1: find fresh Pan-African stories.

Reads feeds.json, downloads each feed, keeps recent stories you have not seen
before, and saves them to stories.json for the next step (caption writing).

Uses only Python's standard library, so there is nothing to install.
Run it with:  python fetch_stories.py
"""

import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "feeds.json"
SEEN_FILE = BASE / "seen.json"        # remembers stories already picked
OUTPUT_FILE = BASE / "stories.json"   # the result of this run

ATOM = "{http://www.w3.org/2005/Atom}"


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def plain_title(story):
    """Google News adds ' - Source Name' to headlines. Remove it, to compare with the queue."""
    title, suffix = story["title"], " - " + story.get("source", "")
    return title[: -len(suffix)] if story.get("source") and title.endswith(suffix) else title


def parse_date(text):
    """Turn a feed date into a timezone-aware datetime, or None."""
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)  # RSS style
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))  # Atom style
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def download(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (pan-african-news-bot)"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def read_feed(name, url):
    """Return a list of stories from one RSS or Atom feed."""
    root = ET.fromstring(download(url))
    stories = []

    # RSS: <item> elements
    for item in root.iter("item"):
        source = item.find("source")
        stories.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "summary": (item.findtext("description") or "").strip(),
            "published": parse_date(item.findtext("pubDate")),
            "source": (source.text.strip() if source is not None and source.text else name),
            "feed": name,
        })

    # Atom: <entry> elements
    for entry in root.iter(ATOM + "entry"):
        link = entry.find(ATOM + "link")
        stories.append({
            "title": (entry.findtext(ATOM + "title") or "").strip(),
            "link": (link.get("href") if link is not None else "") or "",
            "summary": (entry.findtext(ATOM + "summary") or "").strip(),
            "published": parse_date(
                entry.findtext(ATOM + "published") or entry.findtext(ATOM + "updated")
            ),
            "source": name,
            "feed": name,
        })
    return stories


def main():
    config = load_json(CONFIG_FILE, None)
    if not config:
        sys.exit("feeds.json is missing.")

    seen = set(load_json(SEEN_FILE, []))
    # Also never pick anything that is already in the approval queue (even if seen.json is missing).
    for queued in load_json(BASE / "queue.json", {"items": []}).get("items", []):
        seen.add(queued.get("link", ""))
        seen.add("title:" + queued.get("title", "").lower())
    oldest_allowed = datetime.now(timezone.utc) - timedelta(days=config["max_age_days"])
    banned = [word.lower() for word in config.get("must_not_contain", [])]

    found = []
    for feed in config["feeds"]:
        try:
            stories = read_feed(feed["name"], feed["url"])
            print(f"OK   {feed['name']}: {len(stories)} stories")
        except Exception as error:  # one broken feed should not stop the rest
            print(f"FAIL {feed['name']}: {error}")
            continue
        found.extend(stories)

    fresh, titles_this_run = [], set()
    for story in found:
        if not story["title"] or not story["link"]:
            continue
        if story["published"] and story["published"] < oldest_allowed:
            continue
        key = "title:" + plain_title(story).lower()
        if story["link"] in seen or key in seen or ("title:" + story["title"].lower()) in seen:
            continue  # already picked on an earlier run
        if any(word in story["title"].lower() for word in banned):
            continue
        if key in titles_this_run:  # same headline from two feeds
            continue
        titles_this_run.add(key)
        fresh.append(story)

    # newest first, then keep only as many as the config allows
    fresh.sort(
        key=lambda s: s["published"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    fresh = fresh[: config["max_stories_per_run"]]

    for story in fresh:
        story["published"] = story["published"].isoformat() if story["published"] else None

    OUTPUT_FILE.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")
    remembered = {s["link"] for s in fresh} | {"title:" + plain_title(s).lower() for s in fresh}
    SEEN_FILE.write_text(json.dumps(sorted(seen | remembered), indent=2), encoding="utf-8")

    print(f"\nSaved {len(fresh)} new stories to {OUTPUT_FILE.name}")
    for story in fresh:
        print(f"- {story['title']}  ({story['source']})")


if __name__ == "__main__":
    main()
