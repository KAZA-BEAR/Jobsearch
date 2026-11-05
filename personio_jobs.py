#!/usr/bin/env python3
"""
personio_jobs.py — jobs from employers that recruit through Personio.

Personio is the dominant HR system of German Mittelstand firms and startups,
including many robotics companies with no Greenhouse/Lever board. Every
Personio customer that enabled it publishes a keyless XML feed at
    https://{slug}.jobs.personio.de/xml   (or .personio.com)
(https://support.personio.de/hc/en-us/articles/207576365).

A 307 redirect to personio.com means the slug is wrong or the company does not
use Personio. Find a company's slug from its careers page link
("xyz.jobs.personio.de") and add it to COMPANIES or pass it on the command line.
"""

from __future__ import annotations

import html
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

from linkedin_jobs import Job, RateLimiter


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
