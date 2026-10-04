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

# Sent with every call - extraction, CV bullets, the letter writer and the judge.
SYSTEM = ("You write and check job-application text for one candidate. Follow the output "
          "format exactly. Never state anything about the candidate that the facts you are "
          "given do not say, and never translate names of schools, companies or job titles.")


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


# The rules every model-written sentence about the candidate is held to. The writer,
# the style pass, the recruiter message and both checkers state the same ones, so
# no step asks for what a later step deletes (2026-10-04: the letter prompt asked
# for "transferable skills" and "listed only" requirements, which the clean-up
# then cut as inference and tool lists).
CLAIM_RULES = """- State only what the facts say. Never add a skill, tool, task, purpose, result,
  benefit, number, place or responsibility - not even a likely one ("to improve
  reliability", "strong skills in", "hands-on experience building").
- Keep each fact's own verbs and scope: "coordinating with teams" stays "with teams"
  (not "coordinated teams" or "led"), "1 new feature" stays one, an unfinished degree
  stays unfinished.
- One fact per claim: never join two separate facts into one claim (code from one task
  "for" another task, work at one employer placed at another).
- RESEARCH INTERESTS are interests only. The candidate may say they are interested in
  them - never that they studied, used, focus on, or have skills or experience in them.
- Write school, company and job titles exactly as the facts write them; never translate.
- Do not say how a fact matches, prepares for or aligns with the job; state the fact."""


COVER = """Write the BODY of a cover letter in {language} for the job below, as the candidate.

Content:
- Choose at most FOUR work or project facts - the ones that best match the numbered
  requirements under JOB (a "proof (work done)" line is such a fact). Say what the
  candidate did in each, joining related facts into flowing sentences.
- Say where each fact happened: the job and employer, lab or project written before
  the colon in the fact ("As an Artificial Intelligence Engineer at SOCO Engineers
  GmbH, I wrote ..."). Do not paste the facts one after another as a list.
- A requirement whose proof is "listed only" (a skill or course) may be named in one
  short sentence. Never list tools or skills in bulk: name at most three tools in
  the whole letter, each inside a sentence about what the candidate did.
- The candidate does NOT have: {gaps}. Do not claim them; you may say the candidate is
  keen to learn ONE of them, in one sentence.
- Never write a sentence that only restates a job requirement ("C++ is required for
  this role"), and never claim contact or collaboration with the employer.

Rules for every sentence about the candidate:
{claims}

Form: 3 or 4 paragraphs separated by one blank line, 180-280 words. (1) the role and
the candidate's current studies; (2) and (3) the chosen facts; (4) optional: one
"keen to learn" sentence. First person ("I"); never address the reader as "you" or
write "we"/"our" as the company; never copy sentences from the job ad. No salutation,
sign-off, date, address or markdown.

Citations - required:
- Every fact below has an ID (F1, F2, ... and I1 for research interests). End EVERY
  sentence with the IDs of the facts it uses, in square brackets: "... computation
  engine [F9]." or "... [F3, F9]." The "proof" lines under JOB are copies of these
  facts: cite the matching ID.
- A sentence about the job, the company or the candidate's motivation that states
  nothing the candidate did or has ends with [J].
- A sentence may say only what its cited facts say.

{style}

JOB:
{jd}

CANDIDATE FACTS (the only things you may state about the candidate):
{profile}

Output only the cover letter body, with the citations."""

# "exact title ... never translated": Qwen3-4B translated the BMW title's
# "Flussregelung" (flux control) as "river regulation".
RECRUITER_ONLY = """Write a short message in {language} from the candidate to a recruiter
about the job below, as JSON with three fields, each ONE sentence:
- "intro": in the first person ("I am ..."), who the candidate is and which role they
  are writing about - the role's exact title from the job, copied as written,
- "evidence": ONE piece of work that fits the role - a job or project fact (a line
  with a job title and employer, or "Project:"), never an education, coursework,
  skills or language line - said in the fact's own words with "I" and naming where
  it happened. Only the fact - do not add that it matches, demonstrates or relates
  to a requirement,
- "question": a low-pressure question about the role, team or research, ending with
  a question mark. Never ask the recipient about their own preferences or whether
  THEY want to apply - they are hiring, not applying.
Never claim a relationship, prior contact or interest in the employer's past work. No
greeting ("Dear ..."), no thanks or signature.{recruiter_line}

Rules for every sentence about the candidate:
{claims}

{style}

JOB:
{jd}

CANDIDATE FACTS (the only things you may state about the candidate):
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
                      facts: str = "", cited: bool = True) -> str:
    """The letter prompt. `facts`: the numbered fact list ("F7: ...", "I1: ...") from
    the pipeline; every sentence must cite the IDs it uses, so it can be checked
    against those facts alone. `cited` is kept for older callers; the prompt always
    asks for citations, which the pipeline strips before anything is shown."""
    return COVER.format(language=LANGUAGE_NAMES.get(lang, "English"),
                        gaps=gaps or "none listed", jd=job, style=STYLE,
                        claims=CLAIM_RULES, profile=facts or compact_profile(profile))


# One rewrite for style only, applied to a letter that already passed the checks;
# the result is checked again and dropped if anything in it is unsupported.
STYLE_FIX = """Revise this cover letter body. Fix ONLY these writing problems:
{problems}{predictable}

Change wording, sentence length and sentence order only. Keep every fact exactly as it
is stated and keep the same paragraphs. Do not add a sentence, a claim, a tool list or
a sentence that restates a job requirement, and do not remove a fact.

Rules that still apply to every sentence about the candidate:
{claims}

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
                            claims=CLAIM_RULES, style=STYLE, text=text)


# ------------------------------------------------------- fact-check (judge)

FACTCHECK = """Below are the ONLY true facts about a candidate, and a text written for them.
List every sentence of the text that states something about the candidate which the
facts do NOT support. Copy each such sentence exactly. List a sentence when it:
- adds a skill, tool, task, purpose, result, benefit, number, place or responsibility
  the facts do not state - also when the rest of the sentence is true
  ("I built the manipulator, which involved navigation algorithms");
- presents a research interest as coursework, a focus of study, a skill or experience;
- joins two separate facts into one claim (work from one task or employer described as
  part of another);
- makes a fact bigger: "led" or "coordinated teams" for "coordinating with teams",
  plural for one item, a finished degree for an unfinished one.
Do NOT list: enthusiasm, questions, "keen to learn" sentences, sentences naming the role
applied for, or sentences that only describe the job; and do not list a sentence that
says the same as a fact in other words.
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


# One sentence against the few facts it cites or draws on: a decomposed entailment
# check (claim-level, as grounding checkers like MiniCheck do) instead of judging the
# whole letter against the whole profile, where the 4B judge cut true sentences.
ENTAIL = """Do these facts about a job candidate support the sentence below?

Facts:
{facts}

Sentence: {sentence}

SUPPORTED: the sentence says what the facts say, in any wording, word order or tense,
or names the same thing more generally ("the computation engine" for "a computation
engine"). Opinions about the job and enthusiasm ("I am eager to ...") are supported.

NOT supported - answer false - if the sentence:
- adds anything the facts do not state: a tool, skill, task, purpose ("to improve
  ..."), result, benefit, number, place or responsibility ("led", "coordinated teams"
  for "coordinating with teams");
- calls a research interest coursework, a focus, a skill or experience;
- joins two separate facts into one claim that neither fact states (one task done
  "for", "while" or "as part of" another).

Answer with JSON: "supported" true or false, and "added": the words the facts do not
back (empty when supported)."""

ENTAIL_SCHEMA = {
    "type": "object", "required": ["supported", "added"],
    "properties": {"supported": {"type": "boolean"},
                   "added": {"type": "string", "maxLength": 160}},
}


def entail_prompt(facts: list, sentence: str) -> str:
    return ENTAIL.format(facts="\n".join(f"- {f}" for f in facts), sentence=sentence)


def recruiter_only_prompt(profile: dict, job: str, lang: str,
                          recruiter: str = "", facts: str = "") -> str:
    """`facts`: the same fact list the letter gets (research interests on their own
    line). Without it, the whole profile as JSON, as before."""
    return RECRUITER_ONLY.format(
        language=LANGUAGE_NAMES.get(lang, "English"),
        recruiter_line=f" Address it to {recruiter}." if recruiter else "",
        claims=CLAIM_RULES, jd=job, style=STYLE, profile=facts or compact_profile(profile))


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
