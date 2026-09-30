#!/usr/bin/env python3
"""
research_jobs.py — research institutes and universities: HiWi, thesis, PhD.

Public research employers rarely use commercial job boards. Each source here
is the institute's own feed:

  fraunhofer  Fraunhofer-Gesellschaft (76 institutes). SAP SuccessFactors
              RSS with server-side keyword search.
  dlr         German Aerospace Center, incl. the Institute of Robotics and
              Mechatronics (Oberpfaffenhofen). Same RSS format.
  mpg         Max Planck Society. RSS of all openings; keyword-filtered here.
  thd         Technische Hochschule Deggendorf (your university). Parsed
              from the public job listing page.

Usage:
    python research_jobs.py search robotik --sources fraunhofer,dlr
    python research_jobs.py search "" --sources thd          # everything at THD
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from hashlib import sha1

import requests

from linkedin_jobs import Job, RateLimiter, Store, parse_posted

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
            title = (item.findtext("title") or "").strip()
            desc = _clean(item.findtext("description") or "")
            blob = f"{title} {desc}".lower()
            if words and not any(w in blob for w in words):
                continue
            link = (item.findtext("link") or "").strip()
            m = re.search(r"\bin ([A-ZÄÖÜ][\wäöüß-]+(?: [A-ZÄÖÜ][\wäöüß-]+)?)", desc)
            out.append(Job(
                job_id=f"mpg-{link.rstrip('/').split('/')[-2] if '/' in link else sha1(link.encode()).hexdigest()[:12]}",
                title=title,
                company="Max Planck Society",
                location=f"{m.group(1)}, DE" if m else "DE",
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


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="robotics_jobs.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("keywords", nargs="?", default="robotik")
    s.add_argument("--sources", default=",".join(SOURCES))
    args = p.parse_args()

    from fit_score import load_default_scorer
    store = Store(args.db, scorer=(sc.score if (sc := load_default_scorer()) else None))
    feeds = ResearchFeeds()
    for src in [x.strip() for x in args.sources.split(",") if x.strip()]:
        try:
            jobs = feeds.search(src, args.keywords)
        except Exception as exc:
            print(f"{src}: FAILED {exc}")
            continue
        new = 0
        for j in jobs:
            if store.upsert(j):
                new += 1
                print(f"  + [{j.fit_score}] [{j.employment_type}] {j.title} ({j.location})")
        print(f"{SOURCES[src]}: {len(jobs)} roles, {new} new")


if __name__ == "__main__":
    main()
