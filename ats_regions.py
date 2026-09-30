"""
ats_regions.py — CV conventions per market (photo, date of birth, nationality, page limit).

Region-specific CV conventions.

ATS parsers and hiring conventions differ by market. The LaTeX renderer uses
photo, dob and nationality to decide what is typeset; the other fields record
each market's conventions (document name, page limit, date format, spelling,
rules) for reference. The CV is built in code, so no model is told to follow them.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Region:
    code: str
    label: str
    doc_name: str          # what the CV file is called in that market
    photo: bool            # include profile photo
    dob: bool              # include date of birth
    nationality: bool
    max_pages: str
    date_format: str
    spelling: str
    rules: list = field(default_factory=list)

REGIONS = {
    "EU": Region(
        code="EU", label="European Union (general)", doc_name="CV",
        photo=True, dob=False, nationality=True,
        max_pages="2 pages maximum",
        date_format="Mon YYYY (e.g. May 2025)",
        spelling="British English",
        rules=[
            "State visa / work-authorisation status plainly, as non-EU candidates are screened on it.",
            "Include a language section with CEFR-style levels.",
            "Avoid US-style self-promotion; keep claims factual and verifiable.",
        ],
    ),
    "DE": Region(
        code="DE", label="Germany / DACH", doc_name="Lebenslauf (CV)",
        photo=True, dob=True, nationality=True,
        max_pages="2 pages maximum, strictly reverse-chronological",
        date_format="MM/YYYY or Mon YYYY",
        spelling="British English (or German if the job ad is German)",
        rules=[
            "Reverse-chronological order is mandatory; no functional/skills-first layouts.",
            "Account for all periods; do not hide gaps.",
            "Photo, date of birth and place of residence are conventional and accepted here.",
            "Include German language level explicitly - German employers screen on it.",
            "Write in the OUTPUT LANGUAGE stated below. Never switch language on your own: "
            "a German CV implies German fluency the candidate may not have.",
        ],
    ),
    "UK": Region(
        code="UK", label="United Kingdom / Ireland", doc_name="CV",
        photo=False, dob=False, nationality=True,
        max_pages="2 pages maximum",
        date_format="Mon YYYY",
        spelling="British English",
        rules=[
            "No photo, no date of birth, no marital status - equality legislation.",
            "State right-to-work status briefly.",
            "Open with a short professional profile of 3-4 lines.",
        ],
    ),
    "US": Region(
        code="US", label="United States / Canada", doc_name="Resume",
        photo=False, dob=False, nationality=False,
        max_pages="1 page for under 10 years of experience",
        date_format="Mon YYYY",
        spelling="American English",
        rules=[
            "Never include photo, date of birth, marital status, gender or nationality - EEOC risk.",
            "Lead every bullet with a strong past-tense action verb and quantify impact.",
            "Use a single-column layout with standard headings: Summary, Skills, Experience, Education.",
            "Keep it to one page unless the profile genuinely warrants two.",
        ],
    ),
    "ASIA": Region(
        code="ASIA", label="Asia (India, Pakistan, Gulf, SEA)", doc_name="CV / Resume",
        photo=True, dob=True, nationality=True,
        max_pages="2-3 pages accepted",
        date_format="Mon YYYY",
        spelling="British English",
        rules=[
            "Photo, date of birth and nationality are commonly expected.",
            "Include an explicit education section with institution names spelled out; academic pedigree carries weight.",
            "Notice period and current location are worth stating if known.",
            "Certifications are weighted heavily - list them prominently if any exist.",
        ],
    ),
    "JP": Region(
        code="JP", label="Japan / Korea", doc_name="Resume (shokumu keirekisho style)",
        photo=True, dob=True, nationality=True,
        max_pages="2 pages maximum",
        date_format="YYYY/MM",
        spelling="British English",
        rules=[
            "Photo and date of birth are standard.",
            "Be factual and understated; avoid superlatives entirely.",
            "State Japanese language ability (JLPT level) if any, and visa status.",
        ],
    ),
}

DEFAULT_REGION = "DE"


def get_region(code: str) -> Region:
    code = (code or DEFAULT_REGION).upper()
    if code not in REGIONS:
        raise KeyError(f"Unknown region '{code}'. Choose from: {', '.join(REGIONS)}")
    return REGIONS[code]
