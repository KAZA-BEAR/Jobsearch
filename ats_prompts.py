"""
ats_prompts.py — all prompts and JSON schemas sent to the local models.

Prompts for the tailoring pipeline.

The CV is assembled from profile.json in code. The model is only asked for
small, self-contained pieces of text, which keeps the context to ~2k tokens and
makes a 7B local model reliable:

  * bullet_prompt      - rephrase one experience bullet in the job ad's
                         wording (the only model-written part of the CV)
  * cover_only_prompt  - optional cover letter body
  * recruiter_only_prompt - optional three-sentence recruiter message
"""

import json
import re

LANGUAGE_NAMES = {"en": "English", "de": "German"}

SYSTEM = ("You are a precise CV editor. You follow the output format exactly and never "
          "add facts that are not in the text you are given.")


# ------------------------------------------------------------ experience bullets
# One bullet per request, and the model is only told which of the bullet's own
# terms to rephrase in the job ad's wording. In testing, qwen2.5-7b given the ad
# (or even just the role title, or a free keyword list) rewrote bullets into the
# ad's tasks - "Integrated LiDAR sensors into a drone swarm" for a manipulator
# project - and upgraded "coordinated with teams" to "led teams".

BULLET = """Lightly edit this CV bullet.

Rules:
- Keep its exact meaning and every number, tool and fact. Add nothing new.
- Do not make the role sound bigger ("coordinated" must not become "led").
- Plain verbs. Never use enhance, boost, elevate, ensure, leverage, showcase,
  spearhead or similar CV buzzwords.
{mapping}- Start with a verb ({tense} tense), ideally the original one. Put these job
  keywords, which the bullet already contains, right after that verb so they stand
  out: {keep}
- At most 30 words, {language}.{extra}

Answer as JSON: "text" = the edited bullet, "keywords_used" = the keywords from
the list above that the edited bullet contains.

Bullet: {bullet}"""


def bullet_prompt(bullet: str, mapping: list, keep: list, tense: str, lang: str,
                  problems: list = None) -> str:
    extra = ""
    if problems:
        extra = "\n- Your previous edit was rejected: " + "; ".join(problems) + "."
    wording = ""
    if mapping:
        wording = ("- Use the job ad's wording for these terms the bullet already mentions:\n"
                   + "\n".join(f'  "{own}" -> "{ad}"' for own, ad in mapping) + "\n")
    return BULLET.format(
        mapping=wording, keep=", ".join(keep),
        tense=tense, language=LANGUAGE_NAMES.get(lang, "English"), extra=extra,
        bullet=bullet)


# ------------------------------------------------------------------ writing style
# Adapted from Dr Kriukow's "write like a human" prompt: the points that make an
# application read as written by a person. Left out: its academic tone, passive
# voice and meta-discourse ("as mentioned earlier"), which do not suit a one-page
# letter, and its goal of beating AI detectors - the aim here is clear, specific text.

STYLE = """Writing style (sound like a careful person, not a template):
- Mix short, direct sentences with longer ones. Do not start two sentences in a row
  with the same word, and do not repeat a phrase close to itself.
- Plain, everyday words. Never use: enhance, boost, elevate, ensure, crucial,
  leverage, delve, foster, pivotal, seamless, showcase, underscore, robust,
  cutting-edge, dynamic, passionate, thrilled, journey, testament, "aligns perfectly".
- No inflated or dramatic statements, no metaphors, no suspense, no sweeping opening
  about the field; start with the role and a fact.
- Say what something is. Never "not X but Y", "rather than", or "extends beyond".
- Avoid lists of three: use two items, or split them over two sentences.
- At most one ", doing something" clause in the whole text.
- Do not end a paragraph with a summary sentence; let paragraphs differ in length.
- No contractions, no slang. Professional and first person, not academic."""


# ------------------------------------------------------------------ cover letter

def compact_profile(profile: dict) -> str:
    """The profile without contact details - the letter's header comes from code."""
    keep = {k: v for k, v in profile.items() if k not in ("personal", "_comment")}
    keep["title"] = (profile.get("personal") or {}).get("title", "")
    return json.dumps(keep, ensure_ascii=False, separators=(",", ":"))


COVER_ONLY = """Write the BODY of a cover letter in {language} for the job below.

Rules:
- 3 or 4 paragraphs separated by one blank line, 200-300 words in total.
- Use ONLY the candidate facts below, in your own words. Pick the 2-3 most relevant
  and explain what the candidate did; do NOT list skills or tools in bulk.
- Never add a skill, tool, task, result or number that the facts do not state.
- The candidate does NOT have: {gaps}. Do not claim them. You may say the candidate is
  keen to learn ONE of them, in one sentence.
- Never claim a relationship, contact or collaboration with the employer.
- Write as the candidate, in the first person ("I"). Never address the reader as
  "you", never write "we" or "our" as if you were the company, and never copy
  sentences from the job ad.
- No salutation, no sign-off, no date, no address, no markdown. Plain text only.

Structure: (1) the role and why it fits the candidate's studies; (2) the most relevant
evidence: take the 2-3 most important numbered requirements under JOB whose proof is
"work done" and, for each, describe what the candidate did in that line; (3) the
"listed only" requirements, named briefly, plus transferable skills, stated honestly;
(4) optional short close.

{style}

JOB:
{jd}

CANDIDATE FACTS (the only things you may state about the candidate):
{profile}

Output only the cover letter body."""

RECRUITER_ONLY = """Write a short message in {language} from the candidate to a recruiter
about the job below, as JSON with three fields, each ONE sentence:
- "intro": in the first person ("I am ..."), who the candidate is and which role
  they are writing about,
- "evidence": one concrete thing from the profile that fits the role (if the job
  lists numbered requirements with proof lines, use the proof of requirement 1),
- "question": a low-pressure question the candidate asks about the role, team or
  research, ending with a question mark. Never ask the recipient about their own
  preferences or whether THEY want to apply or start - they are hiring, not applying.
Only facts from the profile. Never claim a relationship, prior contact or interest in
the employer's past work. No greeting ("Dear ...") and no thanks or signature.{recruiter_line}

{style}

JOB:
{jd}

CANDIDATE PROFILE (JSON):
{profile}

Output only the JSON."""

# Structured output: LM Studio constrains decoding to this shape, so the message
# always has exactly three parts. (In testing the free-text prompt produced two
# sentences as often as three.)
RECRUITER_SCHEMA = {
    "type": "object",
    "properties": {"intro": {"type": "string"}, "evidence": {"type": "string"},
                   "question": {"type": "string"}},
    "required": ["intro", "evidence", "question"],
}


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def join_recruiter(raw: str) -> str:
    """The three JSON parts as one three-sentence message.

    The schema guarantees three parts but not one sentence per part, so each
    part is trimmed: the first sentence of intro and evidence, and the sentence
    that asks the question. Falls back to the raw text if the backend ignored
    the schema.
    """
    try:
        d = json.loads(raw)
    except ValueError:
        return raw.strip()
    if not isinstance(d, dict):
        return raw.strip()

    def sentences(key):
        return [s.strip() for s in _SENTENCE.split((d.get(key) or "").strip()) if s.strip()]

    intro, evidence, question = sentences("intro"), sentences("evidence"), sentences("question")
    ask = next((s for s in question if s.endswith("?")), question[-1] if question else "")
    return " ".join(x for x in (intro[:1] + evidence[:1] + [ask]) if x).strip()


def cover_only_prompt(profile: dict, job: str, lang: str, gaps: str,
                      facts: str = "") -> str:
    """`facts`: the profile lines that prove the job's requirements. Giving the
    model only these (plus studies) instead of the whole profile keeps the letter
    on relevant, true material."""
    return COVER_ONLY.format(language=LANGUAGE_NAMES.get(lang, "English"),
                             gaps=gaps or "none listed", jd=job, style=STYLE,
                             profile=facts or compact_profile(profile))


# One rewrite for style only, applied to a letter that already passed the fact-check;
# the result is fact-checked again and dropped if anything in it is unsupported.
STYLE_FIX = """Revise this cover letter body. Fix ONLY these writing problems:
{problems}{predictable}

Keep every fact exactly as stated: do not add, remove or change any skill, tool, task,
result, number, employer, degree or place. Do not add new claims. Keep the same
paragraphs. Change wording, sentence length and sentence order only.

{style}

LETTER:
{text}

Output only the revised letter body."""


def style_fix_prompt(text: str, problems: list, predictable: list = ()) -> str:
    pred = ""
    if predictable:
        pred = ("\n- rephrase these sentences, which read as too predictable:\n"
                + "\n".join(f"  {s}" for s in predictable))
    return STYLE_FIX.format(problems="\n".join(f"- {p}" for p in problems), predictable=pred,
                            style=STYLE, text=text)


# ------------------------------------------------------- fact-check (judge)

FACTCHECK = """Below are the ONLY true facts about a candidate, and a text written for them.
List every sentence of the text that states something about the candidate which the facts
do NOT support (a skill, tool, task, result, number, experience or connection that is not
in the facts). Copy each such sentence exactly. General enthusiasm, questions and
"keen to learn" sentences are fine - do not list those.
Also fine, do not list: sentences that restate a fact and say how it relates to the job
("this is relevant to the role", "which relates to developing ROS 2 nodes"), and
sentences saying which role the candidate is writing about. Judge only what the
sentence says the candidate DID or HAS: list it if that part is not in the facts.
A sentence that adds a new skill, tool, task or result to a true fact must still be
listed ("I built the manipulator, which involved navigation algorithms").
"reason": at most 15 words, naming the unsupported part.

FACTS:
{facts}

TEXT:
{text}"""

FACTCHECK_SCHEMA = {
    "type": "object", "required": ["unsupported"],
    # Bounded: an unbounded list ran past max_tokens in the .exe run (at temperature 0
    # a model can repeat an entry until it runs out), and Bonsai 27B wrote
    # paragraph-long reasons. A letter has fewer than 15 sentences anyway.
    "properties": {"unsupported": {"type": "array", "maxItems": 15, "items": {
        "type": "object", "required": ["sentence", "reason"],
        "properties": {"sentence": {"type": "string", "maxLength": 600},
                       "reason": {"type": "string", "maxLength": 160}}}}},
}


def factcheck_prompt(facts: str, text: str) -> str:
    return FACTCHECK.format(facts=facts, text=text)


def recruiter_only_prompt(profile: dict, job: str, lang: str,
                          recruiter: str = "") -> str:
    return RECRUITER_ONLY.format(
        language=LANGUAGE_NAMES.get(lang, "English"),
        recruiter_line=f" Address it to {recruiter}." if recruiter else "",
        jd=job, style=STYLE, profile=compact_profile(profile))


# ----------------------------------------------------- stage 2: job requirements

REQUIREMENTS = """From this job ad, list the technical skills, tools and knowledge the
candidate needs (at most 14).
For each item:
- "skill": short standard name IN ENGLISH, 1-4 words, even if the ad is German
  (e.g. "motion planning", "PVD coating", "tribology")
- "priority": "required", or "preferred" if the ad says nice-to-have / von Vorteil / a plus
- "quote": the shortest exact phrase from the ad that names it (2-8 words), copied
  character for character in the ad's own language, never shortened with "..."
One item per named tool or language: "Python, C++ and/or Matlab" is three items
("Python", "C++", "Matlab"), never "programming". No umbrella words on their own
("robotics", "AI", "programming", "hardware") - name the specific area the ad gives,
e.g. "physical human-robot interaction", "robot hardware".
When the ad asks for knowledge in "at least one" of several areas, give each area as
an item, all with the same priority.
No soft skills, benefits or company descriptions, and no degree or field of study:
"You are studying mechanical engineering, mechatronics or computer science" gives
NO items.

JOB AD:
{ad}"""

REQUIREMENTS_SCHEMA = {
    "type": "object", "required": ["requirements"],
    "properties": {"requirements": {"type": "array", "maxItems": 14, "items": {
        "type": "object", "required": ["skill", "priority", "quote"],
        "properties": {"skill": {"type": "string"},
                       "priority": {"type": "string", "enum": ["required", "preferred"]},
                       "quote": {"type": "string"}}}}},
}


def requirements_prompt(ad: str) -> str:
    return REQUIREMENTS.format(ad=ad)


# A pasted ad has no "Position: / Company:" header, so the letter had no addressee.
IDENTITY = """From this job ad, copy the job title and the name of the hiring company exactly
as they are written in the ad. Use "" for anything the ad does not state.

JOB AD:
{ad}"""

IDENTITY_SCHEMA = {
    "type": "object", "required": ["role", "company"],
    "properties": {"role": {"type": "string"}, "company": {"type": "string"}},
}


def identity_prompt(ad: str) -> str:
    return IDENTITY.format(ad=ad)


# ------------------------------------------------ stage 3: proposed evidence

EVIDENCE = """For each numbered job requirement, give the number of the ONE candidate
profile line that directly shows the candidate has it, or 0 if no line does.
Be strict: related is not enough - the line must show that exact skill.

REQUIREMENTS:
{requirements}

PROFILE LINES:
{lines}"""

EVIDENCE_SCHEMA = {
    "type": "object", "required": ["links"],
    "properties": {"links": {"type": "array", "items": {
        "type": "object", "required": ["requirement", "line"],
        "properties": {"requirement": {"type": "integer"}, "line": {"type": "integer"}}}}},
}


# ------------------------------------- stage 3 with embeddings: expand, verify

EXPAND = """For each numbered job requirement, list 3-8 concrete terms a CV could use for
the SAME skill: specific tools, protocols, methods, synonyms, and the English or German
equivalent (e.g. "serial communication buses" -> UART, I2C, SPI, CAN, RS-232, serial
protocols). Only the skill itself or specific instances of it, never neighbouring fields.

REQUIREMENTS:
{requirements}"""

EXPAND_SCHEMA = {
    "type": "object", "required": ["expansions"],
    "properties": {"expansions": {"type": "array", "items": {
        "type": "object", "required": ["requirement", "terms"],
        "properties": {"requirement": {"type": "integer"},
                       "terms": {"type": "array", "maxItems": 8, "items": {"type": "string"}}}}}},
}


def expand_prompt(requirements: list) -> str:
    return EXPAND.format(requirements="\n".join(f"{i}. {r['skill']}"
                                                 for i, r in enumerate(requirements, 1)))


VERIFY = """Job requirement: "{skill}" (the job ad says: "{quote}")

For each numbered line from a candidate's profile, answer whether that line ALONE shows
the candidate has this requirement:
- "yes": the line names the skill itself or a specific instance of it
  (e.g. "UART, I2C and SPI protocols" shows "serial communication buses").
  A course or a listed skill is "yes" when the ad asks for knowledge or
  understanding of it ("Control Systems (course)" for "basic understanding of
  control engineering"), and "partly" when the ad asks for hands-on experience.
- "partly": the line shows only part of the requirement, or a closely related skill.
- "no": anything else. Working in the same field is not enough.

LINES:
{lines}"""

VERIFY_SCHEMA = {
    "type": "object", "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "required": ["line", "verdict"],
        "properties": {"line": {"type": "integer"},
                       "verdict": {"type": "string", "enum": ["yes", "partly", "no"]}}}}},
}


def verify_prompt(requirement: dict, lines: list) -> str:
    return VERIFY.format(skill=requirement["skill"], quote=requirement.get("quote", ""),
                         lines="\n".join(f"{i}. {t}" for i, t in enumerate(lines, 1)))


def evidence_prompt(requirements: list, units: list) -> str:
    return EVIDENCE.format(
        requirements="\n".join(f"{i}. {r['skill']}" for i, r in enumerate(requirements, 1)),
        lines="\n".join(f"{i}. {line}" for i, (_label, line) in enumerate(units, 1)))


# ------------------------------------------------ stage 5: one bullet, as JSON

BULLET_SCHEMA = {
    "type": "object", "required": ["text", "keywords_used"],
    "properties": {"text": {"type": "string"},
                   "keywords_used": {"type": "array", "items": {"type": "string"}}},
}


def parse_bullet(raw: str) -> tuple[str, list]:
    """(text, keywords_used) from the JSON answer; plain text if the backend
    ignored the schema."""
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return str(d.get("text", "")).strip(), [str(k) for k in d.get("keywords_used") or []]
    except ValueError:
        pass
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    return line.lstrip("-•* ").strip('"'), []
