"""Step 2: turn stories into Facebook captions using Gemini (free).

Reads stories.json (made by fetch_stories.py) and voice.json (your page's style).
Asks Gemini to pick the stories that fit your page and write a caption for each,
in the format of the day (Policy or Palaver on Monday, Did You Know on Tuesday, ...).
Saves the result to captions.json for the next step (your approval).

Uses only Python's standard library. Run it with:  python write_captions.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
STORIES_FILE = BASE / "stories.json"
VOICE_FILE = BASE / "voice.json"
OUTPUT_FILE = BASE / "captions.json"
KEY_FILE = BASE / "gemini_key.txt"
API = "https://generativelanguage.googleapis.com/v1beta"

# Old Windows terminals can crash on emojis or Arabic letters. This makes printing safe.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

COUNTRIES = ["Nigeria", "South Africa", "Ghana", "Kenya", "Egypt"]

# One format per day of the week (Monday = 0). {country} is filled in on Wednesdays.
DAILY_FORMATS = [
    ("Policy or Palaver",
     "Prefer a story about a government decision, law, policy, price or economic move. "
     "Say what happened in 1-2 simple sentences, then add one sentence starting with 'Our take:' "
     "saying whether this looks like it helps Africa move forward or holds it back, and why. "
     "This is an opinion based only on the headline, so word it carefully. "
     "End by asking readers to vote: 👍 helping Africa or 👎 holding Africa back."),
    ("Did You Know",
     "Prefer a story with a surprising or little-known fact. Open with 'Did you know?' and use ONLY "
     "a fact that is stated in the headline. Keep it light and a little funny, then ask a question "
     "that makes people share what they knew."),
    ("Country of the Week: {country}",
     "Prefer a story about {country}. If there is none, use any story. Celebrate or playfully roast "
     "the situation (never the people) and challenge followers from other African countries to compare, "
     "for example: 'Does your country do this too?'"),
    ("Hot Take Thursday",
     "Open with a bold one-sentence opinion about the story, written as the page's own take "
     "('Our hot take:'). Keep it about the situation, policy or idea, never about people's identity. "
     "End by asking people to agree or disagree and say why."),
    ("Africa This Week",
     "Write it like a quick news-anchor update: start with 'This week in Africa:' then say what happened "
     "in 1-2 sentences, then add one witty line. Ask which story people think mattered most this week."),
    ("Friendly Rivalry: football, food, music and slang",
     "Prefer sports, food, music or culture stories. Use playful banter between countries, never insults "
     "about people, tribes or religions. Ask followers to defend their country in the comments."),
    ("Africa Wins",
     "Prefer a positive story about African achievement, innovation, unity or success. Proud, warm and "
     "a little witty. Ask readers to name another African win or to tag a friend who should see this."),
]


def get_key():
    """Find the Gemini key: environment variable, then gemini_key.txt, then ask."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8-sig").strip()
    if not key:
        key = input("Paste your Gemini API key and press Enter: ").strip()
    if not key:
        sys.exit("No key given.")
    return key


def clean_title(story):
    """Google News adds ' - Source Name' to headlines. Remove it."""
    title = story["title"]
    suffix = " - " + story.get("source", "")
    if story.get("source") and title.endswith(suffix):
        title = title[: -len(suffix)]
    return title.strip()


def todays_format(voice):
    """Pick the format for today, using Nigerian time (from voice.json)."""
    offset = float((voice.get("schedule") or {}).get("timezone_offset_hours", 0))
    local = datetime.now(timezone.utc) + timedelta(hours=offset)
    name, instructions = DAILY_FORMATS[local.weekday()]
    country = COUNTRIES[local.isocalendar()[1] % len(COUNTRIES)]
    return name.format(country=country), instructions.format(country=country)


def build_prompt(voice, stories, format_name, format_instructions):
    lines = []
    for number, story in enumerate(stories, start=1):
        age = story.get("age_hours")
        age_text = "reported about %d hours ago" % round(age) if age is not None else "age unknown"
        lines.append("%d. %s  [source: %s; %s]" % (number, clean_title(story), story["source"], age_text))
    story_list = "\n".join(lines)

    return """You are the social media manager for a Facebook page called "{page}".
Audience: {audience}.
Tone: {tone}

TODAY'S FORMAT: {format_name}
{format_instructions}

Below are the latest news headlines. Choose only the ones that clearly fit this page, and write a Facebook caption for each one you choose, in today's format. If no headline fits today's format well, still pick the best story that fits the page and write it in the page's normal bold, witty style.

Prefer stories about:
{prefer}

Skip stories about:
{avoid}

STRICT RULES for every caption:
- Use ONLY facts that appear in the headline. Do not invent numbers, names, dates, quotes, causes or details.
- Never say "today" or "breaking" for a story reported more than 18 hours ago.
- Opinions must be clearly marked as the page's own view (for example "Our take:") and must be about the situation, policy or idea, never about a person's identity.
- Never make claims about a named person that the headline does not state.
- Roast situations, systems and policies only. Never mock any tribe, religion, nationality, ethnic group or people's looks.
- Friendly rivalry between countries is welcome, but keep it playful and never hateful.
- Maximum {words} words, not counting hashtags.
- Start with a short hook, explain in 1-2 simple sentences, and end with a question that invites comments.
- End with the line: Source: <source name>
- Do not include any web link in the caption.
- Add at most {tags} hashtags. Always include: {always}
- No endorsing or attacking political parties or candidates.
- You may use 1 or 2 emojis.

Headlines:
{stories}

Reply with ONLY a JSON list, one object per headline, in this exact shape:
[
  {{"id": 1, "decision": "post", "reason": "short reason", "caption": "the full caption including hashtags"}},
  {{"id": 2, "decision": "skip", "reason": "why it does not fit", "caption": ""}}
]
""".format(
        page=voice["page_name"],
        audience=voice["audience"],
        tone=voice["tone"],
        format_name=format_name,
        format_instructions=format_instructions,
        prefer="\n".join("- " + item for item in voice["prefer"]),
        avoid="\n".join("- " + item for item in voice["avoid"]),
        words=voice["max_caption_words"],
        tags=voice["max_hashtags"],
        always=", ".join(voice["always_hashtags"]),
        stories=story_list,
    )


def call_gemini(key, model, prompt):
    """Send one request to Gemini and return its text reply."""
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"},
    }).encode("utf-8")
    request = urllib.request.Request(
        "%s/models/%s:generateContent" % (API, model),
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["candidates"][0]["content"]["parts"][0]["text"]


def list_models(key):
    """Ask Google which models this key can use (shown when every model fails)."""
    request = urllib.request.Request(API + "/models?pageSize=100", headers={"x-goog-api-key": key})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    return [
        m["name"].replace("models/", "")
        for m in data.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]


def parse_reply(text):
    """Turn Gemini's reply into a Python list. Handles stray ``` fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    result = json.loads(text)
    if not isinstance(result, list):
        raise ValueError("reply was not a list")
    return result


def ask_with_fallback(key, models, prompt):
    """Try each model in turn until one works."""
    for model in models:
        try:
            print("Trying model: %s" % model)
            return parse_reply(call_gemini(key, model, prompt)), model
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            print("  failed (%s): %s" % (error.code, detail.replace("\n", " ")))
            if error.code in (400, 401, 403) and "API key" in detail:
                sys.exit("\nGoogle says the API key is not valid. Check you copied all of it.")
        except Exception as error:  # bad JSON, timeout, etc.
            print("  failed: %s" % error)

    print("\nNone of the models in voice.json worked. Models your key can use:")
    try:
        for name in list_models(key):
            print("  " + name)
        print("\nCopy one or two of these names into the 'models' list in voice.json and run again.")
    except Exception as error:
        print("  (could not list models: %s)" % error)
    sys.exit(1)


def main():
    if not STORIES_FILE.exists():
        sys.exit("stories.json not found. Run fetch_stories.py first.")
    stories = json.loads(STORIES_FILE.read_text(encoding="utf-8"))
    if not stories:
        sys.exit("stories.json is empty: no new stories to write about.")
    voice = json.loads(VOICE_FILE.read_text(encoding="utf-8-sig"))

    format_name, format_instructions = todays_format(voice)
    print("Today's format: %s\n" % format_name)

    key = get_key()
    answers, model = ask_with_fallback(
        key, voice["models"], build_prompt(voice, stories, format_name, format_instructions))
    print("Used model: %s\n" % model)

    chosen, skipped = [], []
    for answer in answers:
        try:
            story = stories[int(answer["id"]) - 1]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        caption = (answer.get("caption") or "").strip()
        if answer.get("decision") == "post" and caption:
            chosen.append({
                "title": clean_title(story),
                "source": story["source"],
                "link": story["link"],
                "published": story.get("published"),
                "caption": caption,
                "status": "waiting_for_approval",
            })
        else:
            skipped.append((clean_title(story), answer.get("reason", "")))

    chosen = chosen[: voice["max_posts_per_run"]]
    OUTPUT_FILE.write_text(json.dumps(chosen, indent=2, ensure_ascii=False), encoding="utf-8")

    for number, item in enumerate(chosen, start=1):
        print("=== POST %d ===" % number)
        print(item["caption"])
        print("(story: %s)\n" % item["link"])
    print("Skipped %d stories that did not fit your page:" % len(skipped))
    for title, reason in skipped:
        print("- %s  [%s]" % (title, reason))
    print("\nSaved %d captions to %s" % (len(chosen), OUTPUT_FILE.name))


if __name__ == "__main__":
    main()
