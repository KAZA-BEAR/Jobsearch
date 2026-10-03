#!/usr/bin/env python3
"""
linkedin_jobs.py — the shared job model, SQLite store and HTTP helpers that
every source module builds on, plus export and stats for the stored jobs.

LinkedIn and Indeed themselves are searched through JobSpy (jobspy_provider.py);
the paid LinkedIn APIs this module used to wrap have been removed.

Usage:
    python linkedin_jobs.py export jobs.csv
    python linkedin_jobs.py stats

Requires: pip install requests
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import requests

DB_PATH = os.environ.get("JOBS_DB", "jobs.db")


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class Job:
    job_id: str
    title: str
    company: str
    location: str = ""
    url: str = ""
    posted_at: str = ""
    employment_type: str = ""
    remote: bool = False
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = ""
    description: str = ""
    source: str = ""
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    fit_score: int | None = None
    # Set once on first insert and never overwritten by later sweeps, so
    # "new since X" and the 24h filter survive re-fetching the same posting.
    first_seen: str = ""
    # Application tracker. Owned by the user, never touched by a sweep.
    status: str = ""          # "" | saved | applied | interview | offer | rejected | withdrawn
    notes: str = ""
    applied_at: str = ""
    follow_up: str = ""

    @staticmethod
    def make_id(company: str, title: str, location: str) -> str:
        raw = f"{company}|{title}|{location}".lower().strip()
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Job":
        keys = r.keys()
        kw = {k: r[k] for k in cls.__dataclass_fields__ if k in keys}
        for k, v in list(kw.items()):
            if v is None and k not in ("salary_min", "salary_max", "fit_score"):
                kw[k] = ""
        kw["remote"] = bool(kw.get("remote"))
        return cls(**kw)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    company          TEXT NOT NULL,
    location         TEXT,
    url              TEXT,
    posted_at        TEXT,
    employment_type  TEXT,
    remote           INTEGER,
    salary_min       REAL,
    salary_max       REAL,
    salary_currency  TEXT,
    description      TEXT,
    source           TEXT,
    fetched_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_posted  ON jobs(posted_at);
"""

# Columns added after the first release. Added with ALTER TABLE so existing
# databases upgrade in place instead of needing to be deleted.
MIGRATIONS = (
    ("fit_score", "INTEGER"),
    ("first_seen", "TEXT"),
    ("status", "TEXT DEFAULT ''"),
    ("notes", "TEXT DEFAULT ''"),
    ("applied_at", "TEXT DEFAULT ''"),
    ("follow_up", "TEXT DEFAULT ''"),
)

# Fields a sweep may overwrite on a posting it has already stored. Anything not
# listed here (first_seen and the tracker fields) is preserved.
SCRAPED_FIELDS = (
    "title", "company", "location", "url", "posted_at", "employment_type",
    "remote", "salary_min", "salary_max", "salary_currency", "description",
    "source", "fetched_at", "fit_score",
)

STATUSES = ("saved", "applied", "interview", "offer", "rejected", "withdrawn")


class Store:
    """SQLite-backed job store.

    The GUI creates this on the main thread but searches run on worker
    threads, so the connection is opened with check_same_thread=False and
    every statement is serialised through a lock.

    If `scorer` is set (a callable Job -> int), every upserted job gets a
    fit_score, so all providers are ranked without each one knowing about it.
    """

    def __init__(self, path: str = DB_PATH, scorer=None):
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.Lock()
        self.scorer = scorer
        with self.lock:
            self.conn.executescript(SCHEMA)
            have = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
            for col, decl in MIGRATIONS:
                if col not in have:
                    self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
            self.conn.execute(
                "UPDATE jobs SET first_seen = fetched_at WHERE first_seen IS NULL OR first_seen = ''")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON jobs(status)")
            self.conn.commit()

    def upsert(self, job: Job) -> bool:
        """Insert, or refresh the scraped fields of a known posting.

        Returns True if the row was new. first_seen and the tracker fields
        (status, notes, applied_at, follow_up) are never overwritten here.
        """
        if self.scorer is not None:
            try:
                job.fit_score = self.scorer(job)
            except Exception as exc:  # a scoring bug must never lose a job
                print(f"  fit score failed for {job.job_id}: {exc}", file=sys.stderr)
        if not job.first_seen:
            job.first_seen = job.fetched_at
        d = asdict(job)
        d["remote"] = int(d["remote"])
        cols = ", ".join(d)
        placeholders = ", ".join(f":{k}" for k in d)
        # A search-listing refetch often carries only a snippet; keep the full
        # description (and the score computed from it) if we already have one.
        keep_longer = ("CASE WHEN length(coalesce(excluded.description, '')) >= "
                       "length(coalesce(jobs.description, '')) THEN excluded.{k} ELSE jobs.{k} END")
        updates = ", ".join(
            f"{k}={keep_longer.format(k=k)}" if k in ("description", "fit_score")
            else f"{k}=excluded.{k}" for k in SCRAPED_FIELDS)
        with self.lock:
            cur = self.conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job.job_id,))
            is_new = cur.fetchone() is None
            self.conn.execute(
                f"INSERT INTO jobs ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(job_id) DO UPDATE SET {updates}", d
            )
            self.conn.commit()
        return is_new

    def all(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM jobs ORDER BY posted_at DESC"
            ).fetchall()

    def get(self, job_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    # ---- application tracker ----

    def set_status(self, job_id: str, status: str) -> None:
        """Set a tracker status. Marking 'applied' stamps today's date and a
        7-day follow-up reminder unless those were already set."""
        today = datetime.now().date()
        with self.lock:
            self.conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id))
            if status == "applied":
                self.conn.execute(
                    "UPDATE jobs SET applied_at = ? WHERE job_id = ? "
                    "AND (applied_at IS NULL OR applied_at = '')",
                    (today.isoformat(), job_id))
                self.conn.execute(
                    "UPDATE jobs SET follow_up = ? WHERE job_id = ? "
                    "AND (follow_up IS NULL OR follow_up = '')",
                    ((today + timedelta(days=7)).isoformat(), job_id))
            elif status in ("offer", "rejected", "withdrawn"):
                self.conn.execute("UPDATE jobs SET follow_up = '' WHERE job_id = ?", (job_id,))
            self.conn.commit()

    def set_tracking(self, job_id: str, *, notes: str | None = None,
                     follow_up: str | None = None, applied_at: str | None = None) -> None:
        sets, args = [], []
        for col, val in (("notes", notes), ("follow_up", follow_up), ("applied_at", applied_at)):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(val)
        if not sets:
            return
        with self.lock:
            self.conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?",
                              (*args, job_id))
            self.conn.commit()

    def tracked(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM jobs WHERE status IS NOT NULL AND status != '' "
                "ORDER BY CASE WHEN follow_up IS NULL OR follow_up = '' THEN 1 ELSE 0 END, "
                "follow_up, applied_at DESC"
            ).fetchall()

    def seen_since(self, iso_ts: str) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM jobs WHERE first_seen >= ? ORDER BY fit_score DESC",
                (iso_ts,)).fetchall()

    def clear(self) -> int:
        """Delete untracked jobs. Jobs with a tracker status are kept, so
        clearing search results never loses your application history."""
        with self.lock:
            n = self.conn.execute(
                "DELETE FROM jobs WHERE status IS NULL OR status = ''").rowcount
            self.conn.commit()
        return n

    def stats(self) -> dict:
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            companies = self.conn.execute(
                "SELECT company, COUNT(*) c FROM jobs GROUP BY company ORDER BY c DESC LIMIT 10"
            ).fetchall()
            remote = self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE remote = 1"
            ).fetchone()[0]
        return {
            "total": total,
            "remote": remote,
            "top_companies": [(r["company"], r["c"]) for r in companies],
        }


# --------------------------------------------------------------------------
# Dates + paths
# --------------------------------------------------------------------------

_RELATIVE_DAYS = re.compile(r"(\d+)\+?\s*(day|tag)", re.IGNORECASE)


def parse_posted(value) -> datetime | None:
    """Best-effort parse of the many posted-date formats providers return.

    Handles ISO timestamps, bare dates, epoch seconds/milliseconds (Lever),
    RFC 822 (RSS feeds) and Workday's relative "Posted 3 Days Ago".
    Returns an aware UTC datetime, or None if the value is unusable.
    """
    if value is None or value == "":
        return None
    now = datetime.now(timezone.utc)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        if n > 1e11:          # milliseconds
            n /= 1000
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    s = str(value).strip()
    low = s.lower()
    if "today" in low or "heute" in low or "just posted" in low:
        return now
    if "yesterday" in low or "gestern" in low:
        return now - timedelta(days=1)
    m = _RELATIVE_DAYS.search(low)
    if m and ("ago" in low or "vor" in low or "posted" in low):
        return now - timedelta(days=int(m.group(1)))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def age_hours(job: Job) -> float | None:
    """Hours since the job was posted, falling back to when we first saw it.

    A date-only posted_at ("2026-09-22") is treated as the end of that day,
    so a posting dated yesterday still counts as under 24h old.
    """
    dt = parse_posted(job.posted_at)
    if dt is not None and len(str(job.posted_at).strip()) == 10:
        dt = dt + timedelta(hours=23, minutes=59)
    if dt is None:
        dt = parse_posted(job.first_seen or job.fetched_at)
    if dt is None:
        return None
    return max((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 0.0)


def data_dir() -> Path:
    """Writable location for the database, sweep config and logs.

    A frozen .exe may sit in Program Files, which is read-only for normal
    users, so store data under %APPDATA% (or the XDG equivalent) instead.
    JOBSEARCH_DATA_DIR overrides both (handy for testing on a scratch copy).
    """
    if os.environ.get("JOBSEARCH_DATA_DIR"):
        path = Path(os.environ["JOBSEARCH_DATA_DIR"])
    elif getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME")
        if not base:
            base = str(Path.home() / ".local" / "share")
        path = Path(base) / "EUJobSearch"
    else:
        path = Path(__file__).resolve().parent
    path.mkdir(parents=True, exist_ok=True)
    return path


def outputs_dir() -> Path:
    """Where generated CVs and letters go: ats_outputs in the project folder, not in
    %APPDATA% with the database. The .exe is built into dist\\, so its project
    folder is the one above; an .exe copied elsewhere uses its own folder."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        base = exe_dir.parent if exe_dir.name.lower() == "dist" else exe_dir
    else:
        base = Path(__file__).resolve().parent
    path = base / "ats_outputs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    """settings.json in the data folder: remembered profile, API keys, SEC email."""
    return data_dir() / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply_saved_api_keys() -> None:
    """Copy API keys saved in the app into the environment, where the providers
    read them. Called by the app and by the daily sweep, which the scheduled
    task runs without the app. A key saved in the app wins over an environment
    variable of the same name, being the more recent choice."""
    for name, value in (load_settings().get("api_keys") or {}).items():
        if value:
            os.environ[name] = value


# --------------------------------------------------------------------------
# Rate limiting + retries
# --------------------------------------------------------------------------

class RateLimiter:
    """Spaces calls at least `interval` apart, safely across threads.

    Each caller reserves the next free slot under the lock, then sleeps
    outside it, so concurrent workers queue up instead of all firing at once.
    """

    def __init__(self, calls_per_minute: int = 20):
        self.interval = 60.0 / max(calls_per_minute, 1)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(self._next, now)
            self._next = slot + self.interval
        if slot > now:
            time.sleep(slot - now)


def request_with_retry(
    session: requests.Session, method: str, url: str, *, tries: int = 4, **kwargs
) -> requests.Response:
    backoff = 1.5
    for attempt in range(1, tries + 1):
        try:
            resp = session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            if attempt == tries:
                raise
            print(f"  network error ({exc}); retrying in {backoff:.0f}s", file=sys.stderr)
        else:
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", backoff))
                print(f"  rate limited; sleeping {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
            elif 500 <= resp.status_code < 600:
                print(f"  server error {resp.status_code}; retrying", file=sys.stderr)
            else:
                resp.raise_for_status()
                return resp
        time.sleep(backoff)
        backoff *= 2
    raise RuntimeError(f"giving up on {url} after {tries} attempts")


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

class Provider:
    name = "base"

    def search(self, query: str, location: str, pages: int, remote: bool) -> Iterator[Job]:
        raise NotImplementedError


# Source modules register their providers here (eures, phd, jobspy, …).
PROVIDERS: dict[str, type[Provider]] = {}


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_export(args) -> None:
    rows = Store(args.db).all()
    if not rows:
        print("nothing to export")
        return
    if args.path.endswith(".json"):
        with open(args.path, "w", encoding="utf-8") as fh:
            json.dump([dict(r) for r in rows], fh, indent=2, ensure_ascii=False)
    else:
        with open(args.path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(dict(r) for r in rows)
    print(f"wrote {len(rows)} rows to {args.path}")


def cmd_stats(args) -> None:
    s = Store(args.db).stats()
    print(f"total jobs : {s['total']}")
    print(f"remote     : {s['remote']}")
    print("top companies:")
    for company, count in s["top_companies"]:
        print(f"  {count:>4}  {company}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DB_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="dump to .csv or .json")
    e.add_argument("path")
    e.set_defaults(func=cmd_export)

    t = sub.add_parser("stats", help="summarise stored jobs")
    t.set_defaults(func=cmd_stats)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
