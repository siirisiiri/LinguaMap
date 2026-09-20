#!/usr/bin/env python3
"""Priority countries with parallel crawls and a prefetch slot.

Keeps at most two language crawls and one OSM fetch running at once.
Adopts already-running child processes so a restart does not duplicate work.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/Users/aryandaga/Documents/LinguaMap")
sys.path.insert(0, str(ROOT))

from run_world_pipeline import (  # noqa: E402
    PYTHON,
    dataset_path,
    fetch_queries,
    load_records,
    log,
    maybe_delete_large_pbf,
    unlabeled_count,
    write_status,
)

STATUS_PATH = ROOT / "logs" / "priority_status.json"
LOG_PATH = ROOT / "logs" / "priority_pipeline.log"
MAX_CRAWLS = 2
MAX_FETCHES = 1

COUNTRIES = [
    {
        "id": "russia",
        "name": "Russian Federation",
        "display": "Russia",
        "query": "Russia",
        "parent": "russia",
    },
    {
        "id": "chile",
        "name": "Chile",
        "display": "Chile",
        "query": "Chile",
        "parent": "south-america",
    },
    {
        "id": "peru",
        "name": "Peru",
        "display": "Peru",
        "query": "Peru",
        "parent": "south-america",
    },
    {
        "id": "colombia",
        "name": "Colombia",
        "display": "Colombia",
        "query": "Colombia",
        "parent": "south-america",
    },
    {
        "id": "argentina",
        "name": "Argentina",
        "display": "Argentina",
        "query": "Argentina",
        "parent": "south-america",
    },
    {
        "id": "mexico",
        "name": "Mexico",
        "display": "Mexico",
        "query": "Mexico",
        "parent": "north-america",
    },
    {
        "id": "brazil",
        "name": "Brazil",
        "display": "Brazil",
        "query": "Brazil",
        "parent": "south-america",
    },
    {
        "id": "canada",
        "name": "Canada",
        "display": "Canada",
        "query": "Canada",
        "parent": "north-america",
    },
]


def _pgrep(pattern: str) -> list[int]:
    result = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True
    )
    pids = []
    for line in (result.stdout or "").split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "state="],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    state = (result.stdout or "").strip()
    return bool(state) and not state.startswith("Z")


def _wait_exit(pid: int) -> int | None:
    try:
        waited, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return 0 if not _alive(pid) else None
    if waited == 0:
        return None if _alive(pid) else 0
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 0


class CountryJob:
    def __init__(self, extract: dict):
        self.extract = extract
        self.path = dataset_path(extract)
        self.fetch_pid: int | None = None
        self.crawl_pid: int | None = None
        self.fetch_done = False
        self.crawl_done = False
        self.result: str | None = None
        self.error: str | None = None

    @property
    def name(self) -> str:
        return self.extract["display"]

    def records_ready(self) -> bool:
        recs = load_records(self.path)
        return bool(recs)

    def unlabeled(self) -> int:
        recs = load_records(self.path)
        return unlabeled_count(recs) if recs else 0

    def needs_fetch(self) -> bool:
        return not self.fetch_done and self.fetch_pid is None and not self.records_ready()

    def needs_crawl(self) -> bool:
        if self.crawl_done or self.crawl_pid is not None:
            return False
        recs = load_records(self.path)
        if not recs:
            return False
        n = len(recs)
        u = unlabeled_count(recs)
        if u == 0:
            self.crawl_done = True
            self.fetch_done = True
            self.result = "complete"
            return False
        labeled_frac = (n - u) / n
        # A finished crawl still leaves fetch-failed URLs unlabeled. Don't
        # occupy a slot retrying those unless almost nothing was labeled.
        if labeled_frac >= 0.15:
            self.crawl_done = True
            self.fetch_done = True
            self.result = "crawled"
            return False
        return True

    def fetch_cmd(self) -> list[str]:
        return [
            PYTHON,
            str(ROOT / "osm_business_websites.py"),
            self.extract["query"],
            "-o",
            str(self.path),
            "--no-clip",
        ]

    def crawl_cmd(self) -> list[str]:
        return [
            PYTHON,
            str(ROOT / "crawl_languages.py"),
            "--data",
            str(self.path),
        ]

    def adopt(self) -> None:
        if self.records_ready():
            self.fetch_done = True
            maybe_delete_large_pbf(self.extract)
        for pid in _pgrep(f"osm_business_websites.py {self.extract['query']} "):
            self.fetch_pid = pid
            log(f"{self.name}: adopting in-flight fetch pid={pid}")
            break
        for pid in _pgrep(f"crawl_languages.py --data {self.path}"):
            self.crawl_pid = pid
            log(f"{self.name}: adopting in-flight crawl pid={pid}")
            break
        if self.records_ready() and self.unlabeled() == 0:
            self.crawl_done = True
            self.result = "complete"


def _spawn(cmd: list[str]) -> int:
    log_fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        start_new_session=True,
        stdout=log_fd,
        stderr=log_fd,
    )
    os.close(log_fd)
    return proc.pid


def _poll_job(job: CountryJob) -> None:
    if job.fetch_pid and not _alive(job.fetch_pid):
        code = _wait_exit(job.fetch_pid)
        job.fetch_pid = None
        if job.records_ready():
            job.fetch_done = True
            maybe_delete_large_pbf(job.extract)
            log(f"{job.name}: OSM fetch finished ({job.unlabeled()} unlabeled)")
        else:
            job.error = f"fetch exited {code}"
            log(f"{job.name}: OSM fetch failed ({job.error})")
    if job.crawl_pid and not _alive(job.crawl_pid):
        code = _wait_exit(job.crawl_pid)
        job.crawl_pid = None
        leftover = job.unlabeled() if job.records_ready() else -1
        if leftover == 0 or (job.records_ready() and code == 0):
            job.crawl_done = True
            job.fetch_done = True
            job.result = "crawled"
            log(f"{job.name}: language crawl finished")
        else:
            job.error = f"crawl exited {code}, unlabeled={leftover}"
            log(f"{job.name}: language crawl failed ({job.error})")


def _snapshot(jobs: list[CountryJob], status: dict) -> None:
    crawling = [j.name for j in jobs if j.crawl_pid]
    fetching = [j.name for j in jobs if j.fetch_pid]
    status["current"] = ", ".join(crawling + fetching) or None
    status["phase"] = (
        f"crawls={crawling or ['-']} fetches={fetching or ['-']}"
    )
    status["completed"] = [
        {"id": j.extract["id"], "name": j.name, "result": j.result or "done"}
        for j in jobs
        if j.crawl_done and not j.error
    ]
    status["failed"] = [
        {"id": j.extract["id"], "name": j.name, "error": j.error}
        for j in jobs
        if j.error
    ]
    write_status(status, STATUS_PATH)


def main() -> None:
    import run_world_pipeline as pipeline

    pipeline.STATUS_PATH = STATUS_PATH
    status = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "overlap": True,
        "max_crawls": MAX_CRAWLS,
        "max_fetches": MAX_FETCHES,
        "queue": [c["display"] for c in COUNTRIES],
        "completed": [],
        "failed": [],
        "current": None,
    }
    write_status(status, STATUS_PATH)
    jobs = [CountryJob(c) for c in COUNTRIES]
    for job in jobs:
        job.adopt()
    log(
        f"Priority parallel run: {len(COUNTRIES)} countries, "
        f"{MAX_CRAWLS} crawls + {MAX_FETCHES} fetch at a time"
    )

    while True:
        for job in jobs:
            _poll_job(job)

        fetch_slots = MAX_FETCHES - sum(1 for j in jobs if j.fetch_pid)
        crawl_slots = MAX_CRAWLS - sum(1 for j in jobs if j.crawl_pid)

        for job in jobs:
            if fetch_slots <= 0:
                break
            if not job.needs_fetch() or job.error:
                continue
            query = fetch_queries(job.extract)[0]
            job.extract["query"] = query
            log(f"{job.name}: starting OSM fetch")
            job.fetch_pid = _spawn(job.fetch_cmd())
            fetch_slots -= 1

        for job in jobs:
            if crawl_slots <= 0:
                break
            if not job.needs_crawl() or job.error:
                continue
            log(f"{job.name}: starting language crawl ({job.unlabeled()} unlabeled)")
            job.crawl_pid = _spawn(job.crawl_cmd())
            crawl_slots -= 1

        _snapshot(jobs, status)
        unfinished = [
            j for j in jobs if not j.crawl_done and not j.error
        ]
        if not unfinished:
            break
        stuck = [
            j for j in unfinished
            if not j.fetch_pid and not j.crawl_pid and not j.needs_fetch() and not j.needs_crawl()
        ]
        if stuck and not any(j.fetch_pid or j.crawl_pid for j in jobs):
            for j in stuck:
                j.error = j.error or "stalled"
            break
        time.sleep(2)

    failed = [j for j in jobs if j.error]
    if failed:
        log(f"Retrying {len(failed)} failed countries once more")
        for job in failed:
            job.error = None
            job.fetch_pid = None
            job.crawl_pid = None
            if job.records_ready():
                job.fetch_done = True
                job.crawl_done = False
            else:
                job.fetch_done = False
                job.crawl_done = False
        while True:
            for job in jobs:
                _poll_job(job)
            fetch_slots = MAX_FETCHES - sum(1 for j in jobs if j.fetch_pid)
            crawl_slots = MAX_CRAWLS - sum(1 for j in jobs if j.crawl_pid)
            for job in jobs:
                if job.error or job.crawl_done:
                    continue
                if fetch_slots and job.needs_fetch():
                    log(f"{job.name}: retry OSM fetch")
                    job.fetch_pid = _spawn(job.fetch_cmd())
                    fetch_slots -= 1
                elif crawl_slots and job.needs_crawl():
                    log(f"{job.name}: retry language crawl")
                    job.crawl_pid = _spawn(job.crawl_cmd())
                    crawl_slots -= 1
            _snapshot(jobs, status)
            if all(j.crawl_done or j.error for j in jobs):
                if not any(j.fetch_pid or j.crawl_pid for j in jobs):
                    break
            time.sleep(2)

    status["finished_at"] = datetime.now(timezone.utc).isoformat()
    _snapshot(jobs, status)
    log(
        f"Priority run finished. completed={len(status['completed'])} "
        f"failed={len(status['failed'])}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
