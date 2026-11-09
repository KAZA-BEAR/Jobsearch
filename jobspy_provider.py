"""
jobspy_provider.py — fallback job search via LinkedIn/Indeed/Glassdoor/Google/
ZipRecruiter scraping (python-jobspy), for when a direct employer ATS board
has no public API or its slug has gone stale.

robotics_track.py and us_asia_jobs.py poll named employers' own ATS boards
(Greenhouse/Lever/SmartRecruiters/Ashby/Recruitee) directly — accurate and
keyless, but brittle: a board disappears the moment a company switches ATS
vendor or renames its slug, and plenty of employers (Workday, SAP
SuccessFactors, Personio, in-house portals) were never reachable that way at
all. This module does keyword+location search across public job boards
instead, so it keeps working even when the per-company board list is stale
or a company was never on it.

Install: python jobspy_provider.py install
(python-jobspy pins numpy==1.26.3 on PyPI, which has no prebuilt wheel on
many setups — Python 3.13+, or Linux without a C compiler — and fails to
build from source. The install command installs its other dependencies first,
then jobspy itself with --no-deps so it can't pull that pin back in.)

Usage:
    python jobspy_provider.py search "robotics graduate engineer" --location Germany --country-indeed Germany
    python jobspy_provider.py search "mechatronics" --location "Munich, Germany" --country-indeed Germany --sites indeed,linkedin,glassdoor
    python jobspy_provider.py search "new grad robotics" --location "United States" --country-indeed USA --sites indeed,linkedin
    python jobspy_provider.py install      # install python-jobspy without building numpy
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Iterator

from linkedin_jobs import Job, PROVIDERS, Provider, Store


try:
    from jobspy import scrape_jobs
except ImportError:  # pragma: no cover
    scrape_jobs = None


# Countries jobspy's Indeed/Glassdoor scraper recognizes (exact spelling it
# expects for `country_indeed`). Kept here so a plain location string like
# "Germany" can double as both `location` and `country_indeed` without the
# caller having to know the distinction.
INDEED_COUNTRIES = {
    "argentina", "australia", "austria", "bahrain", "belgium", "brazil",
    "canada", "chile", "china", "colombia", "costa rica", "czech republic",
    "denmark", "ecuador", "egypt", "finland", "france", "germany", "greece",
    "hong kong", "hungary", "india", "indonesia", "ireland", "israel",
    "italy", "japan", "kuwait", "luxembourg", "malaysia", "mexico",
    "morocco", "netherlands", "new zealand", "nigeria", "norway", "oman",
    "pakistan", "panama", "peru", "philippines", "poland", "portugal",
    "qatar", "romania", "saudi arabia", "singapore", "south africa",
    "south korea", "spain", "sweden", "switzerland", "taiwan", "thailand",
    "turkey", "ukraine", "united arab emirates", "uk", "usa", "uruguay",
    "venezuela", "vietnam",
}

DEFAULT_SITES = ("indeed", "linkedin", "zip_recruiter", "glassdoor")


class JobSpyProvider(Provider):
    """Adapts python-jobspy's scrape_jobs() to the Job/Provider interface."""

    name = "jobspy"

    def __init__(self, site_name=DEFAULT_SITES, country_indeed: str = "",
                 hours_old: int | None = None):
        if scrape_jobs is None:
            raise SystemExit(
                "python-jobspy is not installed. Run: python jobspy_provider.py install"
            )
        self.site_name = list(site_name)
        self.country_indeed = country_indeed
        self.hours_old = hours_old

    def search(self, query: str, location: str, pages: int, remote: bool) -> Iterator[Job]:
        country = self.country_indeed or (
            location if location.strip().lower() in INDEED_COUNTRIES else ""
        )
        sites = list(self.site_name)
        if not country and any(s in ("indeed", "glassdoor") for s in sites):
            print(
                f"[jobspy] no country_indeed given for location='{location}'; "
                "dropping indeed/glassdoor from this search (they require it)",
                file=sys.stderr,
            )
            sites = [s for s in sites if s not in ("indeed", "glassdoor")]
        if not sites:
            return
        kwargs = dict(
            site_name=sites,
            search_term=query,
            location=location,
            results_wanted=max(pages, 1) * 20,
        )
        if remote:
            kwargs["is_remote"] = True
        if country:
            kwargs["country_indeed"] = country
        if self.hours_old:
            kwargs["hours_old"] = self.hours_old
        if "linkedin" in sites:
            # LinkedIn search results carry no description; without this, all 22
            # LinkedIn jobs in the database had an empty one, so the CV generator
            # had only the title to go on (2026-10-21). One extra request per job.
            kwargs["linkedin_fetch_description"] = True
        print(f"[jobspy] {sites} '{query}' in '{location}'…", file=sys.stderr)
        df = scrape_jobs(**kwargs)
        for _, row in df.iterrows():
            yield self._to_job(row)

    @staticmethod
    def _to_job(row) -> Job:
        import math

        def s(key: str, default: str = "") -> str:
            v = row.get(key)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return default
            return str(v)

        def num(key: str):
            v = row.get(key)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return None
            return v

        title = s("title")
        company = s("company")
        loc = s("location")
        site = s("site", "jobspy")
        posted = row.get("date_posted")
        posted_s = posted.isoformat() if hasattr(posted, "isoformat") else s("date_posted")
        return Job(
            job_id=f"jobspy-{site}-{s('id') or Job.make_id(company, title, loc)}",
            title=title,
            company=company,
            location=loc,
            url=s("job_url_direct") or s("job_url"),
            posted_at=posted_s,
            employment_type=s("job_type"),
            remote=bool(row.get("is_remote")),
            salary_min=num("min_amount"),
            salary_max=num("max_amount"),
            salary_currency=s("currency"),
            description=s("description")[:4000],
            source=f"jobspy:{site}",
        )


PROVIDERS["jobspy"] = JobSpyProvider


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_search(args) -> None:
    provider = JobSpyProvider(
        site_name=[s.strip() for s in args.sites.split(",") if s.strip()],
        country_indeed=args.country_indeed,
        hours_old=args.hours_old,
    )
    store = Store(args.db)
    new = seen = 0
    for job in provider.search(args.query, args.location, args.pages, args.remote):
        seen += 1
        if store.upsert(job):
            new += 1
            print(f"  + [{job.source}] {job.title} — {job.company} ({job.location})")
    print(f"\n{seen} results · {new} new · saved to {args.db}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="jobs.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="keyword+location search across job boards")
    s.add_argument("query")
    s.add_argument("--location", default="")
    s.add_argument("--country-indeed", default="",
                   help="required for indeed/glassdoor, e.g. Germany, USA, Japan")
    s.add_argument("--sites", default=",".join(DEFAULT_SITES),
                   help="comma-separated: indeed,linkedin,zip_recruiter,glassdoor,google,bayt,naukri")
    s.add_argument("--pages", type=int, default=1, help="each page ≈ 20 results_wanted")
    s.add_argument("--hours-old", type=int, default=0)
    s.add_argument("--remote", action="store_true")
    s.set_defaults(func=cmd_search)

    i = sub.add_parser("install", help="install python-jobspy without building numpy")
    i.set_defaults(func=lambda _args: install_jobspy())

    args = p.parse_args()
    args.func(args)



# ==========================================================================
# Installing python-jobspy (was install_jobspy.py)
#
# from source.
# 
# python-jobspy's published wheel pins numpy==1.26.3 exactly. That version has
# no prebuilt wheel for newer Python releases (3.13+) or for some Linux setups,
# so a plain `pip install python-jobspy` falls back to compiling numpy from
# source — which then fails immediately if no C compiler (cc/gcc/clang) is on
# PATH:
# 
#     ERROR: Unable to detect GNU compiler type
# 
# The fix isn't a newer numpy — pip will always try to satisfy jobspy's exact
# pin. Instead: install jobspy's other dependencies first (letting pip pick
# whatever prebuilt wheel already exists for numpy/pandas on this platform),
# then install python-jobspy itself with --no-deps so it can't drag the pinned
# version back in.
# ==========================================================================

DEPS = [
    "numpy", "pandas", "requests", "beautifulsoup4", "pydantic",
    "tls-client", "markdownify", "regex",
]


def pip(*args: str) -> None:
    cmd = [sys.executable, "-m", "pip", *args]
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def install_jobspy() -> None:
    pip("install", "-U", *DEPS)
    pip("install", "--no-deps", "-U", "python-jobspy")
    try:
        import jobspy  # noqa: F401
    except ImportError as exc:
        print(f"\ninstalled, but 'import jobspy' still fails: {exc}", file=sys.stderr)
        sys.exit(1)
    print("\npython-jobspy installed and importable.")


if __name__ == "__main__":
    main()
