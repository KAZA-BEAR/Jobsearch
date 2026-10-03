"""
ats_checks.py — rule-based truth, grammar and style checks for generated CVs and letters.

Deterministic checks and keyword analysis for the tailoring pipeline.

Everything here runs in code, not in the model: which keywords the candidate
can honestly use, whether a rewritten bullet kept its facts, and whether the
cover letter and recruiter message have the requested shape. A 7B model
follows rules loosely, so nothing it writes reaches the CV unchecked.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from fit_score import FitScorer
from linkedin_jobs import Job

# Function words that identify a text's language.
_DE = re.compile(r"\b(und|der|die|das|mit|für|wir|sie|ihre|ich|bei|von|eine|einen|sich|nicht)\b", re.I)
_EN = re.compile(r"\b(and|the|with|for|we|you|your|our|of|to|is|are|this|that|in)\b", re.I)

# Generic words that appear in any posting and prove nothing about a skill.
_STOP = set("""
about above after again against allow along already also although always among
another anyone apply applying based become before being below between beyond
candidate candidates career careers change company could daily detail details
during either english every excellent experience experiences first following
friendly future general german germany great having however ideal including
information interest interested international looking making means months
opportunity other others people please position possible practical preferred
provide provides qualification qualifications related relevant required similar
requirements research responsibilities should skills strong student students
support their there these thing things those through together topics under
understanding university using various where which while within without working
would years yourself
aufgaben bereich bewerbung bieten deine diese dieser eines einer erfahrung erste
gerne haben ihnen ihrer kenntnisse leben lernen machen mitarbeit möglich sehr
sowie studium team teams unser unsere unseren unserer weitere werden wichtig
zusammen arbeiten arbeit bereichen jederzeit freuen
""".split())

# Recruiting boilerplate, not skills - kept out of the "missing terms" list.
_HR_WORDS = set("""
employment remuneration salary starting particular opportunities opportunity
applications application changes developing multiple investigate communications
contract duration benefits flexible office location offer offers diversity equal
disabled disabilities welcome encourage colleagues department institute institutes
successful motivated motivation independent independently reliable enjoy exciting
attractive working hours part-time full-time permanent temporary limited semester
current currently completed enrolled studies studying degree master bachelor thesis
apply online documents deadline contact questions please vacancy reference number
""".split())

_CLAIM = re.compile(
    r"((close|personal|long-standing|existing|strong|direct)\s+(relationship|connection|ties|contact)"
    r"|\b(relationship|connection|ties)\s+(with|to)\b|following your|followed your"
    r"|(enge|persönliche|bestehende)\s+(beziehung|verbindung)"
    r"|beziehung\s+zu(m|r)?\b|referred by|empfohlen von|worked with your|zusammengearbeitet)",
    re.IGNORECASE,
)
_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[a-zäöüß][a-zäöüß0-9+\-]{3,}", re.IGNORECASE)
_HEADER = re.compile(r"^(position|company|location|type|apply)\s*:", re.I)


# ------------------------------------------------------------------- language

def text_language(text: str) -> str:
    """'de' or 'en', by counting common function words."""
    de, en = len(_DE.findall(text or "")), len(_EN.findall(text or ""))
    return "de" if de > en else "en"


def choose_language(job_description: str, profile: dict) -> tuple[str, str]:
    """(document language, job-ad language).

    German documents only when the ad is German AND the candidate's German is
    at least B2; otherwise a German CV claims a fluency the interview will
    disprove.
    """
    jd_lang = text_language(job_description)
    level = FitScorer(profile).german_level      # CEFR: 4 == B2
    return ("de" if jd_lang == "de" and level >= 4 else "en"), jd_lang


# ------------------------------------------------------------ job-ad analysis

def parse_header(job_description: str) -> dict:
    """Role/company/city from the 'Position: / Company: / Location:' lines the
    app puts at the top of a job description (empty when pasted by hand)."""
    meta = {"role": "", "company": "", "city": ""}
    lines = job_description.splitlines()
    # Header lines only: the ad text's own "Location: DITZINGEN ... We Say HI* ..."
    # (Workday) would otherwise replace the city with 2,000 characters.
    for i in sorted(_header_rows(job_description)):
        line = lines[i]
        m = re.match(r"\s*(position|company|location)\s*:\s*(.+)", line, re.I)
        if m:
            key = {"position": "role", "company": "company", "location": "city"}[m.group(1).lower()]
            meta[key] = m.group(2).strip()
    return meta


# EURES and some feeds deliver the whole ad as one line: "... Was bringst du mit?
# - Studium der Elektrotechnik. - Umfangreiche Erfahrung mit Matlab Simulink. ...".
# With no line breaks no heading was found, the whole ad went to the model, and the
# PhD's tasks ("direct flux control") came back as requirements while "Matlab
# Simulink" was missed (BMW ad, 2026-09-30).
_INLINE_HEADING = re.compile(r"(?<=[.!?:])\s+([A-ZÄÖÜ][^.!?:\n]{2,45}[?:]"
                             # a question sentence ends a section too: "Bringst du eine hohe
                             # Einsatzbereitschaft mit ...? Dann bewirb dich jetzt!"
                             r"|[A-ZÄÖÜ][^.!?\n]{2,150}\?)(?=\s|$)")
_INLINE_BULLET = re.compile(r"\s+[-•*]\s+(?=[A-ZÄÖÜ0-9])")
# DLR's feed has headings with no ":" or "?" at all: "... What to expect The Embedded
# ... Your tasks Developing ... Your profil A completed university degree ..."
# (DLR real-time systems ad, 2026-09-30). Matched only with a capital letter
# after them, so "about your tasks in the team" stays prose.
_KNOWN_HEADING = re.compile(
    r"(?<=\S)\s+(What to expect|What you can expect|Your tasks|Your responsibilities|"
    r"Your role|Your profile|Your profil|Your qualifications|What you bring|"
    r"What we expect|What we offer|We offer|Our offer|Nice to have|Requirements|"
    r"Qualifications|"
    r"Deine Aufgaben|Ihre Aufgaben|Dein Profil|Ihr Profil|Wir bieten|Das bieten wir|"
    r"Was dich erwartet|Was Sie erwartet)(?=\s+[A-ZÄÖÜ(])"
    # Fraunhofer's fixed headings, followed by a lower-case word as often as not:
    # "... Hier sorgen Sie für Veränderung Mitarbeit in Forschungsprojekten ...
    # Hiermit bringen Sie sich ein abgeschlossenes wissenschaftliches Hochschulstudium
    # ... Was wir für Sie bereithalten Möglichkeit zur Promotion ..." (2026-10-03)
    r"|(?<=\S)\s+(Hier sorgen Sie für Veränderung|Hiermit bringen Sie sich ein|"
    r"Was wir für Sie bereithalten|Was Sie bei uns tun|Was Sie mitbringen|"
    r"Was Sie erwarten können|Das bringen Sie mit|Das erwartet Sie|Was wir Ihnen bieten|"
    # Thales on Workday: "... Your mission as “Werkstudent ...”: Analyze ... document
    # findings We are looking forward to: Student in Computer Science ..." (2026-10-03)
    r"Your mission|We are looking forward to)"
    r":?(?=\s)")     # "We are looking forward to: Student in ..." has the colon attached
# Where the pay and contact text starts: "... spoken English Remuneration is based
# on qualifications ...". A heading is put before it so the requirements end there.
_OFFER_START = re.compile(
    r"(?<=\S)\s+(?=(?:Remuneration|Salary|We look forward|If you have any questions|"
    r"Die Vergütung|Wir freuen uns|Bei Fragen|"
    # Thales' company text after the requirements: "... problem solving The Group
    # invests more than €4,5 billion ... Say HI* - Your journey to us ..."
    r"The Group invests|Say HI\* – Your journey)\b)")
# Items of an unbulleted list: "... other relevant fields Several years' professional
# experience ... Linux Experience working with the Linux kernel ... Python Very good
# written and spoken English". A new item starts with a capitalised gerund
# ("Developing", "Integrating") or a word that typically opens a requirement,
# right after a lower-case word (so "under Linux" is not split).
_ITEM_START = re.compile(
    # -ing nouns are not item starts: "Radar Systems Engineering" was split in two (Thales)
    r"(?<=[a-zäöüß0-9)’'])\s+(?=(?:(?!(?:Engineering|Training|Marketing|Manufacturing|Computing|"
    r"Building|Housing|Banking|Accounting|Consulting|Shipping|Packaging|Printing|Recycling|"
    r"Learning|Planning|Processing|Modeling|Modelling|Sensing|Machining|Welding|Engineering|"
    r"Mapping|Tracking|Imaging|Rendering|Networking)\b)[A-Z][a-z]+ing|Several|Experience|"
    r"In-depth|Programming|Very|"
    r"Good|Excellent|Fluent|Strong|Solid|Sound|Knowledge|Familiarity|Proficiency|Ability|"
    r"Basic|Hands-on|Participation|Completed|Initiative|Enthusiasm|Interest)\b)")


def unflatten(text: str) -> str:
    """Put inline headings and " - " bullets of a one-line ad on lines of their
    own. Lines shorter than 400 characters are left alone: a normal ad already
    has its line breaks, and " - " inside a title ("Ingenieur/in - Elektrotechnik")
    must stay."""
    out = []
    for line in text.splitlines():
        if len(line) >= 400:
            line = _KNOWN_HEADING.sub(lambda m: "\n" + (m.group(1) or m.group(2)) + ":\n", line)
            line = _OFFER_START.sub("\nTerms and contact:\n", line, count=1)
            line = _INLINE_HEADING.sub(lambda m: "\n" + m.group(1) + "\n", line)
            line = _INLINE_BULLET.sub("\n- ", line)
            if "\n- " not in line:          # no bullets at all: split the bare items
                line = _ITEM_START.sub("\n- ", line)
        out.append(line)
    return "\n".join(out)


def _header_rows(job_description: str) -> set:
    """Line numbers of the app's own header ("Position: / Company: / Location: /
    Type: / Apply:"): short lines in the block before the first blank line. Workday
    ad text starts "Location: DITZINGEN SRA OME, Germany We Say HI* Werkstudent ..."
    as one 2,000-character line, and stripping every "Location:" line threw away the
    whole Thales ad (2026-10-03)."""
    rows = set()
    for i, line in enumerate(job_description.splitlines()):
        if not line.strip():
            break
        if _HEADER.match(line.strip()) and len(line) <= 200:
            rows.add(i)
    return rows


def jd_excerpt(job_description: str, limit: int = 1800) -> str:
    """The ad's text without the app's header lines, trimmed to `limit` chars."""
    header = _header_rows(job_description)
    body = "\n".join(l for i, l in enumerate(job_description.splitlines()) if i not in header)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:limit] + (" ..." if len(body) > limit else "")


# Section headings that introduce what the candidate must / should bring.
_REQ_HEADING = re.compile(
    r"(qualific|requirement|your profil|profile:|you bring|you have|you should|we expect|"
    r"what we('re| are) looking for|what you need|skills|a plus|nice.to.have|preferred|desirable|"
    r"bonus|experience in any|must.have|ihr profil|dein profil|anforderung|qualifikation|"
    r"mitbringst|mitbringen|bringst du mit|bringen sie mit|bringen sie sich ein|von vorteil|"
    r"we are looking forward to|"
    r"wünschenswert|"
    r"kenntnisse)", re.I)
# "If your profile matches the requirements, please upload ..." is about applying.
_NOT_REQ_HEADING = re.compile(r"(upload|documents|how to apply|send us|submit|unterlagen|"
                              r"bewerben|we offer|wir bieten|benefits)", re.I)


def _is_heading(line: str) -> bool:
    s = line.strip()
    # A question may be longer ("Bringst du eine hohe Einsatzbereitschaft mit und
    # möchtest ... mitgestalten?" ends the BMW requirements); a long line ending in
    # ":" is more often part of a list ("... at least one of the following tools:").
    limit = 150 if s.endswith("?") else 110
    return 0 < len(s) <= limit and (s.endswith(":") or s.endswith("?")) and not s.startswith(("-", "*", "•"))


def requirement_sections(job_description: str) -> str:
    """Only the ad's qualification sections (headings kept, so 'a plus' still marks
    items as preferred). The research-area blurb ('drawing from probabilistic
    inference, foundation models, ...') describes the field, not the candidate; read
    as requirements it once turned background prose into 'required' gaps. Returns
    the whole ad when it has no recognisable qualification headings."""
    body = unflatten(jd_excerpt(job_description, limit=20_000))
    keep, on = [], False
    for line in body.splitlines():
        if _is_heading(line):
            on = bool(_REQ_HEADING.search(line)) and not _NOT_REQ_HEADING.search(line)
        if on:
            keep.append(line)
    text = "\n".join(keep).strip()
    if len(text) >= 80:
        return text
    blocks = _requirement_blocks(body)
    return blocks if len(blocks) >= 80 else body


# What a block of the ad is about, by its lines. The Arbeitsagentur sends headings
# as bare "# " lines, so the BSH ad's "- Studium: ...", "- Sprachkenntnisse: ..."
# block could not be told from its tasks and benefits by heading (2026-10-03).
_REQ_CUE = re.compile(
    r"\b(studium|studiengang|studierende\w*|student|abschluss|degree|bachelor|master|kenntnis\w*|"
    r"knowledge|erfahrung\w*|experience[ds]?|sprach\w*|deutsch\w*|englisch\w*|english|german|"
    r"fließend|fluent|skills?|fähigkeit\w*|arbeitsweise|qualifi\w*|vorteil|wünschenswert|plus|"
    r"proficien\w*|familiar|vertraut|verfügst|verfügen|mitbringst|you have|you bring|"
    r"ability to|background in)\b", re.I)
_TASK_CUE = re.compile(
    r"\b(durchführung|unterstützung|mitwirkung|mitarbeit|aufbereitung|betreuung|aufgaben|"
    r"you will|your tasks|responsib\w*|in this role)\b", re.I)
_OFFER_CUE = re.compile(
    r"\b(vergütung|gehalt|arbeitszeit\w*|urlaub|benefits?|mobile[ns]? arbeiten|homeoffice|"
    r"flexib\w*|atmosphäre|wir bieten|we offer|salary|tarif\w*|zeitkonto|weiterbildung\w*|"
    r"altersvorsorge|jobticket)\b", re.I)


def _requirement_blocks(body: str) -> str:
    """The blocks (split at "#" headings and blank lines) whose lines mostly read as
    requirements, for ads without a recognisable qualifications heading."""
    picked = []
    for block in re.split(r"\n(?=#)|\n\s*\n", body):
        lines = [l for l in block.splitlines() if l.strip(" #-")]
        req = sum(bool(_REQ_CUE.search(l)) for l in lines)
        task = sum(bool(_TASK_CUE.search(l)) for l in lines)
        offer = sum(bool(_OFFER_CUE.search(l)) for l in lines)
        if req >= 2 and req > task and req > offer:
            picked.append(block.strip())
    return "\n\n".join(picked)


def or_groups(job_description: str) -> list[str]:
    """Ad lines that list alternatives ('Python, C++ and/or Matlab', 'ROS oder ROS2'):
    meeting any one item of such a line meets the line."""
    # a line that continues in lower case was wrapped ("... testing or data\n\nevaluation.")
    text = re.sub(r"\s*\n\s*(?=[a-zäöü])", " ", unflatten(job_description))
    # Over 250 characters it is not one list item but unsplit text: the DLR ad's
    # whole profile section, read as one "A or B" line, made "Linux kernel
    # development" count as met "via Python programming".
    return [l.strip() for l in re.split(r"[\n•;]|(?<=[.!?])\s+", text)
            if len(l.strip()) <= 250
            and re.search(r"\band/or\b|\bor\b|\boder\b|\bbzw\.?|at least one|one of the following|"
                          r"mindestens (ein|eine|einem|einer)\b", l, re.I)]


_MASTER_REQ = re.compile(
    r"(\bm\.?\s?sc\b|\bm\.?\s?eng\b|master'?s?\s+(degree|or equivalent)|qualifying degree|"
    r"completed (university |master'?s? )?degree|abgeschlossene[snm]?\s+(master|hochschul|universitäts|"
    r"wissenschaftliche)|masterabschluss|diplom\b)", re.I)
_DOCTORAL = re.compile(r"(doctoral|\bph\.?\s?d\b|promotion|doktorand|dissertation)", re.I)
# "Erfolgreich abgeschlossenes Studium der Elektrotechnik" (BMW PhD ad) names no
# degree level. For a doctoral position a finished study means a finished master's;
# for other roles a bachelor's may do, so this only counts in doctoral ads.
_STUDIES_DONE = re.compile(
    r"((erfolgreich\s+)?abgeschlossene[snm]?\s+(studium|studiums|hochschulstudium)|"
    r"(successfully\s+)?completed\s+(studies|university studies|degree)|"
    r"studium\s+erfolgreich\s+abgeschlossen)", re.I)


def degree_warning(job_description: str, profile: dict) -> str:
    """A warning when the ad requires a (completed) master's degree and the
    profile's master's is still in progress - the one requirement that can
    disqualify an application outright, which the skill list never covers."""
    body = jd_excerpt(job_description, limit=20_000)
    m = _MASTER_REQ.search(body) or (_DOCTORAL.search(body) and _STUDIES_DONE.search(body))
    if not m:
        return ""
    # "A completed university degree (Master's / Bachelor's)" (DLR ad): a finished
    # bachelor's is enough, so no warning for a candidate who has one.
    if re.search(r"bachelor|\bb\.?\s?(sc|eng|a)\b", body[m.start():m.end() + 150], re.I):
        bachelors = [e for e in profile.get("education") or []
                     if re.match(r"\s*(b\.?\s?(sc|eng|a)\b|bachelor)", e.get("degree", ""), re.I)]
        if any(str(e.get("status", "")).lower() == "completed" for e in bachelors):
            return ""
    masters = [e for e in profile.get("education") or []
               if re.match(r"\s*(m\.?\s?(sc|eng|a)\b|master)", e.get("degree", ""), re.I)]
    if any(str(e.get("status", "")).lower() == "completed" for e in masters):
        return ""
    current = [e for e in masters if str(e.get("end", "")).lower() == "present"
               or "progress" in str(e.get("status", "")).lower()]
    if not current:
        return ""
    e = current[0]
    # Named from the job title: Fraunhofer's research-associate job mentions "Möglichkeit
    # zur Promotion" in its text and was called "a doctoral position" (2026-10-03).
    title = parse_header(job_description)["role"] or next(
        (l for l in body.splitlines() if l.strip()), "")
    what = "a doctoral position" if _DOCTORAL.search(title) else "this role"
    return (f"{what} requires a master's degree (\"{m.group(0).strip()}\"), and your "
            f"{e['degree']} is still in progress - check whether they accept candidates "
            "who graduate before the start date, and say when you expect to finish")


# What German level an ad demands. The BMW PhD ad asked for "Sehr gute Deutsch-
# und Englischkenntnisse"; with German at beginner level the only note was
# "German-language posting", and the report counted 0 requirements not met.
_CEFR = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6}
_DE_STRONG = r"(sehr gute\w*|exzellente\w*|hervorragende\w*|verhandlungssichere\w*|fließende\w*)"
_DE_GOOD = r"(gute\w*|solide\w*|fundierte\w*)"
_LANG_CONTEXT = (r"(?=\s*(?:language|skills|proficiency|knowledge|\(|,|\.|;|\band\b|\bis\b|"
                 r"\brequired\b|\bessential\b|$))")
_GERMAN_REQ = (
    # "Sehr gute Deutschkenntnisse", "sehr gute Englisch- und Deutschkenntnisse"
    (re.compile(_DE_STRONG + r"\s+(?:[a-zäöü]+-\s+(?:und|oder|sowie)\s+)?deutsch", re.I), 5),
    (re.compile(_DE_GOOD + r"\s+(?:[a-zäöü]+-\s+(?:und|oder|sowie)\s+)?deutsch", re.I), 4),
    # "Deutsch fließend", "Deutsch verhandlungssicher"
    (re.compile(r"\bdeutsch\w*\s+(fließend|verhandlungssicher)", re.I), 5),
    # "fluent in German", "excellent German and English", "very good command of German".
    # German must be followed by language context, so "a strong German brand" is not one.
    (re.compile(r"\b(fluent|fluency|excellent|very good|business[- ]fluent|native)\b"
                # same clause only: "Excellent command of English; German skills are an
                # asset" paired "Excellent" with German (ZEISS, 2026-10-03)
                r"[^.;,\n]{0,25}\bgerman\b" + _LANG_CONTEXT, re.I), 5),
    (re.compile(r"\b(good|solid|strong)\b[^.;,\n]{0,25}\bgerman\b" + _LANG_CONTEXT, re.I), 4),
    (re.compile(r"\bgerman\b[^.\n]{0,15}\b(fluent|fluency)\b", re.I), 5),
)
# "Deutsch auf C1-Niveau", "German (min. B2)"
_GERMAN_CEFR = re.compile(r"\b(?:deutsch\w*|german)\b[^.\n]{0,25}?\b([abc][12])\b", re.I)


def german_requirement(job_description: str) -> tuple[int, str]:
    """(CEFR level the ad asks for, the phrase that says so), or (0, "")."""
    body = jd_excerpt(job_description, limit=20_000)
    best = (0, "")
    m = _GERMAN_CEFR.search(body)
    if m and not _OPTIONAL.search(body[m.end():m.end() + 60]):
        best = (_CEFR[m.group(1).lower()], m.group(0))
    for pattern, level in _GERMAN_REQ:
        for m in pattern.finditer(body):
            if level > best[0] and not _OPTIONAL.search(body[m.end():m.end() + 60]):
                best = (level, m.group(0))
    return best


# "German skills are an asset but not required", "Deutschkenntnisse von Vorteil":
# wanted, not required, so no hard requirement.
_OPTIONAL = re.compile(r"^[^.;]{0,40}?\b(not required|not necessary|not a must|optional|an asset|"
                       r"a plus|an advantage|advantageous|nice to have|preferred|von vorteil|"
                       r"wünschenswert|keine voraussetzung|nicht erforderlich)\b", re.I)


def language_warning(job_description: str, profile: dict) -> str:
    """A warning when the ad asks for more German than the profile states - like a
    missing degree, a hard requirement the skill list never covers."""
    need, phrase = german_requirement(job_description)
    have = FitScorer(profile).german_level
    if not need or have >= need:
        return ""
    names = {1: "A1", 2: "A2", 3: "B1", 4: "B2", 5: "C1", 6: "C2"}
    stated = next((str(l.get("level", "")) for l in profile.get("languages") or []
                   if str(l.get("language", "")).lower() in ("german", "deutsch")), "")
    return (f"the ad asks for German at about {names[need]} (\"{phrase.strip()}\"), and your "
            f"profile gives your German as {stated or 'not stated'} - "
            "a hard requirement you do not meet yet")


# "Several years' professional experience in software development for embedded
# real-time systems" (DLR ad) never became a requirement: the prompt asks for skills.
_YEARS_REQ = (
    (re.compile(r"\b(\d{1,2})\s*\+?\s*(?:or more\s+)?years?[’']?\s+(?:of\s+)?"
                r"(?:relevant\s+|professional\s+|industry\s+|industrial\s+|work\s+|practical\s+)*"
                r"experience", re.I), None),
    (re.compile(r"\b(several|multiple)\s+years?[’']?\s+(?:of\s+)?(?:relevant\s+|professional\s+|"
                r"industry\s+|work\s+|practical\s+)*experience", re.I), 3),
    (re.compile(r"\b(many|extensive)\s+years?[’']?\s+(?:of\s+)?(?:relevant\s+|professional\s+|"
                r"industry\s+|work\s+)*experience", re.I), 5),
    (re.compile(r"\b(\d{1,2})\s*\+?\s*jahre\w*\s+(?:\w+\s+)?(?:berufs|praxis|projekt)?erfahrung", re.I), None),
    (re.compile(r"\bmehrjährige\w*\s+(?:\w+\s+)?(?:berufs|praxis|projekt)?erfahrung", re.I), 3),
    (re.compile(r"\blangjährige\w*\s+(?:\w+\s+)?(?:berufs|praxis|projekt)?erfahrung", re.I), 5),
)
_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def _month_index(text: str, today) -> int | None:
    """'Nov 2022' / 'July 2023' / '2021' / 'Present' as a month count."""
    t = str(text or "").strip().lower()
    if t in ("present", "now", "current", "heute", "aktuell"):
        return today.year * 12 + today.month
    m = re.search(r"(\d{4})", t)
    if not m:
        return None
    month = next((n for k, n in _MONTHS.items() if t.startswith(k)), 1)
    return int(m.group(1)) * 12 + month


def experience_months(profile: dict) -> int:
    """Months of work in the profile's experience entries, overlaps not merged."""
    from datetime import date
    today, total = date.today(), 0
    for e in profile.get("experience") or []:
        start, end = _month_index(e.get("start"), today), _month_index(e.get("end"), today)
        if start and end and end >= start:
            total += end - start + 1
    return total


def experience_warning(job_description: str, profile: dict) -> str:
    """A warning when the ad asks for more years of experience than the profile has."""
    body = jd_excerpt(job_description, limit=20_000)
    for pattern, years in _YEARS_REQ:
        m = pattern.search(body)
        if not m:
            continue
        need = years or int(m.group(1))
        have = experience_months(profile)
        if need and have < need * 12:
            return (f"the ad asks for about {need} years of experience (\"{m.group(0).strip()}\"), "
                    f"and your profile shows about {have / 12:.1f} years of work in total - "
                    "check whether the role is open to graduates")
        return ""
    return ""


def _profile_blob(profile: dict, notes: str = "") -> str:
    return (json.dumps(profile, ensure_ascii=False) + " " + (notes or "")).lower()


# Ordinary prose in research / job ads ("push the frontiers", "anchored in",
# "expanding toward"). Not skills: flagging them as "not in your profile" made the
# fact-check delete true sentences that happened to use "toward" or "models".
_PROSE = set("""
learning learn toward towards anchored anchor frontier frontiers expanding expand
expands statement statements academic academia methodologies methodology frameworks
framework thorough theory theories drawing bring brings bringing together reliably
reliable settings setting ranging range across different embodiments dynamic talented
autonomous highly ambition ambitious curriculum perspectives perspective activities
activity contribute contributes contribution benefit benefits disseminate findings
publications publication conferences conference present presentation mentoring
teaching participate stimulating interdisciplinary environment cutting-edge
resources discoveries groundbreaking exciting currently seeking offering offers
positions advised co-advised established network collaborations collaboration visits
internships encouraged practice project projects guidance quality beautiful compensation
starting internationally highlights selected unique individually tailored career
development excited closely proficiency written spoken communication manage
simultaneously style motivated highly fundamental interest interests areas
certificates certificate transcript transcripts records supplements overview
chronological tabular complete original version translation translator official
seal combined document documents issued require appears generated assisted chances
advancing evaluated rolling basis encourage early shortlisted invited interview
travel expenses reimbursed costs process admission guide payscale scale
next-generation understand naturally around adapt interact problem problems
funded individuals whereas whether indicate prefer conduct comparable equivalent
qualifying discipline disciplines groups group doctorate ability knowledge methods
implementation real-world hands-on expertise extensive access diverse
computational technology world-leading internally constraints reporting
""".split())


def _person_names(text: str) -> set:
    """Lower-case words of names that follow an academic title ('Prof. Dr. Simone
    Kager') - a professor's name is not a skill the candidate lacks."""
    names = set()
    for m in re.finditer(r"\b(?:Prof|Dr|Professor)\.?(?:\s+Dr\.?(?:-Ing\.?)?)?\s+"
                         r"((?:[A-ZÄÖÜ][\w-]+\s?){1,3})", text):
        names |= {w.lower() for w in m.group(1).split()}
    return names


def _noise(word: str, names: set) -> bool:
    w = word.lower()
    return w in _PROSE or w in names or bool(re.match(r"tv-?l|tv[oö]d|e\d", w))


# Irregular past forms the profile uses for verbs an ad writes in -ing / base form.
_IRREGULAR_PAST = {"build": "built", "write": "wrote", "make": "made", "lead": "led",
                   "run": "ran", "teach": "taught", "bring": "brought", "grow": "grew",
                   "hold": "held", "draw": "drew", "take": "took", "give": "gave"}


def _known(word: str, blob: str) -> bool:
    """A word, or another form of it, already used by the profile: plural, -ing,
    -ed, or an irregular past. "building" in an ad ("building next-generation
    UAS") was treated as a skill the profile lacks because the profile says
    "Built", and a true sentence about building the manipulator was deleted."""
    w = word.lower()
    if w in blob or (w.endswith("s") and w[:-1] in blob) or (w.endswith("es") and w[:-2] in blob):
        return True
    base = re.sub(r"(ing|ed|es|s)$", "", w)
    if len(base) < 4:
        return False
    return (base in blob or base + "e" in blob                       # develop, create
            or (len(base) > 4 and base[-1] == base[-2] and base[:-1] in blob)   # planning
            or _IRREGULAR_PAST.get(base, "\0") in blob
            or _IRREGULAR_PAST.get(base + "e", "\0") in blob)


# Everyday words an ad uses that are not skills. As "terms the profile lacks" they
# made "my technical background" or "during my internship" count as an unproven claim.
_COMMON = set("""believe believes homepage on-site onsite shuttle canteen discounts wellpass
detailsapply jobdetails
lunch-card corporate employees employee benefits visionary pioneers ambitious
technical technological professional professionals industry industrial
internship internships evaluate evaluated evaluating improve improved improving results
result solution solutions supported supporting needed essential helpful carefully clearly
actively active individual individually necessary suitable depending something options
option compare comparing concept concepts clarify success successes efforts growth
potential mission ownership strengths strength subject subjects preparation prepare
powerful innovative innovation creative curious measurable early-stage turning return
events event service services reviews review generate differences balance practical
together interests interested learning experience experiences projects project
knowledge ability abilities skills skill quality qualities challenges challenge
responsible responsibility ideas modern exciting excellent passionate motivated
reliable reliably independently independent structured analytical thinking
specialising specializing specialised specialized fields several in-depth
forward personally enthuse approach
""".split())


def _foreign_terms(job_description: str, blob: str) -> set[str]:
    """Distinctive job-ad terms the profile does not contain."""
    # all-caps (PVD, ROS) and mixed-case (LiDAR, MATLAB-like) acronyms
    acronyms = {a for a in re.findall(r"\b[A-Z][A-Za-z0-9]{0,6}[A-Z][A-Za-z0-9]*\b|\b[A-Z]{2,7}\b",
                                      job_description) if a.lower() not in blob}
    own_name = {w.lower() for w in _WORD.findall(" ".join(parse_header(job_description).values()))}
    words = {w.lower() for w in _WORD.findall(job_description) if len(w) >= 6}
    names = _person_names(job_description)
    return {a.lower() for a in acronyms if not _noise(a, names) and a.lower() not in own_name} | {
        w for w in words if w not in _STOP and w not in _HR_WORDS and w not in _COMMON
        and not _noise(w, names) and w not in own_name and not _known(w, blob)}


_LOCATION_LINE = re.compile(
    r"\b(germany|deutschland|austria|österreich|switzerland|schweiz|netherlands|france|"
    r"bayern|bavaria|baden-württemberg|berlin|hamburg|hessen|sachsen|nordrhein-westfalen|"
    r"niedersachsen|on-site|remote|hybrid)\b", re.I)


# German ad words that are not skills. The BMW ad's "terms not in your profile"
# were "deinen, definierst, bringst, bewirb, e-mail, Stellenreferenz, Marken":
# matched by prefix, so every inflection ("umfangreiche", "umfangreichen") goes.
_DE_FILLER = ("erfolgreich", "abgeschlossen", "vergleichbar", "umfangreich", "sicher", "sehr",
              "gute", "guten", "hohe", "hohen", "fundiert", "einschlägig", "ausgeprägt",
              "selbstständig", "selbständig", "eigenständig", "strukturiert", "zuverlässig",
              "idealerweise", "wünschenswert", "vorteil", "umgang", "mindestens", "verhandlungs",
              "fließend", "deutsch", "englisch", "kenntniss", "erfahrung", "bereich", "studium",
              "abschluss", "sprachkenntnis", "teamfähig", "kommunikation", "motivation",
              "begeisterung", "interesse", "freude", "bewerb", "dein", "unser", "ihre", "eure",
              "stellenreferenz", "karriere", "e-mail", "marken", "weiterhin", "außerdem", "zudem",
              "insbesondere", "jeweilig", "aktuell", "zukünftig", "innovativ", "neue", "neuen",
              "notwendig", "vergleichend", "definierst", "entwickelst", "bringst", "führst",
              "bewirb", "möchte", "mitgestalt", "einsatzbereit", "laufend",
              # from the requirement sections of the Fraunhofer and BSH ads (2026-10-03)
              "hiermit", "bringen", "ausrichtung", "vorliegt", "beifüg", "notenübersicht",
              "abschlusszeugnis", "hochschulstudium", "wissenschaftlich", "neugier", "tatendrang",
              "eigeninitiative", "fachwissen", "arbeitsweise", "verfüg", "bereits", "theoretisch",
              "dieses", "praktisch", "dynamisch", "unternehmen", "ergänz", "fortlaufend",
              "fachrichtung", "ingenieurwesen", "wirtschaftsingenieur", "wort und", "schrift",
              "interesse", "erste", "ideal", "insbesondere", "perspektivisch", "anleitung")
# Ad word -> English word that means the same in the profile. A German ad against
# an English profile listed "Regelung" as missing although the profile says
# "control systems".
_DE_EN = {"elektrotechnik": "electrical", "mechatronik": "mechatronic", "regelung": "control",
          "regelungstechnik": "control", "steuerung": "control", "steuerungstechnik": "control",
          "robotik": "robot", "informatik": "computer science", "maschinenbau": "mechanical",
          "programmierung": "programming", "entwicklung": "develop", "bildverarbeitung": "vision",
          "simulation": "simulation", "konstruktion": "cad", "sensorik": "sensor",
          "elektronik": "electronic", "automatisierung": "automation", "leistungselektronik":
          "power electronics", "softwareentwicklung": "software"}


def _german_noise(word: str) -> bool:
    return word.lower().startswith(_DE_FILLER)


def keyword_report(profile: dict, job_description: str, title: str = "") -> dict:
    """Which job keywords the candidate has, which are missing, and a 0-100 fit."""
    scorer = FitScorer(profile)
    res = scorer.explain(Job(job_id="ats", title=title, company="", description=job_description))
    blob = _profile_blob(profile)
    german = text_language(job_description) == "de"
    # The qualification section when the ad has one: company blurbs, benefits and
    # "how to apply" text gave "Marken", "Stellenreferenz" and "bewirb".
    body = requirement_sections(job_description)
    # Not job skills: link text and web addresses ("homepage",
    # "working-student-ground-robotics-mfd") and short location lines ("Gilching,
    # Bayern, Germany") came out as "terms not in your profile".
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)|https?://\S+|www\.\S+", " ", body)
    body = "\n".join(l for l in body.splitlines()
                     if not (len(l.split()) <= 6 and _LOCATION_LINE.search(l)))
    counts = Counter(w.lower() for w in _WORD.findall(body) if len(w) >= 6)
    counts.update(re.findall(r"\b[A-Z][A-Z0-9]{1,6}\b", body))
    own_name = {w.lower() for w in _WORD.findall(" ".join(parse_header(job_description).values()))}
    names = _person_names(job_description)
    missing = [t for t, _ in counts.most_common()
               if t.lower() not in _STOP and t.lower() not in _HR_WORDS and t.lower() not in _COMMON
               and not _noise(t, names) and not _known(t, blob) and t.lower() not in own_name
               and not t.lower().startswith("vacancy")
               and not (german and (_german_noise(t) or _DE_EN.get(t.lower(), "\0") in blob))][:15]
    return {"score": res.score, "matched": res.matched, "reasons": res.reasons,
            "missing": missing}


# -------------------------------------------------------------- bullet checks

_NUM_WORDS = {"1": ("one", "a", "an", "single"), "2": ("two",), "3": ("three",),
              "4": ("four",), "5": ("five",), "10": ("ten",)}
_VERBS = set("""built build building developed develop designed design created create
implemented implement wrote write written tested test coordinated coordinating led
managed engineered integrated performed conducted prepared automated contributed
collaborated collaborating teaching taught mentored achieved reduced added included
demonstrated using used enhanced improved delivered supported worked
writing""".split())   # "writing" and "wrote" share no stem; the others do
_IRREGULAR = set("""built wrote taught led ran made set drew held kept gave took won
began brought found grew sought undertook oversaw""".split())
_MODEST = re.compile(r"\b(contribut\w*|assist\w*|support\w*|help\w*|participat\w*)\b", re.I)
_UPGRADE = re.compile(r"\b(improv\w*|streamlin\w*|optimi[sz]\w*|enhanc\w*|boost\w*)\b", re.I)
_INFLATE = re.compile(r"\b(led|lead|leading|managed|headed|spearheaded|directed|supervised|"
                      r"oversaw|owned|drove)\b", re.I)


def _stems(text: str, skip: set = frozenset()) -> set:
    """5-letter stems of content words, ignoring verbs (which a rephrase may
    legitimately change) and the mapped terms themselves."""
    words = {w.lower() for w in _WORD.findall(text)} - _STOP - _VERBS
    return {w[:5] for w in words if not any(w in s for s in skip)}


def term_mapping(profile: dict, job_description: str) -> list:
    """(profile term key, compiled patterns, the job ad's own spelling) for every
    profile skill the ad mentions."""
    out = []
    for key, pats, _w in FitScorer(profile).terms:
        for pat in pats:
            m = pat.search(job_description)
            if m:
                out.append((key, pats, m.group(0)))
                break
    return out


def bullet_mapping(bullet: str, mapping: list) -> list:
    """[(bullet's spelling, ad's spelling)] where the two differ."""
    pairs = []
    for _key, pats, ad in mapping:
        for pat in pats:
            m = pat.search(bullet)
            if not m:
                continue
            own = m.group(0)
            # "ROS2" already covers an ad's "ROS"; only map real wording differences.
            # Never trade a phrase for a shorter one ("path planning" -> "trajectory"
            # lost its meaning); the ad's wording must be at least as specific.
            if (ad.lower() not in own.lower() and own.lower() not in ad.lower()
                    and len(ad.split()) >= len(own.split())):
                pairs.append((own, ad))
            break
    return pairs


def present_terms(bullet: str, mapping: list) -> list:
    """The bullet's own spelling of every job keyword it contains."""
    out = []
    for _k, pats, _ad in mapping:
        for pat in pats:
            m = pat.search(bullet)
            if m:
                out.append(m.group(0))
                break
    return out


def relevance(bullet: str, mapping: list) -> int:
    return sum(1 for _k, pats, _ad in mapping if any(p.search(bullet) for p in pats))


def swap_terms(bullet: str, pairs: list) -> str:
    """Deterministic fallback: replace each own spelling with the ad's."""
    for own, ad in pairs:
        bullet = re.sub(re.escape(own), ad, bullet, count=1, flags=re.I)
    return bullet


def edit_problems(old: str, new: str, pairs: list, foreign: set, lang: str,
                  keep: list = ()) -> list:
    """Why an edited bullet can't be used; empty list = safe to use."""
    problems = []
    if not new or "\n" in new.strip():
        return ["return exactly one line"]
    old_nums, new_nums = set(_NUM.findall(old)), set(_NUM.findall(new))
    dropped = {n for n in old_nums - new_nums
               if not any(re.search(rf"\b{w}\b", new, re.I) for w in _NUM_WORDS.get(n, ()))}
    if dropped:
        problems.append(f"it dropped the number(s) {', '.join(sorted(dropped))}")
    if new_nums - old_nums:
        problems.append(f"it added the number(s) {', '.join(sorted(new_nums - old_nums))}")
    # Tools and acronyms must survive: "Automated workflows in ANSYS CAD design" lost
    # "ANSYS" and passed every other check.
    swapped_own = {w.lower() for own, _ad in pairs for w in own.split()}
    # "C++/Python" is two tools: "Python and C++" keeps both
    lost = [w for w in re.findall(r"\b[A-Z][A-Za-z0-9+/-]*[A-Z0-9][A-Za-z0-9+/-]*\b", old)
            if any(part and part.lower() not in new.lower() for part in w.split("/"))
            and w.lower() not in swapped_own]
    if lost:
        problems.append(f"it dropped {', '.join(lost)}")
    # Actions must survive too: "Wrote and debugged C++/Python code" became "Wrote
    # C++/Python code" (another form of the verb, "debugging", is fine).
    new_stems = {_stem6(w) for w in _WORD.findall(new)} | {w.lower() for w in re.findall(r"\w+", new)}
    verbs = [w for w in re.findall(r"[A-Za-z]+", old)
             if (w.lower() in _VERBS or w.lower() in _IRREGULAR or re.fullmatch(r"[a-z]{3,}ed", w.lower()))
             and _stem6(w) not in new_stems and w.lower() not in new_stems
             and w.lower() not in swapped_own]
    if verbs:
        problems.append(f"it dropped what was done ({', '.join(verbs)})")
    swapped = {own.lower() for own, _ad in pairs}
    missing = [ad for _own, ad in pairs if ad.lower() not in new.lower()]
    missing += [k for k in keep if k.lower() not in swapped and k.lower() not in new.lower()]
    if missing:
        problems.append("it did not use: " + ", ".join(f'"{m}"' for m in missing))
    skip = {w.lower() for pair in pairs for w in pair}
    old_s, new_s = _stems(old, skip), _stems(new, skip)
    if old_s and len(old_s & new_s) < 0.6 * len(old_s):
        problems.append("it changed what the bullet describes")
    if _INFLATE.search(new) and not _INFLATE.search(old):
        problems.append(f'it overstated the role ("{_INFLATE.search(new).group(0)}")')
    # "Contributed to X" must not become "Developed X"; "Automated X" must not
    # become "Improved automated X" (claims a before/after that isn't stated).
    modest = _MODEST.search(old)
    if modest and not re.search(rf"\b{modest.group(1)[:6]}", new, re.I):
        problems.append(f'it dropped "{modest.group(0)}", overstating the contribution')
    upgrade = _UPGRADE.search(new)
    if upgrade and not _UPGRADE.search(old):
        problems.append(f'it claimed an improvement ("{upgrade.group(0)}") the original does not state')
    # A CV bullet opens with what was done: "UAV Prepared ..." (keyword glued on) and
    # "Simulation and ANSYS ... reduced ..." (noun first) were produced by Qwen3.
    first = (new.split() or [""])[0].strip(",.;:").lower()
    if first and not (first in _VERBS or first.endswith(("ed", "ing")) or first in _IRREGULAR):
        problems.append(f'it does not start with a verb ("{first}")')
    # A word the edit repeats more often than the original: "Wrote Python code,
    # debugging C++/Python code" and "Developed embedded software for avionics and
    # embedded hardware" read as broken English on a CV.
    count = lambda s: Counter(w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9+#-]{2,}", s)
                              if w.lower() not in _STOP and w.lower() not in ("and", "the", "for", "with"))
    before, after = count(old), count(new)
    repeated = sorted(w for w, n in after.items() if n > 1 and n > before.get(w, 0))
    if repeated:
        problems.append(f"it repeats {', '.join(repeated)}")
    # CV buzzwords the original did not use ("Enhanced ...", "leveraging ...")
    added = sorted({m.group(0).lower() for m in AI_WORDS.finditer(new)}
                   - {m.group(0).lower() for m in AI_WORDS.finditer(old)})
    if added:
        problems.append(f"it added the buzzword(s) {', '.join(added)}")
    # "Embedded software developer: Developed ..." - a label glued to the front.
    if re.match(r"^[^.:;]{2,40}:\s", new) and not re.match(r"^[^.:;]{2,40}:\s", old):
        problems.append("it put a label before the bullet")
    # At most one new idea: content stems in the edit that the original lacks.
    extra = new_s - old_s - _stems(" ".join(keep))
    if len(extra) > 1:
        problems.append("it added content that is not in the original")
    added = sorted(t for t in foreign
                   if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", new, re.I)
                   and not re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", old, re.I)
                   and not any(t.lower() in ad.lower() for _o, ad in pairs))
    if added:
        problems.append(f"it added job-ad terms the candidate lacks: {', '.join(added)}")
    if len(new.split()) > 40:
        problems.append(f"it is too long ({len(new.split())} words)")
    if len(new.split()) >= 12 and text_language(new) != lang:
        problems.append("it is in the wrong language")
    return problems


# ----------------------------------------------------------- letter checks

def letter_problems(text: str) -> list[str]:
    paras = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    words = len((text or "").split())
    out = []
    # 150, not 200: after unsupported sentences are deleted a letter may be short,
    # and a short true letter is better than a padded one.
    if words < 150 or len(paras) < 3:
        out.append(f"cover letter has {len(paras)} paragraph(s) and {words} words "
                   "(needs 3-4 paragraphs, 150-350 words)")
    elif words > 400:
        out.append(f"cover letter has {words} words (keep under 350)")
    m = _CLAIM.search(text or "")
    if m:
        out.append(f'cover letter claims a connection with the employer ("{m.group(0)}")')
    return out


# ------------------------------------------------ writing style / predictability
# Signs of AI-written text (after Dr Kriukow's "write like a human" prompt, adapted
# to application letters): stock vocabulary, "not X but Y", lists of three, many
# "..., doing" clauses, sweeping openings, summary endings, contractions, and
# sentences and paragraphs that all have the same length.

AI_WORDS = re.compile(
    r"\b(enhanc\w*|boost\w*|elevat\w*|ensur\w*|crucial|leverag\w*|delv\w*|foster\w*|pivotal|"
    r"seamless\w*|showcas\w*|underscor\w*|robust|cutting-edge|dynamic|passionate|passion|"
    r"thrilled|journey|testament|tapestry|realm|landscape|synerg\w*|spearhead\w*|"
    r"navigat\w* the|in today's|ever-evolving|game-chang\w*|unwavering|meticulous\w*|"
    r"aligns? perfectly|perfectly aligns?|resonat\w*|invaluable|holistic|strong fit|"
    r"solid foundation|strong foundation|hands-on exposure|make me a strong)\b", re.I)
_NOT_BUT = re.compile(r"\b(not (just|only|merely|about)\b[^.]*\bbut\b|rather than|it is not about|"
                      r"it's not about|more than just)\b", re.I)
_BEYOND = re.compile(r"\bextends? beyond\b", re.I)
_SWEEPING = re.compile(r"^\s*(in today's|in an era|in the (modern|current|fast)|as technology|"
                       r"\w+(\s\w+)? (has|have) long been|throughout history|now more than ever)", re.I)
_SUMMARY_END = re.compile(r"^\s*(overall|in summary|in short|ultimately|to sum up|in conclusion|"
                          r"this (demonstrates|shows|highlights|reflects|underscores))\b", re.I)
_CONTRACTION = re.compile(r"\b\w+(n't|'re|'ve|'ll|'m|'d)\b|\b(it's|that's|there's|what's|here's|"
                          r"let's|i'm)\b", re.I)
_PARTICIPLE = re.compile(r",\s+(?!including\b|according\b)[a-z]+ing\b")
# A list: "A, B and C" / "A, B, and C", items of 1-3 words. Counted as a list of
# three only with exactly three items ("UART, I2C, SPI and MAVLink" has four).
_LIST = re.compile(r"(?<![\w,] )((?:[\w/+&.-]+(?: [\w/+&.-]+){0,2}, )+)"
                   r"[\w/+&.-]+(?: [\w/+&.-]+){0,2},? and [\w/+&.-]+")


def _triads(text: str) -> int:
    """Lists of exactly three items (items = ", " before the last two + 2)."""
    return sum(m.group(1).count(", ") == 1 for m in _LIST.finditer(text or ""))
_SENT = re.compile(r"(?<=[.!?])\s+")


def _cv(values: list) -> float:
    """Coefficient of variation: 0 = all the same."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return (var ** 0.5) / mean if mean else 0.0


def style_problems(text: str) -> list[str]:
    """Readable notes on what makes the text sound machine-written."""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    sentences = [s for s in _SENT.split(text) if s.strip()]
    words = max(1, len(text.split()))
    out = []
    ai = sorted({m.group(0).lower() for m in AI_WORDS.finditer(text)})
    if ai:
        out.append("stock AI words: " + ", ".join(ai))
    if _NOT_BUT.search(text):
        out.append(f'says what something is not ("{_NOT_BUT.search(text).group(0)}")')
    if _BEYOND.search(text):
        out.append('uses "extends beyond"')
    if sentences and _SWEEPING.search(sentences[0]):
        out.append("opens with a sweeping general statement")
    ends = [p for p in paras if _SUMMARY_END.search(_SENT.split(p.strip())[-1])]
    if ends:
        out.append(f"{len(ends)} paragraph(s) end with a summary sentence")
    if _CONTRACTION.search(text):
        out.append(f'uses a contraction ("{_CONTRACTION.search(text).group(0)}")')
    parts = len(_PARTICIPLE.findall(text))
    if parts > max(1, words // 500):
        out.append(f'{parts} ", doing ..." clauses (at most one per 500 words)')
    triads = _triads(text)
    if triads > 1:
        out.append(f"{triads} lists of three")
    starts = [s.split()[0].lower().strip(",") for s in sentences if s.split()]
    repeats = sum(a == b for a, b in zip(starts, starts[1:]))
    if repeats:
        out.append(f"{repeats} sentence(s) start with the same word as the one before")
    if len(sentences) >= 5 and _cv([len(s.split()) for s in sentences]) < 0.3:
        out.append("sentences are all about the same length")
    if len(paras) >= 3 and _cv([len(p.split()) for p in paras]) < 0.15:
        out.append("paragraphs are all about the same length")
    return out


def predictability(text: str, sentence_logprobs: dict = None) -> dict:
    """How predictable (machine-like) the text reads, 0-100, higher = more AI-like.

    An estimate, not a detector: rhythm (sentence-length variation, the
    "burstiness" AI detectors measure), repeated sentence openings, stock AI
    words, and formula patterns. When the model's own token log-probabilities are
    known for a sentence (LM Studio returns them for text it generates), their
    perplexity is included too: low perplexity = each word was the obvious
    choice."""
    text = (text or "").strip()
    sentences = [s for s in _SENT.split(text) if s.strip()]
    words = max(1, len(text.split()))
    parts, notes = {}, []
    cv = _cv([len(s.split()) for s in sentences])
    parts["rhythm"] = round(max(0.0, min(1.0, (0.5 - cv) / 0.3)) * 30)
    starts = [s.split()[0].lower() for s in sentences if s.split()]
    same = sum(a == b for a, b in zip(starts, starts[1:])) / max(1, len(starts) - 1)
    parts["openings"] = round(min(1.0, same * 2) * 10)
    ai_rate = len(AI_WORDS.findall(text)) * 100 / words
    parts["stock words"] = round(min(1.0, ai_rate / 2) * 25)
    formula = (_triads(text) + len(_PARTICIPLE.findall(text)) * 0.5
               + bool(_NOT_BUT.search(text)) + bool(_BEYOND.search(text))
               + bool(sentences and _SWEEPING.search(sentences[0])))
    parts["formula"] = round(min(1.0, formula / 4) * 15)
    ppl = None
    if sentence_logprobs:
        scored = [(s, lp) for s, lp in sentence_logprobs.items() if lp]
        if scored:
            mean_lp = sum(sum(lp) for _s, lp in scored) / sum(len(lp) for _s, lp in scored)
            ppl = round(2.718281828 ** (-mean_lp), 2)
            # most sampled text sits between 1.5 (very predictable) and 6
            parts["perplexity"] = round(max(0.0, min(1.0, (4.0 - ppl) / 2.5)) * 20)
            lows = sorted(scored, key=lambda x: -sum(x[1]) / len(x[1]))[:2]
            notes = [f'most predictable: "{s[:70]}..." (perplexity '
                     f'{2.718281828 ** (-sum(lp) / len(lp)):.2f})' for s, lp in lows]
    total = sum(parts.values())
    scale = 100 / (100 if ppl is not None else 80)
    score = round(total * scale)
    return {"score": score, "label": "low" if score < 30 else "medium" if score < 55 else "high",
            "parts": parts, "perplexity": ppl, "notes": notes,
            "sentence_length_variation": round(cv, 2)}


def recruiter_problems(text: str) -> list[str]:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s.strip()]
    out = []
    if len(sentences) != 3:
        out.append(f"recruiter message has {len(sentences)} sentence(s) (needs exactly 3)")
    elif not sentences[2].rstrip().endswith("?"):
        out.append("recruiter message does not end with a question")
    if re.match(r"\s*(dear|hello|hi|sehr geehrte)\b", text or "", re.I):
        out.append("recruiter message starts with a greeting")
    m = _CLAIM.search(text or "")
    if m:
        out.append(f'recruiter message claims a connection with the employer ("{m.group(0)}")')
    if sentences and not re.search(r"\b(i|i'm|i am|my)\b", sentences[0], re.I):
        out.append("recruiter message intro is not in the first person")
    if sentences and _WRONG_ASK.search(sentences[-1]):
        out.append("recruiter message asks the recipient a question meant for the candidate")
    return out


# The recipient is asked about THEIR preferences or plans to apply: the model copied
# the ad's "please indicate whether you would prefer MI or ARR" as its question.
_WRONG_ASK = re.compile(r"\b(would you (prefer|like to (start|join|apply))|do you (prefer|want to "
                        r"(start|join|apply))|are you (interested in|planning to|considering) "
                        r"(starting|joining|applying|the position)|which (group|position|team) "
                        r"would you)\b", re.I)
_SAFE_QUESTION = {"en": "Could you tell me more about the research topics planned for this position?",
                  "de": "Könnten Sie mir mehr über die geplanten Themen dieser Stelle erzählen?"}


def fix_recruiter_question(text: str) -> str:
    """Replace a question aimed the wrong way with a neutral one about the role."""
    parts = [x for x in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if x.strip()]
    if parts and _WRONG_ASK.search(parts[-1]):
        parts[-1] = _SAFE_QUESTION[text_language(text)]
        return " ".join(parts)
    return text


_LEARNING = re.compile(r"\b(learn\w*|eager|keen|looking forward|interested in|curious|"
                       r"new to|to gain|to deepen|to build (up|on)|would like to|excited to|"
                       r"lernen|einarbeiten|kennenlernen)\b", re.I)


# First-person phrases that present what follows as the candidate's own.
_OWN = re.compile(
    r"\b(my (experience|expertise|background|skills?|knowledge|work|proficiency|projects?|"
    r"coursework|studies|research)|i (have|'ve|had|built|developed|worked|used|implemented|"
    r"designed|gained|applied|integrated|created|programmed|tested)|i am (experienced|"
    r"proficient|skilled|familiar)|experience (with|in)|proficient (in|with)|skilled in|"
    r"expertise in|familiar with|hands-on|knowledge of)\b", re.I)
_CLAIM_WINDOW = 10      # words after the phrase that count as "claimed"
_JOB_LINK = re.compile(r"\b(which|that|align\w*|relevant|relates?|match\w*|fits?|suits?|"
                       r"for (this|the|your)|to (this|the|your)|in (this|the|your)|"
                       r"of (this|the|your)|such as the)\b", re.I)


def gap_claims(text: str, forbidden) -> list:
    """Human-readable version of claimed_gaps()."""
    return [f"claims a skill that is not in your profile ({why}): \"{sent[:90]}\""
            for sent, why in claimed_gaps(text, forbidden)]


def claimed_gaps(text: str, forbidden) -> list:
    """Sentences that present an unproven skill as the candidate's own.

    A forbidden term counts as claimed when it follows a first-person phrase
    ("My experience with ROS, LiDAR ...", "I have used PVD ...") within a few
    words. Describing the job ("aligns with developing a robust mapping
    framework"), wanting to learn it, and questions are fine.
    """
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        if _LEARNING.search(sentence) or sentence.rstrip().endswith("?"):
            continue
        hits = set()
        for m in _OWN.finditer(sentence):
            window = " ".join(sentence[m.end():].split()[:_CLAIM_WINDOW])
            # "..., which aligns with developing a mapping framework" describes the
            # job, not the candidate: the claim ends where the link to the job starts.
            window = _JOB_LINK.split(window, maxsplit=1)[0]
            hits |= {t for t in forbidden
                     if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", window, re.I)}
        if hits:
            out.append((sentence.strip(), ", ".join(sorted(hits))))
    return out


# ------------------------------------------------------------ attribution check

_ORG_NOISE = set("""robotics engineers engineer engineering gmbh club team study case institute
university national technische hochschule sciences technologies technology school college lab""".split())
# Words too common across jobs to prove which organisation a sentence is about.
_COMMON_WORK = set("""software hardware systems system design development documentation
performance communication electrical engineer engineering different across country national positions
heavy ground create future teams domains problem processes technologies interface
interfaces variables maintaining identify""".split())


def _signature(text: str) -> set:
    """Distinctive nouns of a piece of work: 6+ letters, not verbs or common words."""
    return {w.lower() for w in _WORD.findall(text) if len(w) >= 6} \
        - _STOP - _VERBS - _COMMON_WORK - {w for w in _WORD.findall(text.lower())
                                           if w.endswith(("ed", "ing"))}


def _org_tokens(org: str) -> set:
    toks = {w.lower() for w in _WORD.findall(org)} - _STOP - _ORG_NOISE
    toks |= {a.lower() for a in re.findall(r"\b[A-Z]{3,}\b", org)}     # GIK, FAST, SOCO
    return toks


def _org_groups(profile: dict) -> list:
    """[(org tokens, words of that organisation's own work)] - one group per
    organisation (a job and a degree at the same school share a group)."""
    raw = []
    for e in profile.get("experience") or []:
        raw.append((e.get("employer", ""), " ".join([e.get("title", "")] + (e.get("bullets") or []))))
    for p in profile.get("projects") or []:
        raw.append((p.get("context", ""), " ".join([p.get("name", "")] + (p.get("bullets") or []))))
    for ed in profile.get("education") or []:
        raw.append((ed.get("institution", ""), " ".join((ed.get("coursework") or []) +
                                                       [ed.get("thesis", "")])))
    groups = []
    for org, work in raw:
        toks = _org_tokens(org)
        if not toks:
            continue
        # the organisation's own name counts as its work ("Robotics Lab" -> robotics)
        words = _signature(work) | ({w.lower() for w in _WORD.findall(org)} - _COMMON_WORK - _STOP)
        for g in groups:
            if g[0] & toks:
                g[0].update(toks)
                g[1].update(words)
                break
        else:
            groups.append((set(toks), set(words)))
    # keep only the words that belong to exactly one organisation (compared against
    # the original sets, so the order of removal cannot matter)
    original = [set(w) for _t, w in groups]
    for i, (_t, words) in enumerate(groups):
        others = set().union(*(w for j, w in enumerate(original) if j != i))
        words.difference_update(others)
    return groups


def misattributions(text: str, profile: dict) -> list:
    """Sentences that name one organisation but describe another's work, e.g.
    'At TH Deggendorf I built a UAV for traffic observation' (that project was
    at Pakistan Ordnance Factories)."""
    groups = _org_groups(profile)
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        words = {w.lower() for w in _WORD.findall(sentence)}
        words |= {a.lower() for a in re.findall(r"\b[A-Z]{3,}\b", sentence)}
        named = [i for i, (toks, _w) in enumerate(groups) if toks & words]
        if not named:
            continue
        for j, (toks, own) in enumerate(groups):
            if j in named:
                continue
            borrowed = sorted(words & own)
            if borrowed:
                out.append((sentence.strip(), f"describes work from {' '.join(sorted(toks))} "
                                              f"({', '.join(borrowed[:3])}) under another organisation"))
                break
    return out


# "coursework in X", or "my master's program gave me a foundation in X"
_TAUGHT = re.compile(r"\b(coursework|courses?|modules?|lectures?)\b|\b(program(me)?|studies|degree)\b"
                     r".{0,60}\b(foundation|taught|covered|trained|training|provided)\b", re.I)


def _names_interest(interest: str, sentence: str) -> bool:
    """The sentence names the interest, also in part: "Motion Planning" names
    "Motion and Trajectory Planning" (the exact-name match let "my coursework in
    Autonomous Systems and Motion Planning" through)."""
    words = [w for w in re.findall(r"[A-Za-z0-9+-]+", interest)
             if len(w) >= 3 and w.lower() != "and"]
    said = [w for w in words if re.search(rf"\b{re.escape(w)}\b", sentence, re.I)]
    return len(said) >= min(2, len(words))


def coursework_mixups(text: str, profile: dict) -> list:
    """Sentences that call a research interest 'coursework' (e.g. "my coursework
    in SLAM" when SLAM is only listed as a research interest)."""
    only_interest = set()
    for ed in profile.get("education") or []:
        courses = " ".join(ed.get("coursework") or []).lower()
        only_interest |= {r for r in ed.get("research_interests") or [] if r.lower() not in courses}
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        taught = _TAUGHT.search(sentence)
        if not taught or re.search(r"\binterests?\b", sentence, re.I):
            continue
        hits = [r for r in only_interest if _names_interest(r, sentence)]
        if hits:
            out.append((sentence.strip(), f"lists {', '.join(hits)} as coursework; "
                                          "in your profile it is a research interest"))
    return out


# Words that describe any work and prove nothing by themselves.
_PLAIN = set("""systems system skills skill projects project tools tool experience exposure
foundation background coding ability abilities knowledge understanding field fields areas area
technical practical hands-on insight insights challenges challenge environment environments
solutions solution tasks task methods approach approaches complex various different multiple
teams team roles role studies program programme master's bachelor's degree
involved involving involves include includes included including allowed enabled provided
gave given focused focusing helped required requires gained learned learnt worked
including alongside during toward towards capacity""".split())
# Evaluative and connecting words: they judge or link a fact but add no skill, tool
# or result. Flagging them deleted true sentences over "further", "solid",
# "supports", "enthusiasm", "listed" (a qwen3-8b letter went from 3 paragraphs to 59
# words). Words ending in -ly are skipped for the same reason.
_SOFT = set("""further furthermore additionally solidified solidify solid strong confident
confidence enthusiasm enthusiastic passion passionate innovative dynamic meaningful
valuable effective successful professional academic applied applying focus focuses
familiar listed support supports supported supporting prepared equipped needed required
essential robust ready make makes making believe allow allows will yours ours ongoing
efforts responsibility responsibilities contributions contribute contributing
independently independent collaborative combined""".split())
_IMPACT = re.compile(r"\b(enhanc|improv|increas|boost|optimi[sz]|streamlin|strengthen)\w*\s+"
                     r"((?:the |its |their |our |my |your |a |an |this |system |overall )*)"
                     r"([a-z][\w-]+)", re.I)
# "... equipped me with the skills to develop X", "... needed for X", "a key part of
# your team": what follows describes the JOB, not the candidate's past work.
_TO_JOB = re.compile(r"\b((skills?|abilit(y|ies)|prepared|equipped( me)?|ready|allows?( me)?) "
                     # "needed for X" links to the job; "which required understanding
                     # of mechanical-electrical integration" is a claim and stays checked
                     r"(to|for)|(needed|required) (for|by|in)\b|essential|key|central to|yours|"
                     r"(strong|good) (fit|candidate)|this (role|position))\b", re.I)
# "This project involved developing navigation algorithms" describes the
# candidate's own work without "I" or "my" and slipped past every check.
_OWN_WIDE = re.compile(_OWN.pattern[:-3] + r"|exposure to|equipped me with|insight into|"
                       r"(this|that|the) (project|role|work|position|internship|job) "
                       r"(involved|included|required|consisted of|focused on)|"
                       # "This involved working with Linux environments" (no noun)
                       r"(this|that|it|these|which) (also )?(involved|included|required|meant))\b",
                       re.I)


def _profile_stems(profile: dict) -> set:
    return {_stem6(w) for w in _WORD.findall(json.dumps(profile, ensure_ascii=False))}


def _stem6(word: str) -> str:
    """First 6 letters of the singular: 'models' and 'model' must match
    (a true sentence about 'CAD models' was deleted because the profile says 'model')."""
    w = word.lower()
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w[:6]


def invented_details(text: str, profile: dict) -> list:
    """(sentence, why) for sentences that attach something to the candidate's own
    work that the profile never mentions: 'exposure to ... real-time data
    processing', 'test cases that enhanced system reliability'."""
    stems = _profile_stems(profile)
    out = []
    for original in re.split(r"(?<=[.!?])\s+", text or ""):
        sentence = _aliases(original)
        if _LEARNING.search(sentence) or sentence.rstrip().endswith("?"):
            continue
        unknown = set()
        for m in _OWN_WIDE.finditer(sentence):
            # "I do not have experience with intralogistics" denies, it does not claim.
            if _DENIAL.search(" ".join(sentence[:m.start()].split()[-4:])):
                continue
            window = _JOB_LINK.split(" ".join(sentence[m.end():].split()[:_CLAIM_WINDOW]), 1)[0]
            window = _TO_JOB.split(window, maxsplit=1)[0]
            window = _SELF_ASSESS.split(window, maxsplit=1)[0]
            unknown |= {w.lower() for w in _WORD.findall(window)
                        if len(w) >= 5 and _stem6(w) not in stems
                        and w.lower() not in _STOP and w.lower() not in _VERBS
                        and w.lower() not in _PLAIN and w.lower() not in _SOFT
                        and not w.lower().endswith("ly")}
        for m in _IMPACT.finditer(sentence):
            obj = m.group(3).lower()
            if _stem6(obj) not in stems and obj not in _PLAIN and obj not in _SOFT:
                unknown.add(f"{m.group(1)}… {obj}")
        if unknown:
            # the original sentence: the caller finds it in the letter by exact text
            out.append((original.strip(), "adds " + ", ".join(sorted(unknown))
                        + " - not in your profile"))
    return out


# ", demonstrating my ability to translate theory into practice" judges the fact
# before it; only the fact is checked (the judgement deleted a true manipulator sentence).
_SELF_ASSESS = re.compile(r",?\s*\b(demonstrat\w*|showing|showcasing|highlighting|reflecting|"
                          r"proving|underscoring|illustrating|which (shows|demonstrates|highlights|"
                          r"reflects|proves))\b", re.I)
_FILLER = set("""into onto from with that this than then them they were been their your
have has had like along well also such both many much more most very
strong solid good great deep extensive broad wide ability able""".split())
# Stronger than _JOB_LINK: a bare "that" often continues the candidate's own claim
# ("systems that require real-time data processing"), so it must not end it here.
_JOB_LINK_STRONG = re.compile(
    r"\b(which (is|are|was|align\w*|make\w*|relate\w*|mirror\w*)|align\w*|relevant|essential|"
    r"critical|key to|directly applicable|applicable to|mirrors?|for (this|the|your) "
    r"(role|position|project|team|thesis|job)|to (this|the|your) (role|position|project|team))\b", re.I)


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_SPELLED_DOF = re.compile(r"\b(one|two|three|four|five|six|seven|\d)[- ]degrees?[- ]of[- ]freedom\b",
                          re.I)


def _aliases(text: str) -> str:
    """Spellings the profile writes differently: 'four-degree-of-freedom' is the
    profile's '4-DOF' (a true manipulator sentence was deleted as 'adds
    four-degree-of-freedom', Ubica 2026-10-03)."""
    return _SPELLED_DOF.sub(lambda m: f"{_NUMBER_WORDS.get(m.group(1).lower(), m.group(1))}-DOF",
                            text or "")


def _content_stems(text: str) -> set:
    text = _aliases(text)
    return {_stem6(w) for w in _WORD.findall(text)
            if w.lower() not in _STOP and w.lower() not in _VERBS and w.lower() not in _PLAIN
            and w.lower() not in _FILLER and w.lower() not in _SOFT
            and not w.lower().endswith("ly") and len(w) >= 4}


def ungrounded_claims(text: str, fact_lines: list, min_cover: float = 0.6) -> list:
    """(sentence, why) for first-person experience claims that no one or two
    profile lines back up. Real words can be strung into false statements
    ('my work on SLAM and motion planning projects' when SLAM is only a research
    interest), which word-level checks miss; this needs most of the claim's
    specific words to come from actual profile lines."""
    lines = [_content_stems(l) for l in fact_lines]
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        m = _OWN_WIDE.search(sentence)
        if not m or _LEARNING.search(sentence) or sentence.rstrip().endswith("?"):
            continue
        claim = _JOB_LINK_STRONG.split(sentence[m.start():], maxsplit=1)[0]
        claim = _TO_JOB.split(claim, maxsplit=1)[0]
        claim = _SELF_ASSESS.split(claim, maxsplit=1)[0]
        stems = _content_stems(claim)
        if len(stems) < 3:
            continue
        best = 0.0
        for i, a in enumerate(lines):
            for b in lines[i:]:
                best = max(best, len(stems & (a | b)) / len(stems))
        if best < min_cover:
            missing = sorted(stems - set().union(*lines)) if lines else sorted(stems)
            out.append((sentence.strip(), "no profile line backs this claim"
                                          + (f" ({', '.join(missing[:4])})" if missing else "")))
    return out


_DENIAL = re.compile(r"\b(do not|don't|does not|doesn't|have not|haven't|lack\w*|no (prior |direct )?"
                     r"(experience|knowledge|background)|not (yet |currently )?(have|familiar|experienced)|"
                     r"without (any )?(experience|knowledge))\b", re.I)


_CLAUSE_END = re.compile(r",\s*(?=(i|but|however|so|yet|which|while|although)\b)|"
                         r"\b(but|however|although|though|whereas)\b|[;:]", re.I)


def false_denials(text: str, profile: dict) -> list:
    """(sentence, why) for sentences that deny a skill the profile HAS, e.g.
    'I do not currently have programming knowledge' for a Python/C++ candidate."""
    scorer = FitScorer(profile)
    categories = [c.lower() for c in (profile.get("skills") or {})]
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        m = _DENIAL.search(sentence)
        if not m:
            continue
        # Only the denied clause: in "While I do not have intralogistics experience,
        # I am keen to learn how autonomous systems ..." the second clause was read
        # as denying autonomous systems.
        tail = _CLAUSE_END.split(sentence[m.end():], maxsplit=1)[0]
        denied =[k for k, pats, _w in scorer.terms if any(p.search(tail) for p in pats)]
        denied += [c for c in categories
                   if any(re.search(rf"\b{re.escape(w[:6])}", tail, re.I)
                          for w in _WORD.findall(c) if len(w) >= 6)]
        if denied:
            out.append((sentence.strip(), f"denies {', '.join(sorted(set(denied))[:3])}, "
                                          "which IS in your profile"))
    return out


# ------------------------------------------------ interest / detail / inflation

# Wanting to learn something is not a claim. Narrower than _LEARNING, whose bare
# "learn\w*" also exempted "perception and learning for HRI" sentences.
_WANTS = re.compile(r"\b(eager|keen|looking forward|would like to|excited to|hope to|want to|"
                    r"to (learn|gain|deepen|explore)|interested in (learning|gaining|exploring))\b", re.I)

_EXPERTISE = re.compile(r"\b(expertise|expert|experience[ds]?|proficien\w*|skilled|background|"
                        r"track record|hands-on|strong (foundation|knowledge)|worked (on|with)|"
                        r"my work (on|in|with)|i (have|'ve) (built|developed|worked|used|applied)|"
                        # "where I focus on autonomous systems and trajectory planning"
                        r"focus(es|ed|ing)? on|speciali[sz]\w* in|concentrat\w* on|"
                        # "where I work on topics like SLAM" (present tense)
                        r"i (work|am working|currently work) on)\b",
                        re.I)


def _claim_part(sentence: str) -> str:
    """The sentence without its wanting-to-learn clauses: in "While I am keen to
    learn more about pHRI, I believe my current expertise in perception ..."
    only the second clause is a claim (the whole sentence used to be exempt)."""
    return ", ".join(c for c in re.split(r",\s*", sentence) if not _WANTS.search(c))


_UNFINISHED = re.compile(r"\b(unfinished|incomplete|not completed?|did not (complete|finish)|"
                         r"didn't (complete|finish)|discontinued|partial(ly)?|left)\b", re.I)


# The letter is the candidate speaking. A 3B draft copied the job ad into it:
# "As a Working Student ..., you will support our predevelopment team", "You are
# studying mechanical engineering ...".
_READER = re.compile(r"^\s*you\b|\byou(?:'re|'ll)?\s+(?:will|are|have|had|work|bring|ask|enjoy|"
                     r"can|should|must|take|need|gain|get|join|support|study|studying)\b|"
                     r"\b(?:our|we|us)\b", re.I)
_READER_OK = re.compile(r"\b(thank you|your (team|company|group|lab|work|projects?|"
                        r"organisation|organization))\b", re.I)


# The model talking about its instructions: "The three requirements that I think are
# most relevant to me are ..." (Llama 3.2 3B echoed the prompt's structure).
_PROMPT_ECHO = re.compile(r"\b(the|these|those)\s+(\w+\s+)?requirements?\s+(that|which)\s+i\b|"
                          r"\bmost relevant to me\b|\bnumbered requirements?\b|\bproof line\b|"
                          r"\b(listed only|work done)\b", re.I)


def reader_or_ad_copy(text: str, ad: str) -> list:
    """(sentence, why) for sentences that address the reader, speak as the company,
    or repeat a sentence of the job ad instead of saying something about the
    candidate."""
    ad_sents = [_content_stems(s) for s in re.split(r"(?<=[.!?:])\s+|\n+", ad or "")]
    ad_sents = [s for s in ad_sents if len(s) >= 4]
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        if not sentence.strip() or sentence.rstrip().endswith("?"):
            continue
        if _READER.search(_READER_OK.sub("", sentence)):
            out.append((sentence.strip(), "addresses the reader or speaks as the company "
                                          "(copied from the job ad)"))
            continue
        if _PROMPT_ECHO.search(sentence):
            out.append((sentence.strip(), "talks about the instructions or the requirement "
                                          "list instead of the candidate"))
            continue
        own = _content_stems(sentence)
        first_person = re.search(r"\b(i|my|me)\b", sentence, re.I)
        if len(own) >= 4 and not first_person and any(len(own & a) >= 0.7 * len(own)
                                                       for a in ad_sents):
            out.append((sentence.strip(), "repeats the job ad instead of saying something "
                                          "about you"))
    return out


_LEARN_OBJ = re.compile(r"\b(?:keen|eager|excited|looking forward|want|hope|like)\s+to\s+"
                        # not "deepen": that says the knowledge is already there
                        r"(?:learn|explore|gain experience (?:in|with)|study|pick up)"
                        r"(?:\s+more)?(?:\s+about)?\s+([^.;:?!]+)", re.I)


def learning_known(text: str, profile: dict) -> list:
    """(sentence, why) for "keen to learn X" when X is already in the profile: a
    letter said "I am keen to learn ROS2" one sentence before the ROS2 package
    the candidate built. Only technical terms count (skill vocabulary, tool names,
    acronyms), so "keen to learn about your team" is fine."""
    blob = _profile_blob(profile)
    scorer = FitScorer(profile)
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        m = _LEARN_OBJ.search(sentence)
        if not m:
            continue
        # only the thing to learn: in "learn about sensor fusion in autonomous systems"
        # it is sensor fusion - "autonomous systems" is context, not the object
        obj = re.split(r",\s*(?:which|as|because|while|and I)\b|\s+(?:at|in|with) your\b|"
                       r"\s+(?:in|within|for|as part of|through|during)\s+", m.group(1), maxsplit=1)[0]
        known = {key for key, pats, _w in scorer.terms
                 if any(p.search(obj) for p in pats) and any(p.search(blob) for p in pats)}
        known |= {w for w in re.findall(r"\b[A-Za-z]*[A-Z][A-Za-z]*\d*[A-Z0-9][A-Za-z0-9]*\b", obj)
                  if len(w) >= 3 and w.lower() in blob}
        # one entry per term, keeping the capitalised spelling ("ROS2", not "ros2")
        known = sorted({k.lower(): k for k in sorted(known, reverse=True)}.values(), key=str.lower)
        if known:
            out.append((sentence.strip(), f"says you want to learn {', '.join(known)}, "
                                          "which your profile already shows"))
    return out


def unfinished_degrees(text: str, profile: dict) -> list:
    """(sentence, why) for a sentence naming a degree the profile marks unfinished
    without saying so: "My education includes an M.Sc in Integrated Chip Design"
    reads as a completed degree."""
    degrees = []
    for e in profile.get("education") or []:
        label = f"{e.get('degree', '')} {e.get('status', '')}"
        if _UNFINISHED.search(label):
            # "M.Sc Integrated Chip Design (unfinished)" -> "Integrated Chip Design"
            field = re.sub(r"\(.*?\)", "", e.get("degree", ""))
            field = re.sub(r"^\s*(b|m)\.?\s?(sc|eng|a|s)\.?\s*(in\s+)?|^\s*(bachelor|master)'?s?\s*(of|in)?\s*",
                           "", field, flags=re.I).strip()
            if field:
                degrees.append((e.get("degree", ""), field, e.get("coursework") or []))
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        if _UNFINISHED.search(sentence):
            continue
        for degree, field, courses in degrees:
            if re.search(re.escape(field), sentence, re.I):
                out.append((sentence.strip(), f"names the {field} degree without saying it is "
                                              "unfinished"))
                break
            # "My coursework included Analog and Discrete Electronics, Digital
            # Integrated Circuits ..." - that degree's courses, degree unnamed
            named = [c for c in courses if re.search(re.escape(c), sentence, re.I)]
            if len(named) >= 2:
                out.append((sentence.strip(), f"lists courses from the unfinished {field} "
                                              "degree without saying it is unfinished"))
                break
    return out


def interest_claims(text: str, profile: dict) -> list:
    """(sentence, why) for sentences that present a research interest as expertise
    or experience: 'bridges my expertise in computer vision and motion planning'
    when both are only listed as research interests. An interest counts as backed
    only when the same words appear in a bullet, project, skill or course."""
    backed = json.dumps({k: v for k, v in profile.items() if k != "education"},
                        ensure_ascii=False).lower()
    backed += " " + " ".join(" ".join(e.get("coursework") or []) + " " + e.get("thesis", "")
                             for e in profile.get("education") or []).lower()
    interests = []
    for e in profile.get("education") or []:
        for r in e.get("research_interests") or []:
            # "Motion and Trajectory Planning" is also written "motion planning"
            forms = {r.lower()} | ({f"{r.split()[0]} {r.split()[-1]}".lower()}
                                   if len(r.split()) > 2 else set())
            if not any(f in backed for f in forms):
                interests.append((r, forms))
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        part = _claim_part(sentence)
        if re.search(r"\binterest", part, re.I) or sentence.rstrip().endswith("?"):
            continue
        m = _EXPERTISE.search(part)
        if not m:
            continue
        claim = part[m.start():]
        # also named in part: "trajectory planning" for "Motion and Trajectory Planning"
        hits = [r for r, forms in interests if any(re.search(rf"\b{re.escape(f)}\b", claim, re.I)
                                                    for f in forms) or _names_interest(r, claim)]
        if hits:
            out.append((sentence.strip(), f"presents {', '.join(hits)} as {m.group(1).lower()}; "
                                          "in your profile it is only a research interest"))
    return out


def _work_units(profile: dict) -> list:
    """[(label, text)] - each job, project and thesis as one piece of work."""
    units = [(f"{e.get('title')} @ {e.get('employer')}",
              " ".join([e.get("title", ""), e.get("employer", "")] + (e.get("bullets") or [])))
             for e in profile.get("experience") or []]
    units += [(f"project '{p.get('name')}'", " ".join([p.get("name", "")] + (p.get("bullets") or [])))
              for p in profile.get("projects") or []]
    units += [(f"thesis '{e['thesis']}'", e["thesis"]) for e in profile.get("education") or []
              if e.get("thesis")]
    return units


# "This project required ...", ", which involved ...": what follows is presented as
# part of what came before.
_PART_OF_NEXT = re.compile(r"^\s*(this|that|the|these|those) (project|work|role|position|"
                           r"manipulator|platform|system|task|experience)s? [^.]*?\b(required|"
                           r"involved|included|meant|needed|consisted of|combined)\b", re.I)
_PART_OF_INLINE = re.compile(r",?\s*\b(which|where it|that) (also )?(required|involved|included|"
                             r"meant|needed|consisted of)\b", re.I)


def merged_claims(text: str, profile: dict) -> list:
    """(sentence, why) for a sentence presenting one bullet as part of another:
    "I built a 4-DOF manipulator ... This project required coordination across
    different domains to create a robot platform" - the robot platform is a
    separate bullet, not part of the manipulator project."""
    bullets = [(e.get("title", ""), b) for e in profile.get("experience") or []
               for b in e.get("bullets") or []]
    bullets += [(p.get("name", ""), b) for p in profile.get("projects") or []
                for b in p.get("bullets") or []]
    stems = [_content_stems(b) for _l, b in bullets]

    def match(part: str):
        own = _content_stems(part)
        scored = [(len(own & s) / len(s), i) for i, s in enumerate(stems) if s]
        cov, i = max(scored, default=(0, -1))
        return i if cov >= 0.5 else None

    out, previous = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        m = _PART_OF_NEXT.search(sentence)
        if m:
            head, tail = previous, sentence[m.end():]
        else:
            m = _PART_OF_INLINE.search(sentence)
            head, tail = (sentence[:m.start()], sentence[m.end():]) if m else ("", "")
        a, b = (match(head), match(tail)) if m else (None, None)
        if a is not None and b is not None and a != b:
            out.append((sentence.strip(), f"presents \"{bullets[b][1][:60]}\" as part of "
                                          f"\"{bullets[a][1][:60]}\" - separate items in your profile"))
        previous = sentence
    return out


def mixed_details(text: str, profile: dict) -> list:
    """(sentence, why) for sentences about one piece of work that borrow a skill
    from another: 'an autonomous UAV for traffic observation, where I integrated
    object detection and path planning' - path planning was the competition
    robots, not the UAV. Skills listed only under 'skills' (in no bullet) are free."""
    units = _work_units(profile)
    sigs = [_signature(t) for _l, t in units]
    unique = [s - set().union(*(o for j, o in enumerate(sigs) if j != i)) for i, s in enumerate(sigs)]
    terms = FitScorer(profile).terms
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        # Clause by clause: "I coordinated ... a robot platform for path planning, and
        # I prepared ... documentation for UAV operations" states two separate facts;
        # read as one, path planning was "attached" to the UAV job.
        # A clause that alone fits no single piece of work ("I implemented path
        # planning for the UAV" - two UAV entries) is read with the sentence's work.
        whole, at = _borrowed(sentence, units, unique, terms)
        for clause in _CLAUSES.split(sentence):
            borrowed, a = _borrowed(clause, units, unique, terms)
            if a < 0 and at >= 0:
                borrowed, a = [b for b in whole if re.search(re.escape(b), clause, re.I)], at
            if borrowed:
                out.append((sentence.strip(), f"attaches {', '.join(borrowed)} to {units[a][0]}, "
                                              "but that comes from other work in your profile"))
                break
    return out


# Where one fact ends and the next begins: "and I", "while (I) ...", ", <verb>ing"
# ("..., contributing to UAV development"), "as well as", "and <verb>ed". A true
# sentence joining two bullets this way ("wrote C++/Python code ... while automating
# ANSYS CAD workflows") was deleted because it was read as one unmatched clause.
_CLAUSES = re.compile(r",?\s+and I\b|;\s*|,?\s+and also\b|,?\s+while\s+|,?\s+whilst\s+|"
                      r",\s+(?=[a-z]+ing\b)|,?\s+as well as\s+|,?\s+along with\s+|"
                      r",?\s+and\s+(?=[a-z]+ed\b)")


def _borrowed(clause: str, units: list, unique: list, terms: list) -> tuple:
    """(skills the clause borrows from other work, index of the work it is about)."""
    words = {w.lower() for w in _WORD.findall(clause)}
    hits = [len(words & u) for u in unique]
    best = max(hits, default=0)
    if best < 2 or hits.count(best) > 1:
        return [], -1                   # not clearly about one piece of work
    a = hits.index(best)
    others = [t for j, (_l, t) in enumerate(units) if j != a and hits[j] == 0]
    borrowed = []
    for key, pats, _w in terms:
        said = next((p for p in pats if p.search(clause)), None)
        if said and not any(p.search(units[a][1]) for p in pats) \
                and any(said.search(t) for t in others):
            borrowed.append(said.pattern.replace("(?<![a-z0-9])", "").replace("(?![a-z0-9])", "")
                            .replace("\\", ""))
    return borrowed, a


_INFLATE_WIDE = re.compile(r"\b(led|headed|spearheaded|directed|supervised|oversaw|"
                           r"cross-functional|expertise|expert|extensive|mastered)\b", re.I)


_PRESUPPOSED = re.compile(r"\b(?:deepen|further|expand|broaden|strengthen|grow|build on|"
                          r"build upon)\s+(?:my|existing|current|the)\s+(?:\w+\s+)?"
                          r"(expertise|mastery|proficiency)\b", re.I)


def count_inflation(text: str, profile: dict) -> list:
    """(sentence, why) for sentences that turn a counted-once result plural:
    "adding 1 new feature and test case" became "to add features and test cases".
    Only sentences clearly about that bullet (3+ of its content words) count."""
    out, singles = [], []
    for e in profile.get("experience") or []:
        for b in e.get("bullets") or []:
            for m in re.finditer(r"\b(?:1|one|a single)\s+(?:new\s+)?([a-z][a-z -]+?)(?=\s+(?:to|in|for|on)\b|[,.;])",
                                 b, re.I):
                nouns = [part.split()[-1] for part in re.split(r"\s+and\s+", m.group(1)) if part.split()]
                singles.append((b, m.group(0), nouns))
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        own = _content_stems(sentence)
        for bullet, said, nouns in singles:
            plural = [n for n in nouns if re.search(rf"\b{re.escape(n)}s\b", sentence, re.I)]
            if plural and len(own & _content_stems(bullet)) >= 3:
                out.append((sentence.strip(), f'makes "{said}" plural ({", ".join(n + "s" for n in plural)})'))
                break
    return out


def inflated_claims(text: str, profile: dict) -> list:
    """(sentence, why) for first-person sentences that upgrade the candidate's role
    with a word the profile never uses: 'I coordinated cross-functional teams'
    for 'Coordinating with teams across different domains'."""
    blob = _profile_blob(profile)
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        part = _claim_part(sentence)
        if not re.search(r"\b(i|my|me)\b", part, re.I) or sentence.rstrip().endswith("?"):
            continue
        hits = {m.group(1).lower() for m in _INFLATE_WIDE.finditer(part)
                if m.group(1).lower() not in blob}
        # "keen to deepen my expertise" is exempt as wanting to learn, but it says
        # the expertise already exists
        hits |= {m.group(1).lower() for m in _PRESUPPOSED.finditer(sentence)
                 if m.group(1).lower() not in blob}
        hits = sorted(hits)
        if hits:
            out.append((sentence.strip(), f"uses {', '.join(hits)} - your profile never says that"))
    return out
