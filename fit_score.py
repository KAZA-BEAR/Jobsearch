#!/usr/bin/env python3
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

Usage:
    python fit_score.py rescore --db robotics_jobs.db
    python fit_score.py explain "Werkstudent Robotik ROS2 (m/w/d)" --desc "..."
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from linkedin_jobs import Job, Store

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


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=str(default_profile_path()))
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rescore", help="recompute fit for every stored job")
    r.add_argument("--db", default="robotics_jobs.db")
    e = sub.add_parser("explain", help="score one title/description")
    e.add_argument("title")
    e.add_argument("--desc", default="")
    e.add_argument("--location", default="")
    args = p.parse_args()

    scorer = FitScorer.from_file(args.profile)
    if args.cmd == "rescore":
        n = rescore(Store(args.db), scorer)
        print(f"rescored {n} jobs")
    else:
        job = Job(job_id="x", title=args.title, company="", location=args.location,
                  description=args.desc)
        print(scorer.explain(job).summary())


if __name__ == "__main__":
    main()
