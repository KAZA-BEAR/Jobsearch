"""
ats_plan.py — read the job ad and plan the tailoring (requirements, gaps, rewrite plan).

Stages 1-4 of the tailoring pipeline: everything decided BEFORE any text is written.

Modelled on ResumeAdapter's staged design (structured resume, structured job,
deterministic gap analysis, rewrite plan, then small validated rewrites):

  1. validate_profile      profile.json is the only source of facts; check it is complete
  2. extract_requirements  the job ad as structured data: skill, required/preferred and an
                           exact quote. Items whose quote is not in the ad are dropped.
  3. analyse_gaps          which requirements the profile proves, and with which line.
                           Vocabulary matches in code; for the rest, profile lines are
                           ranked by meaning (ats_embed) and the model verifies the top
                           three. Without an embedding model, the model PROPOSES a line,
                           accepted when that line really shares the term.
  4. make_plan             which bullets get which of their OWN keywords brought forward.

The rule throughout: nothing is added to the CV that profile.json does not contain.
A requirement the profile does not meet is reported as a gap and never written in.
"""

from __future__ import annotations

import json
import re

import ats_checks as checks
import ats_embed as embedding
import ats_prompts as prompts
from fit_score import FitScorer

_DATE = re.compile(r"^((Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? )?\d{4}$|^present$",
                   re.IGNORECASE)


# ------------------------------------------------------------ 1. profile check

def validate_profile(profile: dict) -> tuple[list, list]:
    """(errors, warnings). Errors stop the pipeline; warnings are logged."""
    errors, warnings = [], []
    personal = profile.get("personal") or {}
    for key in ("name", "email", "phone", "location"):
        if not str(personal.get(key, "")).strip():
            errors.append(f"personal.{key} is empty")
    for sect, need in (("experience", ("title", "employer", "start", "end", "bullets")),
                       ("education", ("degree", "institution", "start", "end"))):
        entries = profile.get(sect)
        if not isinstance(entries, list) or not entries:
            errors.append(f"'{sect}' must be a non-empty list")
            continue
        for i, e in enumerate(entries, 1):
            for key in need:
                if not e.get(key):
                    errors.append(f"{sect} #{i} has no '{key}'")
            for key in ("start", "end"):
                v = str(e.get(key, "")).strip()
                if v and not _DATE.match(v):
                    warnings.append(f"{sect} #{i} {key} '{v}' is not like 'May 2025' or 'Present'")
            if sect == "experience" and not all(isinstance(b, str) and b.strip()
                                                for b in e.get("bullets") or []):
                errors.append(f"experience #{i} has an empty or non-text bullet")
    if not isinstance(profile.get("skills"), dict) or not profile["skills"]:
        errors.append("'skills' must be an object of category -> list of skills")
    for lang in profile.get("languages") or []:
        if not lang.get("language") or not lang.get("level"):
            errors.append("each language needs 'language' and 'level'")
    return errors, warnings


# ------------------------------------------------- 2. the job as structured data

def _norm(text: str) -> str:
    return re.sub(r"[\s ]+", " ", text.lower()).strip(" .,;:\"'()")


def extract_requirements(job_description: str, call, log) -> list:
    """[{skill, priority, quote}] from the ad; each quote verified to be in the ad."""
    ad = checks.requirement_sections(job_description)[:3000]
    try:
        raw = call(prompts.requirements_prompt(ad), max_tokens=1200,
                   schema=prompts.REQUIREMENTS_SCHEMA, temperature=0)
        items = json.loads(raw).get("requirements", [])
    except (ValueError, AttributeError) as exc:
        log(f"Could not read the job requirements ({exc}); using keyword matching only.")
        return []
    hay = _norm(job_description)
    kept, dropped, degree, seen = [], [], [], set()
    for it in items:
        skill = str(it.get("skill", "")).strip()
        quote = str(it.get("quote", "")).strip()
        if not skill or skill.lower() in seen:
            continue
        if not quote or not _quoted(quote, hay):
            dropped.append(skill)
        elif _DEGREE_LINE.search(_sentence_of(quote, hay)):
            degree.append(skill)    # the prompt forbids these; Bonsai listed them anyway
        else:
            kept.append({"skill": skill, "quote": quote,
                         "priority": "preferred" if it.get("priority") == "preferred" else "required"})
            seen.add(skill.lower())
    _same_priority_per_group(kept, job_description)
    log(f"Job requirements: {len(kept)} found"
        + (f", {len(dropped)} discarded because their quote is not in the ad" if dropped else "")
        + (f", {len(degree)} fields of study left out" if degree else ""))
    return kept


# Where a model may have left words out of a quote: "..." or, without saying so,
# after a colon or semicolon (Qwen3-4B: "basic knowledge in at least one area:
# electronics and wiring", the ad lists CAD and mechanics first).
_QUOTE_GAP = re.compile(r"\.{3}|…|[:;]")


def _quoted(quote: str, hay: str) -> bool:
    """The quote is in the (normalised) ad; a quote shortened with "..." (or cut at a
    colon) counts when every part is there, in order. Bonsai wrote "basic knowledge
    in at least one area: ... Python/Linux", and the strict check threw away 7 of 9
    requirements. A part that is not in the ad still fails the quote."""
    pos = 0
    for part in (p for p in (_norm(x) for x in _QUOTE_GAP.split(quote)) if p):
        pos = hay.find(part, pos)
        if pos < 0:
            return False
        pos += len(part)
    return pos > 0


def _sentence_of(quote: str, hay: str) -> str:
    """The ad line or sentence the quote comes from."""
    first = next((p for p in (_norm(x) for x in _QUOTE_GAP.split(quote)) if p), "")
    i = hay.find(first)
    if i < 0:
        return ""
    start = max(hay.rfind(c, 0, i) for c in ".*•;") + 1
    ends = [e for e in (hay.find(c, i + len(first)) for c in ".*•;") if e >= 0]
    return hay[start:min(ends) if ends else len(hay)]


# "You are studying mechanical engineering, mechatronics or computer science":
# fields of study are not skills ("computer science" was once proven by a Python line).
_DEGREE_LINE = re.compile(r"\b(stud(y|ying|ies|ent of)|degree|bachelor|master'?s?|diploma|"
                          r"major(ing)? in|studium|studiengang|studierst|studierende|abschluss)\b")


def _same_priority_per_group(reqs: list, job_description: str) -> None:
    """Items from one "at least one of" / either-or line share one priority, the
    stronger one: the same line came out "required" from the LinkedIn text and
    "preferred" from the company website."""
    for g in (_norm(x) for x in checks.or_groups(job_description)):
        members = [r for r in reqs if _quoted(r["quote"], g)]
        if len(members) > 1 and any(r["priority"] == "required" for r in members):
            for r in members:
                r["priority"] = "required"


# "(m/f/d)", "(m/w/d)", "(f/m/x)", "(all genders)": in German ads this marks the title.
_GENDER_NOTE = re.compile(r"\((?:[mwfdx]\s*/\s*){2,3}[mwfdx]\)|\((?:all genders|gn\*?)\)", re.I)


def _title_line(job_description: str) -> str:
    """The first short line carrying a gender note - the job title in German-style
    ads. Qwen3-4B returned no title for "Working Student Ground Robotics (m/f/d)"."""
    for line in job_description.splitlines()[:80]:
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line).strip(" *#-•\t")
        text = re.sub(r"^(position|title|job title|stelle|role)\s*:\s*", "", text, flags=re.I)
        if _GENDER_NOTE.search(text) and 2 <= len(text.split()) <= 14:
            return text
    return ""


def job_identity(job_description: str, call) -> dict:
    """{"role", "company"} for an ad without the app's "Position: / Company:" header
    (pasted by hand). The title is found in code where the ad marks it; the model
    reads the rest. Each value is kept only if it is written in the ad."""
    role = _title_line(job_description)
    body = checks.jd_excerpt(job_description, limit=20_000)
    ad = body[:1500] + ("\n...\n" + body[-1200:] if len(body) > 2700 else body[1500:])
    try:
        found = json.loads(call(prompts.identity_prompt(ad), max_tokens=150,
                                schema=prompts.IDENTITY_SCHEMA, temperature=0))
    except (ValueError, AttributeError):
        found = {}
    hay = _norm(job_description)
    out = {k: str(found.get(k, "")).strip() if _norm(str(found.get(k, ""))) and
           _norm(str(found.get(k, ""))) in hay else "" for k in ("role", "company")}
    if role:
        out["role"] = role
    return out


# ----------------------------------------------------------- 3. gap analysis

def evidence_units(profile: dict) -> list:
    """Every line of the profile that can prove a skill, with a readable label."""
    units = []
    for e in profile.get("experience") or []:
        units += [(f"{e['title']} @ {e['employer']}", b) for b in e.get("bullets") or []]
    for p in profile.get("projects") or []:
        units += [(f"Project: {p['name']}", b) for b in p.get("bullets") or []]
    for cat, items in (profile.get("skills") or {}).items():
        units.append((f"Skills: {cat}", ", ".join(items)))
    for ed in profile.get("education") or []:
        extra = (ed.get("coursework") or []) + (ed.get("research_interests") or [])
        if extra:
            units.append((f"Education: {ed['degree']}", ", ".join(extra)))
        if ed.get("thesis"):
            units.append((f"Thesis: {ed['degree']}", ed["thesis"]))
    return units


def _stems(text: str) -> set:
    return {w.lower()[:5] for w in checks._WORD.findall(text)
            if w.lower() not in checks._STOP and w.lower() not in checks._HR_WORDS and len(w) >= 4}


def analyse_gaps(profile: dict, requirements: list, call, log,
                 job_description: str = "", embed_url: str = None) -> list:
    """Add status/evidence to each requirement.

    have   - every specific word of the skill is backed by the profile: a profile
             term (fit_score vocabulary) or the word itself appears in a profile
             line (bullets, skills, coursework, job titles, project names), or the
             model verified a line found by meaning ("yes")
    likely - only part of it is backed ("drone swarms": drones yes, swarms no), the
             model judged a line found by meaning "partly", or (without
             embeddings) proposed a line that shares a real word; shown for you
             to judge, never used in the CV
    gap    - nothing in the profile supports it
    """
    scorer = FitScorer(profile)
    units = evidence_units(profile)
    out, unresolved = [], []
    for r in requirements:
        # Match the skill NAME only. Matching the ad's quote too marked "LiDAR sensing"
        # as proven because its quote ("Experience with ROS, LiDAR, or simulation")
        # also names ROS, which the profile has.
        skill = r["skill"]
        terms = [(key, pats) for key, pats, _w in scorer.terms
                 if any(p.search(skill) for p in pats)]
        lines = [f"{label}: {line}" for label, line in units
                 if any(p.search(line) for _k, pats in terms for p in pats)]
        # Words of the skill the vocabulary match did not cover, e.g. "swarms".
        rest = skill
        for _k, pats in terms:
            for p in pats:
                rest = p.sub(" ", rest)
        leftover = [w.lower() for w in checks._WORD.findall(rest)
                    if w.lower() not in checks._STOP and w.lower() not in _GENERIC]
        # A skill named literally in a job title / project name / line counts too.
        literal = [f"{label}: {line}" for label, line in units
                   if leftover and all(re.search(rf"\b{re.escape(w[:max(5, len(w) - 2)])}",
                                                 f"{label} {line}", re.I) for w in leftover)]
        if literal:
            lines, leftover = literal + lines, []
        if not terms and not leftover:
            # Only generic words ("programming knowledge"): too vague to call a gap -
            # calling it one once made a letter say "I do not have programming
            # knowledge" for a candidate who knows Python and C++.
            words = [w.lower() for w in checks._WORD.findall(skill) if w.lower() not in checks._STOP]
            lines = [f"{label}: {line}" for label, line in units
                     if any(re.search(rf"\b{re.escape(w[:max(5, len(w) - 3)])}", f"{label} {line}", re.I)
                            for w in words)]
            status = "likely"
        elif lines and not leftover:
            status = "have"
        elif lines:
            status = "likely"          # partly backed: shown, never used in the CV
        else:
            status = "gap"
        item = dict(r, terms=[k for k, _ in terms] if status == "have" else [],
                    evidence=lines[:2], status=status,
                    source="vocabulary" if lines else "")
        out.append(item)
        if item["status"] == "gap":
            unresolved.append(item)

    # Meaning-based matching for what the vocabulary missed ("control engineering"
    # vs the course "Control Systems"). Falls back to the model proposing a line.
    # Gaps only: "likely" marks a partial vocabulary match ("drone swarms": drones
    # yes, swarms no), and a high similarity would wrongly prove the missing part.
    open_reqs = [r for r in out if r["status"] == "gap"]
    if open_reqs and embed_url:
        try:
            verdicts = _match_by_meaning(open_reqs, profile, call, embed_url)
        except embedding.EmbedError as exc:
            log(f"Meaning-based matching unavailable ({exc}); using the model instead.")
        else:
            proven = 0
            for req, (status, line) in zip(open_reqs, verdicts):
                if status != "gap":
                    req.update(status=status, source="meaning", evidence=[line])
                    proven += status == "have"
            if proven:
                log(f"Meaning-based matching proved {proven} more requirement(s).")
            unresolved = []          # replaces the model's line proposal below
    if unresolved:
        try:
            raw = call(prompts.evidence_prompt(unresolved, units), max_tokens=600,
                       schema=prompts.EVIDENCE_SCHEMA, temperature=0)
            links = json.loads(raw).get("links", [])
        except (ValueError, AttributeError):
            links = []
        for link in links:
            try:
                req = unresolved[int(link.get("requirement")) - 1]
                idx = int(link.get("line"))
            except (TypeError, ValueError, IndexError):
                continue
            if not 1 <= idx <= len(units):
                continue            # the model pointed at a line that does not exist
            label, line = units[idx - 1]
            shared = _stems(f"{req['skill']} {req['quote']}") & _stems(line)
            if shared:
                req.update(status="likely", source="model", evidence=[f"{label}: {line}"])
    _mark_alternatives(out, job_description or "")
    have = sum(r["status"] == "have" for r in out)
    alt = sum(r["status"] == "alternative" for r in out)
    likely = sum(r["status"] == "likely" for r in out)
    log(f"Gap analysis: {have} proven by your profile, "
        + (f"{alt} met through an alternative you have, " if alt else "")
        + f"{likely} possibly, {len(out) - have - alt - likely} not in your profile")
    return out


def _match_by_meaning(reqs: list, profile: dict, call, embed_url: str) -> list:
    """[(status, evidence line)] per requirement: expand it into concrete terms
    (one call for all), rank profile lines by meaning, and have the model judge
    the top three. "yes" -> have, "partly" -> likely, otherwise gap. The verdict
    decides, not the similarity: nomic scored "serial communication buses" vs
    the UART/I2C/SPI bullet 0.58, below unrelated pairs."""
    try:
        raw = call(prompts.expand_prompt(reqs), max_tokens=min(4000, 150 * len(reqs) + 200),
                   schema=prompts.EXPAND_SCHEMA, temperature=0)
        found = {int(e["requirement"]): [str(t) for t in e.get("terms") or []][:8]
                 for e in json.loads(raw).get("expansions", [])}
    except (ValueError, AttributeError, KeyError, TypeError):
        found = {}
    terms = [found.get(i, []) for i in range(1, len(reqs) + 1)]
    out = []
    for req, top in zip(reqs, embedding.rank(reqs, profile, embed_url, terms=terms)):
        try:
            raw = call(prompts.verify_prompt(req, [item for _s, _l, item in top]),
                       max_tokens=300, schema=prompts.VERIFY_SCHEMA, temperature=0)
            said = {int(v["line"]): v["verdict"] for v in json.loads(raw).get("verdicts", [])}
        except (ValueError, AttributeError, KeyError, TypeError):
            said = {}
        best = ("gap", "")
        for i, (_s, line, _item) in enumerate(top, 1):
            if said.get(i) == "yes":
                best = ("have", line)
                break
            if said.get(i) == "partly" and best[0] == "gap":
                best = ("likely", line)
        out.append(best)
    return out


def _mark_alternatives(reqs: list, job_description: str) -> None:
    """'Python, C++ and/or Matlab': when the profile proves one item of an
    either/or line, the others on that line are not gaps. They become
    'alternative' - met for coverage, but still never claimed (gap_terms keeps
    them off-limits in the letter)."""
    groups = [_norm(g) for g in checks.or_groups(job_description)]
    for r in reqs:
        if r["status"] == "have":
            continue
        for g in groups:
            if not _quoted(r["quote"], g):
                continue
            met = [o for o in reqs if o["status"] == "have" and _quoted(o["quote"], g)]
            if met:
                r.update(status="alternative", evidence=[f"met via {met[0]['skill']} "
                                                         f"(the ad accepts either)"])
                break


def coverage(requirements: list) -> dict:
    req = [r for r in requirements if r["priority"] == "required"]
    pref = [r for r in requirements if r["priority"] == "preferred"]
    met = ("have", "alternative")
    return {"required": len(req), "required_have": sum(r["status"] in met for r in req),
            "required_likely": sum(r["status"] == "likely" for r in req),
            "preferred": len(pref), "preferred_have": sum(r["status"] in met for r in pref)}


# Words too common to count as a claimed skill on their own ("global", "analysis").
_GENERIC = set("""global local general analysis analyse analyze testing support development
knowledge programming systems system design research methods method technical technology
technologies management maintenance strategies strategy efficiency scalability performance
assessment environment environments application applications data field
real world time multiple sensor sensors""".split())


def gap_terms(requirements: list, profile: dict) -> set:
    """Words of requirements the profile does not PROVE (gaps and "? check" ones)
    that the profile never uses - they must not appear in anything written on the
    candidate's behalf. ("LiDAR sensing" was only "possibly" met via "sensors",
    and a recruiter message then claimed LiDAR experience.)"""
    blob = checks._profile_blob(profile)
    terms = set()
    for r in requirements:
        if r["status"] == "have":
            continue
        skill = r["skill"].lower().strip()
        if skill and skill not in blob:
            terms.add(skill)
        # 4+ letters: "LiDAR" (5) slipped through a 6-letter minimum and was then
        # claimed in a recruiter message. Acronyms like "PVD" count at any length.
        terms |= {w.lower() for w in checks._WORD.findall(r["skill"])
                  if len(w) >= 4 and w.lower() not in blob and w.lower() not in checks._STOP
                  and w.lower() not in _GENERIC}
        terms |= {a.lower() for a in re.findall(r"\b[A-Z][A-Za-z]*[A-Z]\w*\b", r["skill"])
                  if a.lower() not in blob}
    return terms


# -------------------------------------------------------------- 4. rewrite plan

def make_plan(profile: dict, job_description: str, requirements: list, lang: str,
              jd_lang: str) -> list:
    """One entry per experience bullet: which of its own keywords to bring forward.

    Only terms the bullet already contains are ever planned, so the plan cannot
    add a skill. Without extracted requirements, falls back to profile terms the
    ad mentions.
    """
    mapping = checks.term_mapping(profile, job_description)
    wanted = {k for r in requirements if r["status"] == "have" for k in r["terms"]}
    if requirements:
        mapping = [m for m in mapping if m[0] in wanted]
    align = jd_lang == lang
    plan = []
    for i, job in enumerate(profile.get("experience") or [], 1):
        for j, bullet in enumerate(job.get("bullets") or [], 1):
            keep = checks.present_terms(bullet, mapping)
            pairs = checks.bullet_mapping(bullet, mapping) if align else []
            plan.append({"job": i, "bullet": j, "employer": job.get("employer"),
                         "title": job.get("title"), "end": job.get("end"), "text": bullet,
                         "keep": keep, "pairs": pairs, "edit": bool(keep or pairs),
                         "relevance": checks.relevance(bullet, mapping)})
    return plan
