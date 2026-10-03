#!/usr/bin/env python3
"""
discover_jobs.py — open-ended discovery of employers you don't know yet.

Fixed source lists only find the employers you already know about. This module
finds new companies, startups and research groups, then reads their own job
boards. Every source is public and keyless:

  crawl     Common Crawl's URL index (index.commoncrawl.org) lists every
            Personio, Greenhouse, Ashby, Recruitee and Workable board it has
            crawled: thousands of employers, most of them small. Each run reads
            a batch of boards it has not seen yet and keeps the ones hiring in
            your field; those are re-read on every later run. The same trick is
            used by open projects such as danki1337/startups-board and
            O-Marsters-1997/job-scraper. (Lever blocks Common Crawl, so it is
            only reached through the career-page detector.)
  yc        Y Combinator's company directory via the yc-oss open mirror:
            hardware / robotics / industrial startups in your region.
  wikidata  Wikidata (CC0): companies in engineering, automation, robotics,
            automotive and aerospace industries, plus research institutes,
            that list a website.
  sites     Your own list of URLs: company, startup or lab home pages.
  hn        Hacker News "Ask HN: Who is hiring?" for this month (Algolia API):
            mostly startups, often posted by the founders.

yc, wikidata and sites go through a career-page detector: read the home page,
follow a careers / Karriere link, then look for a job-board link (Personio,
Greenhouse, Lever, Ashby, Recruitee, Workable, SmartRecruiters) or schema.org
JobPosting data. robots.txt is honoured, and every lookup is cached in the data
folder so later runs only look at what is new or stale.

Usage:
    python discover_jobs.py crawl --budget 300 --region germany
    python discover_jobs.py yc wikidata hn --region europe
    python discover_jobs.py sites https://www.example-robotics.de
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

from company_radar import Company
from eu_student_jobs import APPRENTICESHIP, EU_FIELD_MARKERS, EU_FIELD_STRONG
from linkedin_jobs import Job, Store, data_dir
from personio_jobs import PersonioCompany, PersonioFeeds

SOURCES = {
    "crawl": "Job boards found by Common Crawl (Personio, Greenhouse, Ashby, Recruitee, Workable)",
    "yc": "Y Combinator startups",
    "wikidata": "Wikidata companies and research institutes",
    "hn": "Hacker News 'Who is hiring?'",
    "sites": "My own websites",
}
REGIONS = ("germany", "dach", "europe", "anywhere")

UA = "Mozilla/5.0 (compatible; jobsearch-discovery/1.0; personal job search)"
# Wikimedia rejects browser-like agents; its policy asks for a bot name and a link.
WIKI_UA = "EUJobSearch-discovery/1.0 (https://github.com/KAZA-BEAR/Jobsearch) python-requests"
CACHE_NAME = "discovery_cache.json"

# Roles worth keeping: the app's robotics / mechatronics words plus the
# startup vocabulary those markers don't cover.
FIELD_EXTRA = re.compile(
    r"\b(?:autonom|slam\b|lidar|drone|uav\b|uas\b|motion planning|sensor fusion|"
    r"mechanical design|hardware engineer|controls? engineer|simulation engineer|"
    r"mechatron|cyber-physical|manipulat|humanoid|exoskelet|teleoperat|"
    r"embedded|fpga|pcb\b|electronics engineer|test engineer|systems engineer)",
    re.IGNORECASE)
# A description that mentions robots only counts for a technical title: a
# robotics startup's finance and sales roles carry the same company blurb.
TECH_TITLE = re.compile(
    r"engineer|ingenieur|developer|entwickl|software|hardware|scientist|research|forsch|"
    r"technolog|\bphd\b|doktorand|thesis|abschlussarbeit|masterarbeit|bachelorarbeit|"
    r"programm|informatik|robot", re.IGNORECASE)
NON_TECH = re.compile(
    r"\b(?:sales|vertrieb|account(?:ant|ing)?|finance|finanz|buchhalt|marketing|hr|"
    r"human resources|recruit|personal(?:referent|sachbearbeit)|legal|jurist|office|"
    r"assistant|assistenz|customer success|business development|procurement|einkauf|"
    r"kfz|kraftfahrzeug|fahrer|driver|lager|warehouse|reinigung|kaufm|"
    # skilled trades (Ausbildung level), not roles for a degree
    r"mechatroniker|schlosser|fachkraft|monteur|elektriker|elektroniker|"
    r"industriemechaniker|zerspan|schweiß|schweiss|"
    # construction / installation trade and site management (electrical contractors)
    r"bauleit|obermonteur|servicetechnik|servicekoordinat|kalkulat|einkäuf|einkaeuf|"
    r"planer|projektleit|projektmanag|schülerprakt|schuelerprakt|manager)", re.IGNORECASE)
SENIOR = re.compile(r"\b(?:senior|sr\.?|lead|principal|staff|head of|director|vp|chief|"
                    r"leiter|leitung|teamlead|expert)\b|teamleit|gruppenleit|abteilungsleit",
                    re.IGNORECASE)

_DE_CITIES = (
    "germany|deutschland|munich|münchen|muenchen|berlin|hamburg|stuttgart|frankfurt|"
    "cologne|köln|koeln|düsseldorf|dusseldorf|dresden|leipzig|hannover|hanover|"
    "nuremberg|nürnberg|nuernberg|karlsruhe|darmstadt|aachen|bremen|bonn|mannheim|"
    "heidelberg|augsburg|regensburg|ingolstadt|ulm|freiburg|erlangen|wolfsburg|"
    "braunschweig|jena|potsdam|kiel|dortmund|essen|münster|muenster|bochum|garching|"
    "oberpfaffenhofen|deggendorf|passau|würzburg|wuerzburg|chemnitz|magdeburg|rostock|"
    "saarbrücken|saarbruecken|kaiserslautern|paderborn|bielefeld|göttingen|goettingen|"
    "tübingen|tuebingen|konstanz|friedrichshafen|ottobrunn|unterschleißheim|"
    "unterschleissheim|weßling|wessling|sindelfingen|böblingen|boeblingen|"
    "herzogenaurach|bavaria|bayern|baden-württemberg|nrw|hessen|sachsen")
_DACH = ("austria|österreich|oesterreich|vienna|wien|graz|linz|salzburg|innsbruck|"
         "switzerland|schweiz|suisse|svizzera|zurich|zürich|zuerich|basel|bern|"
         "geneva|genève|lausanne|lucerne|luzern|zug")
_EUROPE = ("europe|emea|european union|netherlands|nederland|amsterdam|rotterdam|"
           "eindhoven|delft|utrecht|belgium|belgique|brussels|leuven|ghent|france|paris|"
           "lyon|toulouse|grenoble|denmark|copenhagen|aarhus|odense|sweden|stockholm|"
           "gothenburg|göteborg|lund|norway|oslo|trondheim|finland|helsinki|espoo|tampere|"
           "italy|italia|milan|milano|turin|torino|rome|roma|bologna|spain|españa|madrid|"
           "barcelona|valencia|portugal|lisbon|lisboa|porto|poland|polska|warsaw|warszawa|"
           "krakow|kraków|wroclaw|wrocław|czech|prague|praha|brno|ireland|dublin|"
           "united kingdom|\\buk\\b|england|london|cambridge|oxford|edinburgh|manchester|"
           "bristol|luxembourg|estonia|tallinn|latvia|riga|lithuania|vilnius|hungary|"
           "budapest|slovakia|bratislava|slovenia|ljubljana|croatia|zagreb|romania|"
           "bucharest|greece|athens|bulgaria|sofia")
_ISO = {"germany": {"DE"}, "dach": {"DE", "AT", "CH"},
        "europe": {"DE", "AT", "CH", "NL", "BE", "FR", "DK", "SE", "NO", "FI", "IT", "ES",
                   "PT", "PL", "CZ", "IE", "GB", "UK", "LU", "EE", "LV", "LT", "HU", "SK",
                   "SI", "HR", "RO", "GR", "BG"}}
_REGION_RX = {
    "germany": re.compile(rf"\b(?:{_DE_CITIES})\b", re.IGNORECASE),
    "dach": re.compile(rf"\b(?:{_DE_CITIES}|{_DACH})\b", re.IGNORECASE),
    "europe": re.compile(rf"\b(?:{_DE_CITIES}|{_DACH}|{_EUROPE})\b", re.IGNORECASE),
}
_ISO_TOKEN = re.compile(r"(?:^|[\s,(/|-])([A-Z]{2})(?=$|[\s,)/|-])")
_REMOTE = re.compile(r"\bremote\b|\bhome ?office\b|\banywhere\b", re.IGNORECASE)

# Wikidata industries (P452) of the employers worth reading.
WIKIDATA_INDUSTRIES = (
    "Q170978",     # robotics
    "Q184199",     # automation
    "Q787422",     # automation technology
    "Q5086032",    # industrial automation
    "Q101333",     # mechanical engineering
    "Q107597925",  # machinery industry and plant construction
    "Q1957908",    # manufacture of machinery and equipment
    "Q190117",     # automotive industry
    "Q3477381",    # automotive supplier
    "Q124192",     # automotive engineering
    "Q3477363",    # aerospace industry
    "Q3798668",    # aerospace engineering
    "Q5358497",    # electronics industry
    "Q11650",      # electronics
    "Q43035",      # electrical engineering
    "Q2986369",    # semiconductor industry
    "Q107598010",  # precision engineering and optical industry
    "Q327092",     # biomedical engineering
    "Q6554101",    # medical device
)
WIKIDATA_COUNTRIES = {
    "germany": ("Q183",),
    "dach": ("Q183", "Q40", "Q39"),
    "europe": ("Q183", "Q40", "Q39", "Q55", "Q31", "Q142", "Q35", "Q34", "Q20", "Q33",
               "Q38", "Q29", "Q45", "Q36", "Q213", "Q27", "Q145", "Q32", "Q191"),
}
YC_TOPICS = {"Robotics", "Hardware", "Industrials", "Manufacturing", "Drones",
             "Autonomous Delivery", "Industrial Workplace", "Automotive",
             "Aviation and Space", "Computer Vision", "Hard Tech", "Machine Learning",
             "Climate", "Energy", "Healthcare IT", "Medical Devices", "Space Exploration",
             "Defense", "Aerospace", "Construction", "Agriculture", "Supply Chain and Logistics"}

# --------------------------------------------------------------------------
# Job-board platforms
# --------------------------------------------------------------------------

# Common Crawl URL patterns → slug regex.
CRAWL_PATTERNS = (
    ("personio", "*.jobs.personio.de", r"https?://([a-z0-9-]+)\.jobs\.personio\.de"),
    ("personio", "*.jobs.personio.com", r"https?://([a-z0-9-]+)\.jobs\.personio\.com"),
    ("greenhouse", "job-boards.greenhouse.io/*", r"greenhouse\.io/([a-z0-9_-]+)"),
    ("greenhouse", "job-boards.eu.greenhouse.io/*", r"greenhouse\.io/([a-z0-9_-]+)"),
    ("ashby", "jobs.ashbyhq.com/*", r"jobs\.ashbyhq\.com/([^/?#\"]+)"),
    ("recruitee", "*.recruitee.com", r"https?://([a-z0-9-]+)\.recruitee\.com"),
    ("workable", "apply.workable.com/*", r"apply\.workable\.com/([a-z0-9_-]+)"),
)
# Links that give a board away on a company's own site.
ATS_LINKS = (
    ("personio", re.compile(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)),
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?"
        r"([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._%-]+)", re.I)),
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(
        r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
)
# Portals the detector recognises but cannot read: reported for a manual look.
OTHER_PORTALS = re.compile(
    r"myworkdayjobs\.com|softgarden\.io|join\.com/companies|b-ite\.com|onlyfy\.jobs|"
    r"prescreen\.io|rexx-systems\.com|umantis\.com|dvinci|successfactors|interamt\.de|"
    r"jobbase\.io|concludis|heyrecruit|hr4you|talention|kenjo|teamtailor\.com", re.I)
JUNK_SLUGS = {"www", "api", "app", "apps", "embed", "robots.txt", "j", "jobs", "job", "careers",
              "static", "assets", "favicon.ico", "sitemap.xml", "login", "signup", "about",
              "help", "support", "blog", "status", "cdn", "images", "img", "js", "css",
              "public", "search", "en", "de", "v1", "v2", "v3", "candidate", "careers-page",
              "privacy", "terms", "legal", "imprint", "impressum", "admin", "auth", "oauth",
              "demo", "demorecruiting", "test", "example", "sandbox"}
CAREER_LINK = re.compile(
    r"karriere|career|/jobs?\b|stellen|vacanc|join[- ]?us|work[- ]with[- ]us|"
    r"arbeiten[- ]bei|offene[- ]positionen|jobangebote|stellenangebote|stellenausschreibung|"
    r"jobs?[- ]and[- ]careers|opportunit", re.I)

# Link text that reads like a vacancy on a plain HTML career page.
JOB_LINK = re.compile(
    r"\((?:[mwfdxi]\s*/\s*){2,3}[mwfdxi]\)|all genders|werkstudent|working student|\bhiwi\b|"
    r"hilfskraft|praktik|internship|\bintern\b|thesis|masterarbeit|bachelorarbeit|"
    r"abschlussarbeit|doktorand|\bphd\b|postdoc|wissenschaftliche[rs]?\s+mitarbeiter|"
    r"research (?:assistant|associate|engineer|scientist)|stellenangebot|"
    r"ingenieur(?:in)?\b|engineer\b",          # not "Engineering": that's a menu entry
    re.IGNORECASE)
_LINK_NOISE = re.compile(r"\b(?:view job|mehr erfahren|jetzt bewerben|details|read more)\b|-->",
                         re.IGNORECASE)

_TAG = re.compile(r"<[^>]+>")


def _text(raw: str) -> str:
    raw = re.sub(r"<(?:br|p|/p|/li|/h\d)\b[^>]*>", "\n", raw or "", flags=re.I)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(_TAG.sub(" ", raw))).strip()


def _pretty(slug: str) -> str:
    words = re.split(r"[-_ ]+", unquote(slug))
    fixed = {"gmbh": "GmbH", "ag": "AG", "se": "SE", "kg": "KG", "ug": "UG", "bv": "BV",
             "ab": "AB", "sa": "SA", "ltd": "Ltd", "inc": "Inc", "co": "Co"}
    return " ".join(fixed.get(w.lower(), w[:1].upper() + w[1:]) for w in words if w)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _older_than(stamp: str, days: int) -> bool:
    if not stamp:
        return True
    try:
        return datetime.fromisoformat(stamp) < _now() - timedelta(days=days)
    except ValueError:
        return True


def in_region(location: str, region: str, remote: bool = False) -> bool:
    """Does a job's location fall in the chosen region?

    Fully remote roles count when the posting names the region or the region
    is 'anywhere'; 'Remote (US)' does not count for Germany.
    """
    if region == "anywhere":
        return True
    loc = location or ""
    if _REGION_RX[region].search(loc):
        return True
    if any(code in _ISO[region] for code in _ISO_TOKEN.findall(loc)):
        return True
    if (remote or _REMOTE.search(loc)) and re.search(r"\b(?:EU|EMEA|Europe)\b", loc, re.I):
        return region == "europe" or region in ("dach", "germany") and "germany" in loc.lower()
    return False


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

class DiscoveryCache:
    """JSON file next to the jobs DB: harvested slugs, board and site status."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or data_dir() / CACHE_NAME)
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        for key in ("harvest", "boards", "sites", "lists"):
            self.data.setdefault(key, {})

    @property
    def boards(self) -> dict:
        return self.data["boards"]

    @property
    def sites(self) -> dict:
        return self.data["sites"]

    def add_board(self, ats: str, slug: str, company: str = "", origin: str = "crawl") -> str:
        key = f"{ats}:{slug}"
        with self.lock:
            b = self.boards.get(key)
            if b is None:
                self.boards[key] = {"ats": ats, "slug": slug, "company": company,
                                    "origin": origin, "status": "new", "checked": "",
                                    "roles": 0, "kept": 0}
            elif company and not b.get("company"):
                b["company"] = company
        return key

    def save(self) -> None:
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

class Discovery:
    def __init__(self, region: str = "germany", keywords: str = "", entry_only: bool = True,
                 cache: DiscoveryCache | None = None, on_log=None, cancelled=None,
                 workers: int = 8):
        if region not in REGIONS:
            raise ValueError(f"region must be one of {', '.join(REGIONS)}")
        self.region = region
        self.words = [w.lower() for w in keywords.split() if w]
        self.entry_only = entry_only
        self.cache = cache or DiscoveryCache()
        self.on_log = on_log
        self.cancelled = cancelled or threading.Event()
        self.workers = workers
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "en,de;q=0.8"})
        # Each board is its own company subdomain; the default 60/min limiter
        # would make the parallel workers queue one request per second.
        self.personio = PersonioFeeds(rpm=600)
        self.personio.session = self.session
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        # Employers worth showing in the 'New companies' list.
        self.companies: dict[str, Company] = {}

    def log(self, msg: str) -> None:
        print(msg, file=sys.stderr)
        if self.on_log:
            self.on_log(msg)

    # ---- filtering ----

    def keep(self, job: Job) -> bool:
        # A link on a site the user named has no location; trust the user's choice.
        unplaced = job.source == "discover-site" and not job.location
        if not unplaced and not in_region(job.location, self.region, job.remote):
            return False
        # Link jobs inherit the employer's country; a title naming another place wins.
        if job.source == "discover-site" and self.region != "anywhere" and \
                _REGION_RX["europe"].search(job.title) and not _REGION_RX[self.region].search(job.title):
            return False
        title = job.title
        if NON_TECH.search(title) or APPRENTICESHIP.search(title):
            return False
        if not (EU_FIELD_MARKERS.search(title) or FIELD_EXTRA.search(title)
                or TECH_TITLE.search(title) and EU_FIELD_STRONG.search(job.description[:1500])):
            return False
        if self.entry_only and SENIOR.search(job.title):
            return False
        if self.words:
            blob = f"{job.title} {job.description}".lower()
            if not any(w in blob for w in self.words):
                return False
        return True

    # ---- Common Crawl harvest ----

    def harvest(self, max_pages: int = 6, refresh_days: int = 30) -> int:
        """Collect board slugs from the newest Common Crawl index. Returns new boards."""
        info = self.session.get("https://index.commoncrawl.org/collinfo.json", timeout=60).json()
        index_id, api = info[0]["id"], info[0]["cdx-api"]
        added = 0
        for ats, pattern, rx in CRAWL_PATTERNS:
            if self.cancelled.is_set():
                break
            done = self.cache.data["harvest"].get(pattern, {})
            if done.get("index") == index_id or not _older_than(done.get("at", ""), refresh_days):
                continue
            slug_rx = re.compile(rx, re.I)
            slugs: set[str] = set()
            pages = self._cdx(api, {"url": pattern, "output": "json", "showNumPages": "true"})
            try:
                n_pages = min(int(json.loads(pages)["pages"]), max_pages)
            except (ValueError, KeyError, TypeError):
                n_pages = 1
            for page in range(n_pages):
                if self.cancelled.is_set():
                    break
                text = self._cdx(api, {"url": pattern, "output": "json", "fl": "url",
                                       "page": page})
                for line in text.splitlines():
                    m = slug_rx.search(line)
                    if m:
                        slug = unquote(m.group(1)).strip().strip('"')
                        if ats != "ashby":
                            slug = slug.lower()
                        if slug.lower() not in JUNK_SLUGS and len(slug) > 1:
                            slugs.add(slug)
            before = len(self.cache.boards)
            for slug in slugs:
                self.cache.add_board(ats, slug, origin="crawl")
            added += len(self.cache.boards) - before
            self.cache.data["harvest"][pattern] = {"index": index_id, "at": _iso(_now()),
                                                    "slugs": len(slugs)}
            self.log(f"[crawl] {pattern}: {len(slugs)} boards in {index_id}")
        self.cache.save()
        return added

    def _cdx(self, api: str, params: dict) -> str:
        """The CDX server answers 503/504 under load; back off and retry."""
        for attempt in range(5):
            try:
                r = self.session.get(api, params=params, timeout=180)
                if r.status_code == 200:
                    return r.text
                if r.status_code == 404:      # pattern not in this crawl
                    return ""
            except requests.RequestException:
                pass
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"Common Crawl index not answering for {params.get('url')}")

    # ---- reading boards ----

    def fetch_board(self, ats: str, slug: str, company: str = "") -> tuple[str, list[Job]] | None:
        """(company name, jobs) for one board, or None if the board does not exist."""
        # A bare domain (from 'My own websites') is a worse name than the board's own.
        if "." in company:
            company = ""
        name = company or _pretty(slug)
        get = lambda url, **kw: self.session.get(url, timeout=25, **kw)
        if ats == "personio":
            jobs = self.personio.fetch(PersonioCompany(name, slug, ""))
            if jobs is None:
                return None
            for j in jobs:
                j.source = "discover-personio"
            return name, jobs
        if ats == "greenhouse":
            r = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
            if r.status_code != 200:
                return None
            rows = r.json().get("jobs", [])
            if rows and rows[0].get("company_name"):
                name = company or rows[0]["company_name"]
            return name, [Job(
                job_id=f"gh-{slug}-{x['id']}", title=x.get("title", ""), company=name,
                location=(x.get("location") or {}).get("name", ""), url=x.get("absolute_url", ""),
                posted_at=x.get("first_published") or x.get("updated_at", ""),
                description=_text(html.unescape(x.get("content", "")))[:4000],
                source="discover-greenhouse") for x in rows]
        if ats == "ashby":
            r = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            if r.status_code != 200:
                return None
            out = []
            for x in r.json().get("jobs", []):
                if x.get("isListed") is False:
                    continue
                locs = [x.get("location") or ""] + [s.get("location", "") for s in
                                                    x.get("secondaryLocations") or []]
                out.append(Job(
                    job_id=f"ashby-{slug}-{x['id']}", title=x.get("title", ""), company=name,
                    location=", ".join(l for l in locs if l), url=x.get("jobUrl", ""),
                    posted_at=x.get("publishedAt", ""), employment_type=x.get("employmentType", ""),
                    remote=bool(x.get("isRemote")),
                    description=(x.get("descriptionPlain") or "")[:4000], source="discover-ashby"))
            return name, out
        if ats == "recruitee":
            r = get(f"https://{slug}.recruitee.com/api/offers/")
            if r.status_code != 200:
                return None
            rows = r.json().get("offers", [])
            if rows and rows[0].get("company_name"):
                name = company or rows[0]["company_name"]
            return name, [Job(
                job_id=f"recruitee-{slug}-{x['id']}", title=x.get("title", ""), company=name,
                location=", ".join(v for v in (x.get("city"), x.get("country")) if v)
                or x.get("location", ""),
                url=x.get("careers_url", ""), posted_at=x.get("published_at", ""),
                employment_type=x.get("employment_type_code", ""), remote=bool(x.get("remote")),
                description=_text(x.get("description", ""))[:4000],
                source="discover-recruitee") for x in rows]
        if ats == "workable":
            r = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
            if r.status_code != 200:
                return None
            data = r.json()
            name = company or data.get("name") or name
            return name, [Job(
                job_id=f"workable-{slug}-{x.get('shortcode')}", title=x.get("title", ""),
                company=name,
                location=", ".join(v for v in (x.get("city"), x.get("state"), x.get("country")) if v),
                url=x.get("url", ""), posted_at=x.get("published_on", ""),
                employment_type=x.get("employment_type", ""), remote=bool(x.get("telecommuting")),
                description=_text(x.get("description", ""))[:4000],
                source="discover-workable") for x in data.get("jobs", [])]
        if ats == "lever":
            r = get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
            if r.status_code != 200 or not isinstance(r.json(), list):
                return None
            return name, [Job(
                job_id=f"lever-{slug}-{x['id']}", title=x.get("text", ""), company=name,
                location=(x.get("categories") or {}).get("location", ""), url=x.get("hostedUrl", ""),
                posted_at=_iso(datetime.fromtimestamp(x["createdAt"] / 1000, timezone.utc))
                if x.get("createdAt") else "",
                employment_type=(x.get("categories") or {}).get("commitment", ""),
                remote=(x.get("workplaceType") == "remote"),
                description=(x.get("descriptionPlain") or "")[:4000],
                source="discover-lever") for x in r.json()]
        if ats == "smartrecruiters":
            r = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 100})
            if r.status_code != 200:
                return None
            rows = r.json().get("content", [])
            if not rows:          # answers any slug with an empty list
                return None
            name = company or (rows[0].get("company") or {}).get("name") or name
            return name, [Job(
                job_id=f"sr-{slug}-{x['id']}", title=x.get("name", ""), company=name,
                location=", ".join(v for v in ((x.get("location") or {}).get("city"),
                                               (x.get("location") or {}).get("country")) if v),
                url=f"https://jobs.smartrecruiters.com/{slug}/{x['id']}",
                posted_at=x.get("releasedDate", ""), remote=bool((x.get("location") or {}).get("remote")),
                source="discover-smartrecruiters") for x in rows]
        return None

    def _scan_one(self, key: str) -> list[Job]:
        b = self.cache.boards[key]
        try:
            res = self.fetch_board(b["ats"], b["slug"], b.get("company", ""))
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            with self.cache.lock:
                b["checked"], b["status"] = _iso(_now()), "error"
            self.log(f"  ! {key}: {type(exc).__name__}")
            return []
        with self.cache.lock:
            b["checked"] = _iso(_now())
            if res is None:
                b["status"] = "dead"
                return []
            name, jobs = res
            kept = [j for j in jobs if j.title and self.keep(j)]
            b["company"] = b.get("company") or name
            b["roles"], b["kept"] = len(jobs), len(kept)
            b["status"] = "hiring" if kept else ("off-field" if jobs else "empty")
        if kept:
            self._note_company(name, b, kept)
        return kept

    def _note_company(self, name: str, b: dict, kept: list[Job]) -> None:
        board_url = kept[0].url.split("/job")[0] if kept[0].url else ""
        self.companies[name.lower()] = Company(
            name=name, market=self.region.upper()[:2] if self.region != "anywhere" else "?",
            signal="discovered", detail=f"{len(kept)} matching of {b['roles']} open · "
                                        f"e.g. {kept[0].title[:60]} · via {b.get('origin', 'crawl')}",
            ats=b["ats"], ats_slug=b["slug"], board_url=board_url, open_roles=b["roles"])

    def scan(self, keys: list[str], label: str) -> list[Job]:
        """Read the given boards in parallel; return the jobs worth keeping."""
        out: list[Job] = []
        if not keys:
            return out
        done = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(self._scan_one, k): k for k in keys}
            for fut in as_completed(futs):
                if self.cancelled.is_set():
                    for f in futs:
                        f.cancel()
                    break
                jobs = fut.result()
                done += 1
                if jobs:
                    b = self.cache.boards[futs[fut]]
                    self.log(f"  + {b.get('company') or b['slug']} ({b['ats']}): "
                             f"{len(jobs)} of {b['roles']} roles match")
                    out += jobs
                if done % 50 == 0:
                    self.log(f"[{label}] {done}/{len(keys)} boards read")
                    self.cache.save()
        self.cache.save()
        return out

    def crawl(self, budget: int = 300) -> list[Job]:
        """Harvest if due, then read hiring boards again plus `budget` unseen/stale ones."""
        try:
            self.harvest()
        except Exception as exc:          # a slow index must not stop the board reads
            self.log(f"[crawl] Common Crawl harvest skipped: {exc}")
        boards = self.cache.boards
        hiring = [k for k, b in boards.items() if b["status"] == "hiring"]
        fresh = [k for k, b in boards.items() if b["status"] == "new"]
        stale = [k for k, b in boards.items()
                 if b["status"] in ("off-field", "empty", "error") and _older_than(b["checked"], 30)
                 or b["status"] == "dead" and _older_than(b["checked"], 120)]
        # Personio is almost entirely DACH, so read it first for a DACH search.
        first = {"personio"} if self.region in ("germany", "dach") else set()
        random.shuffle(fresh)
        fresh.sort(key=lambda k: boards[k]["ats"] not in first)
        random.shuffle(stale)
        batch = (fresh + stale)[:max(budget, 0)]
        unseen = sum(1 for b in boards.values() if b["status"] == "new")
        self.log(f"[crawl] {len(boards)} known boards · re-reading {len(hiring)} hiring · "
                 f"reading {len(batch)} more ({unseen} never read)")
        return self.scan(hiring + batch, "crawl")

    # ---- career-page detector ----

    def _allowed(self, url: str) -> bool:
        parts = urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if host not in self._robots:
                rp = urllib.robotparser.RobotFileParser()
                try:
                    r = self.session.get(f"{host}/robots.txt", timeout=10)
                    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                except requests.RequestException:
                    rp.parse([])
                self._robots[host] = rp
        return self._robots[host].can_fetch(UA, url)

    def _page(self, url: str) -> tuple[str, str] | None:
        if not self._allowed(url):
            return None
        try:
            r = self.session.get(url, timeout=15, allow_redirects=True)
        except requests.RequestException:
            return None
        if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "html"):
            return None
        return r.url, r.text[:1_500_000]

    @staticmethod
    def _boards_in(page: str) -> list[tuple[str, str]]:
        found = []
        for ats, rx in ATS_LINKS:
            for m in rx.finditer(page):
                slug = unquote(m.group(1)).strip("./")
                if ats != "ashby":
                    slug = slug.lower()
                if slug.lower() not in JUNK_SLUGS and (ats, slug) not in found:
                    found.append((ats, slug))
        return found

    def _jsonld_jobs(self, page: str, url: str, company: str) -> list[Job]:
        out = []
        for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                                page, re.S | re.I):
            try:
                data = json.loads(block.strip())
            except ValueError:
                continue
            stack = [data]
            while stack:
                d = stack.pop()
                if isinstance(d, list):
                    stack += d
                    continue
                if not isinstance(d, dict):
                    continue
                stack += [v for k, v in d.items() if k in ("@graph", "itemListElement", "item")]
                kind = d.get("@type")
                if kind != "JobPosting" and not (isinstance(kind, list) and "JobPosting" in kind):
                    continue
                org = d.get("hiringOrganization") or {}
                locs = d.get("jobLocation") or []
                locs = locs if isinstance(locs, list) else [locs]
                places = []
                for loc in locs:
                    addr = (loc or {}).get("address") or {} if isinstance(loc, dict) else {}
                    if isinstance(addr, dict):
                        country = addr.get("addressCountry")
                        if isinstance(country, dict):
                            country = country.get("name", "")
                        places.append(", ".join(str(v) for v in (addr.get("addressLocality"),
                                                                 country) if v))
                title = _text(str(d.get("title", "")))
                link = d.get("url") or url
                out.append(Job(
                    job_id=Job.make_id(org.get("name", company) if isinstance(org, dict) else company,
                                       title, link),
                    title=title,
                    company=(org.get("name") if isinstance(org, dict) else "") or company,
                    location="; ".join(p for p in places if p),
                    url=link, posted_at=str(d.get("datePosted", "")),
                    employment_type=str(d.get("employmentType", "")),
                    remote=d.get("jobLocationType") == "TELECOMMUTE",
                    description=_text(str(d.get("description", "")))[:4000],
                    source="discover-site"))
        return out

    @staticmethod
    def _link_jobs(page: str, url: str, company: str, loc: str) -> list[Job]:
        """Job-like links on a plain HTML career page (most institutes and labs)."""
        out, seen = [], set()
        for href, label in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
            title = re.sub(r"\s+", " ", _LINK_NOISE.sub(" ", _text(label)))
            title = re.sub(r"^[\W_]+|[\W_]+$", "", title).strip()
            if title.endswith("(m/w/d") or title.endswith("(w/m/d"):
                title += ")"
            if not 12 <= len(title) <= 180 or not JOB_LINK.search(title):
                continue
            full = urljoin(url, html.unescape(href))
            if not full.startswith("http") or full in seen:
                continue
            seen.add(full)
            out.append(Job(job_id="site-" + sha1(full.encode()).hexdigest()[:16], title=title,
                           company=company, location=loc, url=full, source="discover-site"))
        return out[:80]

    def _reread(self, url: str) -> list[Job]:
        """Read a known career page again for new job links."""
        rec = self.cache.sites[url]
        got = self._page(rec["career"])
        if not got:
            return []
        return self._link_jobs(got[1], got[0], rec["company"], rec.get("loc", ""))

    def detect(self, url: str, company: str, origin: str,
               loc: str = "") -> tuple[list[str], list[Job], str]:
        """(board keys, jobs found on the pages, career page URL) for one home page."""
        if not url.startswith("http"):
            url = "https://" + url
        first = self._page(url)
        if not first:
            return [], [], ""
        base, page = first
        boards, jobs, career, portal = self._boards_in(page), self._jsonld_jobs(page, base, company), "", ""
        if not boards and not jobs:
            home = urlparse(base).netloc.split(":")[0].removeprefix("www.")
            links = []
            for href, label in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
                full = urljoin(base, html.unescape(href))
                host = urlparse(full).netloc.removeprefix("www.")
                if full.startswith("http") and (host.endswith(home) or OTHER_PORTALS.search(full)
                                                or any(rx.search(full) for _, rx in ATS_LINKS)) \
                        and (CAREER_LINK.search(href) or CAREER_LINK.search(_text(label)[:60])):
                    if full not in links:
                        links.append(full)
            if not links:       # JavaScript-built menus hide the link; try the usual paths
                links = [urljoin(base, p) for p in ("/careers", "/karriere", "/jobs")]
            for link in links[:3]:
                if any(rx.search(link) for _, rx in ATS_LINKS):
                    boards += [b for b in self._boards_in(link) if b not in boards]
                    break
                if OTHER_PORTALS.search(link):
                    career, portal = link, OTHER_PORTALS.search(link).group(0)
                    break
                got = self._page(link)
                if not got:
                    continue
                career, sub = got
                boards += [b for b in self._boards_in(sub) if b not in boards]
                jobs += self._jsonld_jobs(sub, career, company)
                m = OTHER_PORTALS.search(sub)
                portal = m.group(0) if m else ""
                if boards or jobs:
                    break
                links_found = self._link_jobs(sub, career, company, loc)
                if links_found:
                    jobs += links_found
                    break
        keys = [self.cache.add_board(ats, slug, company, origin) for ats, slug in boards[:3]]
        n_links = sum(1 for j in jobs if j.job_id.startswith("site-"))
        with self.cache.lock:
            self.cache.sites[url] = {"checked": _iso(_now()), "company": company, "origin": origin,
                                     "boards": keys, "career": career, "portal": portal,
                                     "jsonld": len(jobs) - n_links, "links": n_links, "loc": loc}
        if not keys and not jobs and career:
            self.companies.setdefault(company.lower(), Company(
                name=company, signal="discovered", source_url=career,
                detail=f"career page{' on ' + portal if portal else ''} — no machine-readable "
                       f"jobs, open it to look · via {origin}"))
        return keys, jobs, career

    def check_sites(self, sites: list[tuple], origin: str, budget: int) -> list[Job]:
        """Run the detector on (name, url[, location]) not checked in 30 days, then read
        the boards it found. Career pages that listed job links are read again every run."""
        sites = [(s[0], s[1] if s[1].startswith("http") else "https://" + s[1],
                  s[2] if len(s) > 2 else "") for s in sites]
        todo = [s for s in sites if _older_than(self.cache.sites.get(s[1], {}).get("checked", ""), 30)]
        fresh = {s[1] for s in todo[:max(budget, 0)]}
        known = [k for _, u, _ in sites if u not in fresh for k in self.cache.sites.get(u, {}).get("boards", [])]
        again = [u for _, u, _ in sites if u not in fresh and self.cache.sites.get(u, {}).get("links")]
        todo = todo[:max(budget, 0)]
        self.log(f"[{origin}] {len(sites)} sites · checking {len(todo)} for a job board · "
                 f"re-reading {len(again)} career pages")
        keys, jobs = list(dict.fromkeys(known)), []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = [pool.submit(self.detect, u, n, origin, loc) for n, u, loc in todo]
            futs += [pool.submit(lambda u: ([], self._reread(u), ""), u) for u in again]
            for fut in as_completed(futs):
                if self.cancelled.is_set():
                    for f in futs:
                        f.cancel()
                    break
                try:
                    k, j, _career = fut.result()
                except Exception as exc:
                    self.log(f"  ! detector: {type(exc).__name__}: {exc}")
                    continue
                keys += [x for x in k if x not in keys]
                jobs += j
        self.cache.save()
        kept = [j for j in jobs if j.title and self.keep(j)]
        if jobs:
            self.log(f"[{origin}] {len(jobs)} postings read straight off career pages, {len(kept)} match")
        for j in kept:
            if j.company.lower() not in self.companies:
                rec = next((r for r in self.cache.sites.values() if r.get("company") == j.company), {})
                self.companies[j.company.lower()] = Company(
                    name=j.company, signal="discovered", source_url=rec.get("career", j.url),
                    detail=f"career page lists e.g. {j.title[:70]} · via {origin}")
        self.log(f"[{origin}] {len(keys)} job boards to read")
        return kept + self.scan(keys, origin)

    # ---- company lists ----

    def yc(self, budget: int = 80) -> list[Job]:
        companies = self._cached_list("yc", lambda: self.session.get(
            "https://yc-oss.github.io/api/companies/all.json", timeout=90).json())
        sites = []
        for c in companies:
            if c.get("status") not in ("Active", None) or not c.get("website"):
                continue
            topics = set(c.get("tags") or []) | set(c.get("industries") or [])
            if not topics & YC_TOPICS:
                continue
            if self.region != "anywhere" and not in_region(c.get("all_locations") or "", self.region):
                continue
            sites.append((c.get("isHiring", False), c["name"], c["website"],
                          c.get("all_locations") or ""))
        sites.sort(key=lambda s: not s[0])     # hiring first
        return self.check_sites([s[1:] for s in sites], "yc", budget)

    def wikidata(self, budget: int = 80, kinds=("company", "research")) -> list[Job]:
        region = self.region if self.region in WIKIDATA_COUNTRIES else "europe"
        countries = " ".join(f"wd:{q}" for q in WIKIDATA_COUNTRIES[region])
        sites: list[tuple[str, str]] = []
        for kind in kinds:
            if kind == "company":
                inds = " ".join(f"wd:{q}" for q in WIKIDATA_INDUSTRIES)
                where = f"VALUES ?ind {{ {inds} }} ?item wdt:P452 ?ind."
            else:
                where = "?item wdt:P31/wdt:P279* wd:Q31855."
            query = (f"SELECT DISTINCT ?item ?itemLabel ?site ?countryLabel WHERE {{ "
                     f"VALUES ?country {{ {countries} }} "
                     f"{where} ?item wdt:P17 ?country; wdt:P856 ?site. "
                     f"FILTER NOT EXISTS {{ ?item wdt:P576 [] }} "
                     f"SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"en,de\". }} }}")
            rows = self._cached_list(f"wikidata-{kind}-{region}", lambda: self.session.get(
                "https://query.wikidata.org/sparql", params={"query": query, "format": "json"},
                headers={"User-Agent": WIKI_UA}, timeout=120).json()["results"]["bindings"])
            seen = set()
            for r in rows:
                site = r["site"]["value"]
                if r["item"]["value"] in seen:
                    continue
                seen.add(r["item"]["value"])
                name = r.get("itemLabel", {}).get("value", "")
                if not name or re.fullmatch(r"Q\d+", name):
                    name = urlparse(site).netloc.removeprefix("www.")
                country = r.get("countryLabel", {}).get("value") or \
                    {"germany": "Germany"}.get(region, "")
                sites.append((name, site, country))
            self.log(f"[wikidata] {len(seen)} {kind} websites in {region}")
        random.shuffle(sites)    # spread the budget across both lists over several runs
        return self.check_sites(sites, "wikidata", budget)

    def sites(self, urls: list[str]) -> list[Job]:
        pairs = [(urlparse(u if u.startswith("http") else "https://" + u).netloc.removeprefix("www."), u)
                 for u in urls if u.strip()]
        for _, u in pairs:      # the user asked for these now, so never skip them as fresh
            self.cache.sites.pop(u if u.startswith("http") else "https://" + u, None)
        return self.check_sites(pairs, "sites", len(pairs))

    def _cached_list(self, name: str, fetch, days: int = 14) -> list:
        slot = self.cache.data["lists"].get(name)
        if slot and not _older_than(slot.get("at", ""), days):
            return slot["rows"]
        rows = fetch()
        self.cache.data["lists"][name] = {"at": _iso(_now()), "rows": rows}
        return rows

    # ---- Hacker News ----

    def hn(self) -> list[Job]:
        hits = self.session.get("https://hn.algolia.com/api/v1/search_by_date",
                                params={"tags": "story,author_whoishiring", "hitsPerPage": 6},
                                timeout=30).json()["hits"]
        story = next((h for h in hits if h["title"].startswith("Ask HN: Who is hiring")), None)
        if not story:
            return []
        item = self.session.get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}",
                                timeout=90).json()
        out = []
        for c in item.get("children") or []:
            text = _text(c.get("text") or "")
            if not text:
                continue
            head = text.split("\n", 1)[0]
            # Header fields only: a long part is already the description.
            parts = [p.strip() for p in head.split("|") if p.strip()]
            parts = parts[:1] + [p for p in parts[1:] if len(p) <= 90 and "://" not in p]
            if len(parts) < 2:
                continue
            company = re.sub(r"\s*\(.*?\)\s*$", "", parts[0])[:80]
            loc = next((p for p in parts[1:] if in_region(p, self.region)), "")
            if not loc and self.region != "anywhere":
                continue
            role = next((p for p in parts[1:] if EU_FIELD_MARKERS.search(p) or FIELD_EXTRA.search(p)), "")
            if not role and not EU_FIELD_STRONG.search(text):
                continue
            if not role:
                role = next((p for p in parts[1:] if re.search(r"engineer|developer|intern|scientist",
                                                               p, re.I)), "Open roles (see post)")
            job = Job(job_id=f"hn-{c['id']}", title=role[:120], company=company,
                      location=loc or parts[1][:80], url=f"https://news.ycombinator.com/item?id={c['id']}",
                      posted_at=c.get("created_at", ""), remote=bool(_REMOTE.search(head)),
                      description=text[:4000], source="discover-hn")
            if self.entry_only and SENIOR.search(job.title):
                continue
            if self.words and not any(w in text.lower() for w in self.words):
                continue
            out.append(job)
        self.log(f"[hn] {story['title']}: {len(item.get('children') or [])} posts, {len(out)} match")
        return out

    # ---- everything ----

    def run(self, sources: list[str], budget: int = 300, site_budget: int = 80,
            urls: list[str] | None = None) -> list[Job]:
        jobs: list[Job] = []
        for src in sources:
            if self.cancelled.is_set():
                break
            try:
                if src == "crawl":
                    got = self.crawl(budget)
                elif src == "yc":
                    got = self.yc(site_budget)
                elif src == "wikidata":
                    got = self.wikidata(site_budget)
                elif src == "hn":
                    got = self.hn()
                elif src == "sites":
                    got = self.sites(urls or [])
                else:
                    raise ValueError(f"unknown source '{src}'")
            except Exception as exc:
                self.log(f"[{src}] FAILED — {type(exc).__name__}: {exc}")
                continue
            self.log(f"[{src}] {len(got)} matching roles")
            jobs += got
        uniq = {j.job_id: j for j in jobs}
        return list(uniq.values())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("sources", nargs="+", help=f"{', '.join(SOURCES)} — or URLs for 'sites'")
    p.add_argument("--region", default="germany", choices=REGIONS)
    p.add_argument("--keywords", default="", help="extra words a role must mention (any)")
    p.add_argument("--budget", type=int, default=300, help="unseen Common Crawl boards per run")
    p.add_argument("--site-budget", type=int, default=80, help="websites checked per run")
    p.add_argument("--all-levels", action="store_true", help="keep senior / lead roles too")
    p.add_argument("--db", default=str(data_dir() / "robotics_jobs.db"))
    args = p.parse_args()
    sources = [s for s in args.sources if s in SOURCES]
    urls = [s for s in args.sources if s not in SOURCES]
    if urls and "sites" not in sources:
        sources.append("sites")
    d = Discovery(region=args.region, keywords=args.keywords, entry_only=not args.all_levels)
    jobs = d.run(sources, budget=args.budget, site_budget=args.site_budget, urls=urls)
    store = Store(args.db)
    new = sum(1 for j in jobs if store.upsert(j))
    for j in jobs:
        print(f"{j.company[:28]:28}  {j.title[:60]:60}  {j.location[:30]}")
    print(f"\n{len(jobs)} matching roles, {new} new → {args.db}; "
          f"{len(d.companies)} employers worth a look", file=sys.stderr)


if __name__ == "__main__":
    main()
