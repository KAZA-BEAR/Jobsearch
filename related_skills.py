"""
related_skills.py — search keywords related to the skills in profile.json.

When the profile's skills change, each skill is looked up in ESCO, the EU's free
skills database (no key): its names in English and German, alternative names and
the skills it links to. ESCO matches technical terms loosely ("SLAM" found "Islam",
"path planning" found oil-well paths), so the local judge model then keeps only the
candidates that mean the skill or a close relative an employer would write in a job
ad, and may add up to three common job-ad terms of its own.

The result is a list of suggested search keywords for the daily sweep (Settings ->
Daily sweep -> Suggested keywords). It never feeds the CV, the letter or the fit
score: these are words to search with, not skills the candidate has.

Stored in related_skills.json in the data folder. Only skills that are new since the
last run are looked up; a failed lookup is retried next time.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from linkedin_jobs import data_dir

ESCO = "https://ec.europa.eu/esco/api"
UA = "jobsearch-assistant/1.0 (personal job search; related-skill lookup)"
TIMEOUT = 30
_LOCK = threading.Lock()          # one refresh at a time (start-up and a profile change)


def store_path() -> Path:
    return data_dir() / "related_skills.json"


def skill_terms(profile: dict) -> list[str]:
    """The profile's skills, job tools and research interests, without the part in
    brackets ("Linux (Ubuntu)" -> "Linux") and without long hardware phrases."""
    raw = [v for vals in (profile.get("skills") or {}).values() for v in vals]
    raw += [t for e in profile.get("experience") or [] for t in e.get("tools") or []]
    raw += [r for ed in profile.get("education") or [] for r in ed.get("research_interests") or []]
    out = []
    for t in raw:
        t = re.sub(r"\s*\(.*?\)", "", str(t)).strip()
        if t and len(t.split()) <= 4 and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def profile_key(profile: dict) -> str:
    """Changes when the skills change, not when an address or a bullet does."""
    return hashlib.sha256(json.dumps(sorted(t.lower() for t in skill_terms(profile))).encode()).hexdigest()[:16]


def load() -> dict:
    try:
        return json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    tmp = store_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(store_path())


def esco_candidates(term: str, session: requests.Session) -> list[str]:
    """Names around `term` in ESCO: the top skill matches' preferred and alternative
    names (English and German) and the skills they link to. Many are unrelated
    matches; the model sorts them out."""
    hits = session.get(f"{ESCO}/search", params={"text": term, "type": "skill", "language": "en",
                                                 "limit": 3}, timeout=TIMEOUT).json()
    out: list[str] = []

    def add(x):
        x = " ".join(str(x or "").split())
        if x and len(x.split()) <= 5 and x.lower() not in (o.lower() for o in out):
            out.append(x)
    for h in (hits.get("_embedded") or {}).get("results", [])[:3]:
        time.sleep(0.2)
        r = session.get(f"{ESCO}/resource/skill", params={"uri": h["uri"], "language": "en"},
                        timeout=TIMEOUT).json()
        for lang in ("en", "de"):
            add((r.get("preferredLabel") or {}).get(lang))
            for a in ((r.get("alternativeLabel") or {}).get(lang) or [])[:6]:
                add(a)
        for rel in ("hasEssentialSkill", "hasOptionalSkill", "broaderSkill", "narrowerSkill"):
            for x in ((r.get("_links") or {}).get(rel) or [])[:5]:
                add(x.get("title"))
    return out[:30]


PICK_PROMPT = """A robotics and mechatronics student lists this skill in their profile:
"{term}" (listed with: {context})

These phrases come from the EU skills database. Many are unrelated matches of the
same words (a religion, oil drilling, assembly-line work, another tool):
{candidates}

1. "keep": the phrases above that mean the SAME skill - another name for it, its
   German name, or the activity of using it. Copy them exactly. Not a different
   tool or field: for "Linux" keep nothing like "Kali Linux" or "web programming";
   for "motion planning" not "motion graphics". Usually 0 to 3.
2. "more": up to 3 other specific terms (1-4 words, English or German) that a job ad
   would use for exactly this skill or its direct neighbour. Examples: "SPI" (with
   UART, I2C) -> "Serial Peripheral Interface", "serial bus"; "KiCAD" -> "PCB design",
   "schematic capture"; "SLAM" -> "localization", "mapping". Never broad words like
   "programming", "software development", "automation" or "robotics".
Answer as JSON."""

# One-word answers too broad to search with.
_GENERIC = {"code", "coding", "programming", "software", "development", "modeling", "modelling",
            "automation", "robotics", "engineering", "computing", "technology", "design"}

PICK_SCHEMA = {
    "type": "object", "required": ["keep", "more"],
    "properties": {"keep": {"type": "array", "items": {"type": "string"}},
                   "more": {"type": "array", "items": {"type": "string"}}},
}


def pick(term: str, candidates: list[str], call, context: str = "") -> tuple[list[str], list[str]]:
    """(ESCO candidates the model keeps, up to three job-ad terms it adds). `call` is
    ats_pipeline.call_model bound to the judge model; `context` is the profile line the
    skill sits in, so "SPI" next to UART and I2C reads as a serial bus."""
    raw = call(PICK_PROMPT.format(term=term, context=context or "its other skills",
                                  candidates="\n".join(f"- {c}" for c in candidates) or "(none found)"),
               # 300 ran out for "CAD" (2026-11-08): its many ESCO names were echoed back
               max_tokens=600, schema=PICK_SCHEMA, temperature=0)
    data = json.loads(raw)
    by_lower = {c.lower(): c for c in candidates}
    keep = [by_lower[k.strip().lower()] for k in data.get("keep") or [] if k.strip().lower() in by_lower]
    more = []
    for m in data.get("more") or []:
        m = " ".join(str(m).split()).strip(" .")
        low = m.lower()
        if m and len(m.split()) <= 4 and low != term.lower() and low not in _GENERIC \
                and low not in (x.lower() for x in keep + more):
            more.append(m)
    return [k for k in keep if k.lower() not in _GENERIC][:3], more[:3]


def _contexts(profile: dict) -> dict:
    """{skill: its neighbours in the profile}, e.g. "SPI" -> "UART, I2C, MAVLink"."""
    groups = [list(vals) for vals in (profile.get("skills") or {}).values()]
    groups += [list(e.get("tools") or []) for e in profile.get("experience") or []]
    groups += [list(ed.get("research_interests") or []) for ed in profile.get("education") or []]
    out = {}
    for g in groups:
        names = [re.sub(r"\s*\(.*?\)", "", str(x)).strip() for x in g]
        for n in names:
            out.setdefault(n, ", ".join(x for x in names if x != n)[:120])
    return out


def refresh(profile: dict, base_url: str = None, model: str = None, log=print,
            force: bool = False, stop: threading.Event | None = None) -> dict:
    """Look up the skills that are new since the last run (all with `force`) and save.
    Returns the stored data. Network or model failures leave the old data in place
    and the profile marked as not yet done, so the next start tries again."""
    import ats_pipeline
    if not _LOCK.acquire(blocking=False):
        return load()
    try:
        data = load()
        key = profile_key(profile)
        if data.get("profile_key") == key and not force:
            return data
        terms = skill_terms(profile)
        done = {} if force else dict(data.get("terms") or {})
        todo = [t for t in terms if t not in done]
        judge = (model or ats_pipeline.DEFAULT_LOCAL_MODELS).split(",")[0].strip()

        def call(prompt, **kw):
            return ats_pipeline.call_model(prompt, model=judge, base_url=base_url, timeout=300, **kw)
        log(f"[skills] Looking up related search keywords for {len(todo)} skill(s) "
            f"(ESCO + {judge})...")
        session = requests.Session()
        session.headers["User-Agent"] = UA
        failed = 0
        contexts = _contexts(profile)
        for t in todo:
            if stop is not None and stop.is_set():
                break
            try:
                cands = esco_candidates(t, session)
                keep, more = pick(t, cands, call, contexts.get(t, ""))
            except (requests.RequestException, ValueError, ats_pipeline.PipelineError) as exc:
                failed += 1
                if failed == 1:
                    log(f"[skills] Lookup failed for '{t}' ({str(exc)[:120]}); will retry next time.")
                if failed >= 3:          # offline or no model: stop early, try next start
                    break
                continue
            done[t] = {"esco": keep, "model": more}
        complete = all(t in done for t in terms)
        data = {"profile_key": key if complete else data.get("profile_key", ""),
                "updated": datetime.now().isoformat(timespec="seconds"),
                "model": judge,
                "terms": {t: done[t] for t in terms if t in done}}
        _save(data)
        n = len(keywords(data, profile))
        log(f"[skills] {n} suggested search keyword(s) saved"
            + ("" if complete else f"; {len([t for t in terms if t not in done])} skill(s) "
                                   "left for next time") + ".")
        return data
    finally:
        _LOCK.release()


def keywords(data: dict, profile: dict = None, exclude=()) -> list[tuple[str, str, str]]:
    """[(keyword, the profile skill it came from, "ESCO" or "model")], without the
    profile's own skills and without anything in `exclude` (the sweep's current terms)."""
    have = {t.lower() for t in (skill_terms(profile) if profile else [])}
    have |= {e.strip().lower() for e in exclude if e and e.strip()}
    out, seen = [], set()
    for term, found in (data.get("terms") or {}).items():
        for src, words in (("ESCO", found.get("esco") or []), ("model", found.get("model") or [])):
            for w in words:
                if w.lower() not in have and w.lower() not in seen:
                    seen.add(w.lower())
                    out.append((w, term, src))
    return out


def refresh_in_background(profile_path, base_url=None, model=None, log=print) -> bool:
    """Start refresh() in a daemon thread when the profile's skills changed since the
    last run. Returns True if a lookup started."""
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if load().get("profile_key") == profile_key(profile):
        return False
    threading.Thread(target=refresh, args=(profile, base_url, model, log), daemon=True,
                     name="related-skills").start()
    return True
