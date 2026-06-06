#!/usr/bin/env python3
"""DEPRECATED — use run.sh instead.

This script no longer performs ad removal and is kept only to prevent
silent failures if it was previously scheduled. Remove it from any cron
or launchd job and replace with:

    ./run.sh

"""
import sys

print(
    "\n"
    "╔══════════════════════════════════════════════════════════════╗\n"
    "║  DEPRECATED: sync_podcast.py does not remove ads.           ║\n"
    "║  Use run.sh instead — it is the correct entry point.        ║\n"
    "║                                                              ║\n"
    "║  If this is running on a schedule, update your cron/launchd ║\n"
    "║  job to call ./run.sh instead of ./sync_podcast.py          ║\n"
    "╚══════════════════════════════════════════════════════════════╝\n",
    file=sys.stderr,
)
sys.exit(1)
