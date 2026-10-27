"""Step 3: approve posts from your phone with Telegram.

Three jobs, picked by the word you type after the file name:

  python telegram_approval.py setup     (once) connect your Telegram bot
  python telegram_approval.py send      send new captions to your phone
  python telegram_approval.py collect   read your Approve / Reject taps

It keeps every caption and its status in queue.json:
  sent -> approved or rejected
The next step (Facebook posting) will post the approved ones.

Uses only Python's standard library.
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
CAPTIONS_FILE = BASE / "captions.json"
QUEUE_FILE = BASE / "queue.json"
CONFIG_FILE = BASE / "telegram_config.json"  # holds your bot token. Never share it.

# Old Windows terminals can crash on emojis or Arabic letters. This makes printing safe.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


# ---------- talking to Telegram ----------

def api(token, method, payload=None):
    """Call one Telegram method and return its result."""
    request = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (token, method),
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    body = None
    for attempt in range(1, 4):  # slow or unstable internet: try up to 3 times
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                body = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:  # Telegram answered, but with an error
            try:
                body = json.loads(error.read().decode("utf-8"))
            except Exception:
                body = {"ok": False, "description": "HTTP error %s" % error.code}
            break
        except OSError:  # no answer at all: timeout, no internet, blocked
            if attempt < 3:
                print("Could not reach Telegram (try %d of 3). Trying again..." % attempt)
                time.sleep(4)
    if body is None:
        raise RuntimeError(
            "Could not reach Telegram from this computer. Your internet may be slow, "
            "or your network may be blocking api.telegram.org. Try again, or try a "
            "different network (for example your phone's hotspot)."
        )
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "Telegram said no"))
    return body["result"]


# ---------- settings and queue files ----------

def load_config():
    """Token and chat id come from environment variables, or from telegram_config.json."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if (not token or not chat_id) and CONFIG_FILE.exists():
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        token = token or str(saved.get("token", "")).strip()
        chat_id = chat_id or str(saved.get("chat_id", "")).strip()
    if not token or not chat_id:
        sys.exit("Telegram is not set up yet. Run:  python telegram_approval.py setup")
    return token, int(chat_id)


def load_queue():
    if QUEUE_FILE.exists():
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    return {"offset": 0, "items": []}


def save_queue(queue):
    QUEUE_FILE.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")


def caption_id(link):
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:8]


def answer_tap(token, tap_id, text):
    """Tell Telegram we saw the tap. Telegram only allows this for a few seconds after
    the tap, so when you collect later it fails. That is harmless, so we ignore it."""
    try:
        api(token, "answerCallbackQuery", {"callback_query_id": tap_id, "text": text})
    except RuntimeError:
        pass


def load_schedule():
    """Settings for the automatic 'veto window' mode, from the "schedule" part of voice.json."""
    try:
        voice = json.loads((BASE / "voice.json").read_text(encoding="utf-8-sig"))
    except Exception:
        voice = {}
    schedule = voice.get("schedule") or {}
    return {
        "mode": schedule.get("approval_mode", "tap"),  # "tap" = you approve, "veto" = it posts unless you reject
        "veto_hours": float(schedule.get("veto_hours", 2)),
        "offset": float(schedule.get("timezone_offset_hours", 0)),
    }


def message_text(item, footer=""):
    text = "%s\n\nStory: %s" % (item["caption"], item["link"])
    return text + ("\n\n" + footer if footer else "")


# ---------- the three jobs ----------

def do_setup():
    token = input("Paste your bot token from BotFather and press Enter: ").strip()
    try:
        bot = api(token, "getMe")
    except Exception as error:
        sys.exit("That token did not work (%s). Copy it again from BotFather." % error)
    print("Found your bot: @%s" % bot["username"])
    print("\nNow open Telegram, find @%s, press START, and send it the word: hi" % bot["username"])
    input("When you have done that, come back here and press Enter... ")

    updates = api(token, "getUpdates", {"timeout": 0})
    chats = {}
    for update in updates:
        message = update.get("message")
        if message and message["chat"].get("type") == "private":
            chats[message["chat"]["id"]] = message["chat"].get("first_name", "")
    if not chats:
        sys.exit("I did not see your message. Send 'hi' to the bot, then run setup again.")

    chat_id = list(chats)[-1]  # the most recent person who wrote to the bot
    CONFIG_FILE.write_text(
        json.dumps({"token": token, "chat_id": chat_id}), encoding="utf-8"
    )
    api(token, "sendMessage", {"chat_id": chat_id, "text": "Connected. I will send you posts to approve here."})
    print("\nDone! %s's chat is connected. Check Telegram: you should see a message." % (chats[chat_id] or "Your"))
    print("Saved to %s. Keep that file private." % CONFIG_FILE.name)


def do_send():
    token, chat_id = load_config()
    schedule = load_schedule()
    veto = schedule["mode"] == "veto"
    if not CAPTIONS_FILE.exists():
        sys.exit("captions.json not found. Run write_captions.py first.")
    captions = json.loads(CAPTIONS_FILE.read_text(encoding="utf-8"))
    queue = load_queue()
    known = {item["id"] for item in queue["items"]}

    sent = 0
    for caption in captions:
        cid = caption_id(caption["link"])
        if cid in known:
            continue  # already sent on an earlier run
        item = {
            "id": cid,
            "title": caption["title"],
            "source": caption["source"],
            "link": caption["link"],
            "caption": caption["caption"],
            "status": "sent",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        if veto:
            # Veto window: it posts by itself after a few hours unless you tap Reject.
            due = datetime.now(timezone.utc) + timedelta(hours=schedule["veto_hours"])
            item["post_after"] = due.isoformat()
            local = due + timedelta(hours=schedule["offset"])
            footer = "Posts automatically after about %s unless you tap Reject." % local.strftime("%H:%M on %d %b")
            buttons = [
                {"text": "❌ Reject", "callback_data": "r:" + cid},
                {"text": "⚡ Post now", "callback_data": "a:" + cid},
            ]
        else:
            footer = ""
            buttons = [
                {"text": "✅ Approve", "callback_data": "a:" + cid},
                {"text": "❌ Reject", "callback_data": "r:" + cid},
            ]
        result = api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": message_text(item, footer),
            "reply_markup": {"inline_keyboard": [buttons]},
        })
        item["message_id"] = result["message_id"]
        queue["items"].append(item)
        save_queue(queue)  # save after every message so nothing is sent twice
        sent += 1
        print("Sent: %s" % item["title"])
    print("\nSent %d new captions to Telegram." % sent)
    if not veto:
        print("Tap Approve or Reject on your phone, then run:  python telegram_approval.py collect")


def do_collect():
    token, chat_id = load_config()
    schedule = load_schedule()
    veto = schedule["mode"] == "veto"
    queue = load_queue()
    updates = api(token, "getUpdates", {
        "offset": queue["offset"], "timeout": 0, "allowed_updates": ["callback_query"],
    })

    for update in updates:
        queue["offset"] = max(queue["offset"], update["update_id"] + 1)
        tap = update.get("callback_query")
        if not tap:
            continue
        if tap["from"]["id"] != chat_id:  # only YOU can approve
            answer_tap(token, tap["id"], "Not allowed.")
            continue

        action, _, cid = (tap.get("data") or "").partition(":")
        item = next((i for i in queue["items"] if i["id"] == cid), None)
        if item is None or action not in ("a", "r"):
            answer_tap(token, tap["id"], "Unknown post.")
            continue
        if item["status"] != "sent":  # tapped twice, or changed their mind
            answer_tap(token, tap["id"], "Already %s." % item["status"])
            continue

        item["status"] = "approved" if action == "a" else "rejected"
        item["decided_at"] = datetime.now(timezone.utc).isoformat()
        if action == "a":
            label = "⚡ POSTING NOW" if veto else "✅ APPROVED"
        else:
            label = "❌ REJECTED"
        save_queue(queue)  # save the decision first, so a later hiccup cannot lose it
        answer_tap(token, tap["id"], label)
        try:  # remove the buttons and mark the message
            api(token, "editMessageText", {
                "chat_id": chat_id, "message_id": item["message_id"],
                "text": message_text(item, label),
            })
        except RuntimeError:
            pass
        print("%s: %s" % (item["status"].upper(), item["title"]))

    if veto:  # taps are handled first, so a Reject always beats the timer
        now = datetime.now(timezone.utc)
        for item in queue["items"]:
            if item["status"] != "sent" or not item.get("post_after"):
                continue
            if datetime.fromisoformat(item["post_after"]) > now:
                continue
            item["status"] = "approved"
            item["decided_at"] = now.isoformat()
            item["auto_released"] = True
            save_queue(queue)
            try:
                api(token, "editMessageText", {
                    "chat_id": chat_id, "message_id": item["message_id"],
                    "text": message_text(item, "⏰ Time is up. Posting automatically."),
                })
            except RuntimeError:
                pass
            print("AUTO-APPROVED (nobody rejected it): %s" % item["title"])

    save_queue(queue)
    counts = {}
    for item in queue["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    print("\nWaiting: %d | Approved (ready to post): %d | Rejected: %d" % (
        counts.get("sent", 0), counts.get("approved", 0), counts.get("rejected", 0)))


def main():
    jobs = {"setup": do_setup, "send": do_send, "collect": do_collect}
    if len(sys.argv) != 2 or sys.argv[1] not in jobs:
        sys.exit("Type one of:\n  python telegram_approval.py setup\n"
                 "  python telegram_approval.py send\n  python telegram_approval.py collect")
    try:
        jobs[sys.argv[1]]()
    except RuntimeError as error:
        sys.exit("\nProblem: %s" % error)


if __name__ == "__main__":
    main()
