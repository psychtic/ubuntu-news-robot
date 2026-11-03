"""Prof. Ubuntu's "Roast Africa" video maker.

TEST MODE: it makes ONE video about ONE country and sends it to your Telegram only.
It never posts to Facebook.

Steps: pick a country from facts.json -> Gemini writes a short funny script using ONLY
those facts -> a voice reads it (edge-tts) -> Pillow draws black and gold cards with
big captions -> ffmpeg joins everything into a vertical MP4 -> sent to Telegram.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import wave
from functools import lru_cache
from pathlib import Path

import post_to_facebook as pf
import telegram_approval as ta
import write_captions as wc

BASE = Path(__file__).parent
FACTS_FILE = BASE / "facts.json"
STATE_FILE = BASE / "video_state.json"
WORK = BASE / "video_work"
OUT = BASE / "roast_video.mp4"

W, H = 1080, 1920
GOLD, BLACK, WHITE = "#D4AF37", "#0B0B0B", "#FFFFFF"
VOICES = ["en-NG-AbeoNeural", "en-GB-RyanNeural", "en-US-GuyNeural"]
GAP = 0.25  # seconds of pause after each beat
FONT = None

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


# ---------- small helpers ----------

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError:
            return default
    return default


def run(cmd):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            universal_newlines=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError("command failed (%s):\n%s" % (" ".join(cmd[:2]), result.stdout[-900:]))
    return result.stdout


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip().splitlines()[-1])


def silence_wav(path, seconds):
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(24000)
        f.writeframes(b"\x00\x00" * int(24000 * seconds))


def clean_text(text):
    """Remove emojis and symbols the video font cannot draw."""
    return "".join(c for c in text if ord(c) < 0x2600).strip()


# ---------- 1. pick a country ----------

def pick_country(facts, state):
    wanted = os.environ.get("VIDEO_COUNTRY", "").strip().lower()
    for entry in facts:
        if wanted and entry["country"].lower() == wanted:
            return entry
    used = state.get("used", [])
    fresh = [e for e in facts if e["country"] not in used]
    if not fresh:
        state["used"] = []
        fresh = facts
    return fresh[0]


# ---------- 2. write the script with Gemini ----------

def build_prompt(entry):
    return """You write short, funny, educational scripts for "Prof. Ubuntu", the host of "Roast Africa", a video series on the Pan-African Facebook page "Ubuntu News". Prof. Ubuntu is a confident, dramatic teacher who is very done with everyone, and who loves Africa.

Today's country: {country}

VERIFIED FACTS (the ONLY facts you may use):
{facts}
FLEX FACT (the respectful ending): {flex}

Write these parts:
- "hook": one line, 14 words or fewer, that makes viewers stay. A funny question or bold claim that names {country}.
- "roasts": exactly 3 lines. Each roast is built on ONE of the verified facts, with a joke. 22 words or fewer each.
- "flex": one line, 22 words or fewer, giving a real compliment based on the flex fact.
- "closer": one line ending with a question mark that makes viewers argue in the comments, for example asking which country to roast next. 18 words or fewer.

RULES:
- Use ONLY the facts above. Do not invent numbers, dates, names or claims.
- Roast the country's famous habits, systems and claims. Never mock its people's looks, tribes, religions, poverty, wars or disasters.
- Playful, never hateful. Simple English, short sentences, no emojis, no hashtags.

Reply with ONLY JSON in this shape:
{{"hook": "...", "roasts": ["...", "...", "..."], "flex": "...", "closer": "..."}}
""".format(country=entry["country"],
           facts="\n".join("- " + f for f in entry["facts"]),
           flex=entry["flex"])


def count(text):
    return len(text.split())


def check_script(s):
    """The retention check: returns a list of problems (empty means it passes)."""
    try:
        hook = s["hook"].strip()
        roasts = [r.strip() for r in s["roasts"]]
        flex = s["flex"].strip()
        closer = s["closer"].strip()
    except Exception:
        return ["some parts are missing"]
    problems = []
    if not hook or count(hook) > 16:
        problems.append("hook missing or too long")
    if len(roasts) != 3 or any((not r) or count(r) > 24 for r in roasts):
        problems.append("needs exactly 3 short roasts")
    if not flex or count(flex) > 24:
        problems.append("flex missing or too long")
    if not closer.endswith("?") or count(closer) > 20:
        problems.append("closer must be a short question")
    if count(" ".join([hook] + roasts + [flex, closer])) > 120:
        problems.append("script too long for about 45 seconds")
    return problems


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("reply was not an object")
    return data


def write_script(entry, voice_cfg):
    key = wc.get_key()
    prompt = build_prompt(entry)
    for model in voice_cfg.get("models", ["gemini-2.5-flash-lite"]):
        try:
            print("Trying model: %s" % model)
            script = parse_json(wc.call_gemini(key, model, prompt))
            problems = check_script(script)
            if not problems:
                print("Retention check passed.")
                return script
            print("  script rejected: %s" % "; ".join(problems))
        except urllib.error.HTTPError as error:
            print("  failed (%s): %s" % (error.code, error.read().decode("utf-8", "replace")[:200]))
        except Exception as error:
            print("  failed: %s" % error)
    sys.exit("Could not get a good script from Gemini. Try again in a few minutes.")


# ---------- 3. the voice ----------

async def _speak(text, voice, path):
    import edge_tts
    await edge_tts.Communicate(text, voice, rate="+8%").save(path)


def make_voice(text, path):
    for voice in VOICES:
        try:
            asyncio.run(_speak(text, voice, str(path)))
            if path.exists() and path.stat().st_size > 1000:
                return True
        except Exception as error:
            print("  voice %s failed: %s" % (voice, error))
    return False


# ---------- 4. the picture cards ----------

@lru_cache(maxsize=None)
def ft(size):
    from PIL import ImageFont
    return ImageFont.truetype(FONT, size)


def fit_font(draw, text, max_w, max_h, start, stop):
    for size in range(start, stop, -6):
        lines = pf.wrap_text(draw, text, ft(size), max_w)
        if len(lines) * int(size * 1.25) <= max_h:
            return ft(size), lines, int(size * 1.25)
    lines = pf.wrap_text(draw, text, ft(stop), max_w)
    return ft(stop), lines, int(stop * 1.25)


def draw_lines(draw, lines, font, line_height, top, hot_words):
    space = draw.textlength(" ", font=font)
    for number, line in enumerate(lines):
        words = line.split()
        widths = [draw.textlength(w, font=font) for w in words]
        x = (W - (sum(widths) + space * (len(words) - 1))) / 2
        for word, width in zip(words, widths):
            bare = re.sub(r"[^A-Za-z0-9]", "", word).lower()
            hot = any(c.isdigit() for c in bare) or bare in hot_words
            draw.text((x, top + number * line_height), word, font=font, fill=GOLD if hot else WHITE)
            x += width + space


def draw_frame(path, index, label, country, text, progress, source=None):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (W, H), BLACK if index % 2 == 0 else "#16120A")
    d = ImageDraw.Draw(image)
    d.rectangle([0, 0, W, 18], fill="#2A2412")                       # progress bar track
    d.rectangle([0, 0, max(int(W * progress), 6), 18], fill=GOLD)    # progress bar
    d.rectangle([28, 40, W - 28, H - 40], outline=GOLD, width=4)     # gold frame

    d.text((80, 90), "PROF. UBUNTU", font=ft(44), fill=GOLD)
    right = "ROAST AFRICA"
    d.text((W - 80 - d.textlength(right, font=ft(36)), 98), right, font=ft(36), fill=WHITE)

    name = country.upper()
    size = 110
    while size > 40 and d.textlength(name, font=ft(size)) > W - 160:
        size -= 6
    d.text((W / 2, 250), name, font=ft(size), fill=GOLD, anchor="mm")

    label_width = d.textlength(label, font=ft(40))
    d.rounded_rectangle([(W - label_width) / 2 - 40, 340, (W + label_width) / 2 + 40, 420], radius=40, fill=GOLD)
    d.text((W / 2, 380), label, font=ft(40), fill=BLACK, anchor="mm")

    font, lines, line_height = fit_font(d, text, W - 160, 820, 130, 56)
    top = 520 + (820 - len(lines) * line_height) // 2
    hot_words = set(re.sub(r"[^A-Za-z0-9]", "", w).lower() for w in country.split())
    draw_lines(d, lines, font, line_height, top, hot_words)

    if source:
        small = ft(34)
        for number, line in enumerate(pf.wrap_text(d, "Source: " + source, small, W - 200)):
            d.text((W / 2, 1600 + number * 44), line, font=small, fill="#BBBBBB", anchor="mm")
    d.text((W / 2, 1790), "UBUNTU NEWS", font=ft(40), fill=GOLD, anchor="mm")
    image.save(str(path), "PNG")


def split_chunks(text, size=5):
    chunks, current = [], []
    for word in text.split():
        current.append(word)
        if len(current) >= size or (word[-1] in ".?!,;:" and len(current) >= 2):
            chunks.append(" ".join(current))
            current = []
    if current:
        chunks.append(" ".join(current))
    if len(chunks) > 1 and len(chunks[-1].split()) == 1:
        chunks[-2] += " " + chunks.pop()
    return chunks


# ---------- 5. send to Telegram ----------

def send_to_telegram(path, caption, seconds):
    token, chat_id = ta.load_config()
    boundary = "----roast" + os.urandom(8).hex()
    body = b""
    for name, value in (("chat_id", str(chat_id)), ("caption", caption[:1000]),
                        ("supports_streaming", "true"), ("width", str(W)),
                        ("height", str(H)), ("duration", str(int(seconds)))):
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n' % (boundary, name)).encode("utf-8")
        body += value.encode("utf-8") + b"\r\n"
    body += ('--%s\r\nContent-Disposition: form-data; name="video"; filename="roast.mp4"\r\n'
             "Content-Type: video/mp4\r\n\r\n" % boundary).encode("utf-8")
    body += path.read_bytes() + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendVideo" % token, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError("Telegram said: " + error.read().decode("utf-8", "replace")[:300])


# ---------- main ----------

def main():
    global FONT
    FONT = pf.find_font()
    facts = load_json(FACTS_FILE, [])
    if not facts:
        sys.exit("facts.json is missing or empty.")
    state = load_json(STATE_FILE, {"used": []})
    entry = pick_country(facts, state)
    country = entry["country"]
    print("Country for this video: %s" % country)

    script = write_script(entry, load_json(BASE / "voice.json", {}))
    beats = [("HOOK", clean_text(script["hook"]))]
    for number, roast in enumerate(script["roasts"], start=1):
        beats.append(("ROAST %d" % number, clean_text(roast)))
    beats.append(("THE FLEX", clean_text(script["flex"])))
    beats.append(("YOUR TURN", clean_text(script["closer"])))

    WORK.mkdir(exist_ok=True)
    gap = WORK / "gap.wav"
    silence_wav(gap, GAP)

    wavs, lengths, voiced = [], [], 0
    for index, (label, text) in enumerate(beats):
        mp3, wav = WORK / ("beat%d.mp3" % index), WORK / ("beat%d.wav" % index)
        if make_voice(text, mp3):
            run(["ffmpeg", "-y", "-i", str(mp3), "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
            voiced += 1
        else:
            silence_wav(wav, max(1.8, count(text) / 2.5))  # no voice available: silent beat
        wavs.append(wav)
        lengths.append(duration(wav))
    if voiced == 0:
        print("WARNING: the voice tool did not work, so this video is silent (captions only).")

    audio_list = WORK / "audio.txt"
    audio_list.write_text("".join("file '%s'\nfile '%s'\n" % (w.as_posix(), gap.as_posix()) for w in wavs))
    audio = WORK / "audio.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(audio_list), "-c", "copy", str(audio)])

    total = sum(lengths) + GAP * len(lengths)
    elapsed, frames, number = 0.0, [], 0
    for index, ((label, text), length) in enumerate(zip(beats, lengths)):
        chunks = split_chunks(text)
        weights = [len(c) + 3 for c in chunks]
        for position, chunk in enumerate(chunks):
            seconds = max(0.5, length * weights[position] / sum(weights))
            if position == len(chunks) - 1:
                seconds += GAP
            source = entry["source"] if label in ("THE FLEX", "YOUR TURN") else None
            png = WORK / ("frame%03d.png" % number)
            draw_frame(png, index, label, country, chunk, elapsed / total, source)
            frames.append((png, seconds))
            elapsed += seconds
            number += 1

    frame_list = WORK / "frames.txt"
    lines = []
    for png, seconds in frames:
        lines.append("file '%s'\nduration %.3f\n" % (png.as_posix(), seconds))
    lines.append("file '%s'\n" % frames[-1][0].as_posix())
    frame_list.write_text("".join(lines))

    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(frame_list), "-i", str(audio),
         "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(OUT)])
    seconds_total = duration(OUT)
    size_mb = OUT.stat().st_size / 1e6
    print("Video made: %.1f seconds, %.1f MB" % (seconds_total, size_mb))
    if size_mb > 49:
        sys.exit("The video is too big for Telegram (%.0f MB)." % size_mb)

    caption = "Prof. Ubuntu test video: %s (NOT posted anywhere)\n\n" % country
    if voiced == 0:
        caption += "Note: the voice failed, so this one is silent.\n\n"
    caption += "SCRIPT:\n" + "\n".join("%s: %s" % (l, t) for l, t in beats)
    caption += "\n\nFACTS USED (check them):\n" + "\n".join("- " + f for f in entry["facts"] + [entry["flex"]])
    send_to_telegram(OUT, caption, seconds_total)
    print("Sent to Telegram.")

    state.setdefault("used", []).append(country)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
