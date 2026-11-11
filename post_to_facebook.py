"""Step 4: post the captions you approved to your Facebook Page.

Two jobs, picked by the word you type after the file name:

  python post_to_facebook.py setup   (once) save your Page ID and Page token
  python post_to_facebook.py post    post everything you approved

It reads queue.json (kept by telegram_approval.py). Each approved caption is posted
once, then marked "posted". It never posts a rejected or unapproved caption.

Freshness rule: an approved caption that has been waiting longer than
"max_draft_age_hours" (in the "schedule" part of voice.json, default 6) is marked
"expired" and is never posted, so old news does not go out.

Uses only Python's standard library.
"""

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
QUEUE_FILE = BASE / "queue.json"
CONFIG_FILE = BASE / "facebook_config.json"  # holds your Page token. Never share it.
GRAPH = "https://graph.facebook.com/v26.0"
MAX_POSTS_PER_RUN = int(os.environ.get("FB_MAX_PER_RUN", "3"))  # a safety limit, so a mistake can never flood your page
USE_IMAGES = not os.environ.get("FB_NO_IMAGES")  # set FB_NO_IMAGES=1 to post text only
STOCK_PHOTOS = not os.environ.get("FB_NO_STOCK_PHOTOS")  # set FB_NO_STOCK_PHOTOS=1 to go back to the plain gold card

# Old Windows terminals can crash on emojis or Arabic letters. This makes printing safe.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


class NoAnswer(Exception):
    """Facebook did not answer at all (timeout, no internet)."""


class FacebookError(Exception):
    def __init__(self, message, code=None):
        Exception.__init__(self, message)
        self.code = code


# ---------- talking to Facebook ----------

def graph(path, token, fields=None, post=False):
    """Call Facebook. GET requests are retried on network trouble; POSTs never are,
    because a POST that timed out may still have gone through."""
    params = dict(fields or {})
    params["access_token"] = token  # sent in the request body, never printed
    data = urllib.parse.urlencode(params).encode("utf-8")
    if post:
        request = urllib.request.Request("%s/%s" % (GRAPH, path), data=data)
    else:
        request = urllib.request.Request("%s/%s?%s" % (GRAPH, path, data.decode("utf-8")))

    attempts = 1 if post else 3
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:  # Facebook answered with an error
            try:
                info = json.loads(error.read().decode("utf-8")).get("error", {})
            except Exception:
                info = {}
            raise FacebookError(info.get("message", "HTTP error %s" % error.code), info.get("code"))
        except OSError:
            if attempt < attempts:
                print("Could not reach Facebook (try %d of %d). Trying again..." % (attempt, attempts))
                time.sleep(4)
    raise NoAnswer()


# ---------- the picture card ----------

CARD_SIZE = 1080
FONT_CANDIDATES = [
    str(BASE / "font.ttf"),  # put your own bold font here to override
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/verdanab.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def find_font():
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return path
    raise RuntimeError("no bold font found. Copy any bold .ttf font into this folder and name it font.ttf")


def wrap_text(draw, text, font, width):
    lines, line = [], ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


STOP_WORDS = set("""about after again against also amid among and are been before being between both but can could
during each every for from has have her here his how into its just more most new not now off one only other our out over
says said than that the their them then there these they this those through under until upon was were what when where which
while who why will with without would year years your""".split())


def photo_queries(title):
    """Turn a headline into 2-3 short searches for Pixabay, best guess first."""
    words = [w for w in re.findall(r"[a-z]+", title.lower()) if len(w) > 3 and w not in STOP_WORDS]
    words = [w for w in words if w not in ("africa", "african", "africans")] or words
    queries = []
    if len(words) >= 2:
        queries.append(" ".join(words[:3]))
    if words:
        queries.append("africa " + words[0])
    queries.append("africa")
    return queries


def stock_background(item):
    """Find a free stock photo (from Pixabay) that fits the headline.
    Returns (square picture, credit text), or (None, None) so the plain gold card is used."""
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not STOCK_PHOTOS or not key:
        return None, None
    try:
        import io
        from PIL import Image
        for query in photo_queries(item["title"]):
            url = "https://pixabay.com/api/?" + urllib.parse.urlencode(
                {"key": key, "q": query, "image_type": "photo", "safesearch": "true", "per_page": 10})
            request = urllib.request.Request(url, headers={"User-Agent": "ubuntu-news-robot"})
            with urllib.request.urlopen(request, timeout=40) as response:
                photos = json.loads(response.read().decode("utf-8")).get("hits", [])
            if not photos:
                continue
            photo = random.choice(photos[:5])
            picture_url = photo.get("largeImageURL") or photo["webformatURL"]
            request = urllib.request.Request(picture_url, headers={"User-Agent": "ubuntu-news-robot"})
            with urllib.request.urlopen(request, timeout=60) as response:
                picture = Image.open(io.BytesIO(response.read())).convert("RGB")
            side = min(picture.width, picture.height)  # cut a square from the middle
            left, top = (picture.width - side) // 2, (picture.height - side) // 2
            picture = picture.crop((left, top, left + side, top + side)).resize((CARD_SIZE, CARD_SIZE))
            print("Stock photo found for: %s" % query)
            return picture, "Photo: %s / Pixabay" % photo.get("user", "Pixabay")
        print("Note: no stock photo found. Using the plain card.")
    except urllib.error.HTTPError as error:
        print("Note: stock photo failed (%s). Using the plain card." % error.code)
    except Exception as error:
        print("Note: stock photo failed (%s). Using the plain card." % error)
    return None, None


def make_card(item):
    """Draw a square picture: the headline on a stock photo (or, if that fails, on the dark
    gold card). Needs Pillow (pip install pillow). Returns the path of the saved PNG."""
    from PIL import Image, ImageDraw, ImageFont  # imported here so text posting works without Pillow

    font_path = find_font()
    try:
        voice = json.loads((BASE / "voice.json").read_text(encoding="utf-8"))
    except Exception:
        voice = {}
    page_name = voice.get("page_name", "")
    # Black and gold by default. Change them with a "card_colors" section in voice.json.
    colors = {"background": "#0B0B0B", "bars": ["#D4AF37"], "page_name": "#D4AF37",
              "underline": "#D4AF37", "text": "#FFFFFF"}
    colors.update(voice.get("card_colors", {}))

    picture, credit = stock_background(item)
    if picture is not None:
        # Stock photo, darkened so the white words are easy to read. No gold bars.
        image = Image.blend(picture, Image.new("RGB", (CARD_SIZE, CARD_SIZE), "#000000"), 0.5)
        colors["page_name"] = colors["underline"] = colors["text"] = "#FFFFFF"
    else:
        image = Image.new("RGB", (CARD_SIZE, CARD_SIZE), colors["background"])
    draw = ImageDraw.Draw(image)
    bars = colors["bars"]
    for row_top in (0, CARD_SIZE - 24):  # colour bars, top and bottom
        if picture is not None:
            break
        for number, bar_color in enumerate(bars):
            left = number * CARD_SIZE // len(bars)
            right = (number + 1) * CARD_SIZE // len(bars)
            draw.rectangle([left, row_top, right, row_top + 24], fill=bar_color)

    margin = 80
    if page_name:
        draw.text((margin, 80), page_name.upper(), font=ImageFont.truetype(font_path, 40), fill=colors["page_name"])
        draw.rectangle([margin, 140, margin + 120, 146], fill=colors["underline"])

    headline = item["title"].strip()
    box_width, box_height = CARD_SIZE - 2 * margin, 660
    for size in range(88, 35, -4):  # biggest text that fits
        font = ImageFont.truetype(font_path, size)
        lines = wrap_text(draw, headline, font, box_width)
        line_height = int(size * 1.28)
        if len(lines) * line_height <= box_height:
            break
    else:  # still too long: cut it and add ...
        lines = lines[: box_height // line_height]
        lines[-1] = lines[-1].rstrip(" .,") + "..."
    top = 190 + (box_height - len(lines) * line_height) // 2
    for number, line in enumerate(lines):
        draw.text((margin, top + number * line_height), line, font=font, fill=colors["text"],
                  stroke_width=3 if picture is not None else 0, stroke_fill="#000000")
    if credit:
        small = ImageFont.truetype(font_path, 26)
        draw.text((CARD_SIZE - margin - draw.textlength(credit, font=small), CARD_SIZE - 70), credit,
                  font=small, fill="#FFFFFF", stroke_width=2, stroke_fill="#000000")

    folder = BASE / "cards"
    folder.mkdir(exist_ok=True)
    path = folder / (item["id"] + ".png")
    image.save(str(path), "PNG")
    return path


def upload_photo(page_id, token, caption, image_path):
    """Post a picture with its caption to the Page. Never retried (see graph())."""
    boundary = "----ubuntunews" + os.urandom(8).hex()
    body = b""
    for name, value in (("caption", caption), ("published", "true"), ("access_token", token)):
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n' % (boundary, name)).encode("utf-8")
        body += value.encode("utf-8") + b"\r\n"
    body += ('--%s\r\nContent-Disposition: form-data; name="source"; filename="card.png"\r\n'
             "Content-Type: image/png\r\n\r\n" % boundary).encode("utf-8")
    body += Path(image_path).read_bytes() + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    request = urllib.request.Request(
        "%s/%s/photos" % (GRAPH, page_id), data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            info = json.loads(error.read().decode("utf-8")).get("error", {})
        except Exception:
            info = {}
        raise FacebookError(info.get("message", "HTTP error %s" % error.code), info.get("code"))
    except OSError:
        raise NoAnswer()


# ---------- settings and queue files ----------

def clean(text):
    """Remove spaces and stray quote marks that come along when copying from a web page."""
    return text.strip().strip("\"'“”‘’").strip()


def load_config():
    page_id = clean(os.environ.get("FB_PAGE_ID", ""))
    token = clean(os.environ.get("FB_PAGE_TOKEN", ""))
    if (not page_id or not token) and CONFIG_FILE.exists():
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        page_id = page_id or clean(str(saved.get("page_id", "")))
        token = token or clean(str(saved.get("page_token", "")))
    if not page_id or not token:
        sys.exit("Facebook is not set up yet. Run:  python post_to_facebook.py setup")
    return page_id, token


def load_queue():
    if not QUEUE_FILE.exists():  # nothing has been sent for approval yet
        return {"offset": 0, "items": []}
    return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))


def load_schedule():
    """The "schedule" part of voice.json (quiet hours, daily limit). None means no limits."""
    try:
        voice = json.loads((BASE / "voice.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    schedule = voice.get("schedule")
    if not schedule:
        return None
    return {
        "offset": float(schedule.get("timezone_offset_hours", 0)),
        "quiet_start": int(schedule.get("quiet_hours_start", 22)),
        "quiet_end": int(schedule.get("quiet_hours_end", 7)),
        "max_per_day": int(schedule.get("max_posts_per_day", 3)),
    }


def load_max_draft_age_hours():
    """How long an approved caption may wait before it is too old to post (default 6 hours)."""
    try:
        voice = json.loads((BASE / "voice.json").read_text(encoding="utf-8-sig"))
        return float((voice.get("schedule") or {}).get("max_draft_age_hours", 6))
    except Exception:
        return 6.0


def in_quiet_hours(hour, start, end):
    if start > end:  # for example 22 to 7 crosses midnight
        return hour >= start or hour < end
    return start <= hour < end


def save_queue(queue):
    QUEUE_FILE.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")


def expire_old_drafts(queue):
    """Mark approved captions that waited too long as 'expired', so old news never goes out."""
    max_age = timedelta(hours=load_max_draft_age_hours())
    now = datetime.now(timezone.utc)
    changed = False
    for item in queue["items"]:
        if item["status"] != "approved" or not item.get("sent_at"):
            continue
        age = now - datetime.fromisoformat(item["sent_at"])
        if age > max_age:
            item["status"] = "expired"
            item["expired_at"] = now.isoformat()
            changed = True
            print("EXPIRED (too old to post, %.1f hours): %s" % (age.total_seconds() / 3600, item["title"]))
    if changed:
        save_queue(queue)


# ---------- the two jobs ----------

def do_setup():
    page_id = clean(input("Paste your Facebook Page ID (numbers only) and press Enter: "))
    token = clean(input("Paste your Page access token and press Enter: "))
    try:
        page = graph(page_id, token, {"fields": "name,id"})
        # With a Page token, "me" IS the Page. With a User token, "me" is you.
        me = graph("me", token, {"fields": "id"})
        if str(me.get("id")) != page_id:
            print("\nThis looks like your personal (User) token. Asking Facebook for the Page token...")
            swap = graph(page_id, token, {"fields": "access_token"})
            if not swap.get("access_token"):
                sys.exit("\nFacebook did not give a Page token. In Graph API Explorer, run me/accounts and\n"
                         "copy the access_token from the block that says \"name\": \"Ubuntu News\".")
            token = swap["access_token"]
            print("Done: now using the Page token.")
    except FacebookError as error:
        sys.exit("\nFacebook said: %s\nCheck the Page ID and token, then run setup again." % error)
    except NoAnswer:
        sys.exit("\nCould not reach Facebook. Check your internet and try again.")
    CONFIG_FILE.write_text(json.dumps({"page_id": page_id, "page_token": token}), encoding="utf-8")
    print("\nConnected to the Page: %s" % page.get("name", page_id))
    print("Saved to %s. Keep that file private." % CONFIG_FILE.name)


def build_message(item):
    return "%s\n\nRead more: %s" % (item["caption"], item["link"])


def do_post():
    page_id, token = load_config()
    queue = load_queue()
    expire_old_drafts(queue)  # first, throw away anything too old
    waiting = [item for item in queue["items"] if item["status"] == "approved"]
    if not waiting:
        print("Nothing approved is waiting to be posted.")
        return

    limit = MAX_POSTS_PER_RUN
    schedule = load_schedule()
    if schedule:
        local_now = datetime.now(timezone.utc) + timedelta(hours=schedule["offset"])
        if in_quiet_hours(local_now.hour, schedule["quiet_start"], schedule["quiet_end"]):
            print("Quiet hours: not posting now. %d approved post(s) will go out later." % len(waiting))
            return
        today = 0
        for item in queue["items"]:
            if item["status"] == "posted" and item.get("posted_at"):
                when = datetime.fromisoformat(item["posted_at"]) + timedelta(hours=schedule["offset"])
                if when.date() == local_now.date():
                    today += 1
        room = schedule["max_per_day"] - today
        if room <= 0:
            print("Daily limit reached (%d posts today). %d approved post(s) will go out tomorrow." % (
                schedule["max_per_day"], len(waiting)))
            return
        limit = min(limit, room)

    posted = 0
    for item in waiting[:limit]:
        card = None
        if USE_IMAGES:
            try:
                card = make_card(item)
            except Exception as error:  # no Pillow, no font...: still post, just without a picture
                print("Note: could not make a picture (%s). Posting text only." % error)
        try:
            if card:
                result = upload_photo(page_id, token, build_message(item), card)
                result["id"] = result.get("post_id") or result.get("id")
            else:
                result = graph("%s/feed" % page_id, token, {"message": build_message(item)}, post=True)
        except FacebookError as error:
            print("FAILED: %s\n  Facebook said: %s" % (item["title"], error))
            if error.code in (190, 102):  # token expired or invalid: every post will fail
                sys.exit("\nYour Page token is no longer valid. Make a new one with make_long_token.py. "
                         "If the robot runs on GitHub, also paste it into the FB_PAGE_TOKEN secret there.")
            item["error"] = str(error)  # stays 'approved', so the next run tries again
            save_queue(queue)
            continue
        except NoAnswer:
            # We cannot know whether Facebook received it. Posting again could duplicate it.
            item["status"] = "check_page"
            save_queue(queue)
            sys.exit("\nNo answer from Facebook while posting:\n  %s\n"
                     "It may or may not have been posted. Look at your Page. If it is there,\n"
                     "nothing more to do. If not, change 'check_page' to 'approved' for it in\n"
                     "queue.json and run post again." % item["title"])

        item["status"] = "posted"
        item["posted_at"] = datetime.now(timezone.utc).isoformat()
        item["fb_post_id"] = result.get("id")
        item.pop("error", None)
        save_queue(queue)  # save after every post so nothing is posted twice
        posted += 1
        print("POSTED: %s  (id %s)" % (item["title"], result.get("id")))

    left = len(waiting) - posted
    print("\nPosted %d. Still approved and waiting: %d" % (posted, max(left, 0)))


def main():
    jobs = {"setup": do_setup, "post": do_post}
    if len(sys.argv) != 2 or sys.argv[1] not in jobs:
        sys.exit("Type one of:\n  python post_to_facebook.py setup\n  python post_to_facebook.py post")
    jobs[sys.argv[1]]()


if __name__ == "__main__":
    main()
