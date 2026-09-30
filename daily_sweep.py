#!/usr/bin/env python3
"""
daily_sweep.py — run your saved searches once a day and report what's new.

Applying in the first 24-48 hours after a posting goes live measurably raises
response rates, so the sweep is built around "what appeared since yesterday":

  * runs the sources and queries in sweep_config.json (created with sensible
    defaults on first run; edit it, or use the Daily sweep tab in the app)
  * limited to ONE run per calendar day. A second call the same day is a
    no-op unless --force is given, so the scheduled task, the app's
    run-on-start option and a manual click can never hammer the job sites
  * every job is fit-scored against profile.json; the summary lists new jobs
    at or above min_fit, and a Windows notification is shown when any are found

Run it:
    python daily_sweep.py                 # respects the once-per-day limit
    python daily_sweep.py --force         # run again today anyway
    python daily_sweep.py --install 09:00 # register a daily Windows task
    python daily_sweep.py --uninstall
    EUJobSearch.exe --sweep               # same thing from the built .exe
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from linkedin_jobs import Job, Store, data_dir

CONFIG_PATH = data_dir() / "sweep_config.json"
STATE_PATH = data_dir() / "sweep_state.json"
LOG_PATH = data_dir() / "sweep.log"
DB_PATH = data_dir() / "robotics_jobs.db"
TASK_NAME = "EUJobSearch Daily Sweep"

DEFAULT_CONFIG = {
    "min_fit": 50,
    "notify": True,
    "run_on_app_start": True,
    "sources": {
        "ba": True,
        "personio": True,
        "workday": True,
        "research": True,
        "boards": True,
        "eures": False,
    },
    "ba": {
        # where "" = nationwide
        "searches": [
            {"query": "Werkstudent Robotik", "where": "Straubing", "radius": 150},
            {"query": "Werkstudent Mechatronik", "where": "Straubing", "radius": 150},
            {"query": "Praktikum Robotik", "where": "Straubing", "radius": 150},
            {"query": "Masterarbeit Robotik", "where": "", "radius": 0},
            {"query": "Werkstudent ROS", "where": "", "radius": 0},
            {"query": "Junior Robotik Ingenieur", "where": "München", "radius": 50},
        ],
        "days": 2,
    },
    "workday": {"queries": ["Werkstudent", "working student robotics", "Praktikum"],
                "country": "Germany"},
    "research": {"sources": ["fraunhofer", "dlr", "mpg", "thd"],
                 "keywords": ["robotik", "mechatronik", "autonom"]},
    "eures": {"countries": ["DE"], "field": "robotics"},
}


# --------------------------------------------------------------------------
# Config + once-per-day gate
# --------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except ValueError:
        cfg = {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def ran_today(state: dict | None = None) -> bool:
    state = load_state() if state is None else state
    return state.get("last_run_date") == datetime.now().date().isoformat()


def _claim_run(force: bool) -> tuple[bool, str]:
    """Take the once-per-day slot. Returns (ok, reason_if_not)."""
    state = load_state()
    if state.get("running_since"):
        started = datetime.fromisoformat(state["running_since"])
        if datetime.now() - started < timedelta(hours=2):
            return False, f"a sweep is already running (started {started:%H:%M})"
    if ran_today(state) and not force:
        return False, f"already ran today at {state.get('last_run_at', '?')[11:16]}"
    state["running_since"] = datetime.now().isoformat(timespec="seconds")
    _save_state(state)
    return True, ""


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

def run_sweep(force: bool = False, log=print, cancelled: threading.Event | None = None,
              on_job=None, store: Store | None = None) -> dict | None:
    """Run every enabled source once. Returns a summary, or None if gated."""
    ok, why = _claim_run(force)
    if not ok:
        log(f"[sweep] skipped: {why}. Use force to run again.")
        return None

    cfg = load_config()
    if store is None:
        from fit_score import load_default_scorer
        scorer = load_default_scorer()
        store = Store(str(DB_PATH), scorer=scorer.score if scorer else None)
    started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts: dict[str, dict] = {}
    stop = cancelled or threading.Event()

    def cached(job_id: str) -> str | None:
        r = store.get(job_id)
        return r["description"] if r is not None and len(r["description"] or "") > 200 else None

    def keep(source: str, jobs: list[Job]) -> None:
        c = counts.setdefault(source, {"seen": 0, "new": 0, "errors": 0})
        for j in jobs:
            c["seen"] += 1
            if store.upsert(j):
                c["new"] += 1
                if on_job:
                    on_job(j)

    def fail(source: str, what: str, exc: Exception) -> None:
        counts.setdefault(source, {"seen": 0, "new": 0, "errors": 0})["errors"] += 1
        log(f"[sweep] {source} {what}: FAILED {exc}")

    src = cfg["sources"]
    try:
        if src.get("ba") and not stop.is_set():
            from ba_jobsuche import BAJobsucheProvider
            ba = BAJobsucheProvider()
            for s in cfg["ba"]["searches"]:
                if stop.is_set():
                    break
                try:
                    keep("ba", ba.search(s["query"], s.get("where", ""), s.get("radius", 50),
                                         days=cfg["ba"].get("days", 2), pages=2,
                                         on_log=log, cancelled=stop, cached=cached))
                except Exception as exc:
                    fail("ba", s["query"], exc)

        if src.get("personio") and not stop.is_set():
            from personio_jobs import COMPANIES, PersonioFeeds
            feeds = PersonioFeeds()
            for c in COMPANIES + tuple(_custom_personio(cfg)):
                if stop.is_set():
                    break
                try:
                    keep("personio", feeds.fetch(c) or [])
                except Exception as exc:
                    fail("personio", c.company, exc)
            log(f"[sweep] personio: {counts.get('personio', {}).get('seen', 0)} roles")

        if src.get("workday") and not stop.is_set():
            from workday_jobs import SITES, WorkdayBoards, parse_url
            wb = WorkdayBoards()
            sites = list(SITES) + [parse_url(u) for u in cfg["workday"].get("extra_sites", [])]
            for site in sites:
                for q in cfg["workday"]["queries"]:
                    if stop.is_set():
                        break
                    try:
                        keep("workday", wb.search(site, q, cfg["workday"].get("country", ""),
                                                  max_jobs=60, details=20, on_log=log,
                                                  cancelled=stop, cached=cached))
                    except Exception as exc:
                        fail("workday", f"{site.company} '{q}'", exc)

        if src.get("research") and not stop.is_set():
            from research_jobs import ResearchFeeds
            rf = ResearchFeeds()
            for source in cfg["research"]["sources"]:
                # MPG and THD feeds aren't keyword-searchable server-side; fetch once.
                kws = cfg["research"]["keywords"] if source in ("fraunhofer", "dlr") else [
                    " ".join(cfg["research"]["keywords"])]
                for kw in kws:
                    if stop.is_set():
                        break
                    try:
                        keep("research", rf.search(source, kw, on_log=log))
                    except Exception as exc:
                        fail("research", f"{source} '{kw}'", exc)

        if src.get("boards") and not stop.is_set():
            from robotics_track import BOARDS, ATSBoards, _grad_keep
            fetcher = ATSBoards()
            kept: list[Job] = []
            for b in BOARDS:
                if stop.is_set():
                    break
                try:
                    for job in fetcher.fetch(b):
                        ok_, is_grad = _grad_keep(job, False)
                        if ok_:
                            job.employment_type = "graduate" if is_grad else "entry-candidate"
                            kept.append(job)
                except Exception as exc:
                    fail("boards", b.company, exc)
            keep("boards", kept)
            log(f"[sweep] employer boards: {len(kept)} relevant roles")

        if src.get("eures") and not stop.is_set():
            from eu_student_jobs import EuresProvider, is_student_suitable
            ep = EuresProvider()
            for code in cfg["eures"]["countries"]:
                try:
                    jobs = ep.search(cfg["eures"]["field"], "", 1, False, country=code, on_log=log)
                    keep("eures", [j for j in jobs if is_student_suitable(j)[0]])
                except Exception as exc:
                    fail("eures", code, exc)
    finally:
        new_rows = store.seen_since(started_utc)
        min_fit = int(cfg.get("min_fit", 50))
        top = [dict(title=r["title"], company=r["company"], location=r["location"],
                    fit=r["fit_score"], url=r["url"])
               for r in new_rows if (r["fit_score"] or 0) >= min_fit][:25]
        state = load_state()
        state.pop("running_since", None)
        summary = {
            "started_utc": started_utc,
            "finished": datetime.now().isoformat(timespec="seconds"),
            "cancelled": stop.is_set(),
            "by_source": counts,
            "new_total": len(new_rows),
            "new_good_fit": len(top),
            "top": top,
        }
        state.update(last_run_date=datetime.now().date().isoformat(),
                     last_run_at=summary["finished"], last_summary=summary)
        _save_state(state)

    log(f"[sweep] done: {summary['new_total']} new jobs, "
        f"{summary['new_good_fit']} with fit >= {min_fit}")
    for t in top[:10]:
        log(f"   [{t['fit']:>3}] {t['title']} — {t['company']}")
    if cfg.get("notify") and top:
        notify("New job matches",
               f"{len(top)} new jobs with fit ≥ {min_fit}. Top: {top[0]['title'][:60]}")
    return summary


def _custom_personio(cfg: dict):
    from personio_jobs import PersonioCompany
    for slug in cfg.get("personio", {}).get("extra_slugs", []) if isinstance(cfg.get("personio"), dict) else []:
        yield PersonioCompany(slug, slug)


# --------------------------------------------------------------------------
# Windows integration
# --------------------------------------------------------------------------

def notify(title: str, body: str) -> None:
    """Best-effort Windows toast via PowerShell; silently does nothing elsewhere."""
    if os.name != "nt":
        return
    esc = lambda s: s.replace("'", "''").replace("<", "").replace(">", "").replace("&", "and")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$x=$t.GetElementsByTagName('text');$x[0].AppendChild($t.CreateTextNode('{esc(title)}'))>$null;"
        f"$x[1].AppendChild($t.CreateTextNode('{esc(body)}'))>$null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       timeout=20, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


def _task_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --sweep'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}"'


def install_task(at: str = "09:00") -> str:
    """Register a per-user daily task. Returns schtasks output. No admin needed."""
    res = subprocess.run(
        ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", at,
         "/TN", TASK_NAME, "/TR", _task_command()],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip())
    return res.stdout.strip()


def uninstall_task() -> str:
    res = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                         capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip())
    return res.stdout.strip()


def task_installed() -> str:
    """Next run time if the task exists, else ''."""
    if os.name != "nt":
        return ""
    res = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
                         capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if res.returncode != 0:
        return ""
    for line in res.stdout.splitlines():
        if line.lower().startswith(("next run time", "nächste laufzeit")):
            return line.split(":", 1)[1].strip()
    return "installed"


# --------------------------------------------------------------------------
# CLI / headless entry point
# --------------------------------------------------------------------------

def headless(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sweep", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--force", action="store_true", help="ignore the once-per-day limit")
    p.add_argument("--install", metavar="HH:MM", help="register a daily Windows task")
    p.add_argument("--uninstall", action="store_true")
    args = p.parse_args(argv)

    if args.install:
        print(install_task(args.install))
        return 0
    if args.uninstall:
        print(uninstall_task())
        return 0

    fh = open(LOG_PATH, "a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
        fh.write(line + "\n")
        fh.flush()
        if sys.stdout is not None:
            try:
                print(line)
            except (OSError, UnicodeEncodeError):
                pass

    # A windowed .exe has no stderr; providers print progress there.
    if sys.stderr is None:
        sys.stderr = fh
    try:
        run_sweep(force=args.force, log=log)
    finally:
        fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(headless())
