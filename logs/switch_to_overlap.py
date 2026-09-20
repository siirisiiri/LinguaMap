#!/usr/bin/env python3
"""Wait for Russia.json, then restart the priority runner in overlap mode.

Does not kill the Russia PBF parse. Only switches after that file exists.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/aryandaga/Documents/LinguaMap")
RUSSIA = ROOT / "data" / "Russia.json"
LOG = ROOT / "logs" / "priority_pipeline.log"
DAEMONIZE = ROOT / "logs" / "daemonize_priority.py"


def say(message: str) -> None:
    line = f"[overlap-switch] {message}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def russia_ready() -> bool:
    try:
        return RUSSIA.exists() and RUSSIA.stat().st_size > 1000
    except OSError:
        return False


def pids(pattern: str) -> list[int]:
    result = subprocess.run(
        ["pgrep", "-f", pattern],
        capture_output=True,
        text=True,
    )
    out = []
    for line in (result.stdout or "").split():
        try:
            out.append(int(line))
        except ValueError:
            pass
    return out


def wait_for_russia_parse() -> None:
    say("waiting for data/Russia.json (will not interrupt the 4GB parse)")
    while not russia_ready():
        time.sleep(5)
    say(f"Russia.json is here ({RUSSIA.stat().st_size} bytes)")
    # If the fetch process is somehow still running, wait it out.
    while pids("osm_business_websites.py Russia"):
        say("Russia fetch process still exiting; waiting")
        time.sleep(2)


def stop_sequential_runner() -> None:
    patterns = [
        "logs/run_priority_countries.py",
        "crawl_languages.py --data /Users/aryandaga/Documents/LinguaMap/data/Russia.json",
    ]
    killed = []
    for pattern in patterns:
        for pid in pids(pattern):
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except ProcessLookupError:
                pass
    if killed:
        say(f"stopped sequential runner pids {killed}")
        time.sleep(3)
    still = pids("logs/run_priority_countries.py")
    for pid in still:
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def start_overlap_runner() -> None:
    say("starting overlap runner (Russia crawl || Chile fetch)")
    subprocess.Popen(
        [sys.executable, str(DAEMONIZE)],
        cwd=str(ROOT),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    say(f"overlap runner pids={pids('logs/run_priority_countries.py')}")


def main() -> None:
    os.chdir(ROOT)
    wait_for_russia_parse()
    stop_sequential_runner()
    start_overlap_runner()


if __name__ == "__main__":
    main()
