#!/usr/bin/env python3
"""
company_radar.py — find new companies and startups hiring in each market.

There is no single open registry covering EU + US + Asia, so this uses three
complementary signals:

  1. FUNDING (US)     SEC EDGAR Form D. Every US company raising private capital
                      files one, with issuer name, amount and industry. Official,
                      keyless, indexed within 60s of filing.
                      https://efts.sec.gov/LATEST/search-index

  2. EMERGENCE (all)  New employers appearing in your own job database. Works for
                      every market with no extra dependency: a company that shows
                      up hiring for the first time is new *to you*, which is the
                      signal that actually matters.

  3. CONFIRMATION     Given a company name, probe the five ATS providers for a
                      public job board. A hit means they're actively hiring and
                      can be added to the monitored board list permanently.

Usage:
    python company_radar.py funding --field robotics --months 6
    python company_radar.py emerging --db robotics_jobs.db --days 30
    python company_radar.py probe "Figure AI" "Cartken" "Nimble Robotics"
    python company_radar.py watchlist
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone

import requests

from linkedin_jobs import RateLimiter, Store, request_with_retry
from robotics_track import ATS_ENDPOINTS, BOARDS

RADAR_DB = "company_radar.db"

# SIC codes that map to robotics / mechatronics / industrial automation.
ROBOTICS_SIC = {
    "3559": "Special industry machinery",
    "3560": "General industrial machinery",
    "3561": "Pumps and pumping equipment",
    "3569": "General industrial machinery NEC",
    "3577": "Computer peripheral equipment",
    "3585": "Refrigeration & service machinery",
    "3612": "Power distribution & specialty transformers",
    "3621": "Motors and generators",
    "3625": "Relays and industrial controls",
    "3674": "Semiconductors",
    "3679": "Electronic components NEC",
    "3690": "Electrical machinery & equipment",
    "3711": "Motor vehicles",
    "3721": "Aircraft",
    "3724": "Aircraft engines",
    "3728": "Aircraft parts",
    "3761": "Guided missiles and space vehicles",
    "3812": "Search, detection, navigation, guidance",
    "3823": "Industrial instruments for measurement",
    "3826": "Laboratory analytical instruments",
    "3827": "Laboratory apparatus",
    "3829": "Measuring & controlling devices NEC",
    "3841": "Surgical & medical instruments",
    "3845": "Electromedical apparatus",
    "7372": "Prepackaged software",
    "8731": "Commercial physical & biological research",
}

FUNDING_KEYWORDS = (
    "robotics", "robotic", "autonomous", "mechatronic", "automation",
    "drone", "unmanned", "actuator", "lidar", "perception", "manipulator",
)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

RADAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    key          TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    market       TEXT,
    signal       TEXT,
    detail       TEXT,
    source_url   TEXT,
    first_seen   TEXT,
    ats          TEXT,
    ats_slug     TEXT,
    board_url    TEXT,
    open_roles   INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_radar_signal ON companies(signal);
"""


@dataclass
class Company:
    name: str
    market: str = ""
    signal: str = ""          # funding | emerging | probe
    detail: str = ""
    source_url: str = ""
    ats: str = ""
    ats_slug: str = ""
    board_url: str = ""
    open_roles: int = 0
    status: str = "new"
    first_seen: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def key(self) -> str:
        return re.sub(r"[^a-z0-9]+", "", self.name.lower())[:60]


class RadarStore:
    def __init__(self, path: str = RADAR_DB):
        import threading
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(RADAR_SCHEMA)

    def upsert(self, c: Company) -> bool:
        d = asdict(c)
        d["key"] = c.key
        with self.lock:
            existing = self.conn.execute(
                "SELECT 1 FROM companies WHERE key = ?", (c.key,)).fetchone()
            if existing:
                # Don't clobber first_seen or an already-found board.
                self.conn.execute(
                    "UPDATE companies SET detail=:detail, open_roles=:open_roles, "
                    "ats=COALESCE(NULLIF(:ats,''), ats), "
                    "ats_slug=COALESCE(NULLIF(:ats_slug,''), ats_slug), "
                    "board_url=COALESCE(NULLIF(:board_url,''), board_url) "
                    "WHERE key=:key", d)
            else:
                cols = ", ".join(d)
                ph = ", ".join(f":{k}" for k in d)
                self.conn.execute(f"INSERT INTO companies ({cols}) VALUES ({ph})", d)
            self.conn.commit()
        return not existing

    def all(self, signal: str = "") -> list[sqlite3.Row]:
        q = "SELECT * FROM companies"
        args: tuple = ()
        if signal:
            q += " WHERE signal = ?"
            args = (signal,)
        q += " ORDER BY first_seen DESC"
        with self.lock:
            return self.conn.execute(q, args).fetchall()

    def set_status(self, key: str, status: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE companies SET status=? WHERE key=?", (status, key))
            self.conn.commit()


# --------------------------------------------------------------------------
# Signal 1: SEC EDGAR Form D (US funding rounds)
# --------------------------------------------------------------------------

class FormDScanner:
    """US private funding rounds from SEC EDGAR full-text search.

    The SEC permits scripted access at up to 10 requests/second provided a
    descriptive User-Agent carrying a contact address is sent. We stay well
    under that. sec.gov/robots.txt disallows /cgi-bin, which is never touched.
    """

    SEARCH = "https://efts.sec.gov/LATEST/search-index"
    FILING = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm"

    def __init__(self, contact_email: str = "", rpm: int = 120):
        email = contact_email or "jobsearch-tool@example.com"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"robotics-job-radar/1.0 ({email})",
            "Accept": "application/json",
            "Origin": "https://www.sec.gov",
            "Referer": "https://www.sec.gov/",
        })
        self.limiter = RateLimiter(rpm)
        self.last_total = 0

    def scan(self, field: str = "robotics", months: int = 6,
             max_pages: int = 4, on_log=None) -> list[Company]:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=30 * months)
        found: dict[str, Company] = {}

        for page in range(max_pages):
            self.limiter.wait()
            params = {
                "q": f'"{field}"',
                "forms": "D",
                "dateRange": "custom",
                "startdt": start.isoformat(),
                "enddt": end.isoformat(),
                "from": page * 10,
            }
            try:
                resp = request_with_retry(self.session, "GET", self.SEARCH, params=params)
                payload = resp.json()
            except Exception as exc:
                msg = f"[form-d] request failed: {exc}"
                print(msg, file=sys.stderr)
                if on_log:
                    on_log(msg)
                break

            hits = ((payload.get("hits") or {}).get("hits")) or []
            total = (((payload.get("hits") or {}).get("total")) or {}).get("value", 0)
            self.last_total = total
            msg = f"[form-d] '{field}' page {page + 1}: {len(hits)} of {total}"
            print(msg, file=sys.stderr)
            if on_log:
                on_log(msg)
            if not hits:
                break

            for h in hits:
                c = self._to_company(h, field)
                if c and c.key not in found:
                    found[c.key] = c
            if len(hits) < 10:
                break

        return list(found.values())

    @classmethod
    def _to_company(cls, hit: dict, field: str) -> Company | None:
        src = hit.get("_source") or {}
        names = src.get("display_names") or []
        if not names:
            return None
        # display_names look like "Acme Robotics Inc.  (CIK 0001234567)"
        raw = names[0]
        name = re.sub(r"\s*\(CIK\s*\d+\)\s*$", "", raw).strip()
        ciks = src.get("ciks") or []
        cik = ciks[0].lstrip("0") if ciks else ""
        filed = src.get("file_date") or ""

        sics = src.get("sics") or []
        sic_desc = ""
        for s in sics:
            if str(s) in ROBOTICS_SIC:
                sic_desc = ROBOTICS_SIC[str(s)]
                break
        if not sic_desc and sics:
            sic_desc = f"SIC {sics[0]}"

        # _id is like "0001234567-25-000123:primary_doc.xml"
        acc = (hit.get("_id") or "").split(":")[0]
        url = ""
        if cik and acc:
            url = cls.FILING.format(cik=cik, acc_nodash=acc.replace("-", ""), acc=acc)

        detail = f"Form D filed {filed}"
        if sic_desc:
            detail += f" · {sic_desc}"

        return Company(
            name=name,
            market="US",
            signal="funding",
            detail=detail,
            source_url=url,
        )


# --------------------------------------------------------------------------
# Signal 2: new employers appearing in your own job data
# --------------------------------------------------------------------------

def _known_board_companies() -> set[str]:
    """Companies already on a monitored board list, across all regions.

    Imported lazily because us_asia_jobs imports from robotics_track, and a
    top-level import here would create a cycle.
    """
    known = {b.company.lower() for b in BOARDS}
    try:
        from us_asia_jobs import US_ASIA_BOARDS
        known |= {b.company.lower() for b in US_ASIA_BOARDS}
    except ImportError:
        pass
    return known


KNOWN_BOARD_COMPANIES = _known_board_companies()


def emerging_employers(job_db: str, days: int = 30,
                       min_roles: int = 1, on_log=None) -> list[Company]:
    """Employers that appear in the job DB but aren't on the monitored board list.

    Works for every market, because it reads whatever you've already collected —
    EURES, USAJOBS, Adzuna, MyCareersFuture, all of it.
    """
    store = Store(job_db)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    tally: dict[str, dict] = {}

    for r in store.all():
        company = (r["company"] or "").strip()
        if not company or len(company) < 3:
            continue
        posted = r["posted_at"] or r["fetched_at"] or ""
        if posted and posted < cutoff:
            continue
        if company.lower() in KNOWN_BOARD_COMPANIES:
            continue
        slot = tally.setdefault(company, {"n": 0, "loc": r["location"] or "",
                                          "src": r["source"] or "", "title": r["title"]})
        slot["n"] += 1

    out = []
    for name, info in sorted(tally.items(), key=lambda kv: -kv[1]["n"]):
        if info["n"] < min_roles:
            continue
        market = _market_from(info["loc"], info["src"])
        out.append(Company(
            name=name,
            market=market,
            signal="emerging",
            detail=f"{info['n']} open role(s) · e.g. {info['title'][:50]}",
            open_roles=info["n"],
        ))
    msg = f"[emerging] {len(out)} employers not already on the board list"
    print(msg, file=sys.stderr)
    if on_log:
        on_log(msg)
    return out


def _market_from(location: str, source: str) -> str:
    s = f"{location} {source}".lower()
    for needle, code in (
        ("usajobs", "US"), ("adzuna-us", "US"), ("mcf", "SG"), ("adzuna-in", "IN"),
        ("eures", "EU"), ("germany", "DE"), ("munich", "DE"), ("berlin", "DE"),
        ("singapore", "SG"), ("tokyo", "JP"), ("bangalore", "IN"),
    ):
        if needle in s:
            return code
    m = re.match(r"^([A-Z]{2})\b", location or "")
    return m.group(1) if m else "?"


# --------------------------------------------------------------------------
# Signal 3: probe for a public ATS board
# --------------------------------------------------------------------------

class BoardProber:
    """Given a company name, guess ATS slugs and see which return a live board.

    Confirms a discovered company is actually hiring, and yields the exact
    endpoint to add to the permanent monitored list.
    """

    def __init__(self, rpm: int = 60):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "robotics-job-radar/1.0"})
        self.limiter = RateLimiter(rpm)

    @staticmethod
    def slug_candidates(name: str) -> list[str]:
        base = re.sub(r"[^a-z0-9 ]+", "", name.lower())
        base = re.sub(r"\b(inc|llc|ltd|gmbh|corp|corporation|co|limited|ag|bv|ab|technologies|technology)\b",
                      "", base).strip()
        squashed = re.sub(r"\s+", "", base)
        hyphen = re.sub(r"\s+", "-", base)
        first = base.split(" ")[0] if base else ""
        out = [squashed, hyphen, first]
        seen, uniq = set(), []
        for s in out:
            if s and len(s) > 2 and s not in seen:
                seen.add(s)
                uniq.append(s)
        return uniq

    def probe(self, name: str, on_log=None) -> Company:
        c = Company(name=name, signal="probe")
        for slug in self.slug_candidates(name):
            for ats, tmpl in ATS_ENDPOINTS.items():
                if not tmpl:        # Workday has no slug-based endpoint to probe
                    continue
                self.limiter.wait()
                url = tmpl.format(slug=slug)
                try:
                    resp = self.session.get(url, timeout=12)
                except requests.RequestException:
                    continue
                if resp.status_code != 200:
                    continue
                try:
                    data = resp.json()
                except ValueError:
                    continue
                count = self._count(ats, data)
                # SmartRecruiters (and others) answer ANY slug with 200 and an
                # empty list, so an empty board is not evidence the company uses
                # that ATS. Only a board with open roles counts as a hit.
                if not count:
                    continue
                c.ats, c.ats_slug, c.open_roles = ats, slug, count
                c.board_url = url
                # A guessed slug can belong to a different company with the same
                # name (e.g. lever/zeiss is a San Francisco startup, not ZEISS),
                # so show sample roles to let the user judge before adding it.
                sample = self._sample(ats, data)
                c.detail = f"{count} open roles on {ats} — verify: {sample}"
                msg = f"  ? {name}: {ats}/{slug} — {count} roles, e.g. {sample}"
                print(msg, file=sys.stderr)
                if on_log:
                    on_log(msg)
                return c
        c.detail = "no public ATS board found"
        if on_log:
            on_log(f"  – {name}: no public board")
        return c

    @staticmethod
    def _sample(ats: str, data, n: int = 2) -> str:
        """'Title (Location); Title (Location)' for the first n postings."""
        try:
            if ats == "lever":
                rows = [(r.get("text"), (r.get("categories") or {}).get("location")) for r in data]
            elif ats == "greenhouse":
                rows = [(r.get("title"), (r.get("location") or {}).get("name")) for r in data["jobs"]]
            elif ats == "smartrecruiters":
                rows = [(r.get("name"), (r.get("location") or {}).get("city")) for r in data["content"]]
            elif ats == "ashby":
                rows = [(r.get("title"), r.get("location")) for r in data["jobs"]]
            elif ats == "recruitee":
                rows = [(r.get("title"), r.get("location")) for r in data["offers"]]
            else:
                return ""
        except (KeyError, TypeError, AttributeError):
            return ""
        return "; ".join(f"{t} ({loc})" if loc else str(t) for t, loc in rows[:n])

    @staticmethod
    def _count(ats: str, data) -> int | None:
        try:
            if ats == "greenhouse":
                return len(data["jobs"])
            if ats == "lever":
                return len(data) if isinstance(data, list) else None
            if ats == "smartrecruiters":
                return len(data["content"])
            if ats == "ashby":
                return len(data["jobs"])
            if ats == "recruitee":
                return len(data["offers"])
        except (KeyError, TypeError):
            return None
        return None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_funding(args) -> None:
    scanner = FormDScanner(args.email)
    radar = RadarStore(args.radar_db)
    fields = [args.field] if args.field else list(FUNDING_KEYWORDS[:6])
    new = 0
    for f in fields:
        for c in scanner.scan(f, args.months):
            if radar.upsert(c):
                new += 1
                print(f"  + {c.name}  — {c.detail}")
    print(f"\n{new} new US companies with recent funding filings")
    print("Run 'probe' on the interesting ones to check if they're hiring.")


def cmd_emerging(args) -> None:
    radar = RadarStore(args.radar_db)
    new = 0
    for c in emerging_employers(args.db, args.days, args.min_roles):
        if radar.upsert(c):
            new += 1
            print(f"  + [{c.market}] {c.name} — {c.detail}")
    print(f"\n{new} newly-seen employers")


def cmd_probe(args) -> None:
    prober = BoardProber()
    radar = RadarStore(args.radar_db)
    names = args.names
    if not names:
        rows = radar.all()
        names = [r["name"] for r in rows if not r["ats"]][: args.limit]
        print(f"probing {len(names)} companies with no known board")
    hits = 0
    for name in names:
        c = prober.probe(name)
        if c.ats:
            hits += 1
        radar.upsert(c)
    print(f"\n{hits}/{len(names)} have a public job board")


def cmd_watchlist(args) -> None:
    rows = RadarStore(args.radar_db).all(args.signal)
    if not rows:
        print("radar is empty — run 'funding' or 'emerging' first")
        return
    with_board = [r for r in rows if r["ats"]]
    print(f"{len(rows)} companies tracked · {len(with_board)} with a live job board\n")
    for r in rows[: args.limit]:
        flag = f"[{r['ats']}:{r['ats_slug']}]" if r["ats"] else ""
        print(f"  {r['market']:<3} {r['name'][:38]:<38} {r['signal']:<9} {flag}")
        if r["detail"]:
            print(f"      {r['detail']}")
    if with_board:
        print("\nAdd these to BOARDS in robotics_track.py to monitor them permanently:")
        for r in with_board[:10]:
            print(f'    Board("{r["name"]}", "{r["ats"]}", "{r["ats_slug"]}", '
                  f'"{r["market"]}", "robotics"),')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--radar-db", default=RADAR_DB)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("funding", help="US startups from SEC Form D filings")
    f.add_argument("--field", default="robotics")
    f.add_argument("--months", type=int, default=6)
    f.add_argument("--email", default="", help="contact address for the SEC User-Agent")
    f.set_defaults(func=cmd_funding)

    e = sub.add_parser("emerging", help="new employers in your own job database")
    e.add_argument("--db", default="robotics_jobs.db")
    e.add_argument("--days", type=int, default=30)
    e.add_argument("--min-roles", type=int, default=1)
    e.set_defaults(func=cmd_emerging)

    pr = sub.add_parser("probe", help="check companies for a public ATS board")
    pr.add_argument("names", nargs="*")
    pr.add_argument("--limit", type=int, default=25)
    pr.set_defaults(func=cmd_probe)

    w = sub.add_parser("watchlist", help="show tracked companies")
    w.add_argument("--signal", default="", choices=["", "funding", "emerging", "probe"])
    w.add_argument("--limit", type=int, default=60)
    w.set_defaults(func=cmd_watchlist)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
