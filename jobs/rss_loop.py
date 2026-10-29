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

from jobs.rss_monitor import run as rss_run

print(f"[rss_loop] started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')} "
      f"— {ITERATIONS} checks × {INTERVAL_SECONDS//60} min")

for i in range(ITERATIONS):
    result = rss_run(verbose=True)
    if i < ITERATIONS - 1:
        next_at = datetime.fromtimestamp(time.time() + INTERVAL_SECONDS).strftime("%H:%M:%S")
        print(f"[rss_loop] sleeping {INTERVAL_SECONDS//60} min — next check at {next_at}")
        time.sleep(INTERVAL_SECONDS)

print(f"[rss_loop] done at {datetime.now().strftime('%H:%M:%S')} — exiting")
