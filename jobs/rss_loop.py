#!/usr/bin/env python3
"""
Runs the RSS monitor every 5 minutes for ~55 minutes (11 iterations).
Designed to be launched by the hourly scheduled task — one process covers
the full hour, checking every 5 minutes from within.

Usage:
    python jobs/rss_loop.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.expanduser("~/irtteja/nselib"))

import time
from datetime import datetime

INTERVAL_SECONDS = 300   # 5 minutes
ITERATIONS       = 11    # 11 × 5 min = 55 min (leaves 5 min buffer before next hourly trigger)

import subprocess

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from jobs.rss_monitor import run as rss_run


def _git_commit_state():
    """Commit rss_state + rss_feed to GitHub so the next container picks up where we left off."""
    try:
        subprocess.run(["git", "add",
                        "memory_store/rss_state/",
                        "memory_store/rss_feed/"],
                       cwd=REPO_DIR, check=True, capture_output=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                cwd=REPO_DIR, capture_output=True)
        if result.returncode != 0:  # there are staged changes
            subprocess.run(["git", "commit", "-m",
                            f"chore: rss state + feed {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
                           cwd=REPO_DIR, check=True, capture_output=True)
            subprocess.run(["git", "push", "-u", "origin", "master"],
                           cwd=REPO_DIR, check=True, capture_output=True)
            print("[rss_loop] state committed to GitHub")
    except Exception as e:
        print(f"[rss_loop] git commit failed (state may reset next run): {e}")


print(f"[rss_loop] started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')} "
      f"— {ITERATIONS} checks × {INTERVAL_SECONDS//60} min")

total_new = 0
for i in range(ITERATIONS):
    result = rss_run(verbose=True)
    total_new += result.get("new", 0)
    if i < ITERATIONS - 1:
        next_at = datetime.fromtimestamp(time.time() + INTERVAL_SECONDS).strftime("%H:%M:%S")
        print(f"[rss_loop] sleeping {INTERVAL_SECONDS//60} min — next check at {next_at}")
        time.sleep(INTERVAL_SECONDS)

# Persist state + feed log to GitHub before container is recycled
_git_commit_state()

print(f"[rss_loop] done at {datetime.now().strftime('%H:%M:%S')} — "
      f"{total_new} new announcements this hour — exiting")
