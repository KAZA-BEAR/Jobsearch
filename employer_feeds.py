"""
employer_feeds.py — job sources that read an employer's or agency's own listings.

Four keyless sources, one section each (each was its own module until 2026-11-09):

  Arbeitsagentur — Bundesagentur für Arbeit (Federal Employment Agency) job search.

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

  Personio — jobs from employers that recruit through Personio.

  Personio is the dominant HR system of German Mittelstand firms and startups,
  including many robotics companies with no Greenhouse/Lever board. Every
  Personio customer that enabled it publishes a keyless XML feed at
      https://{slug}.jobs.personio.de/xml   (or .personio.com)
  (https://support.personio.de/hc/en-us/articles/207576365).

  A 307 redirect to personio.com means the slug is wrong or the company does not
  use Personio. Find a company's slug from its careers page link
  ("xyz.jobs.personio.de") and add it to COMPANIES or pass it on the command line.

  Workday — jobs from employers whose careers site runs on Workday.

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

  Research — research institutes and universities: HiWi, thesis, PhD.

  Public research employers rarely use commercial job boards. Each source here
  is the institute's own feed:

    fraunhofer  Fraunhofer-Gesellschaft (76 institutes). SAP SuccessFactors
                RSS with server-side keyword search.
    dlr         German Aerospace Center, incl. the Institute of Robotics and
                Mechatronics (Oberpfaffenhofen). Same RSS format.
    mpg         Max Planck Society. RSS of all openings; keyword-filtered here.
    thd         Technische Hochschule Deggendorf (your university). Parsed
                from the public job listing page.
"""

from __future__ import annotations

import base64
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from hashlib import sha1
from urllib.parse import quote

import requests

from linkedin_jobs import Job, RateLimiter, request_with_retry, parse_posted


# ==========================================================================
# Bundesagentur für Arbeit (Arbeitsagentur) job search
# ==========================================================================

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


# ==========================================================================
# Personio career feeds
# ==========================================================================

@dataclass(frozen=True)
class PersonioCompany:
    company: str
    slug: str
    country: str = "DE"
    tier: str = "robotics"


# Verified to serve a live Personio feed (Sep 2026). A feed with 0 open roles
# is still valid: the company can post again at any time.
COMPANIES: tuple[PersonioCompany, ...] = (
    PersonioCompany("Magazino", "magazino", "DE", "robotics"),
    PersonioCompany("Franka Robotics", "franka-robotics", "DE", "robotics"),
    PersonioCompany("NEURA Robotics", "neura-robotics", "DE", "robotics"),
    PersonioCompany("sewts", "sewts", "DE", "robotics"),
    PersonioCompany("Wandelbots", "wandelbots", "DE", "robotics"),
    PersonioCompany("Blickfeld", "blickfeld", "DE", "mobility"),
    PersonioCompany("Micropsi Industries", "micropsi-industries", "DE", "robotics"),
    PersonioCompany("Quantum Systems", "quantum-systems", "DE", "mobility"),
    PersonioCompany("KONUX", "konux", "DE", "automation"),
    PersonioCompany("tado", "tado", "DE", "automation"),
)

HOSTS = ("https://{slug}.jobs.personio.de", "https://{slug}.jobs.personio.com")


class PersonioFeeds:
    name = "personio"

    def __init__(self, rpm: int = 60, language: str = "en"):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; jobsearch/1.0)"})
        self.limiter = RateLimiter(rpm)
        self.language = language

    def fetch(self, c: PersonioCompany) -> list[Job] | None:
        """Jobs for one company, or None if it has no Personio feed."""
        for host in HOSTS:
            base = host.format(slug=c.slug)
            self.limiter.wait()
            try:
                resp = self.session.get(f"{base}/xml", params={"language": self.language},
                                        timeout=20, allow_redirects=False)
            except requests.RequestException as exc:
                print(f"  ! {c.company}: {exc}", file=sys.stderr)
                continue
            if resp.status_code != 200 or b"<workzag-jobs" not in resp.content[:400]:
                continue
            jobs = self._parse(c, base, resp.content)
            if any(not j.description for j in jobs):
                self._fill_descriptions(c, base, jobs)
            return jobs
        return None

    def _fill_descriptions(self, c: PersonioCompany, base: str, jobs: list[Job]) -> None:
        """An ad written only in German is in the English feed with its title but no
        text (16 Personio jobs had empty descriptions, 2026-10-21); the feed in the
        company's own language has it."""
        self.limiter.wait()
        try:
            resp = self.session.get(f"{base}/xml", timeout=20, allow_redirects=False)
        except requests.RequestException:
            return
        if resp.status_code != 200 or b"<workzag-jobs" not in resp.content[:400]:
            return
        own = {j.job_id: j.description for j in self._parse(c, base, resp.content)}
        for j in jobs:
            if not j.description:
                j.description = own.get(j.job_id, "")

    @staticmethod
    def _parse(c: PersonioCompany, base: str, xml_bytes: bytes) -> list[Job]:
        root = ET.fromstring(xml_bytes)
        out = []
        for pos in root.findall("position"):
            def t(tag: str) -> str:
                el = pos.find(tag)
                return (el.text or "").strip() if el is not None and el.text else ""

            pid = t("id")
            parts = []
            for jd in pos.findall("jobDescriptions/jobDescription"):
                name = (jd.findtext("name") or "").strip()
                val = jd.findtext("value") or ""
                val = html.unescape(re.sub(r"<[^>]+>", " ", val))
                parts.append(f"{name}: {val}")
            desc = re.sub(r"\s+", " ", " ".join(parts)).strip()
            offices = [o.text.strip() for o in pos.findall("additionalOffices/office") if o.text]
            loc = ", ".join([x for x in [t("office"), *offices] if x]) or c.country
            seniority = t("seniority")
            etype = " · ".join(x for x in (t("employmentType"), seniority, t("schedule")) if x)
            out.append(Job(
                job_id=f"personio-{c.slug}-{pid}",
                title=t("name"),
                company=c.company,
                location=f"{loc}, {c.country}" if c.country not in loc else loc,
                url=f"{base}/job/{pid}",
                posted_at=t("createdAt"),
                employment_type=etype,
                description=desc[:4000],
                source="personio",
            ))
        return out


def slug_candidates(name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 -]+", "", name.lower())
    base = re.sub(r"\b(gmbh|ag|se|inc|ltd|co|kg)\b", "", base).strip()
    words = base.split()
    out = ["-".join(words), "".join(words), words[0] if words else ""]
    return [s for i, s in enumerate(out) if s and s not in out[:i]]


# ==========================================================================
# Workday career sites
# ==========================================================================

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
            # Only a real description: a job past the detail limit is stored with the
            # list's bullet fields, its requisition ID ("JR10443551"), which then
            # counted as known and was never fetched (8 jobs, 2026-10-21).
            if known and len(known) >= 200:
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


# ==========================================================================
# Research institutes and universities
# ==========================================================================

SUCCESSFACTORS = {
    "fraunhofer": ("Fraunhofer", "https://jobs.fraunhofer.de/services/rss/job/"),
    "dlr": ("DLR", "https://jobs.dlr.de/services/rss/job/"),
}
MPG_RSS = "https://www.mpg.de/feeds/stellenangebote.rss"
THD_LIST = "https://www.th-deg.de/stellenanzeigen"
THD_BASE = "https://www.th-deg.de"

SOURCES = {
    "fraunhofer": "Fraunhofer institutes",
    "dlr": "DLR (German Aerospace Center)",
    "mpg": "Max Planck institutes",
    "thd": "TH Deggendorf",
}

# SuccessFactors titles end with "(City, DE, 12345)" or "(City)".
_TITLE_LOC = re.compile(r"\s*\(([^()]*)\)\s*$")

KIND_MARKERS = (
    ("thesis", re.compile(r"(abschlussarbeit|masterarbeit|bachelorarbeit|thesis)", re.I)),
    ("hiwi", re.compile(r"(studentische|hilfskraft|hiwi|werkstudent|working student)", re.I)),
    ("internship", re.compile(r"(praktik|intern)", re.I)),
    ("phd", re.compile(r"(doktorand|promotion|phd|doctoral)", re.I)),
)


def _kind(title: str) -> str:
    for label, pat in KIND_MARKERS:
        if pat.search(title):
            return label
    return "research"


def _iso(rfc822: str) -> str:
    dt = parse_posted(rfc822)
    return dt.isoformat(timespec="seconds") if dt else ""


def _clean(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _job_posting(page: str) -> dict:
    """The schema.org JobPosting a job page embeds as JSON-LD, or {}."""
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                return d
    return {}


def _text(raw: str) -> str:
    """HTML to plain text, one line per paragraph or list item."""
    text = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\b[^>]*>", "\n", html.unescape(raw or ""))
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return "\n".join(" ".join(l.split()) for l in text.splitlines() if l.strip())


class ResearchFeeds:
    name = "research"

    def __init__(self, rpm: int = 30):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; jobsearch/1.0)"})
        self.limiter = RateLimiter(rpm)

    def search(self, source: str, keywords: str = "", on_log=None) -> list[Job]:
        if source in SUCCESSFACTORS:
            jobs = self._successfactors(source, keywords)
        elif source == "mpg":
            jobs = self._mpg(keywords)
        elif source == "thd":
            jobs = self._thd(keywords)
        else:
            raise ValueError(f"unknown source '{source}'. Choose from {', '.join(SOURCES)}")
        msg = f"[research] {SOURCES[source]} '{keywords}': {len(jobs)} roles"
        print(msg, file=sys.stderr)
        if on_log:
            on_log(msg)
        return jobs

    # ---- feeds ----

    def _get(self, url: str, **params) -> requests.Response:
        self.limiter.wait()
        resp = self.session.get(url, params=params or None, timeout=30)
        resp.raise_for_status()
        return resp

    def _posting(self, url: str) -> dict:
        """The JobPosting on a job's own page; {} when the page cannot be read."""
        if not url:
            return {}
        try:
            return _job_posting(self._get(url).text)
        except (requests.RequestException, ValueError):
            return {}

    def _successfactors(self, source: str, keywords: str) -> list[Job]:
        company, url = SUCCESSFACTORS[source]
        resp = self._get(url, locale="de_DE", keywords=keywords)
        out = []
        for item in ET.fromstring(resp.content).iter("item"):
            raw_title = (item.findtext("title") or "").strip()
            m = _TITLE_LOC.search(raw_title)
            loc = m.group(1) if m else "DE"
            title = _TITLE_LOC.sub("", raw_title) if m else raw_title
            link = (item.findtext("link") or "").strip()
            out.append(Job(
                job_id=f"{source}-{sha1(link.encode()).hexdigest()[:14]}",
                title=title,
                company=company,
                location=loc if "DE" in loc else f"{loc}, DE",
                url=link,
                posted_at=_iso(item.findtext("pubDate") or ""),
                employment_type=_kind(title),
                description=_clean(item.findtext("description") or "")[:4000],
                source=source,
            ))
        return out

    def _mpg(self, keywords: str) -> list[Job]:
        resp = self._get(MPG_RSS)
        words = [w.lower() for w in keywords.split() if w]
        out = []
        for item in ET.fromstring(resp.content).iter("item"):
            # "Finanzen &amp; Einkauf": the feed escapes its titles twice
            title = html.unescape((item.findtext("title") or "").strip())
            desc = _clean(item.findtext("description") or "")
            blob = f"{title} {desc}".lower()
            if words and not any(w in blob for w in words):
                continue
            link = (item.findtext("link") or "").strip()
            m = re.search(r"\bin ([A-ZÄÖÜ][\wäöüß-]+(?: [A-ZÄÖÜ][\wäöüß-]+)?)", desc)
            city = f"{m.group(1)}, DE" if m else "DE"
            # The feed has a two-line teaser (22 of 24 jobs under 500 characters,
            # 2026-10-21); the job's page embeds the full ad and its city.
            posting = self._posting(link)
            if posting.get("description"):
                desc = _text(posting["description"])
                town = ((posting.get("jobLocation") or {}).get("address") or {}) \
                    if isinstance(posting.get("jobLocation"), dict) else {}
                if town.get("addressLocality"):
                    city = f"{town['addressLocality']}, {town.get('addressCountry') or 'DE'}"
            out.append(Job(
                job_id=f"mpg-{link.rstrip('/').split('/')[-2] if '/' in link else sha1(link.encode()).hexdigest()[:12]}",
                title=title,
                company="Max Planck Society",
                location=city,
                url=link,
                posted_at=_iso(item.findtext("pubDate") or ""),
                employment_type=_kind(title),
                description=desc[:4000],
                source="mpg",
            ))
        return out

    def _thd(self, keywords: str) -> list[Job]:
        page = self._get(THD_LIST).text
        words = [w.lower() for w in keywords.split() if w]
        out = []
        for block in page.split("flex-stellenanzeige-container stellenanzeige-entry")[1:]:
            m = re.search(r"name=\"stellenanzeigeValues\" value='([^']*)'", block)
            link = re.search(r'href="(/de/Stellenanzeige\?id=(\d+))"', block)
            if not m or not link:
                continue
            try:
                meta = json.loads(html.unescape(m.group(1)))
            except ValueError:
                continue
            title = meta.get("title") or ""
            extra = " · ".join(
                x for x in [meta.get("untertitel") or "",
                            ", ".join(meta.get("fakultaeten") or []),
                            ", ".join(meta.get("kategorien") or []),
                            meta.get("arbeitsumfang") or ""] if x)
            if words and not any(w in f"{title} {extra}".lower() for w in words):
                continue
            out.append(Job(
                job_id=f"thd-{link.group(2)}",
                title=title,
                company="TH Deggendorf",
                location=", ".join(meta.get("locations") or ["Deggendorf"]) + ", DE",
                url=THD_BASE + link.group(1),
                employment_type=_kind(title),
                description=extra,
                source="thd",
            ))
        return out
