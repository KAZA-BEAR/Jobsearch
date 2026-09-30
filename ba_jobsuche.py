#!/usr/bin/env python3
"""
ba_jobsuche.py — Bundesagentur für Arbeit (Federal Employment Agency) job search.

Germany's largest job database, including many employers that never post on
LinkedIn or run their own ATS board. The API behind arbeitsagentur.de/jobsuche
is public; its client id is the fixed string "jobboerse-jobsuche" (documented
at https://github.com/bundesAPI/jobsuche-api, not a personal key).

Useful filters the other providers don't have:
  * radius search around a town (umkreis, km)
  * offer type: Arbeit / Ausbildung-Duales Studium / Praktikum-Trainee
  * working time incl. part-time (Werkstudent jobs are usually "tz")
  * published within the last N days (veroeffentlichtseit, 0-100)

The search endpoint returns no description, so the provider fetches the detail
record for the first `details` hits: that text feeds the fit score and the
German-language check.

Usage:
    python ba_jobsuche.py search "Robotik" --where Straubing --radius 100
    python ba_jobsuche.py search "Werkstudent Mechatronik" --where Regensburg --part-time --days 7
    python ba_jobsuche.py search "Praktikum Robotik" --offer praktikum
"""

from __future__ import annotations

import argparse
import base64
import sys
from urllib.parse import quote

import requests

from linkedin_jobs import Job, RateLimiter, Store, request_with_retry

BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
SEARCH = BASE + "/pc/v6/jobs"
DETAIL = BASE + "/pc/v4/jobdetails/{b64}"
PUBLIC_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
CLIENT_ID = "jobboerse-jobsuche"

OFFER_TYPES = {          # angebotsart
    "arbeit": 1,         # regular employment (includes Werkstudent jobs)
    "ausbildung": 4,     # apprenticeship / duales Studium
    "praktikum": 34,     # internship / trainee
}
WORK_TIME = {            # arbeitszeit
    "vollzeit": "vz", "teilzeit": "tz", "schicht": "snw", "homeoffice": "ho", "minijob": "mj",
}


class BAJobsucheProvider:
    name = "ba"

    def __init__(self, rpm: int = 40):
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": CLIENT_ID,
            "Accept": "application/json",
            "User-Agent": "Jobsuche/2.9.2 (de.arbeitsagentur.jobboerse; build:1077; iOS 15.1.0) Alamofire/5.4.4",
        })
        self.limiter = RateLimiter(rpm)
        self.last_total = 0

    def search(self, query: str, where: str = "", radius: int = 50, offer: str = "",
               work_time: str = "", days: int = 0, pages: int = 2, per_page: int = 50,
               details: int = 40, on_log=None, cancelled=None, cached=None) -> list[Job]:
        """Raises on network failure; callers decide how to surface it.

        cached(job_id) -> str | None lets the caller supply a description it
        already stored, skipping that detail request.
        """
        out: list[Job] = []
        for page in range(1, pages + 1):
            if cancelled is not None and cancelled.is_set():
                break
            params: dict = {"was": query, "page": page, "size": per_page}
            if where:
                params["wo"] = where
                params["umkreis"] = radius
            if offer:
                params["angebotsart"] = OFFER_TYPES.get(offer, offer)
            if work_time:
                params["arbeitszeit"] = WORK_TIME.get(work_time, work_time)
            if days:
                params["veroeffentlichtseit"] = max(0, min(int(days), 100))
            self.limiter.wait()
            resp = request_with_retry(self.session, "GET", SEARCH, params=params)
            data = resp.json()
            rows = data.get("stellenangebote") or data.get("ergebnisliste") or []
            self.last_total = int(data.get("maxErgebnisse") or 0)
            msg = (f"[ba] '{query}'{' @ ' + where if where else ''} page {page}: "
                   f"{len(rows)} of {self.last_total}")
            print(msg, file=sys.stderr)
            if on_log:
                on_log(msg)
            out.extend(self._to_job(r) for r in rows)
            if len(rows) < per_page:
                break

        fetched = 0
        for job in out:
            if cancelled is not None and cancelled.is_set():
                break
            known = cached(job.job_id) if cached else None
            if known:
                job.description = known
            elif fetched < details:
                self._fill_description(job)
                fetched += 1
        return out

    def _fill_description(self, job: Job) -> None:
        refnr = job.job_id.removeprefix("ba-")
        b64 = base64.b64encode(refnr.encode()).decode()
        self.limiter.wait()
        try:
            resp = self.session.get(DETAIL.format(b64=b64), timeout=20)
            if resp.status_code != 200:
                return
            d = resp.json()
        except (requests.RequestException, ValueError):
            return
        job.description = (d.get("stellenangebotsBeschreibung") or job.description)[:4000]

    @staticmethod
    def _to_job(r: dict) -> Job:
        refnr = r.get("refnr") or r.get("referenznummer") or ""
        locs = r.get("stellenlokationen") or []
        if locs:
            addr = (locs[0].get("adresse") or {})
        else:
            addr = r.get("arbeitsort") or {}
        loc = ", ".join(x for x in (addr.get("ort"), addr.get("region"), "DE") if x)
        if len(locs) > 1:
            loc += f" (+{len(locs) - 1} more)"
        kind = {"ARBEIT": "job", "AUSBILDUNG": "ausbildung",
                "PRAKTIKUM_TRAINEE": "internship"}.get(r.get("stellenangebotsart", ""), "")
        if r.get("arbeitszeitVollzeit") is False and any(
                r.get(k) for k in ("arbeitszeitTeilzeitFlexibel", "arbeitszeitTeilzeitVormittag",
                                   "arbeitszeitTeilzeitNachmittag", "arbeitszeitTeilzeitAbend")):
            kind = (kind + " · part-time").strip(" ·")
        berufe = ", ".join(r.get("alleBerufe") or [r.get("hauptberuf") or r.get("beruf") or ""])
        salary_min = r.get("gehaltsspanneVon")
        salary_max = r.get("gehaltsspanneBis")
        return Job(
            job_id=f"ba-{refnr}",
            title=r.get("stellenangebotsTitel") or r.get("titel") or "",
            company=r.get("firma") or r.get("arbeitgeber") or "",
            location=loc,
            url=r.get("externeURL") or PUBLIC_URL.format(refnr=quote(refnr, safe="")),
            posted_at=r.get("datumErsteVeroeffentlichung") or r.get("aktuelleVeroeffentlichungsdatum") or "",
            employment_type=kind,
            remote=bool(r.get("homeofficemoeglich")),
            salary_min=float(salary_min) if salary_min else None,
            salary_max=float(salary_max) if salary_max else None,
            salary_currency="EUR" if salary_min else "",
            description=f"Berufe: {berufe}" if berufe else "",
            source="ba",
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
    s.add_argument("query")
    s.add_argument("--where", default="")
    s.add_argument("--radius", type=int, default=50, help="km around --where")
    s.add_argument("--offer", choices=list(OFFER_TYPES), default="")
    s.add_argument("--part-time", action="store_true")
    s.add_argument("--days", type=int, default=0, help="published within N days (1-100)")
    s.add_argument("--pages", type=int, default=2)
    s.add_argument("--details", type=int, default=40,
                   help="fetch full descriptions for the first N hits")
    args = p.parse_args()

    from fit_score import load_default_scorer
    store = Store(args.db, scorer=(sc.score if (sc := load_default_scorer()) else None))
    jobs = BAJobsucheProvider().search(
        args.query, args.where, args.radius, args.offer,
        "teilzeit" if args.part_time else "", args.days, args.pages, details=args.details)
    new = 0
    for j in jobs:
        if store.upsert(j):
            new += 1
            print(f"  + [{j.fit_score:>3}] {j.title} — {j.company} ({j.location})")
    print(f"\n{len(jobs)} results · {new} new")


if __name__ == "__main__":
    main()
