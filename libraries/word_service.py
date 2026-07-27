"""
🌸 word_service.py
Auto-reply service — reads trigger words from libraries/detect/*.txt
Called by bot_service.py via subprocess.run()

Input  (stdin): JSON → {"content": "...", "author": "...", "author_id": ...}
Output (stdout): JSON → {"match": true, "reply": "..."} or {"match": false}

Trigger file format (one rule per line):
    trigger|reply
    hello|Hi there! 👋
    good morning|Good morning! 🌸 Have a great day!
"""

import sys
import json
import os
import glob

DETECT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "detect")


def load_triggers() -> dict[str, str]:
    """
    Read all *.txt files from libraries/detect/ and build a trigger→reply map.
    Later files override earlier ones if the same trigger appears twice.
    """
    triggers = {}

    if not os.path.isdir(DETECT_DIR):
        return triggers

    for filepath in sorted(glob.glob(os.path.join(DETECT_DIR, "*.txt"))):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue  # skip blanks & comments
                    if "|" not in line:
                        continue  # skip malformed lines
                    trigger, _, reply = line.partition("|")
                    trigger = trigger.strip().lower()
                    reply   = reply.strip()
                    if trigger and reply:
                        triggers[trigger] = reply
        except Exception as e:
            # Don't crash — just skip bad files
            print(json.dumps({"match": False, "error": str(e)}), flush=True)

    return triggers


def check_message(content: str, triggers: dict[str, str]) -> str | None:
    """
    Check if any trigger word/phrase appears in the message content.
    Returns the reply string or None if no match.
    Case-insensitive, matches anywhere in the message.
    """
    content_lower = content.lower()
    for trigger, reply in triggers.items():
        if trigger in content_lower:
            return reply
    return None


def main():
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            print(json.dumps({"match": False}), flush=True)
            return

        data    = json.loads(raw)
        content = str(data.get("content", "")).strip()

        if not content:
            print(json.dumps({"match": False}), flush=True)
            return

        triggers = load_triggers()
        reply    = check_message(content, triggers)

        if reply:
            print(json.dumps({"match": True, "reply": reply}), flush=True)
        else:
            print(json.dumps({"match": False}), flush=True)

    except json.JSONDecodeError:
        print(json.dumps({"match": False, "error": "Invalid JSON input"}), flush=True)
    except Exception as e:
        print(json.dumps({"match": False, "error": str(e)}), flush=True)


if __name__ == "__main__":
    main()
