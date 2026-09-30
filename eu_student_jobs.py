#!/usr/bin/env python3
"""
eu_student_jobs.py — European student / entry-level job targeting.

Extends linkedin_jobs.py with:
  * localised student search terms per country (Werkstudent, alternance, stage...)
  * a free EURES provider (European Commission, no API key needed)
  * a classifier that tags roles as internship / working-student / graduate / thesis
  * filters that drop senior roles that leak into student queries

Usage:
    python eu_student_jobs.py sweep --countries DE,NL,FR --field "data science"
    python eu_student_jobs.py sweep --countries DE --kind werkstudent --provider eures
    python eu_student_jobs.py report
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from linkedin_jobs import (
    Job,
    Provider,
    PROVIDERS,
    RateLimiter,
    Store,
    request_with_retry,
)

# --------------------------------------------------------------------------
# Country configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Market:
    code: str
    name: str
    cities: tuple[str, ...]
    internship: tuple[str, ...]
    working_student: tuple[str, ...]
    graduate: tuple[str, ...]
    thesis: tuple[str, ...] = ()
    note: str = ""


MARKETS: dict[str, Market] = {
    "DE": Market(
        "DE", "Germany",
        ("Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne", "Stuttgart"),
        ("Praktikum", "Pflichtpraktikum", "internship", "Praktikant"),
        ("Werkstudent", "Werkstudentin", "working student"),
        ("Absolvent", "Berufseinsteiger", "graduate", "Trainee", "Junior"),
        ("Abschlussarbeit", "Masterarbeit", "Bachelorarbeit", "thesis"),
        "Werkstudent roles are the volume play: ~20h/week, capped at 20h during term.",
    ),
    "AT": Market(
        "AT", "Austria", ("Vienna", "Graz", "Linz", "Salzburg"),
        ("Praktikum", "Praktikant", "internship"),
        ("Werkstudent", "geringfügig"),
        ("Trainee", "Junior", "Berufseinsteiger"),
        ("Diplomarbeit", "Masterarbeit"),
    ),
    "CH": Market(
        "CH", "Switzerland", ("Zurich", "Geneva", "Lausanne", "Basel", "Bern"),
        ("Praktikum", "stage", "internship"),
        ("Werkstudent", "Studentenjob"),
        ("Trainee", "Junior", "graduate programme"),
        ("Masterarbeit", "travail de master"),
        "Not EU — needs a residence permit; students may work 15h/week during term.",
    ),
    "NL": Market(
        "NL", "Netherlands", ("Amsterdam", "Rotterdam", "Utrecht", "Eindhoven", "Delft"),
        ("stage", "stagiair", "internship"),
        ("bijbaan", "werkstudent", "part-time student"),
        ("traineeship", "starter", "junior", "graduate"),
        ("afstudeerstage", "afstudeeropdracht", "graduation internship"),
        "Most tech roles are advertised in English; search both languages.",
    ),
    "FR": Market(
        "FR", "France", ("Paris", "Lyon", "Toulouse", "Nantes", "Bordeaux", "Lille"),
        ("stage", "stagiaire", "internship"),
        ("alternance", "apprentissage", "contrat de professionnalisation"),
        ("jeune diplômé", "junior", "VIE", "graduate programme"),
        ("stage de fin d'études", "PFE"),
        "Alternance is the big one — paid, structured, and employers get subsidies for it.",
    ),
    "ES": Market(
        "ES", "Spain", ("Madrid", "Barcelona", "Valencia", "Seville", "Malaga"),
        ("prácticas", "becario", "internship"),
        ("estudiante", "media jornada"),
        ("junior", "recién graduado", "programa de graduados"),
        ("trabajo fin de grado", "TFM"),
    ),
    "IT": Market(
        "IT", "Italy", ("Milan", "Rome", "Turin", "Bologna", "Florence"),
        ("tirocinio", "stage", "internship"),
        ("studente", "part-time"),
        ("junior", "neolaureato", "graduate program"),
        ("tesi", "tesi di laurea"),
    ),
    "PL": Market(
        "PL", "Poland", ("Warsaw", "Krakow", "Wroclaw", "Gdansk", "Poznan"),
        ("staż", "praktyki", "internship"),
        ("praca dla studenta", "student"),
        ("junior", "absolwent", "trainee"),
        ("praca dyplomowa",),
        "Huge shared-services and R&D hub market; most postings accept English CVs.",
    ),
    "SE": Market(
        "SE", "Sweden", ("Stockholm", "Gothenburg", "Malmö", "Lund", "Uppsala"),
        ("praktik", "praktikant", "internship"),
        ("studentmedarbetare", "extrajobb"),
        ("junior", "traineeprogram", "nyexaminerad"),
        ("exjobb", "examensarbete"),
        "Exjobb (paid thesis projects) are a standard route into Swedish employers.",
    ),
    "DK": Market(
        "DK", "Denmark", ("Copenhagen", "Aarhus", "Odense", "Aalborg"),
        ("praktik", "praktikant", "internship"),
        ("studiejob", "studentermedhjælper"),
        ("junior", "graduate", "nyuddannet"),
        ("speciale", "bachelorprojekt"),
    ),
    "IE": Market(
        "IE", "Ireland", ("Dublin", "Cork", "Galway", "Limerick"),
        ("internship", "intern", "work placement"),
        ("part-time student", "student assistant"),
        ("graduate programme", "graduate", "junior"),
    ),
    "PT": Market(
        "PT", "Portugal", ("Lisbon", "Porto", "Braga", "Coimbra"),
        ("estágio", "estagiário", "internship"),
        ("part-time", "estudante"),
        ("junior", "recém-licenciado", "trainee"),
        ("tese", "dissertação"),
    ),
    "BE": Market(
        "BE", "Belgium", ("Brussels", "Antwerp", "Ghent", "Leuven"),
        ("stage", "stagiair", "internship"),
        ("studentenjob", "jobstudent", "job étudiant"),
        ("junior", "starter", "graduate programme"),
        ("masterproef", "mémoire"),
        "Jobstudent contracts carry reduced social-security rates up to 600h/year.",
    ),
    "CZ": Market(
        "CZ", "Czechia", ("Prague", "Brno", "Ostrava"),
        ("stáž", "praxe", "internship"),
        ("brigáda", "práce pro studenty"),
        ("junior", "absolvent", "trainee"),
        ("diplomová práce",),
    ),
    "FI": Market(
        "FI", "Finland", ("Helsinki", "Espoo", "Tampere", "Oulu"),
        ("harjoittelu", "harjoittelija", "internship"),
        ("osa-aikainen", "opiskelija"),
        ("junior", "trainee", "graduate"),
        ("diplomityö", "opinnäytetyö"),
    ),
}

KINDS = ("internship", "werkstudent", "graduate", "thesis")

# Terms that signal the role is NOT student-appropriate despite matching the query.
SENIOR_MARKERS = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|head of|director|manager|architect|"
    r"chef de projet|teamleiter|expert|vp|chief)\b",
    re.IGNORECASE,
)
EXPERIENCE_DEMAND = re.compile(
    r"\b([5-9]|1\d)\+?\s*(years?|jahre|ans|años|jaar|lat)\b", re.IGNORECASE
)


# --------------------------------------------------------------------------
# EURES provider — free, official, no key
# --------------------------------------------------------------------------

class EuresProvider(Provider):
    """European Commission EURES portal — official public search API.

    Endpoint and payload follow the documented contract at
    https://github.com/rorar/EURES-API-Documentation

    Notes that cost real debugging time:
      * locationCodes are LOWERCASE NUTS codes ("de", not "DE").
      * The request body must carry every filter array, even when empty;
        omitting them yields an error, not an empty result set.
      * creationDate is epoch MILLISECONDS, not a string.
      * description is HTML.
    """

    name = "eures"
    URL = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
    DETAIL = "https://europa.eu/eures/portal/jv-se/jv-details/{id}"

    def __init__(self, rpm: int = 30, request_language: str = "en"):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; eu-student-jobs/1.1)",
            "Origin": "https://europa.eu",
            "Referer": "https://europa.eu/eures/portal/jv-se/search",
        })
        self.limiter = RateLimiter(rpm)
        self.lang = request_language
        self.session_id = f"jobsearch-{uuid.uuid4().hex[:12]}"
        self.last_total = 0

    def build_body(self, query: str, page: int, country: str = "",
                   per_page: int = 50, **filters) -> dict:
        """Exactly the fields in the documented working request, defaulted.

        Field order and names match the EURES public API contract. Adding
        undocumented fields (e.g. userPreferredLanguage) or an invalid
        sortSearch value causes the backend to return zero rows rather than
        an error, so we keep this list tight. filters overrides any of them.
        """
        body = {
            "resultsPerPage": per_page,
            "page": page,
            "sortSearch": "BEST_MATCH",
            "keywords": ([{"keyword": query, "specificSearchCode": "EVERYWHERE"}]
                         if query else []),
            "publicationPeriod": None,
            "occupationUris": [],
            "skillUris": [],
            "requiredExperienceCodes": [],
            "positionScheduleCodes": [],
            "sectorCodes": [],
            "educationAndQualificationLevelCodes": [],
            "positionOfferingCodes": [],
            "locationCodes": [country.lower()] if country else [],
            "euresFlagCodes": [],
            "otherBenefitsCodes": [],
            "requiredLanguages": [],
            "minNumberPost": None,
            "sessionId": self.session_id,
            "requestLanguage": self.lang,
        }
        body.update(filters)
        return body

    # The response has been observed to use several different key names for the
    # records array and the total, depending on portal version. Read all of them.
    _RECORD_KEYS = ("jvs", "jvItems", "items", "results", "data", "content", "vacancies")
    _TOTAL_KEYS = ("numberRecords", "totalRecords", "totalResults", "total",
                   "numberOfResults", "count", "numFound")

    @classmethod
    def _extract_records(cls, payload):
        """Find the records array and total no matter which key the API used."""
        if not isinstance(payload, dict):
            return [], 0
        records = []
        for k in cls._RECORD_KEYS:
            v = payload.get(k)
            if isinstance(v, list) and v:
                records = v
                break
        # Total may be nested (e.g. {"hits": {"total": {"value": N}}})
        total = 0
        for k in cls._TOTAL_KEYS:
            v = payload.get(k)
            if isinstance(v, int):
                total = v
                break
            if isinstance(v, dict) and isinstance(v.get("value"), int):
                total = v["value"]
                break
        # Elasticsearch-style: {"hits": {"hits": [...], "total": {...}}}
        if not records and isinstance(payload.get("hits"), dict):
            inner = payload["hits"]
            if isinstance(inner.get("hits"), list):
                records = inner["hits"]
            t = inner.get("total")
            if isinstance(t, dict) and isinstance(t.get("value"), int):
                total = t["value"]
            elif isinstance(t, int):
                total = t
        if not total and records:
            total = len(records)
        return records, total

    def ping(self, on_log=None) -> int:
        """Hit the statistics endpoint to prove the API is reachable.

        Returns the total number of active EURES vacancies, or -1 if the call
        failed. This is independent of any search query, so it separates
        'the API is down / blocked' from 'my query matched nothing'.
        """
        url = "https://europa.eu/eures/api/jv-searchengine/public/statistics/getNumberOfJobs"
        try:
            resp = request_with_retry(self.session, "GET", url, tries=2)
            data = resp.json()
            n = data if isinstance(data, int) else data.get("value", data.get("count", 0))
            msg = f"[eures] connection OK — {n:,} active vacancies portal-wide"
            print(msg, file=sys.stderr)
            if on_log:
                on_log(msg)
            return int(n)
        except Exception as exc:
            msg = (f"[eures] CONNECTION FAILED: {exc}. The portal may be blocking "
                   "automated access from your network, or is temporarily down.")
            print(msg, file=sys.stderr)
            if on_log:
                on_log(msg)
            return -1

    def search(self, query, location, pages, remote, country: str = "",
               on_log=None, **filters) -> "list[Job]":
        """Raises on failure. Callers decide how to surface the error."""
        out: list[Job] = []
        for page in range(1, pages + 1):
            self.limiter.wait()
            body = self.build_body(query, page, country, **filters)
            resp = request_with_retry(self.session, "POST", self.URL, json=body)
            try:
                payload = resp.json()
            except ValueError:
                msg = f"[eures] {country or 'EEA'}: response was not JSON (HTTP {resp.status_code})"
                print(msg, file=sys.stderr)
                if on_log:
                    on_log(msg)
                break

            records, total = self._extract_records(payload)
            self.last_total = total

            # Diagnostic: if the API returned a dict we didn't understand, show its keys
            # once so the failure is debuggable instead of a silent "0 of 0".
            if not records and isinstance(payload, dict) and page == 1:
                keys = ", ".join(list(payload.keys())[:12]) or "(empty object)"
                diag = f"[eures] {country or 'EEA'} · {query or '(any)'}: 0 results. Response keys: {keys}"
                print(diag, file=sys.stderr)
                if on_log:
                    on_log(diag)
            else:
                msg = (f"[eures] {country or 'EEA'} · {query or '(any)'} · "
                       f"page {page}: {len(records)} of {total}")
                print(msg, file=sys.stderr)
                if on_log:
                    on_log(msg)

            for r in records:
                # ES-style hits wrap the doc in _source
                src = r.get("_source") if isinstance(r, dict) and "_source" in r else r
                out.append(self._to_job(src, country))
            if len(records) < body["resultsPerPage"]:
                break
        return out

    @classmethod
    def _to_job(cls, r: dict, country: str) -> Job:
        employer = r.get("employer") or {}
        # Some records carry the employer as a bare string instead of an object.
        if isinstance(employer, dict):
            company = employer.get("name") or ""
        else:
            company = str(employer)
        title = r.get("title") or ""

        # locationMap: {"DE": ["DE12B"]} — country code to NUTS regions
        loc = country.upper()
        lm = r.get("locationMap")
        if isinstance(lm, dict) and lm:
            parts = []
            for cc, regions in lm.items():
                regs = [x for x in (regions or []) if x]
                parts.append(f"{cc} ({', '.join(regs)})" if regs else cc)
            loc = "; ".join(parts)

        # creationDate is epoch milliseconds
        posted = ""
        raw = r.get("creationDate") or r.get("lastModificationDate")
        if isinstance(raw, (int, float)) and raw > 0:
            try:
                posted = datetime.fromtimestamp(raw / 1000, tz=timezone.utc).isoformat(
                    timespec="seconds")
            except (ValueError, OSError):
                posted = ""

        desc = re.sub(r"<[^>]+>", " ", r.get("description") or "")
        desc = re.sub(r"\s+", " ", desc).strip()

        jid = str(r.get("id") or Job.make_id(company, title, loc))
        schedules = r.get("positionScheduleCodes") or []
        offering = r.get("positionOfferingCode") or ""

        return Job(
            job_id=f"eures-{jid}",
            title=title,
            company=company,
            location=loc,
            url=cls.DETAIL.format(id=quote(jid, safe="")),
            posted_at=posted,
            employment_type=offering or (schedules[0] if schedules else ""),
            remote=False,
            description=desc[:4000],
            source="eures",
        )


PROVIDERS["eures"] = EuresProvider


# --------------------------------------------------------------------------
# Classification + filtering
# --------------------------------------------------------------------------

def classify(job: Job, market: Market) -> str | None:
    blob = f"{job.title} {job.description[:600]}".lower()
    buckets = (
        ("thesis", market.thesis),
        ("werkstudent", market.working_student),
        ("internship", market.internship),
        ("graduate", market.graduate),
    )
    for label, terms in buckets:
        if any(t.lower() in blob for t in terms):
            return label
    return None


def is_student_suitable(job: Job) -> tuple[bool, str]:
    title = job.title
    if SENIOR_MARKERS.search(title):
        return False, "senior-level title"
    m = EXPERIENCE_DEMAND.search(job.description[:1500])
    if m:
        return False, f"requires {m.group(0)}"
    return True, ""


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_sweep(args) -> None:
    codes = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    unknown = [c for c in codes if c not in MARKETS]
    if unknown:
        raise SystemExit(f"unknown country codes: {', '.join(unknown)}. "
                         f"Available: {', '.join(sorted(MARKETS))}")

    provider = PROVIDERS[args.provider]()
    store = Store(args.db)
    kinds = [args.kind] if args.kind else list(KINDS)
    total_new = total_seen = skipped = 0

    for code in codes:
        market = MARKETS[code]
        print(f"\n=== {market.name} ===")
        if market.note:
            print(f"    {market.note}")
        terms: list[str] = []
        for kind in kinds:
            attr = {"internship": "internship", "werkstudent": "working_student",
                    "graduate": "graduate", "thesis": "thesis"}[kind]
            terms.extend(getattr(market, attr))

        for term in terms[: args.max_terms]:
            query = f"{term} {args.field}".strip()
            locations = market.cities[: args.max_cities] if args.by_city else (market.name,)
            for loc in locations:
                try:
                    if isinstance(provider, EuresProvider):
                        results = provider.search(query, loc, args.pages, False, country=code)
                    else:
                        results = provider.search(query, loc, args.pages, args.remote)
                    for job in results:
                        total_seen += 1
                        ok, reason = is_student_suitable(job)
                        if not ok:
                            skipped += 1
                            continue
                        label = classify(job, market)
                        if args.strict and label is None:
                            skipped += 1
                            continue
                        job.employment_type = label or job.employment_type
                        if store.upsert(job):
                            total_new += 1
                            tag = f"[{label}]" if label else ""
                            print(f"  + {tag} {job.title} — {job.company} ({job.location})")
                except KeyboardInterrupt:
                    print("\ninterrupted", file=sys.stderr)
                    return
                except Exception as exc:
                    print(f"  ! {query} @ {loc}: {exc}", file=sys.stderr)

    print(f"\n{total_seen} seen · {total_new} new · {skipped} filtered out")


def cmd_report(args) -> None:
    rows = Store(args.db).all()
    if not rows:
        print("no jobs stored yet — run a sweep first")
        return
    by_kind: dict[str, int] = {}
    by_country: dict[str, int] = {}
    for r in rows:
        by_kind[r["employment_type"] or "other"] = by_kind.get(r["employment_type"] or "other", 0) + 1
        loc = (r["location"] or "").lower()
        for code, m in MARKETS.items():
            if m.name.lower() in loc or any(c.lower() in loc for c in m.cities):
                by_country[code] = by_country.get(code, 0) + 1
                break
    print(f"{len(rows)} stored roles\n")
    print("by type:")
    for k, v in sorted(by_kind.items(), key=lambda x: -x[1]):
        print(f"  {v:>4}  {k}")
    print("\nby market:")
    for k, v in sorted(by_country.items(), key=lambda x: -x[1]):
        print(f"  {v:>4}  {MARKETS[k].name}")


def cmd_markets(args) -> None:
    for code, m in sorted(MARKETS.items()):
        print(f"{code}  {m.name:<14} {', '.join(m.cities[:4])}")
        if m.note:
            print(f"    → {m.note}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="eu_student_jobs.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sweep", help="search multiple countries and role types")
    s.add_argument("--countries", default="DE,NL,FR,SE,PL")
    s.add_argument("--field", default="", help="e.g. 'data science', 'mechanical engineering'")
    s.add_argument("--kind", choices=KINDS, help="restrict to one role type")
    s.add_argument("--provider", choices=list(PROVIDERS), default="eures")
    s.add_argument("--pages", type=int, default=1)
    s.add_argument("--by-city", action="store_true", help="search per city instead of country-wide")
    s.add_argument("--max-cities", type=int, default=3)
    s.add_argument("--max-terms", type=int, default=6)
    s.add_argument("--remote", action="store_true")
    s.add_argument("--strict", action="store_true", help="drop anything not clearly student-level")
    s.set_defaults(func=cmd_sweep)

    r = sub.add_parser("report", help="breakdown by role type and market")
    r.set_defaults(func=cmd_report)

    m = sub.add_parser("markets", help="list configured countries")
    m.set_defaults(func=cmd_markets)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
