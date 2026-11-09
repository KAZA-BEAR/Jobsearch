"""
fit_score.py — rank jobs 0-100 against profile.json.

A fast, dependency-free keyword scorer: no LLM call per job, so every search
result can be ranked the moment it arrives. The CV generator on the
Applications tab is where a single job gets the deep, model-checked fit analysis.

The score adds up five signals:

  skills    (0-45)  profile skills/tools/coursework found in the posting,
                    with English/German aliases (ROS2 ~ ROS, Regelungstechnik
                    ~ control systems, Bildverarbeitung ~ computer vision...)
  domain    (0-20)  robotics / mechatronics / embedded words in the TITLE
  level     (-30..20) student / entry-level markers vs senior titles and
                    "5+ years" demands
  language  (-20..5) penalises postings that demand fluent German when the
                    profile says German is below B2
  location  (0-10)  near the profile's home city (Bavaria, Straubing area)

Also: suggested search keywords for the profile's skills (ESCO + the local judge model),
looked up when the skills change - the section at the end (related_skills.py until
2026-11-09). They only widen the daily sweep's searches; nothing here scores with them.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests

from linkedin_jobs import Job, Store, data_dir


# Profile term (lower-case) -> extra spellings to match in a posting.
ALIASES: dict[str, tuple[str, ...]] = {
    "ros2": ("ros2", "ros 2", "ros", "robot operating system"),
    "c++": ("c++", "cpp"),
    "python": ("python",),
    "linux (ubuntu)": ("linux", "ubuntu"),
    "gazebo": ("gazebo", "ignition gazebo"),
    "moveit2": ("moveit",),
    "nav2": ("nav2", "navigation stack"),
    "opencv": ("opencv",),
    "docker": ("docker", "container"),
    "git": ("git", "gitlab", "github"),
    "solidworks": ("solidworks",),
    "freecad": ("freecad",),
    "altium": ("altium",),
    "kicad": ("kicad",),
    "ltspice": ("ltspice", "spice"),
    "ansys": ("ansys",),
    "xml": ("xml",),
    "cad": ("cad", "konstruktion"),
    # Not "simulink": a simulation bullet is no proof of MATLAB Simulink, and the
    # alias reported "Matlab Simulink" as partly met for the BMW PhD ad.
    "simulation": ("simulation",),
    "uart": ("uart",),
    "i2c": ("i2c",),
    "spi": ("spi",),
    "mavlink": ("mavlink", "px4", "ardupilot"),
    "embedded systems": ("embedded", "eingebettete", "mikrocontroller", "microcontroller", "firmware"),
    "slam": ("slam", "localization", "lokalisierung", "mapping"),
    "motion and trajectory planning": ("motion planning", "trajectory", "trajektorie",
                                       "path planning", "bahnplanung", "pfadplanung"),
    "computer vision": ("computer vision", "bildverarbeitung", "machine vision",
                        "object detection", "perception", "wahrnehmung"),
    "digital twin": ("digital twin", "digitaler zwilling"),
    "autonomous systems": ("autonomous", "autonom"),
    "ar/vr human machine interface": ("ar/vr", "virtual reality", "augmented reality",
                                      "xr", "hmi", "mensch-maschine"),
    "additive manufacturing processes and technologies": ("additive manufacturing",
                                                         "3d printing", "3d-druck"),
    "power electronics": ("power electronics", "leistungselektronik"),
    "circuit design": ("circuit design", "schaltungsdesign", "schaltungsentwicklung"),
    "control systems": ("control systems", "control engineering", "regelungstechnik",
                        "regelung", "steuerungstechnik"),
    "signal & image processing": ("signal processing", "signalverarbeitung", "image processing"),
    "pcb troubleshooting & re-soldering": ("pcb", "leiterplatte", "löten", "soldering"),
    "oscilloscope & function generator": ("oscilloscope", "oszilloskop", "messtechnik"),
    "vlsi design and verification": ("vlsi", "fpga", "vhdl", "verilog"),
    "digital integrated circuits": ("asic", "integrated circuit"),
    "uav": ("uav", "drone", "drohne", "unmanned"),
}

# Words that mark the posting as squarely in the profile's domain when they
# appear in the TITLE (the title is a much stronger signal than the body).
DOMAIN_TITLE = re.compile(
    r"(robot|mechatron|autonom|embedded|control|regelung|automati|vision|"
    r"perception|ros\b|slam|motion|kinemat|uav|drohne|drone|sensor|"
    r"elektrotechn|electrical|hardware|firmware|simulation|digital twin|"
    r"cyber.?physi|mechani)", re.IGNORECASE,
)

STUDENT_LEVEL = re.compile(
    r"(werkstudent|working student|studentische|hiwi|hilfskraft|praktik|"
    r"intern(ship)?\b|trainee|abschlussarbeit|masterarbeit|master thesis|"
    r"thesis|junior|graduate|absolvent|berufseinsteiger|entry.level|"
    r"new grad|doktorand|phd|wissenschaftliche[rn]? mitarbeiter)",
    re.IGNORECASE,
)
SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|head of|director|manager|leiter|"
    r"teamleiter|architect|expert|vp|chief|projektleiter)\b", re.IGNORECASE,
)
# Vocational training (for school leavers), not suitable for a master's student.
VOCATIONAL = re.compile(
    r"(ausbildung|auszubildende|azubi|duales studium|dualer student|umschulung|"
    r"apprentice)", re.IGNORECASE,
)
YEARS_DEMAND = re.compile(
    r"\b([3-9]|1\d)\+?\s*(years?|jahre|jahren)\b", re.IGNORECASE,
)
# "fully funded, 3 years, 100%" / "befristet auf 3 Jahre" is how long the
# position runs, not experience the candidate must already have.
YEARS_DURATION = re.compile(
    r"(funded|funding|fixed.term|contract|duration|appointment|limited to|period of|"
    r"initially|doctoral|phd|promotion|befrist|laufzeit|vertrag|tv-?l|tv[oö]d|"
    r"\d+ ?%)", re.IGNORECASE,
)


def years_demand(text: str):
    """The first 'N years' that asks for experience; position durations are skipped."""
    for m in YEARS_DEMAND.finditer(text):
        around = text[max(0, m.start() - 60):m.end() + 40]
        after = text[m.end():m.end() + 40]
        if YEARS_DURATION.search(around) and not re.search(r"experience|erfahrung", after, re.I):
            continue
        return m
    return None
# Wording that screens out a non-fluent German speaker.
GERMAN_REQUIRED = re.compile(
    r"(fließend\w*\s+deutsch|deutsch\w*\s+(fließend|verhandlungssicher|c1|c2)|"
    r"sehr gute\w*\s+deutschkenntnisse|verhandlungssicher\w*\s+deutsch|"
    r"deutsch\s*\(?(c1|c2|muttersprache)|deutsch als muttersprache|"
    r"fluent (in )?german|german \(?(c1|c2|native|fluent)|"
    r"excellent (command of )?german|business.fluent german|native german)",
    re.IGNORECASE,
)
ENGLISH_OK = re.compile(
    r"(english is our (working|company) language|working language is english|"
    r"no german required|german (is )?(a plus|nice to have|not required)|"
    r"deutschkenntnisse (sind )?(von vorteil|wünschenswert))",
    re.IGNORECASE,
)
# Mostly-German text: counts common German function words.
GERMAN_WORDS = re.compile(r"\b(und|der|die|das|mit|für|wir|sie|ihre|bei|von)\b", re.IGNORECASE)

NEARBY = re.compile(
    r"(straubing|deggendorf|regensburg|landshut|passau|dingolfing|plattling|"
    r"cham\b|bogen\b|niederbayern|oberpfalz)", re.IGNORECASE,
)
BAVARIA = re.compile(
    r"(bayern|bavaria|münchen|munich|muenchen|nürnberg|nuremberg|erlangen|"
    r"ingolstadt|augsburg|garching|oberpfaffenhofen)", re.IGNORECASE,
)

CEFR_ORDER = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6, "native": 7}
LEVEL_WORDS = {"beginner": 1, "elementary": 2, "intermediate": 3, "upper intermediate": 4,
               "advanced": 5, "fluent": 6, "native": 7}


@dataclass
class FitResult:
    score: int
    matched: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        m = ", ".join(self.matched[:10]) or "none"
        return f"{self.score}/100 · matched: {m}" + (
            f" · {'; '.join(self.reasons)}" if self.reasons else "")


class FitScorer:
    """Scores jobs against one profile. Build once, call score() per job."""

    def __init__(self, profile: dict):
        self.profile = profile
        self.terms: list[tuple[str, tuple[re.Pattern, ...], float]] = []
        self._build_terms()
        self.german_level = self._german_level()
        loc = (profile.get("personal", {}) or {}).get("location", "")
        self.home_region_known = bool(re.search(r"straubing|deggendorf|bayern|bavaria", loc, re.I))

    @classmethod
    def from_file(cls, path: str | Path) -> "FitScorer":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- profile parsing ----

    def _add(self, term: str, weight: float) -> None:
        key = term.strip().lower()
        if not key or any(t[0] == key for t in self.terms):
            return
        spellings = ALIASES.get(key, (key,))
        pats = tuple(re.compile(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", re.I)
                     for s in spellings)
        self.terms.append((key, pats, weight))

    def _build_terms(self) -> None:
        p = self.profile
        for items in (p.get("skills") or {}).values():
            for s in items:
                self._add(s, 3.0)
        for e in p.get("experience") or []:
            for t in e.get("tools") or []:
                self._add(t, 2.5)
        for ed in p.get("education") or []:
            w = 2.0 if ed.get("status") != "discontinued" else 1.0
            for c in ed.get("coursework") or []:
                self._add(c, w)
            for r in ed.get("research_interests") or []:
                self._add(r, 3.0)
        # Domain words from the degree titles / project names
        self._add("uav", 2.0)
        self._add("autonomous systems", 2.5)

    def _german_level(self) -> int:
        for lang in self.profile.get("languages") or []:
            if (lang.get("language") or "").lower() in ("german", "deutsch"):
                lvl = (lang.get("level") or "").lower().strip()
                if lvl in CEFR_ORDER:
                    return CEFR_ORDER[lvl]
                for word, n in LEVEL_WORDS.items():
                    if word in lvl:
                        return n
                return 1
        return 0

    # ---- scoring ----

    def explain(self, job: Job) -> FitResult:
        title = job.title or ""
        desc = job.description or ""
        blob = f"{title}\n{desc}"
        reasons: list[str] = []

        matched, raw = [], 0.0
        for key, pats, weight in self.terms:
            if any(p.search(blob) for p in pats):
                matched.append(key)
                raw += weight * (1.5 if any(p.search(title) for p in pats) else 1.0)
        # Saturating: ~8 good matches already count as a strong skills fit.
        skills = min(45.0, raw * 45.0 / 24.0)

        domain = 20.0 if DOMAIN_TITLE.search(title) else (8.0 if DOMAIN_TITLE.search(desc[:1500]) else 0.0)

        level = 0.0
        student_title = bool(STUDENT_LEVEL.search(title))
        if VOCATIONAL.search(title) or (job.employment_type or "").lower() == "ausbildung":
            level -= 25.0
            reasons.append("apprenticeship")
        elif student_title or (job.employment_type or "").lower() in (
                "werkstudent", "internship", "thesis", "graduate", "phd"):
            level += 20.0
        elif STUDENT_LEVEL.search(desc[:1500]):
            level += 8.0
        # "Werkstudent ... Expert" / "Praktikum Projektleitung" are still student
        # roles, so a senior word only counts when the title isn't student-level.
        if SENIOR_TITLE.search(title) and not student_title:
            level -= 30.0
            reasons.append("senior title")
        m = years_demand(desc[:3000])
        if m:
            level -= 15.0
            reasons.append(f"asks {m.group(0)}")

        language = 0.0
        if self.german_level and self.german_level < 4:
            if GERMAN_REQUIRED.search(blob):
                language -= 20.0
                reasons.append("fluent German required")
            elif ENGLISH_OK.search(blob):
                language += 5.0
            elif len(GERMAN_WORDS.findall(desc[:2000])) > 25 and not ENGLISH_OK.search(blob):
                # A posting written entirely in German usually expects German.
                language -= 6.0
                reasons.append("German-language posting")

        location = 0.0
        if self.home_region_known:
            loc = f"{job.location} {desc[:600]}"
            if NEARBY.search(loc):
                location = 10.0
            elif BAVARIA.search(loc):
                location = 6.0
        if job.remote:
            location = max(location, 6.0)

        total = skills + domain + level + language + location
        return FitResult(int(max(0, min(100, round(total)))), matched, reasons)

    def score(self, job: Job) -> int:
        return self.explain(job).score


def _settings_file() -> Path:
    from linkedin_jobs import data_dir
    return data_dir() / "settings.json"


def remember_profile_path(path) -> None:
    """Store the profile the user picked, so the next start uses it again."""
    f = _settings_file()
    try:
        settings = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        settings = {}
    settings["profile_path"] = str(Path(path).resolve())
    try:
        f.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass


def is_bundled_copy(path) -> bool:
    """The copy packed into the .exe at build time (unpacked to a temp folder):
    it never sees edits to the user's own profile.json."""
    bundle = getattr(sys, "_MEIPASS", None)
    return bool(bundle) and Path(path).resolve().is_relative_to(Path(bundle).resolve())


def default_profile_path() -> Path:
    """The profile to use, in this order: the one the user picked (remembered),
    the data dir copy, one next to the .exe or a folder above it (dist\\ inside
    the project), the one next to the code, and only then the copy bundled into
    the .exe. Inside the .exe "next to the code" IS the temporary bundle, so the
    old order always read the build-time copy and ignored the user's own file."""
    from linkedin_jobs import data_dir
    candidates = []
    try:
        remembered = json.loads(_settings_file().read_text(encoding="utf-8")).get("profile_path")
        if remembered:
            candidates.append(Path(remembered))
    except (OSError, ValueError, AttributeError):
        pass
    candidates.append(data_dir() / "profile.json")
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates += [exe_dir / "profile.json", exe_dir.parent / "profile.json"]
    beside = Path(__file__).resolve().parent / "profile.json"
    candidates.append(beside)
    if getattr(sys, "_MEIPASS", None):
        candidates.append(Path(sys._MEIPASS) / "profile.json")
    for p in candidates:
        if p.is_file():
            return p
    return beside


def load_default_scorer() -> FitScorer | None:
    path = default_profile_path()
    try:
        return FitScorer.from_file(path)
    except (OSError, ValueError) as exc:
        print(f"[fit] no usable profile at {path}: {exc}", file=sys.stderr)
        return None


def rescore(store: Store, scorer: FitScorer, only_missing: bool = False) -> int:
    """Recompute fit_score for stored jobs (e.g. after editing profile.json).

    only_missing=True scores just the rows that have none yet, e.g. jobs saved
    before fit scoring existed.
    """
    n = 0
    rows = [r for r in store.all() if not only_missing or r["fit_score"] is None]
    with store.lock:
        for r in rows:
            s = scorer.score(Job.from_row(r))
            store.conn.execute("UPDATE jobs SET fit_score = ? WHERE job_id = ?", (s, r["job_id"]))
            n += 1
        store.conn.commit()
    return n


# ==========================================================================
# Suggested search keywords for the profile's skills (was related_skills.py)
#
# When the profile's skills change, each skill is looked up in ESCO, the EU's free
# skills database (no key): its names in English and German, alternative names and
# the skills it links to. ESCO matches technical terms loosely ("SLAM" found "Islam",
# "path planning" found oil-well paths), so the local judge model then keeps only the
# candidates that mean the skill or a close relative an employer would write in a job
# ad, and may add up to three common job-ad terms of its own.
# 
# The result is a list of suggested search keywords for the daily sweep (Settings ->
# Daily sweep -> Suggested keywords). It never feeds the CV, the letter or the fit
# score: these are words to search with, not skills the candidate has.
# 
# Stored in related_skills.json in the data folder. Only skills that are new since the
# last run are looked up; a failed lookup is retried next time.
# ==========================================================================

ESCO = "https://ec.europa.eu/esco/api"
ESCO_UA = "jobsearch-assistant/1.0 (personal job search; related-skill lookup)"
ESCO_TIMEOUT = 30
_RELATED_LOCK = threading.Lock()          # one refresh at a time (start-up and a profile change)


def related_store_path() -> Path:
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


def load_related() -> dict:
    try:
        return json.loads(related_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_related(data: dict) -> None:
    tmp = related_store_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(related_store_path())


def esco_candidates(term: str, session: requests.Session) -> list[str]:
    """Names around `term` in ESCO: the top skill matches' preferred and alternative
    names (English and German) and the skills they link to. Many are unrelated
    matches; the model sorts them out."""
    hits = session.get(f"{ESCO}/search", params={"text": term, "type": "skill", "language": "en",
                                                 "limit": 3}, timeout=ESCO_TIMEOUT).json()
    out: list[str] = []

    def add(x):
        x = " ".join(str(x or "").split())
        if x and len(x.split()) <= 5 and x.lower() not in (o.lower() for o in out):
            out.append(x)
    for h in (hits.get("_embedded") or {}).get("results", [])[:3]:
        time.sleep(0.2)
        r = session.get(f"{ESCO}/resource/skill", params={"uri": h["uri"], "language": "en"},
                        timeout=ESCO_TIMEOUT).json()
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
_GENERIC_KEYWORDS = {"code", "coding", "programming", "software", "development", "modeling", "modelling",
            "automation", "robotics", "engineering", "computing", "technology", "design"}

PICK_SCHEMA = {
    "type": "object", "required": ["keep", "more"],
    "properties": {"keep": {"type": "array", "items": {"type": "string"}},
                   "more": {"type": "array", "items": {"type": "string"}}},
}


def pick_related(term: str, candidates: list[str], call, context: str = "") -> tuple[list[str], list[str]]:
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
        if m and len(m.split()) <= 4 and low != term.lower() and low not in _GENERIC_KEYWORDS \
                and low not in (x.lower() for x in keep + more):
            more.append(m)
    return [k for k in keep if k.lower() not in _GENERIC_KEYWORDS][:3], more[:3]


def _skill_contexts(profile: dict) -> dict:
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


def refresh_related(profile: dict, base_url: str = None, model: str = None, log=print,
            force: bool = False, stop: threading.Event | None = None) -> dict:
    """Look up the skills that are new since the last run (all with `force`) and save.
    Returns the stored data. Network or model failures leave the old data in place
    and the profile marked as not yet done, so the next start tries again."""
    import ats_pipeline
    if not _RELATED_LOCK.acquire(blocking=False):
        return load_related()
    try:
        data = load_related()
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
        session.headers["User-Agent"] = ESCO_UA
        failed = 0
        contexts = _skill_contexts(profile)
        for t in todo:
            if stop is not None and stop.is_set():
                break
            try:
                cands = esco_candidates(t, session)
                keep, more = pick_related(t, cands, call, contexts.get(t, ""))
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
        _save_related(data)
        n = len(related_keywords(data, profile))
        log(f"[skills] {n} suggested search keyword(s) saved"
            + ("" if complete else f"; {len([t for t in terms if t not in done])} skill(s) "
                                   "left for next time") + ".")
        return data
    finally:
        _RELATED_LOCK.release()


def related_keywords(data: dict, profile: dict = None, exclude=()) -> list[tuple[str, str, str]]:
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


def refresh_related_in_background(profile_path, base_url=None, model=None, log=print) -> bool:
    """Start refresh_related() in a daemon thread when the profile's skills changed since the
    last run. Returns True if a lookup started."""
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if load_related().get("profile_key") == profile_key(profile):
        return False
    threading.Thread(target=refresh_related, args=(profile, base_url, model, log), daemon=True,
                     name="related-skills").start()
    return True
