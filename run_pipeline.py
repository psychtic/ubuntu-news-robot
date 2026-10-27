"""The robot's brain. GitHub runs this once an hour. You never have to.

Every run it does this, in order:
  1. Reads your Telegram taps (Reject / Post now) and releases posts whose veto time is up.
  2. Posts whatever is ready to Facebook (respecting quiet hours and the daily limit).
  3. If it is time for a new batch: finds stories, writes captions, sends them to Telegram.
If anything breaks, it sends you a warning on Telegram (at most one every 12 hours).

Settings live in the "schedule" part of voice.json.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import post_to_facebook as pf
import telegram_approval as ta

BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"   # remembers when the last batch was made
SEEN_FILE = BASE / "seen.json"

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError:
            return default
    return default


def run_step(title, args):
    """Run one of the robot's scripts. Returns (worked, last_lines_of_output)."""
    print("\n===== %s =====" % title, flush=True)
    result = subprocess.run(
        [sys.executable, "-u", str(BASE / args[0])] + args[1:], cwd=str(BASE),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
        encoding="utf-8", errors="replace",
    )
    print(result.stdout, flush=True)
    if result.returncode == 0:
        return True, ""
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return False, " | ".join(lines[-3:])[:300]


def send_alert(state, now, text):
    last = state.get("last_alert")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=12):
        print("(Warning not sent to Telegram: one was already sent in the last 12 hours.)")
        return
    try:
        token, chat_id = ta.load_config()
        ta.api(token, "sendMessage", {"chat_id": chat_id, "text": "⚠️ Ubuntu News robot needs a look:\n" + text})
        state["last_alert"] = now.isoformat()
    except (Exception, SystemExit) as error:
        print("Could not send the warning to Telegram: %s" % error)


def make_batch(problems):
    """Find stories, write captions, send them to Telegram. True if it all worked."""
    seen_before = SEEN_FILE.read_text(encoding="utf-8") if SEEN_FILE.exists() else None

    def undo_seen():  # if captions fail, let the same stories be tried again next time
        if seen_before is None:
            if SEEN_FILE.exists():
                SEEN_FILE.unlink()
        else:
            SEEN_FILE.write_text(seen_before, encoding="utf-8")

    ok, why = run_step("Find new stories", ["fetch_stories.py"])
    if not ok:
        problems.append("finding stories failed: " + why)
        return False
    if not load_json(BASE / "stories.json", []):
        print("No new stories this time. It will look again next hour.")
        return False

    ok, why = run_step("Write captions with Gemini", ["write_captions.py"])
    if not ok:
        undo_seen()
        problems.append("writing captions failed: " + why)
        return False

    ok, why = run_step("Send captions to Telegram", ["telegram_approval.py", "send"])
    if not ok:
        problems.append("sending to Telegram failed: " + why)
        return False
    return True


def main():
    now = datetime.now(timezone.utc)
    schedule = load_json(BASE / "voice.json", {}).get("schedule", {})
    offset = float(schedule.get("timezone_offset_hours", 0))
    local = now + timedelta(hours=offset)
    state = load_json(STATE_FILE, {})
    force = os.environ.get("FORCE_BATCH", "").strip().lower() == "true"
    problems = []
    print("Robot run at %s UTC (%s local)" % (now.strftime("%Y-%m-%d %H:%M"), local.strftime("%H:%M")))

    ok, why = run_step("Read your Telegram taps and release posts whose time is up", ["telegram_approval.py", "collect"])
    if not ok:
        problems.append("reading Telegram taps failed: " + why)

    ok, why = run_step("Post what is ready to Facebook", ["post_to_facebook.py", "post"])
    if not ok:
        problems.append("posting to Facebook failed: " + why)
    for item in load_json(BASE / "queue.json", {"items": []}).get("items", []):
        if item["status"] == "approved" and item.get("error"):
            problems.append('Facebook refused "%s": %s' % (item["title"][:60], item["error"][:150]))

    hours = float(schedule.get("hours_between_batches", 8))
    last = state.get("last_batch")
    due = force or not last or now - datetime.fromisoformat(last) >= timedelta(hours=hours)
    quiet = pf.in_quiet_hours(local.hour, int(schedule.get("quiet_hours_start", 22)),
                              int(schedule.get("quiet_hours_end", 7)))
    if not due:
        wait = datetime.fromisoformat(last) + timedelta(hours=hours) - now
        print("\nNext batch of new posts in about %.1f hours." % (wait.total_seconds() / 3600))
    elif quiet and not force:
        print("\nIt is time for new posts, but it is quiet hours. It will wait until morning.")
    elif make_batch(problems):
        state["last_batch"] = now.isoformat()

    if problems:
        text = "\n".join("- " + p for p in problems)
        print("\nPROBLEMS:\n" + text)
        send_alert(state, now, text)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    if problems:
        sys.exit(1)  # makes the run show a red cross on GitHub


if __name__ == "__main__":
    main()
