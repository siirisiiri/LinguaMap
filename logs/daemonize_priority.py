#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path("/Users/aryandaga/Documents/LinguaMap")
LOG = ROOT / "logs" / "priority_pipeline.log"
PIDFILE = ROOT / "logs" / "priority.pid"
SCRIPT = ROOT / "logs" / "run_priority_countries.py"


def daemonize() -> None:
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.umask(0)
    os.chdir(ROOT)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)


def main() -> None:
    daemonize()
    PIDFILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    log_fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.execv(
        "/usr/bin/caffeinate",
        [
            "caffeinate",
            "-dims",
            sys.executable,
            str(SCRIPT),
        ],
    )


if __name__ == "__main__":
    main()
