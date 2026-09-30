#!/usr/bin/env python3
"""
us_asia_jobs.py — US and Asia-Pacific robotics / mechatronics job search.

Providers, in order of how much I trust them:

  ats        Employer ATS boards (Greenhouse/Lever/Ashby/SmartRecruiters).
             Documented, keyless, stable. Start here.
  usajobs    US federal jobs. Official OPM API, documented, free key.
             https://developer.usajobs.gov/apirequest
  adzuna     US + IN + SG + AU + NZ + JP aggregator. Free tier, documented.
             https://developer.adzuna.com/
  mcf        Singapore MyCareersFuture. Official public JSON, no key.
             Endpoint is undocumented, so this one probes two known paths.

Usage:
    python us_asia_jobs.py boards --region us
    python us_asia_jobs.py ats --region asia --strict
    python us_asia_jobs.py usajobs --field robotics
    python us_asia_jobs.py adzuna --country us --field mechatronics
    python us_asia_jobs.py sg --field robotics
    python us_asia_jobs.py report
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from dataclasses import dataclass

import requests

from linkedin_jobs import Job, RateLimiter, Store, request_with_retry
from robotics_track import (
    ATSBoards,
    Board,
    GRAD_MARKERS,
    MECHATRONICS_MARKERS,
    SENIOR_MARKERS,
)

# --------------------------------------------------------------------------
# Market notes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Region:
    code: str
    name: str
    grad_terms: tuple[str, ...]
    note: str = ""


REGIONS: dict[str, Region] = {
    "us": Region(
        "us", "United States",
        ("new grad", "entry level", "university graduate", "early career",
         "associate engineer", "Engineer I", "rotational program"),
        "New-grad reqs open Aug-Oct for the following summer; many close within days. "
        "Visa status is usually asked up front — 'requires sponsorship' filters you out "
        "of most defense and space work, which is ITAR-restricted to US persons.",
    ),
    "jp": Region(
        "jp", "Japan",
        ("新卒", "shinsotsu", "graduate", "entry level", "第二新卒"),
        "Shinsotsu (new-grad) hiring is a rigid annual cycle: apply ~1 year before an "
        "April start. Foreign-friendly robotics employers increasingly hire off-cycle "
        "in English — those are the ones worth targeting.",
    ),
    "kr": Region(
        "kr", "South Korea",
        ("신입", "graduate", "entry level", "junior"),
        "Samsung/LG/Hyundai run fixed semi-annual open recruitment windows.",
    ),
    "sg": Region(
        "sg", "Singapore",
        ("graduate", "entry level", "junior", "management associate", "fresh graduate"),
        "Salary ranges are legally required on MyCareersFuture, so SG listings are the "
        "most transparent in Asia. Strong robotics cluster around A*STAR and NUS spinoffs.",
    ),
    "in": Region(
        "in", "India",
        ("fresher", "graduate engineer trainee", "GET", "entry level", "junior", "campus"),
        "'GET' (Graduate Engineer Trainee) is the standard entry title in manufacturing.",
    ),
    "tw": Region(
        "tw", "Taiwan",
        ("新鮮人", "graduate", "entry level", "junior"),
        "Dense precision-machinery and semiconductor-equipment cluster around Taichung.",
    ),
    "cn": Region(
        "cn", "China",
        ("应届生", "campus recruitment", "graduate", "junior"),
        "Campus recruitment (校招) runs Sep-Nov. Most postings are on domestic platforms.",
    ),
}

# Checked live 2026-09-23; ats="none" = no public board found (name search only).
US_ASIA_BOARDS: tuple[Board, ...] = (
    # --- United States: robotics ---
    Board("Boston Dynamics", "none", "", "US", "robotics"),
    Board("Agility Robotics", "greenhouse", "agilityrobotics", "US", "robotics"),
    Board("Figure AI", "greenhouse", "figureai", "US", "robotics"),
    Board("Skydio", "ashby", "skydio", "US", "robotics"),
    Board("Zipline", "greenhouse", "flyzipline", "US", "robotics"),
    Board("Diligent Robotics", "greenhouse", "diligentrobotics", "US", "robotics"),
    Board("Dexterity", "lever", "dexterity", "US", "robotics"),
    Board("Path Robotics", "greenhouse", "pathrobotics", "US", "robotics"),
    Board("Bright Machines", "lever", "brightmachines", "US", "robotics"),
    Board("Carbon Robotics", "ashby", "carbon-robotics", "US", "robotics"),
    Board("Gecko Robotics", "ashby", "gecko-robotics", "US", "robotics"),
    Board("Symbotic", "none", "", "US", "automation"),
    Board("Berkshire Grey", "none", "", "US", "automation"),
    # --- United States: mobility / aerospace / space ---
    Board("Zoox", "lever", "zoox", "US", "mobility"),
    Board("Nuro", "greenhouse", "nuro", "US", "mobility"),
    Board("Rivian", "none", "", "US", "mobility"),
    Board("Anduril", "greenhouse", "andurilindustries", "US", "mobility"),
    Board("Relativity Space", "greenhouse", "relativity", "US", "mobility"),
    Board("Astrobotic", "none", "", "US", "mobility"),
    # --- Asia-Pacific ---
    Board("Dyson", "none", "", "SG", "automation"),
    Board("Grab", "smartrecruiters", "grab", "SG", "automation"),
    Board("Sea / Garena", "none", "", "SG", "automation"),
    Board("Woven by Toyota", "lever", "woven-by-toyota", "JP", "mobility"),
    Board("Rapyuta Robotics", "none", "", "JP", "robotics"),
    Board("Preferred Networks", "none", "", "JP", "robotics"),
    Board("Mujin", "none", "", "JP", "robotics"),
    Board("Telexistence", "none", "", "JP", "robotics"),
    Board("GreyOrange", "none", "", "IN", "robotics"),
    Board("Ati Motors", "none", "", "IN", "robotics"),
    Board("Rebellions", "none", "", "KR", "robotics"),
)

REGION_COUNTRIES = {
    "us": {"US"},
    "asia": {"JP", "SG", "IN", "KR", "TW", "CN", "HK"},
    "all": {b.country for b in US_ASIA_BOARDS},
}


# --------------------------------------------------------------------------
# USAJOBS — official US federal API
# --------------------------------------------------------------------------

class USAJobsProvider:
    """US Office of Personnel Management job search API.

    Free key from https://developer.usajobs.gov/apirequest — arrives by email.

    The single most common failure is the User-Agent header: USAJOBS expects
    your REGISTERED EMAIL ADDRESS there, not a browser string. A browser-style
    User-Agent returns 401 even with a valid key.
    """

    name = "usajobs"
    URL = "https://data.usajobs.gov/api/Search"

    def __init__(self, api_key: str | None = None, email: str | None = None, rpm: int = 30):
        self.api_key = api_key or os.environ.get("USAJOBS_API_KEY")
        self.email = email or os.environ.get("USAJOBS_EMAIL")
        if not self.api_key or not self.email:
            raise SystemExit(
                "USAJOBS needs two environment variables:\n"
                "  USAJOBS_API_KEY  — the key emailed to you\n"
                "  USAJOBS_EMAIL    — the address you registered with\n"
                "Request a free key at https://developer.usajobs.gov/apirequest"
            )
        self.session = requests.Session()
        self.session.headers.update({
            "Host": "data.usajobs.gov",
            "User-Agent": self.email,        # deliberately the email, see docstring
            "Authorization-Key": self.api_key,
        })
        self.limiter = RateLimiter(rpm)
        self.last_total = 0

    def search(self, field: str, pages: int = 2, location: str = "",
               entry_level: bool = True, on_log=None) -> list[Job]:
        out: list[Job] = []
        for page in range(1, pages + 1):
            self.limiter.wait()
            params = {
                "Keyword": field,
                "ResultsPerPage": "100",
                "Page": str(page),
                "Fields": "Full",
                "SortField": "OpenDate",
                "SortDirection": "Desc",
            }
            if location:
                params["LocationName"] = location
            if entry_level:
                # GS-05..GS-09 and equivalent cover most new-graduate federal roles.
                params["PayGradeLow"] = "05"
                params["PayGradeHigh"] = "12"
            resp = request_with_retry(self.session, "GET", self.URL, params=params)
            payload = resp.json()
            result = payload.get("SearchResult") or {}
            items = result.get("SearchResultItems") or []
            self.last_total = result.get("SearchResultCountAll", 0)
            msg = f"[usajobs] {field} page {page}: {len(items)} of {self.last_total}"
            print(msg, file=sys.stderr)
            if on_log:
                on_log(msg)
            for item in items:
                job = self._to_job(item)
                if job:
                    out.append(job)
            if len(items) < 100:
                break
        return out

    @staticmethod
    def _to_job(item: dict) -> Job | None:
        d = item.get("MatchedObjectDescriptor") or {}
        title = d.get("PositionTitle") or ""
        if not title:
            return None
        org = d.get("OrganizationName") or d.get("DepartmentName") or ""
        locs = d.get("PositionLocation") or []
        loc = "; ".join(
            x.get("LocationName", "") for x in locs[:3] if isinstance(x, dict)
        ) or (d.get("PositionLocationDisplay") or "")

        remuneration = (d.get("PositionRemuneration") or [{}])[0]
        try:
            smin = float(remuneration.get("MinimumRange") or 0) or None
            smax = float(remuneration.get("MaximumRange") or 0) or None
        except (TypeError, ValueError):
            smin = smax = None

        summary = ((d.get("UserArea") or {}).get("Details") or {}).get("JobSummary", "")
        sched = d.get("PositionSchedule") or []
        sched_name = sched[0].get("Name", "") if sched and isinstance(sched[0], dict) else ""

        return Job(
            job_id=f"usajobs-{item.get('MatchedObjectId') or d.get('PositionID')}",
            title=title,
            company=org,
            location=loc,
            url=d.get("PositionURI") or "",
            posted_at=(d.get("PublicationStartDate") or "")[:19],
            employment_type=sched_name or "federal",
            salary_min=smin,
            salary_max=smax,
            salary_currency=remuneration.get("RateIntervalCode", "") and "USD",
            description=re.sub(r"<[^>]+>", " ", str(summary))[:4000],
            source="usajobs",
        )


# --------------------------------------------------------------------------
# Adzuna — US + Asia-Pacific aggregator
# --------------------------------------------------------------------------

class AdzunaProvider:
    """Adzuna public API. Free tier at https://developer.adzuna.com/

    Country codes it serves that matter here: us, in, sg, au, nz, jp.
    """

    name = "adzuna"
    URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
    COUNTRIES = ("us", "in", "sg", "au", "nz", "jp")

    def __init__(self, app_id: str | None = None, app_key: str | None = None, rpm: int = 25):
        self.app_id = app_id or os.environ.get("ADZUNA_APP_ID")
        self.app_key = app_key or os.environ.get("ADZUNA_APP_KEY")
        if not self.app_id or not self.app_key:
            raise SystemExit(
                "Adzuna needs ADZUNA_APP_ID and ADZUNA_APP_KEY.\n"
                "Free registration: https://developer.adzuna.com/"
            )
        self.session = requests.Session()
        self.limiter = RateLimiter(rpm)
        self.last_total = 0

    def search(self, field: str, country: str = "us", pages: int = 2,
               max_days_old: int = 30, on_log=None) -> list[Job]:
        country = country.lower()
        if country not in self.COUNTRIES:
            raise ValueError(f"Adzuna does not serve '{country}'. "
                             f"Available: {', '.join(self.COUNTRIES)}")
        out: list[Job] = []
        for page in range(1, pages + 1):
            self.limiter.wait()
            params = {
                "app_id": self.app_id,
                "app_key": self.app_key,
                "results_per_page": 50,
                "what": field,
                "max_days_old": max_days_old,
                "content-type": "application/json",
            }
            url = self.URL.format(country=country, page=page)
            payload = request_with_retry(self.session, "GET", url, params=params).json()
            results = payload.get("results") or []
            self.last_total = payload.get("count", 0)
            msg = f"[adzuna:{country}] {field} page {page}: {len(results)} of {self.last_total}"
            print(msg, file=sys.stderr)
            if on_log:
                on_log(msg)
            for r in results:
                out.append(self._to_job(r, country))
            if len(results) < 50:
                break
        return out

    @staticmethod
    def _to_job(r: dict, country: str) -> Job:
        company = (r.get("company") or {}).get("display_name", "")
        loc = (r.get("location") or {}).get("display_name", "") or country.upper()
        return Job(
            job_id=f"adzuna-{country}-{r.get('id')}",
            title=r.get("title") or "",
            company=company,
            location=loc,
            url=r.get("redirect_url") or "",
            posted_at=(r.get("created") or "")[:19],
            employment_type=r.get("contract_time") or "",
            salary_min=r.get("salary_min"),
            salary_max=r.get("salary_max"),
            description=re.sub(r"<[^>]+>", " ", r.get("description") or "")[:4000],
            source=f"adzuna:{country}",
        )


# --------------------------------------------------------------------------
# MyCareersFuture — Singapore, keyless
# --------------------------------------------------------------------------

class MyCareersFutureProvider:
    """Singapore's national job portal.

    The JSON service is public and needs no key, but it is not formally
    documented, and the host has moved at least once (api1.mycareersfuture.sg
    -> api.mycareersfuture.gov.sg). So this probes known shapes in order and
    reports exactly which one worked, rather than failing silently.
    """

    name = "mcf"
    ATTEMPTS = (
        ("POST", "https://api.mycareersfuture.gov.sg/v2/search"),
        ("GET", "https://api.mycareersfuture.gov.sg/v2/jobs"),
        ("GET", "https://api1.mycareersfuture.sg/v2/jobs"),
    )

    def __init__(self, rpm: int = 30):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; us-asia-jobs/1.0)",
            "Origin": "https://www.mycareersfuture.gov.sg",
            "Referer": "https://www.mycareersfuture.gov.sg/",
        })
        self.limiter = RateLimiter(rpm)
        self.session_id = uuid.uuid4().hex
        self.working: tuple[str, str] | None = None
        self.last_total = 0

    def search(self, field: str, pages: int = 2, on_log=None) -> list[Job]:
        out: list[Job] = []
        attempts = (self.working,) if self.working else self.ATTEMPTS
        for page in range(pages):
            self.limiter.wait()
            payload = None
            for method, url in attempts:
                try:
                    if method == "POST":
                        body = {
                            "search": field,
                            "sessionId": self.session_id,
                            "sortBy": ["new_posting_date"],
                        }
                        resp = request_with_retry(
                            self.session, "POST", url, json=body,
                            params={"limit": 100, "page": page}, tries=2)
                    else:
                        resp = request_with_retry(
                            self.session, "GET", url, tries=2,
                            params={"search": field, "limit": 100, "page": page})
                    payload = resp.json()
                    if self.working is None:
                        self.working = (method, url)
                        msg = f"[mcf] using {method} {url}"
                        print(msg, file=sys.stderr)
                        if on_log:
                            on_log(msg)
                    break
                except Exception as exc:
                    if on_log:
                        on_log(f"[mcf] {method} {url} failed: {exc}")
                    continue
            if payload is None:
                raise RuntimeError(
                    "No MyCareersFuture endpoint responded. The service may have moved — "
                    "open mycareersfuture.gov.sg, check the Network tab for the search "
                    "request, and add its URL to MyCareersFutureProvider.ATTEMPTS."
                )
            results = payload.get("results") or payload.get("jobs") or []
            self.last_total = payload.get("total", len(results))
            msg = f"[mcf] {field} page {page + 1}: {len(results)} of {self.last_total}"
            print(msg, file=sys.stderr)
            if on_log:
                on_log(msg)
            for r in results:
                out.append(self._to_job(r))
            if len(results) < 100:
                break
        return out

    @staticmethod
    def _to_job(r: dict) -> Job:
        posting = r.get("metadata") or {}
        company = (r.get("postedCompany") or r.get("hiringCompany") or {})
        cname = company.get("name", "") if isinstance(company, dict) else ""
        salary = r.get("salary") or {}
        addr = (r.get("address") or {})
        loc = addr.get("district") or addr.get("region") or "Singapore"
        if isinstance(loc, dict):
            loc = loc.get("region") or loc.get("name") or "Singapore"
        uuid_ = r.get("uuid") or r.get("id") or ""
        emp = r.get("employmentTypes") or []
        emp_name = emp[0].get("employmentType", "") if emp and isinstance(emp[0], dict) else ""
        return Job(
            job_id=f"mcf-{uuid_}",
            title=r.get("title") or "",
            company=cname,
            location=str(loc),
            url=f"https://www.mycareersfuture.gov.sg/job/{uuid_}",
            posted_at=(posting.get("newPostingDate") or posting.get("originalPostingDate") or "")[:19],
            employment_type=emp_name,
            salary_min=salary.get("minimum"),
            salary_max=salary.get("maximum"),
            salary_currency="SGD",
            description=re.sub(r"<[^>]+>", " ", r.get("description") or "")[:4000],
            source="mcf",
        )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _keep(job: Job, strict: bool) -> tuple[bool, str]:
    blob = f"{job.title} {job.description[:800]}"
    if not MECHATRONICS_MARKERS.search(blob):
        return False, "not mechatronics/robotics"
    if SENIOR_MARKERS.search(job.title):
        return False, "senior title"
    if strict and not GRAD_MARKERS.search(blob):
        return False, "not explicitly entry-level"
    return True, ""


COUNTRY_NAMES = {
    "US": "USA", "JP": "Japan", "SG": "Singapore", "IN": "India",
    "KR": "South Korea", "TW": "Taiwan", "CN": "China",
}


def cmd_ats(args) -> None:
    wanted = REGION_COUNTRIES.get(args.region, REGION_COUNTRIES["all"])
    boards = [b for b in US_ASIA_BOARDS if b.country in wanted]
    fetcher = ATSBoards()
    store = Store(args.db)
    new = seen = filtered = 0
    dead_boards: list[Board] = []
    for b in boards:
        try:
            postings = fetcher.fetch(b)
        except Exception as exc:
            print(f"  ! {b.company}: {exc}", file=sys.stderr)
            continue
        if not postings:
            dead_boards.append(b)
        print(f"[{b.ats}] {b.company} ({b.country}): {len(postings)}", file=sys.stderr)
        for job in postings:
            seen += 1
            ok, _ = _keep(job, args.strict)
            if not ok:
                filtered += 1
                continue
            job.employment_type = "graduate"
            if store.upsert(job):
                new += 1
                print(f"  + {job.title} — {job.company} ({job.location})")
    print(f"\n{seen} scanned · {new} new · {filtered} filtered")

    if args.jobspy_fallback and dead_boards:
        try:
            from jobspy_provider import JobSpyProvider
        except SystemExit as exc:
            print(f"\n! --jobspy-fallback requested but unavailable: {exc}", file=sys.stderr)
            return
        print(f"\n{len(dead_boards)} employer board(s) returned nothing directly; "
              "falling back to LinkedIn/Indeed search for those companies…")
        provider = JobSpyProvider(site_name=["indeed", "linkedin"])
        fb_new = fb_seen = 0
        for b in dead_boards:
            location = COUNTRY_NAMES.get(b.country, b.country)
            try:
                jobs = list(provider.search(
                    f'"{b.company}" graduate OR junior OR entry level', location, 1, False,
                ))
            except Exception as exc:
                print(f"  ! {b.company}: {exc}", file=sys.stderr)
                continue
            wanted_name = re.sub(r"[^a-z0-9]", "", b.company.lower())
            for job in jobs:
                fb_seen += 1
                got_name = re.sub(r"[^a-z0-9]", "", job.company.lower())
                if not got_name or (wanted_name not in got_name and got_name not in wanted_name):
                    continue
                ok, _ = _keep(job, args.strict)
                if not ok:
                    continue
                job.employment_type = "graduate"
                if store.upsert(job):
                    fb_new += 1
                    print(f"  [jobspy] {job.title} — {job.company} ({job.location})")
        print(f"\nfallback: {fb_seen} scanned · {fb_new} new")


def cmd_usajobs(args) -> None:
    provider = USAJobsProvider()
    store = Store(args.db)
    new = 0
    jobs = provider.search(args.field, args.pages, args.location, not args.all_grades)
    for job in jobs:
        ok, _ = _keep(job, False)
        if not ok:
            continue
        if store.upsert(job):
            new += 1
            print(f"  + {job.title} — {job.company} ({job.location})")
    print(f"\n{len(jobs)} returned · {new} new")
    print("\nNote: most federal robotics work (NASA, Navy labs, DoE) is ITAR-restricted "
          "to US citizens or permanent residents.")


def cmd_adzuna(args) -> None:
    provider = AdzunaProvider()
    store = Store(args.db)
    new = 0
    jobs = provider.search(args.field, args.country, args.pages)
    for job in jobs:
        ok, _ = _keep(job, args.strict)
        if not ok:
            continue
        if store.upsert(job):
            new += 1
            print(f"  + {job.title} — {job.company} ({job.location})")
    print(f"\n{len(jobs)} returned · {new} new")


def cmd_sg(args) -> None:
    provider = MyCareersFutureProvider()
    store = Store(args.db)
    new = 0
    jobs = provider.search(args.field, args.pages)
    for job in jobs:
        ok, _ = _keep(job, args.strict)
        if not ok:
            continue
        if store.upsert(job):
            new += 1
            sal = f" [{job.salary_min:.0f}-{job.salary_max:.0f} SGD]" if job.salary_min else ""
            print(f"  + {job.title} — {job.company}{sal}")
    print(f"\n{len(jobs)} returned · {new} new")


def cmd_boards(args) -> None:
    wanted = REGION_COUNTRIES.get(args.region, REGION_COUNTRIES["all"])
    for b in US_ASIA_BOARDS:
        if b.country in wanted:
            print(f"  {b.company:<24} {b.country}  {b.tier:<13} ({b.ats})")


def cmd_markets(args) -> None:
    for code, r in REGIONS.items():
        print(f"\n{code.upper()}  {r.name}")
        print(f"    entry-level terms: {', '.join(r.grad_terms[:5])}")
        if r.note:
            for line in re.findall(r".{1,76}(?:\s|$)", r.note):
                print(f"    {line.strip()}")


def cmd_report(args) -> None:
    rows = Store(args.db).all()
    if not rows:
        print("nothing stored yet")
        return
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r["source"] or "?"] = by_source.get(r["source"] or "?", 0) + 1
    print(f"{len(rows)} roles stored\n")
    for k, v in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"  {v:>4}  {k}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="us_asia_jobs.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ats", help="employer boards (keyless, most reliable)")
    a.add_argument("--region", choices=["us", "asia", "all"], default="all")
    a.add_argument("--strict", action="store_true")
    a.add_argument("--jobspy-fallback", action="store_true",
                   help="for boards that returned nothing, search LinkedIn/Indeed "
                        "for that company instead (needs python-jobspy)")
    a.set_defaults(func=cmd_ats)

    u = sub.add_parser("usajobs", help="US federal jobs (needs free key)")
    u.add_argument("--field", default="robotics")
    u.add_argument("--location", default="")
    u.add_argument("--pages", type=int, default=2)
    u.add_argument("--all-grades", action="store_true", help="don't restrict to GS 05-12")
    u.set_defaults(func=cmd_usajobs)

    d = sub.add_parser("adzuna", help="US/IN/SG/AU/NZ/JP aggregator (needs free key)")
    d.add_argument("--field", default="mechatronics engineer")
    d.add_argument("--country", default="us", choices=list(AdzunaProvider.COUNTRIES))
    d.add_argument("--pages", type=int, default=2)
    d.add_argument("--strict", action="store_true")
    d.set_defaults(func=cmd_adzuna)

    s = sub.add_parser("sg", help="Singapore MyCareersFuture (keyless)")
    s.add_argument("--field", default="robotics")
    s.add_argument("--pages", type=int, default=2)
    s.add_argument("--strict", action="store_true")
    s.set_defaults(func=cmd_sg)

    b = sub.add_parser("boards", help="list employers")
    b.add_argument("--region", choices=["us", "asia", "all"], default="all")
    b.set_defaults(func=cmd_boards)

    m = sub.add_parser("markets", help="hiring-cycle notes per country")
    m.set_defaults(func=cmd_markets)

    r = sub.add_parser("report", help="summary")
    r.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
