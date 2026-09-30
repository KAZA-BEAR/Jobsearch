#!/usr/bin/env python3
"""
robotics_track.py — recent-graduate mechatronics roles + robotics PhD positions in Europe.

Two pipelines:
  1. PhD  — EURAXESS (free, official EU) + academic terminology per country.
  2. Grad — direct ATS board polling (Greenhouse / Lever / SmartRecruiters /
            Ashby / Workday) for European robotics & automation employers.

Usage:
    python robotics_track.py phd --field robotics --countries DE,NL,SE,CH
    python robotics_track.py phd --field "robot learning" --funded-only
    python robotics_track.py grad --tier all
    python robotics_track.py grad --company "Agile Robots,KUKA,ASML"
    python robotics_track.py boards            # list configured employers
    python robotics_track.py report
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

import requests

from linkedin_jobs import Job, PROVIDERS, RateLimiter, Store, request_with_retry
from eu_student_jobs import MARKETS, SENIOR_MARKERS, EuresProvider

# --------------------------------------------------------------------------
# PhD search vocabulary
# --------------------------------------------------------------------------

PHD_TERMS: dict[str, tuple[str, ...]] = {
    "DE": ("Wissenschaftlicher Mitarbeiter", "Doktorand", "PhD student",
           "Promotionsstelle", "Research Associate"),
    "AT": ("Universitätsassistent", "Doktorand", "prae doc", "PhD"),
    "CH": ("Doctoral Assistant", "PhD student", "Doktorand", "assistant doctorant"),
    "NL": ("PhD candidate", "promovendus", "PhD position"),
    "SE": ("doktorand", "PhD student", "doctoral student"),
    "DK": ("PhD fellow", "PhD stipend", "ph.d.-stipendiat"),
    "FI": ("doctoral researcher", "PhD student", "tohtorikoulutettava"),
    "FR": ("doctorant", "thèse", "PhD position", "contrat doctoral"),
    "IT": ("dottorato", "PhD position", "assegno di ricerca"),
    "ES": ("doctorado", "PhD position", "contrato predoctoral", "FPI"),
    "PT": ("doutoramento", "PhD position", "bolsa de doutoramento"),
    "BE": ("PhD student", "doctoraatsbursaal", "aspirant FNRS"),
    "PL": ("doktorant", "PhD student", "szkoła doktorska"),
    "IE": ("PhD scholarship", "structured PhD", "PhD researcher"),
    "CZ": ("PhD student", "doktorand"),
}

ROBOTICS_FIELDS = (
    "robotics", "mechatronics", "autonomous systems", "control engineering",
    "motion planning", "robot learning", "manipulation", "SLAM",
    "computer vision robotics", "human-robot interaction", "soft robotics",
    "medical robotics", "field robotics", "legged locomotion", "embodied AI",
)

PHD_MARKERS = re.compile(
    r"(doktorand|promotion|promovend|ph\.?\s?d\b|doctoral|doctorant|dottorato|"
    r"doctorado|doutoramento|doktorant|wissenschaftliche[rn]?\s+mitarbeiter|"
    r"research associate|research assistant|prae.?doc)", re.IGNORECASE,
)

FUNDING_MARKERS = re.compile(
    r"\b(fully funded|funded position|salary|stipend|TV-?L|E13|EG\s?13|"
    r"MSCA|Marie\s*(Skłodowska-)?Curie|ERC|salaried|employment contract|"
    r"doktorandanställning|contrat doctoral)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# EURAXESS — free EU research jobs portal
# --------------------------------------------------------------------------

class PhdProvider(EuresProvider):
    """Doctoral positions via the EURES API's native education-level filter.

    Replaces the earlier EURAXESS client, whose endpoint I could not verify.
    EURES exposes educationAndQualificationLevelCodes=["doctoral"], which is a
    structured filter rather than keyword guessing, so it catches posts titled
    "Wissenschaftlicher Mitarbeiter" or "doktorand" that never say "PhD".
    """

    name = "phd"

    def search_phd(self, field: str, country: str, pages: int,
                   on_log=None, use_level_filter: bool = True) -> list[Job]:
        filters = {"educationAndQualificationLevelCodes": ["doctoral"]} if use_level_filter else {}
        jobs = self.search(field, "", pages, False, country=country,
                           on_log=on_log, **filters)
        if not jobs and use_level_filter:
            # Many countries (Germany especially) leave the education level
            # "not specified" on nearly every posting, so the structured filter
            # returns nothing. Fall back to the keyword search and keep only
            # postings that are recognisably doctoral positions.
            if on_log:
                on_log(f"[phd] {country}: no postings tagged doctoral; "
                       "falling back to keyword search")
            jobs = [j for j in self.search(field, "", pages, False, country=country,
                                           on_log=on_log)
                    if PHD_MARKERS.search(f"{j.title} {j.description[:1500]}")]
        for j in jobs:
            j.employment_type = "phd"
        return jobs


class InternshipProvider(EuresProvider):
    """Internships and apprenticeships via the position-offering filter."""

    name = "eures-intern"

    def search_intern(self, field: str, country: str, pages: int, on_log=None) -> list[Job]:
        return self.search(
            field, "", pages, False, country=country, on_log=on_log,
            positionOfferingCodes=["internship", "apprenticeship"],
        )


PROVIDERS["phd"] = PhdProvider
PROVIDERS["euraxess"] = PhdProvider


# --------------------------------------------------------------------------
# ATS boards — European robotics / mechatronics employers
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Board:
    company: str
    ats: str          # greenhouse | lever | smartrecruiters | ashby | recruitee
    slug: str
    country: str
    tier: str = "robotics"   # robotics | automation | mobility | semiconductor


# Checked live 2026-09-23. Employers whose old board had gone dead were moved:
# Magazino, Franka Robotics, NEURA Robotics, Wandelbots -> personio_jobs.py;
# ZEISS, ASML -> workday_jobs.py. ats="none" means no public API was found:
# the board is not polled, but the LinkedIn/Indeed fallback still searches the
# company by name, and most German ones post on the Arbeitsagentur anyway.
BOARDS: tuple[Board, ...] = (
    # Pure-play robotics
    Board("Dexory", "greenhouse", "dexory", "GB", "robotics"),
    Board("Wayve", "greenhouse", "wayve", "GB", "mobility"),
    Board("Ocado Technology", "greenhouse", "ocadogroup", "GB", "robotics"),
    Board("ANYbotics", "lever", "anybotics", "CH", "robotics"),
    Board("Robco", "ashby", "robco", "DE", "robotics"),
    Board("Agile Robots", "none", "", "DE", "robotics"),
    Board("Universal Robots", "none", "", "DK", "robotics"),
    Board("Mobile Industrial Robots", "none", "", "DK", "robotics"),
    Board("Verity", "none", "", "CH", "robotics"),
    Board("Flexiv Europe", "none", "", "DE", "robotics"),
    Board("Avular", "none", "", "NL", "robotics"),
    Board("Lely", "none", "", "NL", "automation"),
    Board("Cognibotics", "none", "", "SE", "robotics"),
    # Automation / industrial (own career portals; covered by the Arbeitsagentur)
    Board("Festo", "none", "", "DE", "automation"),
    Board("Beckhoff", "none", "", "DE", "automation"),
    Board("Trumpf", "none", "", "DE", "automation"),
    Board("Sick AG", "none", "", "DE", "automation"),
    Board("Schunk", "none", "", "DE", "automation"),
    # Semiconductor / precision
    Board("VDL ETG", "none", "", "NL", "semiconductor"),
    # Mobility / aerospace
    Board("Einride", "none", "", "SE", "mobility"),
    Board("Zeitview", "none", "", "DE", "mobility"),
)

ATS_ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
    "recruitee": "https://{slug}.recruitee.com/api/offers/",
    "workday": None,
    "none": None,     # no public board found: skipped, searched by name via fallback
}

# Fallback URL templates to try when the primary API endpoint returns a non-JSON
# response or a 4xx/5xx status. The first reachable JSON response will be parsed
# by the existing _parse() helpers; if a web page (HTML) is reached we log it so
# it can be scraped later without failing the whole run.
ATS_FALLBACKS = {
    "greenhouse": [
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        "https://boards.greenhouse.io/{slug}",
    ],
    "lever": [
        "https://api.lever.co/v0/postings/{slug}?mode=json",
        "https://jobs.lever.co/{slug}",
    ],
    "smartrecruiters": [
        "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
        "https://www.smartrecruiters.com/{slug}/jobs",
    ],
    "ashby": [
        "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
        "https://{slug}.ashbyhq.com/",
    ],
    "recruitee": [
        "https://{slug}.recruitee.com/api/offers/",
        "https://{slug}.recruitee.com/",
    ],
}

COUNTRY_NAMES = {
    "DE": "Germany", "NL": "Netherlands", "CH": "Switzerland", "GB": "UK",
    "DK": "Denmark", "SE": "Sweden", "FR": "France", "IT": "Italy",
    "ES": "Spain", "PT": "Portugal", "BE": "Belgium", "PL": "Poland",
    "IE": "Ireland", "CZ": "Czech Republic", "AT": "Austria",
}

GRAD_MARKERS = re.compile(
    r"\b(graduate|junior|entry[- ]level|new grad|einsteiger|absolvent|"
    r"trainee|starter|nyexaminerad|jeune diplômé|associate engineer|"
    r"engineer\s*(i|1)\b)", re.IGNORECASE,
)
MECHATRONICS_MARKERS = re.compile(
    r"\b(mechatronic|robotic|control (engineer|system)|motion control|"
    r"automation engineer|embedded|mechanical engineer|electrical engineer|"
    r"systems engineer|firmware|PLC|kinematic|actuator|servo|perception|"
    r"motion planning|ROS\b)", re.IGNORECASE,
)


class ATSBoards:
    """Fetch postings straight from employers' applicant-tracking systems."""

    def __init__(self, rpm: int = 40):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "robotics-track/1.0"})
        self.limiter = RateLimiter(rpm)

    def fetch(self, board: Board) -> list[Job]:
        """Fetch postings for a single board, with fallbacks.

        Try the primary API template first; if it returns JSON we parse it as
        before. If that fails, attempt known fallback URL patterns for the ATS
        vendor. If a fallback returns JSON we parse it; if it returns HTML we
        log that the vendor web page is reachable so scraping can be added later
        without failing the whole job.
        """
        if board.ats == "none":
            return []
        tmpl = ATS_ENDPOINTS.get(board.ats)
        self.limiter.wait()

        # Try primary template first (if present)
        if tmpl:
            url = tmpl.format(slug=board.slug)
            try:
                resp = request_with_retry(self.session, "GET", url)
                # Prefer parsing JSON responses
                try:
                    data = resp.json()
                    return list(self._parse(board, data))
                except ValueError:
                    # Not JSON — fall through to fallbacks
                    pass
                if resp.status_code >= 400:
                    # Fall through to fallbacks
                    pass
            except Exception as exc:
                print(f"  ! {board.company}: {exc}", file=sys.stderr)

        # Try fallback patterns (may be a web page or alternate API)
        fallbacks = ATS_FALLBACKS.get(board.ats, [])
        for tpl in fallbacks:
            if not tpl:
                continue
            url = tpl.format(slug=board.slug)
            try:
                resp = request_with_retry(self.session, "GET", url)
            except Exception:
                continue
            ctype = resp.headers.get("Content-Type", "")
            # If we got JSON, try to parse and feed it to the existing parser
            if "application/json" in ctype or resp.text.strip().startswith(("{",
                                                                                 "[")):
                try:
                    data = resp.json()
                    return list(self._parse(board, data))
                except Exception:
                    # If parsing fails, continue to next fallback
                    continue
            # If the fallback is an HTML page, attempt rudimentary parsing for
            # common ATS vendors (greenhouse, recruitee, lever). This uses a
            # lightweight regex-based extractor to find job links/titles; it's
            # intentionally conservative and can be replaced with a proper
            # HTML parser (BeautifulSoup) if needed.
            if resp.status_code == 200 and "text/html" in ctype:
                try:
                    return list(self._parse_html(board, resp.text))
                except Exception as e:
                    print(f"  * {board.company}: fallback page reachable but parsing failed: {e}", file=sys.stderr)
                    return []

        # Nothing worked for this board
        return []

    def _parse(self, board: Board, data):
        if board.ats == "greenhouse":
            rows = data.get("jobs", [])
            for r in rows:
                yield self._job(board, r.get("id"), r.get("title"),
                                (r.get("location") or {}).get("name"),
                                r.get("absolute_url"), r.get("updated_at"),
                                r.get("content", ""))
        elif board.ats == "lever":
            for r in data if isinstance(data, list) else []:
                cats = r.get("categories") or {}
                yield self._job(board, r.get("id"), r.get("text"),
                                cats.get("location"), r.get("hostedUrl"),
                                r.get("createdAt"), r.get("descriptionPlain", ""))
        elif board.ats == "smartrecruiters":
            for r in data.get("content", []):
                loc = r.get("location") or {}
                yield self._job(board, r.get("id"), r.get("name"),
                                loc.get("city"),
                                f"https://jobs.smartrecruiters.com/{board.slug}/{r.get('id')}",
                                r.get("releasedDate"), "")
        elif board.ats == "ashby":
            for r in data.get("jobs", []):
                yield self._job(board, r.get("id"), r.get("title"),
                                r.get("location"), r.get("jobUrl"),
                                r.get("publishedAt"), r.get("descriptionPlain", ""))
        elif board.ats == "recruitee":
            for r in data.get("offers", []):
                yield self._job(board, r.get("id"), r.get("title"),
                                r.get("location"), r.get("careers_url"),
                                r.get("published_at"), r.get("description", ""))

    def _parse_html(self, board: Board, html_text: str):
        import re
        # Find anchors and attempt to heuristically extract job links and titles.
        anchors = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html_text, flags=re.I|re.S)
        for href, text in anchors:
            # Strip inner tags from link text
            title = re.sub(r'<[^>]+>', '', text).strip()
            if board.ats == "greenhouse":
                if '/jobs/' in href or '/job/' in href:
                    url = href if href.startswith('http') else f'https://boards.greenhouse.io{href}'
                    jid = href.rstrip('/').split('/')[-1]
                    yield self._job(board, jid, title, board.country, url, "", "")
            elif board.ats == "recruitee":
                if '/offer/' in href or '/jobs/' in href or '/offers/' in href:
                    url = href if href.startswith('http') else f'https://{board.slug}.recruitee.com{href}'
                    jid = href.rstrip('/').split('/')[-1]
                    yield self._job(board, jid, title, board.country, url, "", "")
            elif board.ats == "lever":
                if '/jobs/' in href or href.startswith('/') or 'jobs.lever.co' in href:
                    url = href if href.startswith('http') else f'https://jobs.lever.co{href}'
                    jid = href.rstrip('/').split('/')[-1]
                    yield self._job(board, jid, title, board.country, url, "", "")
        return

    @staticmethod
    def _job(board: Board, jid, title, loc, url, posted, desc) -> Job:
        title = title or ""
        loc = loc or board.country
        return Job(
            job_id=f"{board.ats}-{board.slug}-{jid}",
            title=title,
            company=board.company,
            location=str(loc),
            url=url or "",
            posted_at=str(posted or ""),
            employment_type="graduate",
            description=re.sub(r"<[^>]+>", " ", str(desc))[:4000],
            source=f"ats:{board.ats}",
        )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_phd(args) -> None:
    provider = PhdProvider()
    store = Store(args.db)
    codes = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    fields = [args.field] if args.field else list(ROBOTICS_FIELDS[:6])
    new = seen = filtered = 0

    for code in codes:
        terms = PHD_TERMS.get(code, ("PhD position",))
        print(f"\n=== {MARKETS[code].name if code in MARKETS else code} ===")
        for field in fields:
            for term in terms[: args.max_terms]:
                query = f"{term} {field}"
                try:
                    for job in provider.search_phd(query, code, args.pages):
                        seen += 1
                        if args.funded_only and not FUNDING_MARKERS.search(
                            job.title + " " + job.description
                        ):
                            filtered += 1
                            continue
                        if SENIOR_MARKERS.search(job.title):
                            filtered += 1
                            continue
                        if store.upsert(job):
                            new += 1
                            print(f"  + {job.title} — {job.company} ({job.location})")
                except KeyboardInterrupt:
                    return
    print(f"\n{seen} seen · {new} new · {filtered} filtered")
    print("\nAlso worth checking manually (no clean API):")
    print("  • MSCA Doctoral Networks — fully funded, mobility allowance, "
          "listed on EURAXESS under 'MSCA'")
    print("  • ELLIS PhD Program — ellis.eu/phd-postdoc (robot learning, one application, many labs)")
    print("  • Max Planck IS (Stuttgart/Tübingen), IIT Genoa, DLR, ETH RSL, TU Delft Cognitive Robotics")


def _grad_keep(job: Job, strict: bool) -> tuple[bool, bool]:
    """Returns (keep, is_grad) applying the mechatronics/grad/senior filters."""
    blob = f"{job.title} {job.description[:800]}"
    if not MECHATRONICS_MARKERS.search(blob):
        return False, False
    if SENIOR_MARKERS.search(job.title):
        return False, False
    is_grad = bool(GRAD_MARKERS.search(blob))
    if strict and not is_grad:
        return False, is_grad
    return True, is_grad


def cmd_grad(args) -> None:
    boards = BOARDS
    if args.company:
        wanted = {c.strip().lower() for c in args.company.split(",")}
        boards = tuple(b for b in BOARDS if b.company.lower() in wanted)
        if not boards:
            raise SystemExit("no matching employers; run 'boards' to list them")
    elif args.tier != "all":
        boards = tuple(b for b in BOARDS if b.tier == args.tier)

    fetcher = ATSBoards()
    store = Store(args.db)
    new = seen = filtered = 0
    dead_boards: list[Board] = []

    for board in boards:
        print(f"[{board.ats}] {board.company}…", file=sys.stderr)
        postings = fetcher.fetch(board)
        if not postings:
            dead_boards.append(board)
        for job in postings:
            seen += 1
            keep, is_grad = _grad_keep(job, args.strict)
            if not keep:
                filtered += 1
                continue
            job.employment_type = "graduate" if is_grad else "entry-candidate"
            if store.upsert(job):
                new += 1
                tag = "GRAD" if is_grad else "  ? "
                print(f"  [{tag}] {job.title} — {job.company} ({job.location})")

    print(f"\n{seen} postings scanned · {new} new · {filtered} filtered")

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
        for board in dead_boards:
            country = COUNTRY_NAMES.get(board.country, "")
            location = country or board.country
            try:
                jobs = list(provider.search(
                    f'"{board.company}" graduate OR junior OR entry level',
                    location, 1, False,
                ))
            except Exception as exc:
                print(f"  ! {board.company}: {exc}", file=sys.stderr)
                continue
            wanted_name = re.sub(r"[^a-z0-9]", "", board.company.lower())
            for job in jobs:
                fb_seen += 1
                got_name = re.sub(r"[^a-z0-9]", "", job.company.lower())
                if not got_name or (wanted_name not in got_name and got_name not in wanted_name):
                    continue  # search matched an unrelated employer (or company field missing)
                job.employment_type = "graduate" if GRAD_MARKERS.search(job.title) else "entry-candidate"
                if store.upsert(job):
                    fb_new += 1
                    print(f"  [jobspy] {job.title} — {job.company} ({job.location})")
        print(f"\nfallback: {fb_seen} scanned · {fb_new} new")


def cmd_boards(args) -> None:
    for tier in sorted({b.tier for b in BOARDS}):
        print(f"\n{tier}:")
        for b in BOARDS:
            if b.tier == tier:
                print(f"  {b.company:<26} {b.country}  ({b.ats})")


def cmd_report(args) -> None:
    rows = Store(args.db).all()
    if not rows:
        print("nothing stored yet")
        return
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["employment_type"] or "other"] = kinds.get(r["employment_type"] or "other", 0) + 1
    print(f"{len(rows)} roles stored\n")
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {v:>4}  {k}")
    print("\nmost recent:")
    for r in rows[:10]:
        print(f"  {r['title'][:55]:<55} {r['company'][:20]:<20} {r['location'][:18]}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="robotics_jobs.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("phd", help="robotics PhD positions via EURAXESS")
    d.add_argument("--field", default="", help="e.g. robotics, 'robot learning', SLAM")
    d.add_argument("--countries", default="DE,NL,SE,CH,DK,FR")
    d.add_argument("--pages", type=int, default=2)
    d.add_argument("--max-terms", type=int, default=3)
    d.add_argument("--funded-only", action="store_true",
                   help="keep only positions showing salary/stipend/TV-L/MSCA markers")
    d.set_defaults(func=cmd_phd)

    g = sub.add_parser("grad", help="graduate mechatronics roles from employer ATS boards")
    g.add_argument("--tier", default="all",
                   choices=["all", "robotics", "automation", "mobility", "semiconductor"])
    g.add_argument("--company", help="comma-separated employer names")
    g.add_argument("--strict", action="store_true", help="only explicit graduate/junior roles")
    g.add_argument("--jobspy-fallback", action="store_true",
                   help="for boards that returned nothing, search LinkedIn/Indeed "
                        "for that company instead (needs python-jobspy, see requirements.txt)")
    g.set_defaults(func=cmd_grad)

    b = sub.add_parser("boards", help="list configured employers")
    b.set_defaults(func=cmd_boards)

    r = sub.add_parser("report", help="summary of stored roles")
    r.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
