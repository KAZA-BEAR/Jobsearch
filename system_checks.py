"""
system_checks.py — health checks behind the app's Unit tests tab.

Three groups, each check reporting pass / warn / fail / skip with a reason:

  APIs       every job source answers a small real request; keyed sources
             (USAJOBS, Adzuna, both free) are skipped when no key is set
  LM Studio  the app and its lms tool are installed, every model the CV
             generator needs is downloaded, and the local server answers
  Hardware   64-bit Python, AVX2, RAM, GPU memory against the models' size,
             free disk space, and pdflatex for the Compile PDF option

Run from the app only (Unit tests tab). No check changes anything: nothing is
stored, and no model is loaded or unloaded.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests

from linkedin_jobs import data_dir

GROUPS = ("APIs", "LM Studio", "Hardware")
PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
GB = 1024 ** 3
TIMEOUT = 20


@dataclass
class Result:
    group: str
    name: str
    status: str
    detail: str
    seconds: float = 0.0


class Skip(Exception):
    """Raised by a check that cannot run here, e.g. no API key set."""


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", creationflags=_no_window())


def _need(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise Skip(f"no key set ({', '.join(missing)}); add it under API keys")


def _count(jobs, what: str = "jobs") -> tuple[str, str]:
    jobs = list(jobs)
    return PASS, f"answered, {len(jobs)} {what} in a small test search"


# --------------------------------------------------------------------------
# APIs
# --------------------------------------------------------------------------

def _api_ba():
    from ba_jobsuche import BAJobsucheProvider
    return _count(BAJobsucheProvider().search("Robotik", pages=1, per_page=25, details=0))


def _api_eures():
    from eu_student_jobs import EuresProvider
    return _count(EuresProvider().search("robotics", "", 1, False, country="DE"))


def _api_personio():
    from personio_jobs import COMPANIES, PersonioFeeds
    feeds = PersonioFeeds()
    for c in COMPANIES[:3]:
        jobs = feeds.fetch(c)
        if jobs is not None:
            return PASS, f"answered, {c.company} feed has {len(jobs)} roles"
    return WARN, (f"no feed found for {', '.join(c.company for c in COMPANIES[:3])}; "
                  "the service may be down or these employers left Personio")


def _api_workday():
    from workday_jobs import SITES, WorkdayBoards
    site = SITES[0]
    jobs = WorkdayBoards().search(site, "", "", max_jobs=5, details=0)
    return PASS, f"answered, {site.company}: {len(jobs)} jobs"


def _api_research(source: str):
    def check():
        from research_jobs import ResearchFeeds
        return _count(ResearchFeeds().search(source, ""), "postings")
    return check


def _api_ats(ats: str, company: str, url: str):
    def check():
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return PASS, f"answered for {company}"
        if r.status_code == 404:
            return WARN, (f"{ats} answered, but {company}'s board is gone (404); "
                          "the employer list needs updating")
        return FAIL, f"HTTP {r.status_code} for {company}"
    return check


def _api_sec(email: str):
    def check():
        r = requests.get("https://efts.sec.gov/LATEST/search-index",
                         params={"q": '"robotics"', "forms": "D"}, timeout=TIMEOUT,
                         headers={"User-Agent": f"robotics-job-radar/1.0 "
                                                f"({email or 'jobsearch-tool@example.com'})"})
        if r.status_code == 200:
            try:
                hits = r.json()["hits"]["total"]["value"]
            except (ValueError, KeyError, TypeError):
                return WARN, "answered, but not with the expected search results"
            return PASS, f"answered, {hits} Form D filings mention robotics"
        return FAIL, f"HTTP {r.status_code}"
    return check


def _api_mcf():
    from us_asia_jobs import MyCareersFutureProvider
    p = MyCareersFutureProvider()
    jobs = p.search("robotics", 1)
    return PASS, f"answered via {p.working[1] if p.working else '?'}, {len(jobs)} jobs"


def _api_usajobs():
    _need("USAJOBS_API_KEY", "USAJOBS_EMAIL")
    from us_asia_jobs import USAJobsProvider
    return _count(USAJobsProvider().search("robotics", 1))


def _api_adzuna():
    _need("ADZUNA_APP_ID", "ADZUNA_APP_KEY")
    from us_asia_jobs import AdzunaProvider
    return _count(AdzunaProvider().search("robotics engineer", "us", 1))


def _api_adzuna_eu():
    _need("ADZUNA_APP_ID", "ADZUNA_APP_KEY")
    from eu_student_jobs import ADZUNA_EU_DEFAULTS, keep_adzuna_europe
    from us_asia_jobs import AdzunaProvider
    roles, fields = ADZUNA_EU_DEFAULTS["de"]
    jobs = AdzunaProvider().search(roles[0], "de", 1, any_of=fields)
    kept = sum(keep_adzuna_europe(j, False) for j in jobs)
    return PASS, (f"answered, {len(jobs)} {roles[0]} jobs in Germany, "
                  f"{kept} pass the robotics filter")


def _api_jobspy():
    try:
        import jobspy  # noqa: F401
    except ImportError:
        return WARN, "python-jobspy not installed (optional; run install_jobspy.py)"
    except SystemExit as exc:
        return WARN, str(exc)
    from importlib.metadata import PackageNotFoundError, version
    try:
        v = version("python-jobspy")
    except PackageNotFoundError:
        v = "?"
    return PASS, (f"python-jobspy {v} installed. Not test-searched: scraping LinkedIn "
                  "for a check risks a temporary block")


def api_checks(sec_email: str = "") -> list[tuple[str, callable]]:
    from research_jobs import SOURCES as RESEARCH
    from robotics_track import ATS_ENDPOINTS, BOARDS
    from us_asia_jobs import US_ASIA_BOARDS

    checks = [
        ("Arbeitsagentur (DE)", _api_ba),
        ("EURES (also used for PhD positions)", _api_eures),
        ("Personio", _api_personio),
        ("Workday", _api_workday),
    ]
    checks += [(f"Research: {label}", _api_research(code)) for code, label in RESEARCH.items()]
    # one employer per job-board platform
    seen = set()
    for b in BOARDS + US_ASIA_BOARDS:
        tmpl = ATS_ENDPOINTS.get(b.ats)
        if tmpl and b.ats not in seen:
            seen.add(b.ats)
            checks.append((f"Employer boards: {b.ats}",
                           _api_ats(b.ats, b.company, tmpl.format(slug=b.slug))))
    checks += [
        ("SEC EDGAR (New companies)", _api_sec(sec_email)),
        ("MyCareersFuture (SG)", _api_mcf),
        ("USAJOBS", _api_usajobs),
        ("Adzuna", _api_adzuna),
        ("Adzuna (Europe)", _api_adzuna_eu),
        ("JobSpy (LinkedIn / Indeed)", _api_jobspy),
    ]
    return checks


# --------------------------------------------------------------------------
# LM Studio
# --------------------------------------------------------------------------

def required_models() -> list[str]:
    """Chat models the CV generator loads, then the embedding model."""
    from ats_embed import EMBED_MODEL
    from ats_pipeline import DEFAULT_LOCAL_MODELS
    chat = [m.strip() for m in DEFAULT_LOCAL_MODELS.split(",") if m.strip()]
    return chat + [EMBED_MODEL]


def downloaded_models() -> dict[str, dict] | None:
    """{model key: lms ls entry}, or None if lms is not available."""
    from ats_pipeline import _lms_path
    lms = _lms_path()
    if not lms:
        return None
    res = _run([lms, "ls", "--json"], timeout=60)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[-300:])
    return {m.get("modelKey") or m.get("path"): m for m in json.loads(res.stdout)}


def _lm_app():
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [local / "Programs" / "LM Studio" / "LM Studio.exe",
                  local / "Programs" / "lm-studio" / "LM Studio.exe",
                  Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "LM Studio" / "LM Studio.exe",
                  Path("/Applications/LM Studio.app")]
    for p in candidates:
        if p.exists():
            return PASS, f"installed at {p}"
    from ats_pipeline import _lms_path
    if _lms_path():
        return PASS, "app not in the usual folder, but its lms tool is installed"
    return FAIL, "not found; install it from https://lmstudio.ai"


def _lm_cli():
    from ats_pipeline import _lms_path
    lms = _lms_path()
    if not lms:
        return FAIL, ("lms tool not found; open LM Studio once, or run "
                      "~/.lmstudio/bin/lms bootstrap. Without it models must be loaded by hand")
    return PASS, f"found at {lms}"


def _lm_model(model: str, cache: dict):
    def check():
        if "models" not in cache:
            cache["models"] = downloaded_models()
        models = cache["models"]
        if models is None:
            raise Skip("lms tool not found, so downloaded models cannot be listed")
        m = models.get(model)
        if m is None:
            return FAIL, f"not downloaded; search for '{model}' in LM Studio and download it"
        return PASS, f"downloaded, {m.get('sizeBytes', 0) / GB:.1f} GB"
    return check


def _lm_server(base_url: str):
    def check():
        from ats_pipeline import _loaded_models, list_local_models, PipelineError
        try:
            list_local_models(base_url, timeout=5)
        except PipelineError:
            return WARN, (f"server not running at {base_url}. Only needed for CV generation: "
                          "LM Studio → Developer → Start Server")
        loaded = [m for m, ctx in _loaded_models(base_url).items() if ctx is not None]
        return PASS, ("running; loaded: " + (", ".join(loaded) if loaded else
                                             "nothing yet (the CV generator loads its models)"))
    return check


def lm_checks(base_url: str) -> list[tuple[str, callable]]:
    cache: dict = {}
    checks = [("LM Studio app", _lm_app), ("lms command-line tool", _lm_cli)]
    checks += [(f"Model: {m}", _lm_model(m, cache)) for m in required_models()]
    checks.append(("Local server", _lm_server(base_url)))
    return checks


# --------------------------------------------------------------------------
# Hardware
# --------------------------------------------------------------------------

def _ram() -> tuple[int, int] | None:
    """(total, available) bytes."""
    if os.name == "nt":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        s = MEMORYSTATUSEX()
        s.dwLength = ctypes.sizeof(s)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
            return s.ullTotalPhys, s.ullAvailPhys
        return None
    try:
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        avail = os.sysconf("SC_AVPHYS_PAGES") * size
        return pages * size, avail
    except (ValueError, OSError, AttributeError):
        return None


def _cpu_name() -> str:
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def _gpus() -> list[tuple[str, float | None]]:
    """[(name, VRAM GB or None if unknown)]."""
    smi = shutil.which("nvidia-smi")
    if smi:
        res = _run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        if res.returncode == 0 and res.stdout.strip():
            out = []
            for line in res.stdout.strip().splitlines():
                name, mib = [x.strip() for x in line.rsplit(",", 1)]
                out.append((name, float(mib) / 1024))
            return out
    if os.name == "nt":
        # Win32_VideoController's AdapterRAM caps at 4 GB, so report names only.
        res = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "(Get-CimInstance Win32_VideoController).Name"])
        return [(n.strip(), None) for n in res.stdout.splitlines() if n.strip()]
    return []


def model_footprint() -> float | None:
    """GB the CV generator's models take in memory at their 6k context: file
    size plus ~0.5 GB working memory per chat model. None if unknown."""
    try:
        models = downloaded_models()
    except Exception:  # noqa: BLE001
        return None
    if not models:
        return None
    need = required_models()
    sizes = [models[m].get("sizeBytes", 0) / GB for m in need if m in models]
    if not sizes:
        return None
    return sum(sizes) + 0.5 * (len(need) - 1)


def _hw_python():
    bits = 64 if sys.maxsize > 2 ** 32 else 32
    import tkinter
    detail = (f"Python {platform.python_version()} {bits}-bit, Tk {tkinter.TkVersion}, "
              f"{platform.system()} {platform.release()} ({platform.machine()})")
    return (PASS if bits == 64 else FAIL), detail + ("" if bits == 64 else "; 64-bit is required")


def _hw_cpu():
    name, cores = _cpu_name(), os.cpu_count() or 0
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if os.name == "nt" and not arm:
        import ctypes
        avx2 = bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))  # PF_AVX2
        if not avx2:
            return FAIL, f"{name}, {cores} threads; no AVX2, which LM Studio requires on x86"
        return PASS, f"{name}, {cores} threads, AVX2 supported"
    return PASS, f"{name}, {cores} threads"


def _hw_ram():
    ram = _ram()
    if ram is None:
        raise Skip("could not read the memory size on this system")
    total, avail = ram[0] / GB, ram[1] / GB
    detail = f"{total:.1f} GB total, {avail:.1f} GB free now"
    if total < 8:
        return FAIL, detail + "; LM Studio needs at least 8 GB (16 GB recommended)"
    if total < 15:
        return WARN, detail + "; 16 GB is recommended for LM Studio"
    return PASS, detail


def _hw_gpu():
    gpus = _gpus()
    need = model_footprint()
    need_s = f"the CV models need about {need:.1f} GB" if need else "model size unknown"
    if not gpus:
        return WARN, f"no GPU found; models run on the CPU, which is much slower ({need_s})"
    best = max((v for _n, v in gpus if v), default=None)
    names = ", ".join(f"{n} ({v:.0f} GB)" if v else n for n, v in gpus)
    if best is None:
        return WARN, f"{names}; GPU memory unknown ({need_s})"
    if need and best < need:
        return WARN, (f"{names}; {need_s}, so part runs on the CPU and generation is "
                      "slower. Use smaller models, or one model, to fit")
    return PASS, f"{names}; " + (f"{need_s}, which fits" if need else need_s)


def _hw_disk():
    paths = {"data folder": data_dir(), "LM Studio models": Path.home() / ".lmstudio" / "models"}
    worst, parts = PASS, []
    for label, p in paths.items():
        if not p.exists():
            continue
        free = shutil.disk_usage(p).free / GB
        parts.append(f"{label}: {free:.0f} GB free")
        if free < 2:
            worst = FAIL
        elif free < 10 and worst == PASS:
            worst = WARN
    if not parts:
        raise Skip("no folders to check")
    return worst, "; ".join(parts) + ("" if worst == PASS else " (downloads and outputs need room)")


def _hw_pdflatex():
    exe = shutil.which("pdflatex")
    if exe:
        return PASS, f"found at {exe}"
    return WARN, "not found; the Compile PDF option needs MiKTeX or TeX Live (optional)"


def hw_checks() -> list[tuple[str, callable]]:
    return [("Python & OS", _hw_python), ("CPU", _hw_cpu), ("Memory (RAM)", _hw_ram),
            ("GPU", _hw_gpu), ("Disk space", _hw_disk), ("pdflatex (PDF output)", _hw_pdflatex)]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def _one(group: str, name: str, fn) -> Result:
    t0 = time.monotonic()
    try:
        status, detail = fn()
    except Skip as exc:
        status, detail = SKIP, str(exc)
    except BaseException as exc:  # noqa: BLE001 - providers raise SystemExit for missing keys
        if isinstance(exc, KeyboardInterrupt):
            raise
        status, detail = FAIL, f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    return Result(group, name, status, detail, time.monotonic() - t0)


def run(groups=GROUPS, on_result=None, cancelled: threading.Event | None = None,
        base_url: str = "http://localhost:1234/v1", sec_email: str = "") -> list[Result]:
    """Run the chosen groups; on_result(Result) is called as each check finishes.
    API checks run in parallel (they wait on the network); the rest in order."""
    stop = cancelled or threading.Event()
    results: list[Result] = []

    def done(r: Result) -> None:
        results.append(r)
        if on_result:
            on_result(r)

    for group in groups:
        if stop.is_set():
            break
        if group == "APIs":
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(lambda n=n, f=f: None if stop.is_set() else _one(group, n, f))
                           for n, f in api_checks(sec_email)]
                for fut in as_completed(futures):
                    r = fut.result()
                    if r is not None:
                        done(r)
        else:
            checks = lm_checks(base_url) if group == "LM Studio" else hw_checks()
            for name, fn in checks:
                if stop.is_set():
                    break
                done(_one(group, name, fn))
    return results
