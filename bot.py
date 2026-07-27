#!/data/data/com.termux/files/usr/bin/python3.14
import sys
import os
import time
import subprocess
import signal
from pathlib import Path

# --- Configuration 🌸 ---
# Get the directory where this script is located
SCRIPT_DIR = Path(__file__).parent.resolve()

PYTHON_SCRIPT = SCRIPT_DIR / "libraries" / "bot_service.py"
TOKEN_FILE = SCRIPT_DIR / "auth" / "token.txt"
LOG_FILE = SCRIPT_DIR / "logs" / "bot.log"
SHUTDOWN_FLAG = SCRIPT_DIR / "libraries" / "shutdown.txt"

# Convert to strings for subprocess calls
PYTHON_SCRIPT_STR = str(PYTHON_SCRIPT)
LOG_FILE_STR = str(LOG_FILE)
SHUTDOWN_FLAG_STR = str(SHUTDOWN_FLAG)

# --- Bot Manager Logic ☁️ ---

def get_pid():
    try:
        if os.name == 'nt':
            cmd = f'wmic process where "commandline like \'%{PYTHON_SCRIPT_STR}%\'" get processid'
            output = subprocess.check_output(cmd, shell=True).decode()
            pids = [int(pid) for pid in output.split() if pid.isdigit()]
            return pids[0] if pids else None
        else:
            output = subprocess.check_output(["pgrep", "-f", PYTHON_SCRIPT_STR])
            return int(output.decode().strip().split('\n')[0])
    except:
        return None

def show_unified_help():
    """Prints the CLI menu! 🎀"""
    menu = (
        "\033[38;5;205m✨ --- ENCHANTED MASTER CLI --- ✨\033[0m\n"
        "🍓 Usage: python on.py [flag]\n\n"
        "\033[38;5;118m🚀 SERVICE MANAGEMENT:\033[0m\n"
        "  --on       Launch the bot into the strawberry clouds.\n"
        "  --off      Gracefully bring the bot offline.\n"
        "  --restart  Refresh the service (Off then On).\n"
        "  --status   Check if the bot is breathing.\n\n"
        "\033[38;5;205m🌸 End of Enchanted Menu\033[0m"
    )
    print(menu)


def start_bot():
    pid = get_pid()
    if pid:
        print(f" Already running! 😊👌 (PID: {pid})")
        return
    os.makedirs(LOG_FILE.parent, exist_ok=True)
    args = ["python", "-u", PYTHON_SCRIPT_STR]
    with open(LOG_FILE_STR, "a") as log:
        if os.name == 'nt':
            subprocess.Popen(args, stdout=log, stderr=log, creationflags=0x08000000)
        else:
            subprocess.Popen(args, stdout=log, stderr=log, preexec_fn=os.setpgrp)
    print(f"🚀 SUCCESS: Cutest Thing is ONLINE. 🌸")

def stop_bot():
    print("⏳ Going offline... ☁️")
    with open(SHUTDOWN_FLAG_STR, "w") as f: f.write("OFFLINE")
    time.sleep(2)
    pid = get_pid()
    if pid:
        if os.name == 'nt': subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else: os.kill(pid, signal.SIGTERM)
    if os.path.exists(SHUTDOWN_FLAG_STR): os.remove(SHUTDOWN_FLAG_STR)
    print("✅ SUCCESS: Bot is OFFLINE. 🍓🥛")

# --- Master Execution Logic 🏹 ---

if __name__ == "__main__":
    if len(sys.argv) < 2:
        show_unified_help()
        sys.exit(1)

    flag = sys.argv[1]

    if flag == "--on": start_bot()
    elif flag == "--off": stop_bot()
    elif flag == "--restart":
        stop_bot()
        time.sleep(1)
        start_bot()
    elif flag == "--status":
        pid = get_pid()
        print(f"Current Status: {'🌸 Running' if pid else '💤 Stopped'}")
    elif flag in ["--help", "-h"]:
        show_unified_help()
    else:
        print(f"❓ Unknown flag: {flag}")
        show_unified_help()
        sys.exit(1)
