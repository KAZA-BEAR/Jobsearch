#!/usr/bin/env python3
"""
workday_jobs.py — jobs from employers whose careers site runs on Workday.

Most large manufacturers (ZEISS, Airbus, ASML, NXP, ...) recruit on Workday,
which none of the Greenhouse/Lever/SmartRecruiters pollers can reach. Every
Workday careers site is backed by the same public, keyless JSON endpoint the
site itself calls:

    POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
         {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "..."}
    GET  https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}

The {site} part is case-sensitive and differs per company, so it can't be
guessed reliably (a wrong one returns HTTP 422). Paste the careers URL instead:
parse_url() reads tenant/wd/site from any myworkdayjobs.com link, e.g.
    https://zeissgroup.wd3.myworkdayjobs.com/en-US/External/job/...

Usage:
    python workday_jobs.py search "robotics" --country Germany
    python workday_jobs.py search "Werkstudent" --site https://zeissgroup.wd3.myworkdayjobs.com/External
    python workday_jobs.py sites
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass

import requests

from linkedin_jobs import Job, RateLimiter, Store, parse_posted


@dataclass(frozen=True)
class WorkdaySite:
    company: str
    tenant: str
    wd: str          # wd1 / wd3 / wd5 ... (data-centre shard)
    site: str

    @property
    def host(self) -> str:
        return f"https://{self.tenant}.{self.wd}.myworkdayjobs.com"

    @property
    def api(self) -> str:
        return f"{self.host}/wday/cxs/{self.tenant}/{self.site}"


# Verified against the live endpoint (Sep 2026).
SITES: tuple[WorkdaySite, ...] = (
    WorkdaySite("ZEISS", "zeissgroup", "wd3", "External"),
    WorkdaySite("Airbus", "ag", "wd3", "Airbus"),
    WorkdaySite("ASML", "asml", "wd3", "asmlext1"),
    WorkdaySite("NXP Semiconductors", "nxp", "wd3", "Careers"),
    WorkdaySite("Thales", "thales", "wd3", "Careers"),
    WorkdaySite("NVIDIA", "nvidia", "wd5", "NVIDIAExternalCareerSite"),
    WorkdaySite("Analog Devices", "analogdevices", "wd1", "External"),
    WorkdaySite("KLA", "kla", "wd1", "Search"),
)

_URL = re.compile(
    r"https?://(?P<tenant>[\w-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
    r"(?:wday/cxs/[\w-]+/)?(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[\w-]+)", re.IGNORECASE)


def parse_url(url: str, company: str = "") -> WorkdaySite:
    m = _URL.match(url.strip())
    if not m:
        raise ValueError(f"not a myworkdayjobs.com URL: {url}")
    if not company:
        known = {x.tenant: x.company for x in SITES}
        company = known.get(m["tenant"].lower(), m["tenant"])
    return WorkdaySite(company, m["tenant"].lower(), m["wd"].lower(), m["site"])


class WorkdayBoards:
    name = "workday"

    def __init__(self, rpm: int = 50):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; jobsearch/1.0)",
        })
        self.limiter = RateLimiter(rpm)

    def _post(self, site: WorkdaySite, body: dict) -> dict:
        self.limiter.wait()
        resp = self.session.post(f"{site.api}/jobs", json=body, timeout=30)
        if resp.status_code == 422:
            raise ValueError(f"{site.company}: site name '{site.site}' is wrong (HTTP 422)")
        resp.raise_for_status()
        return resp.json()

    def _country_facet(self, site: WorkdaySite, country: str, search: str) -> dict:
        """Find the facet id for `country` (e.g. Germany) on this site, if any."""
        data = self._post(site, {"appliedFacets": {}, "limit": 1, "offset": 0,
                                 "searchText": search})
        want = country.lower()

        def walk(facets):
            for f in facets or []:
                param = f.get("facetParameter", "")
                for v in f.get("values") or []:
                    if "values" in v:          # nested group (locationMainGroup)
                        hit = walk([v])
                        if hit:
                            return hit
                    elif (v.get("descriptor") or "").lower() == want and "ountry" in param:
                        return {param: [v["id"]]}
            return None

        return walk(data.get("facets")) or {}

    def search(self, site: WorkdaySite, text: str = "", country: str = "",
               max_jobs: int = 100, details: int = 30, on_log=None, cancelled=None,
               cached=None) -> list[Job]:
        """cached(job_id) -> str | None supplies an already-stored description."""
        facets = self._country_facet(site, country, text) if country else {}
        if country and not facets and on_log:
            on_log(f"[workday] {site.company}: no '{country}' location filter; searching all")
        out: list[Job] = []
        offset, total = 0, None
        while offset < max_jobs:
            if cancelled is not None and cancelled.is_set():
                break
            data = self._post(site, {"appliedFacets": facets, "limit": 20,
                                     "offset": offset, "searchText": text})
            if total is None:
                total = int(data.get("total") or 0)
            rows = data.get("jobPostings") or []
            out.extend(self._to_job(site, r) for r in rows if r.get("externalPath"))
            offset += 20
            if len(rows) < 20 or offset >= total:
                break
        msg = f"[workday] {site.company} '{text}'{' · ' + country if country else ''}: {len(out)} of {total or 0}"
        print(msg, file=sys.stderr)
        if on_log:
            on_log(msg)
        fetched = 0
        for job in out:
            if cancelled is not None and cancelled.is_set():
                break
            known = cached(job.job_id) if cached else None
            if known:
                job.description = known
            elif fetched < details:
                self._fill_detail(site, job)
                fetched += 1
        return out

    def _fill_detail(self, site: WorkdaySite, job: Job) -> None:
        path = job.url.split(f"/{site.site}", 1)[-1]
        self.limiter.wait()
        try:
            resp = self.session.get(f"{site.api}{path}", timeout=20)
            if resp.status_code != 200:
                return
            info = resp.json().get("jobPostingInfo") or {}
        except (requests.RequestException, ValueError):
            return
        text = html.unescape(re.sub(r"<[^>]+>", " ", info.get("jobDescription") or ""))
        job.description = re.sub(r"\s+", " ", text).strip()[:4000]
        if info.get("startDate"):
            job.posted_at = info["startDate"]
        if info.get("location"):
            country = (info.get("country") or {}).get("descriptor", "")
            job.location = ", ".join(x for x in (info["location"], country) if x)
        if info.get("timeType"):
            job.employment_type = info["timeType"]

    @staticmethod
    def _to_job(site: WorkdaySite, r: dict) -> Job:
        path = r["externalPath"]
        posted = parse_posted(r.get("postedOn") or "")
        bullets = r.get("bulletFields") or []
        return Job(
            job_id=f"workday-{site.tenant}-{path.rsplit('_', 1)[-1]}",
            title=r.get("title") or "",
            company=site.company,
            location=r.get("locationsText") or "",
            url=f"{site.host}/{site.site}{path}",
            posted_at=posted.date().isoformat() if posted else "",
            employment_type=r.get("timeType") or "",
            description=" · ".join(str(b) for b in bullets),
            source="workday",
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="robotics_jobs.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--country", default="Germany")
    s.add_argument("--site", action="append", help="myworkdayjobs.com URL (repeatable)")
    s.add_argument("--max", type=int, default=100)
    sub.add_parser("sites")
    args = p.parse_args()

    if args.cmd == "sites":
        for s in SITES:
            print(f"  {s.company:<22} {s.host}/{s.site}")
        return
    sites = [parse_url(u) for u in args.site] if args.site else list(SITES)
    from fit_score import load_default_scorer
    store = Store(args.db, scorer=(sc.score if (sc := load_default_scorer()) else None))
    wb = WorkdayBoards()
    for site in sites:
        try:
            jobs = wb.search(site, args.text, args.country, args.max)
        except Exception as exc:
            print(f"{site.company}: FAILED {exc}")
            continue
        new = sum(store.upsert(j) for j in jobs)
        print(f"{site.company}: {len(jobs)} roles, {new} new")


if __name__ == "__main__":
    main()
