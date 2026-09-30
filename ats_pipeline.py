"""
ats_pipeline.py — run the whole CV / cover-letter tailoring pipeline for one job.

Tailor a CV to one job.

The CV is assembled from profile.json in code; the model (LM Studio by default)
only rewords the work-experience bullets toward the job's keywords, and writes
an optional cover letter and recruiter message. Each request is ~2k tokens.

Models: "judge,drafter,..." (default Qwen3-4B + Llama 3.2 3B, loaded in LM Studio
automatically with a fixed context). The first model judges - requirements,
fact-check, bullets, recruiter message; every listed model drafts the cover
letter in parallel. Every draft is fact-checked by the model AND by the code
checks in ats_checks; drafts are merged in code; if too little true text is left,
the letter is built from profile.json instead. Nothing unchecked is ever used.

Zero third-party dependencies: models are called over urllib so the app runs
on a stock Python install on Windows or Linux.
"""

import json
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import ats_checks as checks
import ats_latex as latex
import ats_plan as planning
import ats_prompts as prompts
from ats_regions import get_region

DEFAULT_BACKEND = os.environ.get("ATS_BACKEND", "lmstudio")
DEFAULT_MODEL = os.environ.get("ATS_MODEL", "")          # LM Studio: use whatever is loaded
# LM Studio's local server, and any other OpenAI-compatible server (Ollama, vLLM,
# llama.cpp, LocalAI) - point ATS_BASE_URL at it.
DEFAULT_BASE_URL = os.environ.get("ATS_BASE_URL", "http://localhost:1234/v1")
ROOT = Path(__file__).resolve().parent   # flat layout: all files in one folder


class PipelineError(RuntimeError):
    pass


class FactCheckFailed(PipelineError):
    """The model's fact-check gave no usable answer: the text stays unchecked,
    so it must not be used."""


# ----------------------------------------------------------------------- API

def _post(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise PipelineError(f"API error {e.code}: {e.read().decode('utf-8', 'replace')[:800]}")
    except urllib.error.URLError as e:
        raise PipelineError(
            f"Could not reach {url}: {e.reason}\n"
            "If you are using LM Studio, open it, load a model, go to the Developer "
            "tab and press Start Server.")


# (token, log-probability) pairs of the last answer generated with logprobs=True.
# LM Studio returns them only for text the model writes (it cannot score given
# text), so they give the real perplexity of the model's own draft sentences.
# Per thread: drafts from several models are written at the same time.
_LOGPROBS = threading.local()


def last_logprobs() -> list:
    """This thread's (token, logprob) pairs from its last logprobs=True call."""
    return getattr(_LOGPROBS, "value", None) or []


def _call_openai_compatible(prompt, model, max_tokens, api_key, timeout, base_url,
                            schema=None, temperature=0.3, logprobs=False):
    """LM Studio, Ollama, vLLM, llama.cpp, LocalAI - all speak this.

    `schema` (a JSON schema) turns on structured output: LM Studio constrains
    decoding so the reply is valid JSON of exactly that shape.
    """
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    # Qwen3's thinking mode took 35-43 s per bullet in testing (5-8 s without) and
    # "reasoned" its way into overstating edits, so it is switched off.
    if "qwen3" in (model or "").lower():
        prompt = "/no_think\n" + prompt
    payload = {
        "model": model or "local-model",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        "messages": [{"role": "system", "content": prompts.SYSTEM},
                     {"role": "user", "content": prompt}],
    }
    if schema:
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "answer", "strict": True, "schema": schema}}
    if logprobs:
        payload["logprobs"] = True
    headers = {"authorization": f"Bearer {api_key or 'lm-studio'}"}
    # Reasoning models ignore /no_think (Bonsai 27B spent every token thinking and
    # returned nothing); LM Studio switches thinking off with reasoning_effort.
    try:
        data = _post(f"{base}/chat/completions", dict(payload, reasoning_effort="none"),
                     headers, timeout)
    except PipelineError as e:
        if "API error 4" not in str(e):
            raise
        data = _post(f"{base}/chat/completions", payload, headers, timeout)
    choices = data.get("choices") or []
    if not choices:
        raise PipelineError(f"Empty response from {base}: {str(data)[:400]}")
    msg = choices[0].get("message", {})
    content_lp = (choices[0].get("logprobs") or {}).get("content") or []
    _LOGPROBS.value = [(c.get("token", ""), c.get("logprob")) for c in content_lp
                       if c.get("logprob") is not None]
    if choices[0].get("finish_reason") == "length":
        # max_tokens ran out - not necessarily the context (a 128k context gave the
        # same error when an answer simply ran past max_tokens)
        raise PipelineError(
            f"The local model's answer ran past its {max_tokens}-token limit. If this "
            "keeps happening, check that the context length in LM Studio (Developer tab "
            "> model settings) is 8k or more.")
    # Some local models emit a reasoning block; the content field is what we want.
    return msg.get("content") or ""


def list_local_models(base_url: str = None, timeout: int = 15):
    """Chat models the OpenAI-compatible server has loaded (embedding models,
    which cannot write text, are left out)."""
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/models", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise PipelineError(f"Could not reach {base}/models: {e}")
    ids = [m.get("id", "") for m in data.get("data", [])]
    return [i for i in ids if "embed" not in i.lower()]


# The default pair: Qwen3-4B judges and drafts, Llama 3.2 3B drafts too (see the
# 2026-09-30 pilot: ~75 s a run on a 6 GB RTX 3050). Override with ATS_MODELS.
DEFAULT_LOCAL_MODELS = os.environ.get("ATS_MODELS", "qwen/qwen3-4b-2507,llama-3.2-3b-instruct")
# Context each model is loaded with. Loaded on demand, LM Studio gave Llama its
# maximum (131072), whose working memory filled the GPU so Qwen could not load.
# (LM Studio rounds a requested 6000 up to 6144.)
MODEL_CONTEXT = {"qwen/qwen3-4b-2507": 6144, "llama-3.2-3b-instruct": 6144}
DEFAULT_CONTEXT = 6144


def _lms_path() -> str:
    """LM Studio's command-line tool, which loads models with a chosen context."""
    found = shutil.which("lms")
    if found:
        return found
    for p in (Path.home() / ".lmstudio" / "bin" / "lms.exe", Path.home() / ".lmstudio" / "bin" / "lms",
              Path.home() / ".cache" / "lm-studio" / "bin" / "lms.exe"):
        if p.exists():
            return str(p)
    return ""


def _lms(args: list, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run lms quietly (no console window flashing up from the .exe)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run([_lms_path()] + args, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", creationflags=flags)


def _loaded_models(base_url: str) -> dict:
    """{model id: loaded context length or None if not loaded} from LM Studio."""
    base = (base_url or DEFAULT_BASE_URL).rstrip("/").rsplit("/v1", 1)[0]
    try:
        with urllib.request.urlopen(f"{base}/api/v0/models", timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data", [])
    except Exception:  # noqa: BLE001 - older LM Studio or another server
        return {}
    return {m.get("id", ""): (m.get("loaded_context_length") if m.get("state") == "loaded" else None)
            for m in data}


def ensure_models_loaded(models: list, base_url: str = None, log=print) -> None:
    """Load `models` in LM Studio with the right context, so nothing has to be typed:
    other chat models are unloaded first (6 GB of GPU memory holds the pair, not a
    third model), embedding models stay. Does nothing when the models are already
    loaded as wanted, when the server is not LM Studio, or when lms is missing."""
    wanted = {m: MODEL_CONTEXT.get(m, DEFAULT_CONTEXT) for m in dict.fromkeys(models) if m}
    state = _loaded_models(base_url)
    if not wanted or not state:
        return
    # loaded with enough context, and not the huge default that fills the GPU
    fits = lambda m, ctx: state.get(m) is not None and ctx <= state[m] <= 2 * ctx
    if all(fits(m, ctx) for m, ctx in wanted.items()):
        return
    if not _lms_path():
        log("LM Studio's lms tool was not found; load these models in LM Studio yourself: "
            + ", ".join(wanted))
        return
    missing = [m for m in wanted if m not in state]
    if missing:
        log(f"Not downloaded in LM Studio: {', '.join(missing)} - download them first.")
    for model_id, ctx in state.items():
        if ctx is not None and model_id not in wanted and "embed" not in model_id.lower():
            log(f"Unloading {model_id} to make room on the GPU...")
            _lms(["unload", model_id])
    for model_id, ctx in wanted.items():
        if model_id in missing or fits(model_id, ctx):
            continue
        if state.get(model_id) is not None:
            _lms(["unload", model_id])
        log(f"Loading {model_id} in LM Studio (context {ctx}, on the GPU)...")
        result = _lms(["load", model_id, "--context-length", str(ctx), "--gpu", "max", "-y"])
        if result.returncode != 0:
            log(f"Could not load {model_id}: {(result.stderr or result.stdout).strip()[-300:]}")


def call_model(prompt: str, model: str = DEFAULT_MODEL, max_tokens: int = 1500,
               api_key: str = None, timeout: int = 600,
               backend: str = DEFAULT_BACKEND, base_url: str = None,
               schema: dict = None, temperature: float = 0.3, logprobs: bool = False) -> str:
    """Local models only: LM Studio, or another OpenAI-compatible server on
    this machine. `schema` enforces JSON output.
    `temperature` 0 is for judgements (requirements, verification, fact-check):
    at 0.3 Bonsai answered "yes" for the ANSYS bullet in one run and "no" in the
    next."""
    backend = (backend or DEFAULT_BACKEND).lower()
    if backend in ("lmstudio", "openai", "local"):
        return _call_openai_compatible(prompt, model, max_tokens, api_key,
                                       timeout, base_url, schema, temperature, logprobs)
    raise PipelineError(f"Unknown backend '{backend}'. Use lmstudio.")


# --------------------------------------------------------------------- helpers

def _preclean(raw: str) -> str:
    """Strip what small models wrap around plain-text answers."""
    raw = (raw or "").replace("\\n", "\n")
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"^```\w*\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    # "Title @ Employer" is how the facts list jobs; small models copied the "@"
    # into letters ("As an Electrical Engineer @ Pakistan Ordnance Factories")
    raw = re.sub(r"(?<=\w) @ (?=\w)", " at ", raw)
    return raw.strip()


# -------------------------------------------------------------- CV assembly
# Everything except the experience bullets comes straight from profile.json,
# in the "@ header | fields" + "- bullet" format ats_latex renders.

def _entry(head: list, bullets: list) -> str:
    return "\n".join(["@ " + " | ".join(str(h or "") for h in head)]
                     + [f"- {b}" for b in bullets if b])


def build_resume(profile: dict, experience_bullets: dict[int, list[str]]) -> dict:
    exp = []
    for i, e in enumerate(profile.get("experience") or [], 1):
        exp.append(_entry([e.get("title"), e.get("employer"), e.get("location"),
                           f"{e.get('start')} - {e.get('end')}"],
                          experience_bullets.get(i) or e.get("bullets") or []))
    projects = [_entry([p.get("name"), p.get("context"), "", p.get("year")], p.get("bullets") or [])
                for p in profile.get("projects") or []]
    education = []
    for ed in profile.get("education") or []:
        bullets = []
        if ed.get("coursework"):
            bullets.append("Relevant coursework: " + ", ".join(ed["coursework"]))
        if ed.get("research_interests"):
            bullets.append("Research interests: " + ", ".join(ed["research_interests"]))
        if ed.get("thesis"):
            bullets.append("Thesis: " + ed["thesis"])
        education.append(_entry([ed.get("degree"), ed.get("institution"), ed.get("location"),
                                 f"{ed.get('start')} - {ed.get('end')}"], bullets))
    certs = [c if isinstance(c, str) else ", ".join(str(v) for v in c.values() if v)
             for c in profile.get("certifications") or []]
    return {
        "summary": profile.get("profile_summary", ""),
        "skills": "\n".join(f"{cat}: {', '.join(items)}"
                            for cat, items in (profile.get("skills") or {}).items()),
        "experience": "\n\n".join(exp),
        "projects": "\n\n".join(projects),
        "education": "\n\n".join(education),
        "certifications": "\n".join(certs),
        "languages": "",           # rendered from profile.json by ats_latex
    }


# ------------------------------------------------------ model-written parts

def tailor_experience(profile, job_description, plan, lang, call, forbidden, log):
    """Stage 5: execute the approved plan, one bullet per request.

    Each planned bullet gets its OWN keywords brought forward; the model answers
    as JSON {text, keywords_used}. An edit is used only if it passes every check
    (numbers, meaning, no inflation, no labels, no new content, no job-ad or gap
    terms, keywords it claims really present); otherwise the original stays, or
    a plain term swap in code where the ad uses other words for the same term.
    Returns ({job_index: bullets}, notes).
    """
    foreign = checks._foreign_terms(job_description, checks._profile_blob(profile)) | forbidden
    mapping = checks.term_mapping(profile, job_description)
    by_job = {}
    for item in plan:
        by_job.setdefault(item["job"], []).append(item)
    result, notes = {}, []
    for i, items in sorted(by_job.items()):
        name = f"{items[0]['title']} @ {items[0]['employer']}"
        tense = "present" if str(items[0]["end"]).lower() == "present" else "past"
        edited, n_model, n_swap, n_kept = [], 0, 0, 0
        for it in items:
            bullet, keep, pairs = it["text"], it["keep"], it["pairs"]
            if not it["edit"]:
                edited.append(bullet)
                continue
            problems, new = [], ""
            for attempt in (1, 2):
                log(f"Rewording a {name} bullet ({', '.join(keep + [a for _o, a in pairs])}), "
                    f"attempt {attempt}...")
                raw = _preclean(call(prompts.bullet_prompt(bullet, pairs, keep, tense, lang,
                                                           problems or None),
                                     max_tokens=200, schema=prompts.BULLET_SCHEMA))
                new, claimed = prompts.parse_bullet(raw)
                problems = checks.edit_problems(bullet, new, pairs, foreign, lang, keep)
                absent = [k for k in claimed if k.lower() not in new.lower()]
                if absent:
                    problems.append("it listed keywords it does not contain: " + ", ".join(absent))
                if not problems:
                    break
            if not problems:
                edited.append(new)
                n_model += 1
            elif pairs:
                edited.append(checks.swap_terms(bullet, pairs))
                n_swap += 1
            else:
                edited.append(bullet)
                n_kept += 1
        # Most relevant first; a stable sort keeps the original order for ties.
        ordered = sorted(edited, key=lambda b: -checks.relevance(b, mapping))
        result[i] = ordered
        what = []
        if n_model:
            what.append(f"{n_model} bullet(s) reworded around your own keywords")
        if n_swap:
            what.append(f"{n_swap} term swap(s) done in code (model edit rejected)")
        if n_kept:
            what.append(f"{n_kept} model edit(s) rejected, original kept")
        if ordered != edited:
            what.append("bullets reordered by relevance")
        notes.append(f"✓ {name}: " + ("; ".join(what) or "unchanged"))
    return result, notes


def candidate_facts(profile: dict, requirements: list, meta: dict = None) -> str:
    """What the letter may say: studies, summary, languages, and the profile lines
    that prove this job's requirements (all profile lines if none were found).
    The role applied for is listed too: without it the model's fact-check deleted
    the letter's opening ("The ... role at Quantum Systems fits my studies") as
    "not in the facts", and the letter started mid-argument."""
    lines = []
    role, company = (meta or {}).get("role"), (meta or {}).get("company")
    if role or company:
        lines.append("Applying for: " + " at ".join(x for x in (role, company) if x))
    lines.append(f"Summary: {profile.get('profile_summary', '')}")
    for ed in profile.get("education") or []:
        lines.append(f"Education: {ed['degree']}, {ed['institution']} ({ed['start']} - {ed['end']})"
                     + (f"; coursework: {', '.join(ed['coursework'])}" if ed.get("coursework") else "")
                     + (f"; research interests: {', '.join(ed['research_interests'])}"
                        if ed.get("research_interests") else "")
                     + (f"; thesis: {ed['thesis']}" if ed.get("thesis") else ""))
    proof = []
    for r in requirements:
        if r["status"] == "have":
            proof += [e for e in r["evidence"] if e not in proof]
    # Then every other profile line: all of it is true, and a fact list of only the
    # proven lines made the checker delete true claims ("the facts do not mention
    # any UAV" about the candidate's own UAV project).
    proof += [u for u in (f"{label}: {line}" for label, line in planning.evidence_units(profile))
              if u not in proof]
    lines += proof
    lines.append("Languages: " + ", ".join(f"{l['language']} ({l['level']})"
                                          for l in profile.get("languages") or []))
    if profile.get("additional_context"):
        lines.append(f"Additional context from the candidate: {profile['additional_context']}")
    return "\n".join(f"- {l}" for l in lines)


def job_brief(job_description: str, meta: dict, requirements: list) -> str:
    """What the letter model sees of the job: role, a short opening of the ad, and
    each requirement the profile proves paired with the line that proves it.

    Replaces the 2200-char ad excerpt. With the whole ad the model wrote about the
    ad's tasks rather than the candidate's proof; with the pairs it is pointed at
    the matching lines. Unproven requirements are left out (the prompt's gap rule
    covers them). Falls back to the excerpt when no requirement was proven."""
    matched = [r for r in requirements if r["status"] == "have" and r["evidence"]]
    if not matched:
        return checks.jd_excerpt(job_description, limit=2200)
    head = " at ".join(x for x in (meta.get("role"), meta.get("company")) if x)
    lines = [f"Role: {head}"] if head else []
    lines.append("About the role: " + checks.jd_excerpt(job_description, limit=400))
    lines += ["", "What the job asks for that the candidate has, and the profile line "
              "that proves it:"]
    # Proof from real work first. A skills-list or coursework line only shows the
    # skill is known; asked to "describe what the candidate did" with it, the model
    # invented work ("extensive experience with Gazebo"), which the fact-check then
    # deleted, leaving a two-paragraph letter.
    def work(r):
        return next((e for e in r["evidence"] if not e.startswith(_LISTED)), "")

    matched.sort(key=lambda r: (r["priority"] != "required", not work(r)))
    for i, r in enumerate(matched, 1):
        lines.append(f'{i}. {r["skill"]} ({r["priority"]}; the ad says "{r["quote"]}")')
        if work(r):
            lines.append(f"   proof (work done): {work(r)}")
        else:
            lines.append(f"   proof (listed only - name it, do not describe work with it): "
                         f"{r['evidence'][0]}")
    return "\n".join(lines)


_LISTED = ("Skills:", "Education:")


_ENTHUSIASM = re.compile(r"\b(eager|excited|passion\w*|motivat\w*|look forward|keen|"
                         r"would welcome|thank you|hope to)\b", re.I)
# The checker sometimes lists a sentence and then explains it is fine.
# The checker sometimes flags a sentence while its own reason says the sentence is
# supported (Qwen3-4B: "This is a restatement of a fact, not a new claim").
_FINE_REASON = re.compile(r"(does not state|general enthusiasm|desire to learn|is fine|"
                          r"not a (new )?claim|expresses (a )?(desire|interest|enthusiasm)|"
                          r"restate(ment|s)? (of )?(a |the )?facts?|is (stated|supported|listed) "
                          # "This is supported by facts." (no "the") deleted 3 true sentences
                          r"(in|by) (the )?facts|consistent with (the )?facts|matches (the )?facts|"
                          # "Repeats application fact, not new information" (a true sentence)
                          r"not new information|repeats (an? |the )?(application|stated|known) fact)",
                          re.I)


def _repeats(sentence: str, known: list) -> bool:
    """Whether a sentence restates most of a claim already removed as unsupported."""
    own = planning._stems(sentence)
    return any(k and len(own & k) >= 0.7 * len(k) for k in known)


# The job ad of the run in progress, for the check that finds ad text copied into a
# letter. Set once per run in write_texts: the app runs one generation at a time,
# and the checks are called from many places, some in parallel draft threads.
_CURRENT_AD = {"text": ""}


def _code_checks(text: str, profile: dict, fact_lines: list, forbidden=()) -> list:
    """(sentence, why) from every deterministic check."""
    # Deterministic: a sentence naming one organisation but describing another's work
    # (the 8B checker missed "at TH Deggendorf ... UAV for traffic observation").
    return (checks.reader_or_ad_copy(text, _CURRENT_AD["text"])
            + checks.misattributions(text, profile)
            + checks.coursework_mixups(text, profile)
            + checks.invented_details(text, profile)
            + checks.false_denials(text, profile)
            + checks.interest_claims(text, profile)
            + checks.mixed_details(text, profile)
            + checks.merged_claims(text, profile)
            + checks.inflated_claims(text, profile)
            + checks.count_inflation(text, profile)
            + checks.unfinished_degrees(text, profile)
            + checks.learning_known(text, profile)
            + checks.ungrounded_claims(text, fact_lines)
            + [(s, f"claims {w}, which your profile does not contain")
               for s, w in checks.claimed_gaps(text, forbidden)])


# Where a true statement turns into a judgement or an add-on.
_TRAIL = re.compile(r",\s*(demonstrat\w*|showing|showcasing|highlighting|reflecting|proving|"
                    r"providing|gaining|giving|ensuring|allowing|enabling|which|where|"
                    r"directly|further)\b", re.I)


def _salvage(sentence: str, profile: dict, fact_lines: list, forbidden=()) -> str:
    """The sentence up to its first trailing clause, if that part alone is almost
    entirely profile wording (80% of its words from one or two profile lines) and
    passes every code check; otherwise ''."""
    m = _TRAIL.search(sentence)
    if not m:
        return ""
    head = sentence[:m.start()].strip() + "."
    if len(head.split()) < 6 or _code_checks(head, profile, fact_lines, forbidden):
        return ""
    return head if _profile_share(head, fact_lines) >= 0.8 else ""


def _profile_share(text: str, fact_lines: list) -> float:
    """Share of the text's content words found in one or two profile lines."""
    stems = checks._content_stems(text)
    if not stems:
        return 0.0
    lines = [checks._content_stems(l) for l in fact_lines]
    return max((len(stems & (a | b)) / len(stems) for i, a in enumerate(lines)
                for b in lines[i:]), default=0.0)


def _near_copy(sentence: str, profile: dict, fact_lines: list, forbidden=()) -> bool:
    """A sentence that restates the profile: each clause takes 85% of its content
    words from ONE profile line, and the sentence passes every code check. The
    model's fact-check at temperature 0 flagged such sentences ("building a 4-DOF
    manipulator ... is not in facts", "writing and debugging C++/Python code"), so
    every Bonsai letter fell back to the plain one built from the profile; code
    overrules it here. One line per clause: allowing two let "C++/Python code for
    robot navigation" pass on the SOCO line plus the robot-platform line."""
    if len(checks._content_stems(sentence)) < 5 or _code_checks(sentence, profile,
                                                                fact_lines, forbidden):
        return False
    if any(checks._names_interest(r, sentence) for r in _interests_only(profile)) \
            and not re.search(r"\binterests?\b", sentence, re.I):
        return False                # "my academic focus on SLAM ..." - an interest
    lines = [checks._content_stems(l) for l in fact_lines]
    for clause in checks._CLAUSES.split(sentence):
        stems = checks._content_stems(clause)
        if not stems:
            continue
        # A short clause must come wholly from one line: with more split points,
        # "..., while leading the team" would otherwise pass as too short to judge.
        need = 0.85 if len(stems) >= 3 else 1.0
        if max((len(stems & l) / len(stems) for l in lines), default=0) < need:
            return False
    return True


def _interests_only(profile: dict) -> set:
    """Research interests that are not also coursework."""
    out = set()
    for ed in profile.get("education") or []:
        courses = " ".join(ed.get("coursework") or []).lower()
        out |= {r for r in ed.get("research_interests") or [] if r.lower() not in courses}
    return out


# A paragraph may not open with a connective once the sentence before it is gone
# ("Additionally, during my tenure ..." as a paragraph's first words).
_DANGLING = re.compile(r"^(additionally|also|furthermore|moreover|in addition|as well|"
                       r"for instance|for example),\s+(\w)", re.I)


def factcheck(text: str, facts: str, call, log, profile: dict = None,
              forbidden=(), known=()) -> tuple[str, list]:
    """Second model call as a fact-checker. Whole sentences it flags as unsupported
    are DELETED - this step only ever removes text. Learning / enthusiasm
    sentences, and flags whose own reason says the sentence is fine, are kept.
    `known` are sentences removed from an earlier draft: the checker is not
    deterministic (Bonsai 27B passed a rewrite that repeated a claim it had
    removed from the first draft), so a repeat is removed in code."""
    if not text.strip():
        return text, []
    flagged = None
    for limit in (3000, 5000):
        try:
            raw = call(prompts.factcheck_prompt(facts, text), max_tokens=limit,
                       schema=prompts.FACTCHECK_SCHEMA, temperature=0)
            flagged = json.loads(raw).get("unsupported", [])
            break
        except (PipelineError, ValueError, AttributeError) as exc:
            # A cut-off or unreadable answer is NOT "nothing flagged" (it used to
            # be treated that way): try once more, then give up on this text.
            log(f"The fact-check answer was unusable ({str(exc)[:80]}); retrying...")
    if flagged is None:
        raise FactCheckFailed("the model's fact-check did not return a usable answer")
    paragraphs = [re.split(r"(?<=[.!?])\s+", p.strip()) for p in re.split(r"\n\s*\n", text)]
    fact_lines = [l.lstrip("- ") for l in facts.splitlines() if l.strip()]
    removed = []

    def cut(para, k, sentence, why):
        """Remove a flagged sentence, keeping its first part when only a trailing
        clause overreaches ("I built the manipulator ..., proving my ability to
        run experiments on real robots"): whole-sentence deletion cut a Bonsai
        letter to 16 words, mostly true facts lost with their add-ons."""
        head = _salvage(sentence, profile or {}, fact_lines, forbidden)
        para[k] = head
        if head:
            removed.append(f"…{sentence[len(head) - 1:].strip(' ,.')} ({why}; the first part "
                           "of the sentence was kept)")
        else:
            removed.append(f"{sentence} ({why})")

    overruled = set()
    for f in flagged:
        quote = re.sub(r"\s+", " ", str(f.get("sentence", ""))).strip()
        reason = str(f.get("reason", "")).strip()
        if len(quote) < 15 or _FINE_REASON.search(reason):
            continue
        for para in paragraphs:
            for k, sentence in enumerate(para):
                flat = re.sub(r"\s+", " ", sentence)
                # the checker may quote only part of a sentence: remove the whole one
                if sentence and (quote in flat or (len(flat) > 15 and flat in quote)):
                    if checks._LEARNING.search(flat) or _ENTHUSIASM.search(flat) \
                            or flat.endswith("?"):
                        continue
                    if _near_copy(flat, profile or {}, fact_lines, forbidden):
                        overruled.add(flat)
                        continue
                    cut(para, k, flat, reason)
    # Deterministic: a sentence naming one organisation but describing another's work
    # (the 8B checker missed "at TH Deggendorf ... UAV for traffic observation").
    known_stems = [planning._stems(k) for k in known]
    for para in paragraphs:
        for k, s in enumerate(para):
            if s and known_stems and _repeats(s, known_stems):
                cut(para, k, " ".join(s.split()), "repeats a claim already removed "
                                                  "from the first draft")
    for sentence, why in _code_checks(text, profile or {}, fact_lines, forbidden):
        target = re.sub(r"\s+", " ", sentence)
        for para in paragraphs:
            for k, s in enumerate(para):
                if s and re.sub(r"\s+", " ", s).strip() == target:
                    cut(para, k, target, why)
    if overruled:
        log(f"Kept {len(overruled)} flagged sentence(s) that restate your profile.")
    if removed:
        log(f"Fact-check removed {len(removed)} unsupported sentence(s).")
        text = "\n\n".join(" ".join(s for s in para if s) for para in paragraphs)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        text = "\n\n".join(_DANGLING.sub(lambda m: m.group(2).upper(), p.strip())
                           for p in text.split("\n\n"))
    return text, removed


def _evidence_sentence(profile: dict, requirements: list) -> str:
    """One true sentence built from a real past-tense bullet that proves a job
    requirement: 'At SOCO Engineers GmbH, I wrote and debugged C++/Python code ...'."""
    proven = " ".join(e for r in requirements if r["status"] == "have" for e in r["evidence"])
    for e in profile.get("experience") or []:
        for b in e.get("bullets") or []:
            first = b.split()[0].lower() if b.split() else ""
            past = first.endswith("ed") or first in checks._IRREGULAR
            if past and b in proven:
                return f"At {e['employer']}, I {b[0].lower()}{b[1:].rstrip('.')}."
    return ""


def _intro_sentence(profile: dict, role: str) -> str:
    """'I am a Mechatronics Student (M.Eng ... at ...) writing about the X role.'"""
    title = (profile.get("personal") or {}).get("title") or "candidate"
    current = next((e for e in profile.get("education") or []
                    if str(e.get("end", "")).lower() == "present"), None)
    study = f" ({current['degree']} at {current['institution']})" if current else ""
    about = f"the {role} role" if role else "this role"
    article = "an" if title[:1].lower() in "aeiou" else "a"
    return f"I am {article} {title}{study} writing about {about}."


def _complete_recruiter(text: str, profile: dict, requirements: list, role: str = "") -> str:
    """Rebuild intro + evidence + question from what the fact-check left, filling
    a missing part in code: the intro from the profile, the evidence from a real
    bullet, the question from a plain template. (Bonsai once left only the
    question; always inserting evidence once repeated the manipulator bullet.)"""
    parts = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    if len(parts) == 3 and parts[2].endswith("?") \
            and re.search(r"\b(i|i'm|i am|my)\b", parts[0], re.I) \
            and re.search(r"\b(i|my)\b", parts[1], re.I):
        return text
    question = next((s for s in reversed(parts) if s.endswith("?")),
                    "Could you tell me more about the team's current work?")
    intro = next((s for s in parts if re.match(r"\s*(i am|i'm)\b", s, re.I)),
                 _intro_sentence(profile, role))
    # A kept sentence without "I"/"my" ("Developed and tested ...") is a broken
    # sentence: the code-built evidence replaces it.
    rest = [s for s in parts if s not in (intro, question) and re.search(r"\b(i|my)\b", s, re.I)]
    evidence = rest[0] if rest else _evidence_sentence(profile, requirements)
    if not evidence:
        return " ".join([intro, question])
    same = planning._stems(evidence) & planning._stems(intro)
    if len(same) >= 0.6 * max(1, len(planning._stems(evidence))):
        return " ".join([intro, question])
    return " ".join([intro, evidence, question])


def _first_person(sentence: str) -> str:
    """A resume-style summary fragment as a letter sentence: 'Aspiring engineer
    combining ...' -> 'I am an aspiring engineer combining ...', 'Currently
    pursuing ...' -> 'I am currently pursuing ...'. Full sentences stay."""
    first = sentence.split()[0] if sentence.split() else ""
    if re.match(r"(i|my|we|our|as|with|in|at)$", first, re.I):
        return sentence
    body = _lower_first(sentence)
    # "Bachelor's degree in X." -> "My bachelor's degree is in X." (not "I am a
    # bachelor's degree ...", and not another sentence opening with "I")
    if re.match(r"(bachelor|master|doctoral|phd|diploma|b\.?\s?sc|m\.?\s?sc|m\.?\s?eng|b\.?\s?eng)",
                first, re.I):
        degree = sentence if re.match(r"[bm]\.", first, re.I) else body   # keep "M.Sc"
        m = re.match(r"(.+?)\s+in\s+(.+)", degree)
        return f"My {m.group(1)} is in {m.group(2)}" if m else f"I hold {degree}"
    words = sentence.split()
    # "Pursuing an M.Eng" is a verb; "Aspiring Mechatronics Engineer" is an adjective.
    verb = (first.lower().endswith("ing") and len(words) > 1 and words[1][:1].islower()
            and first.lower() not in ("aspiring", "emerging", "promising", "leading",
                                      "outstanding", "willing", "hardworking", "enterprising"))
    if verb or first.lower() in ("currently", "now"):
        return "I am " + body
    return ("I am an " if first[:1].lower() in "aeiou" else "I am a ") + body


def _lower_first(text: str) -> str:
    """'Built ...' -> 'built ...'; 'XML ...' stays."""
    return text if text[1:2].isupper() else text[:1].lower() + text[1:]


def _work_sentence(evidence: str, variant: int = 0) -> str:
    """'Title @ Employer: Built X.' -> 'As a Title at Employer, I built X.'; `variant`
    changes the frame so consecutive sentences do not open the same way."""
    project = evidence.startswith("Project: ")
    label, _, line = evidence[len("Project: ") if project else 0:].partition(": ")
    label = "Project: " + label if project else label
    line = _lower_first(line.rstrip("."))
    first = line.split()[0].lower() if line.split() else ""
    past = first.endswith("ed") or first in checks._IRREGULAR
    if label.startswith("Project: "):
        name = label[len("Project: "):]
        if past:
            return (f"In my project \"{name}\", I {line}." if variant % 2 == 0
                    else f"For the project \"{name}\", I {line}.")
        return f"My project \"{name}\" included {line}."
    title, _, employer = label.partition(" @ ")
    a_title = ("an " if title[:1].lower() in "aeiou" else "a ") + title
    if not employer:
        return f"As {a_title}, I {line}." if past else f"My work as {a_title} included {line}."
    if past:
        return [f"As {a_title} at {employer}, I {line}.",
                f"At {employer}, where I worked as {a_title}, I {line}.",
                f"During my time as {a_title} at {employer}, I {line}."][variant % 3]
    return (f"My work as {a_title} at {employer} included {line}." if variant % 2 == 0
            else f"At {employer}, my work as {a_title} included {line}.")


_LEVEL_RANK = {"native": 0, "mother tongue": 0, "c2": 1, "fluent": 1, "c1": 2, "advanced": 2,
               "b2": 3, "upper intermediate": 3, "b1": 4, "intermediate": 4, "a2": 5,
               "elementary": 5, "a1": 6, "beginner": 6, "basic": 6}


def _languages_sentences(langs: list) -> list:
    """Languages as a person writes them, strongest first: "Urdu is my native
    language, and I speak English fluently. My German is at beginner level." (not
    "I speak German (beginner) and English (fluent). My Urdu is at native level.")"""
    rank = lambda l: _LEVEL_RANK.get(str(l.get("level", "")).strip().lower(), 3)
    native, fluent, rest = [], [], []
    for l in sorted(langs, key=rank):
        (native if rank(l) == 0 else fluent if rank(l) == 1 else rest).append(l["language"])
    parts = []
    if native:
        parts.append(f"{' and '.join(native)} {'is my native language' if len(native) == 1 else 'are my native languages'}")
    if fluent:
        parts.append(f"I speak {' and '.join(fluent)} fluently")
    out = [(", and ".join(parts) + ".")[0].upper() + (", and ".join(parts) + ".")[1:]] if parts else []
    for l in sorted((l for l in langs if l["language"] in rest), key=rank):
        level = str(l["level"]).strip()
        level = level.upper() if re.fullmatch(r"[abc][12]", level, re.I) else level.lower()
        out.append(f"My {l['language']} is at {level} level.")
    return out


_NUMBER_WORDS = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
                 "7": "seven", "8": "eight", "9": "nine"}


def _polish_prose(text: str) -> str:
    """Letter prose conventions: small numbers as words ("one new feature"), but
    not in terms like "4-DOF", "2-layer", "15-20%" or "3.5"."""
    return re.sub(r"(?<![\w.,/-])([1-9])(?=\s+[a-z])", lambda m: _NUMBER_WORDS[m.group(1)], text or "")


def _in_pairs(items: list) -> list:
    """['A', 'B', 'C', 'D'] -> ['A and B', 'C and D']: lists of three read as
    machine-written, so long lists are said two items at a time."""
    return [" and ".join(items[i:i + 2]) for i in range(0, len(items), 2)]


def profile_letter(profile: dict, meta: dict, requirements: list, gap_names: list) -> str:
    """A cover letter assembled from profile.json alone, for when the model's
    draft does not survive the fact-check in one piece. Every sentence is the
    profile's own wording or a fixed frame around it, so it cannot claim anything
    the profile does not say."""
    p1 = _opening_paragraph(profile, meta)

    have = sorted((r for r in requirements if r["status"] == "have" and r["evidence"]),
                  key=lambda r: r["priority"] != "required")
    work, used, listed = [], set(), []
    for r in have:
        line = next((e for e in r["evidence"] if not e.startswith(_LISTED)), "")
        if line and line not in used and len(work) < 3:
            used.add(line)
            work.append(_work_sentence(line, len(work)))
        elif not line:
            listed.append(r)
    if not work:          # nothing proven by work: the two most recent bullets
        for e in (profile.get("experience") or [])[:2]:
            if e.get("bullets"):
                work.append(_work_sentence(f"{e['title']} @ {e['employer']}: {e['bullets'][0]}",
                                           len(work)))
    skills = [r["skill"] for r in have]
    # A statement about the job, not a claim: "which my experience covers" also
    # claimed experience for coursework-only requirements. Two items, not a list
    # of three.
    # "electronics and wiring as well as Python/Linux", not "... and wiring and Python"
    joiner = " as well as " if any(" and " in s for s in skills[:2]) else " and "
    listing = joiner.join(skills[:2]) + (", among other skills" if len(skills) > 2 else "")
    p2 = (f"The role asks for {listing}. " if skills else "") + " ".join(work)

    p3 = []
    for r in listed:
        ev = r["evidence"][0]
        if ev.startswith("Education: "):
            # the courses themselves: the ad's word ("control engineering") is not
            # what the transcript says ("Control Systems")
            degree, _, courses = ev[len("Education: "):].partition(": ")
            pairs = _in_pairs([c.strip() for c in courses.rstrip(".").split(",") if c.strip()])
            sentence = (f"My {degree} coursework included {pairs[0]}."
                        + (f" It also covered {', as well as '.join(pairs[1:])}." if pairs[1:] else ""))
        else:
            sentence = f"My skills also include {r['skill']}."
        if sentence not in p3:
            p3.append(sentence)
    # Only extracted requirements: the keyword fallback in gap_names once made this
    # "I am keen to learn similar in practice" (from "or similar simulation tools").
    real_gaps = [g for g in gap_names
                 if any(r["skill"] == g and r["status"] == "gap" for r in requirements)]
    if real_gaps:
        p3.append(f"I am keen to learn {real_gaps[0]} in practice.")
    p3 += _languages_sentences(profile.get("languages") or [])
    return "\n\n".join(p for p in (p1, p2, " ".join(p3), _CLOSING) if p.strip())


_CLOSING = ("I would welcome the chance to discuss how I can support your team. "
            "Thank you for considering my application.")
_HAS_CLOSING = re.compile(r"\b(welcome the (chance|opportunity)|look forward|thank you|"
                          r"would be glad|discuss)\b", re.I)


def _opening_paragraph(profile: dict, meta: dict) -> str:
    """The role, then the profile summary in the first person, with varied
    openings: "This letter is...", "I am an aspiring...", "Currently, I am..."."""
    role, company = meta.get("role"), meta.get("company")
    # "(m/f/d)" belongs in the ad, not in the candidate's sentence
    role = re.sub(r"\s*\((?:[mwfdx]\s*/\s*){2,3}[mwfdx]\)|\s*\((?:all genders|gn\*?)\)", "",
                  role or "", flags=re.I).strip()
    position = f"the {role} position" if role else "the advertised position"
    summary = re.split(r"(?<=[.!?])\s+", profile.get("profile_summary", "").strip())
    opening = [f"Please consider my application for {position}"
               f"{' at ' + company if company else ''}."]
    for s in (_first_person(s) for s in summary if s):
        prev = opening[-1].split()[0].lower()
        if s.split()[0].lower() == prev and s.startswith("I am currently "):
            s = "Currently, I am " + s[len("I am currently "):]
        opening.append(s)
    return " ".join(opening)


def _frame_letter(text: str, profile: dict, meta: dict, log) -> str:
    """A model letter that lost its opening or closing to the fact-check gets the
    true ones used by the profile letter (a letter once began "I have hands-on
    experience with Python..." with no word about the role)."""
    # no paragraph opens with a connective ("For instance, ..."), whoever wrote it
    paras = [_DANGLING.sub(lambda m: m.group(2).upper(), p.strip())
             for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if not paras:
        return text
    role, company = meta.get("role") or "", meta.get("company") or ""
    first = re.split(r"(?<=[.!?])\s+", paras[0])[0]
    names_job = any(x and re.search(re.escape(re.sub(r"\s*\(.*?\)", "", x)), first, re.I)
                    for x in (role, company)) or re.search(r"\b(apply|applying|application)\b",
                                                           first, re.I)
    if not names_job:
        paras.insert(0, _opening_paragraph(profile, meta))
        log("The cover letter had no opening about the role; added the one from your profile.")
    if not _HAS_CLOSING.search(paras[-1]):
        paras.append(_CLOSING)
        log("The cover letter had no closing line; added the standard one.")
    return "\n\n".join(paras)


_TRAILING_ROLE = re.compile(r"^I (?P<body>.+?),? (?P<where>(?:as an? |through my (?:role|position|work) "
                            r"as an? |during my time as an? |while working as an? )[^,.]+? at [^,.]+?)"
                            r"(?P<end>[.!?])$")


def _vary_openings(text: str, profile: dict) -> str:
    """Rewords sentences that start with "I" right after another "I" sentence,
    without changing what they say: move a trailing "as a <title> at <employer>"
    to the front, or open with "In that role," when the sentence is a bullet of
    the job the previous sentence named."""
    jobs = [(e.get("employer", ""), e.get("title", ""),
             [checks._content_stems(b) for b in e.get("bullets") or []])
            for e in profile.get("experience") or []]

    def job_named(sentence):
        return next((j for j in jobs if j[0] and (j[0].split(",")[0] in sentence)), None)

    def is_bullet_of(sentence, job):
        own = checks._content_stems(sentence)
        return bool(own) and any(len(own & b) >= 0.6 * len(b) for b in job[2] if b)

    out_paras, frame = [], 0            # frames rotate across the whole letter
    for para in re.split(r"\n\s*\n", text.strip()):
        sents = [s for s in re.split(r"(?<=[.!?])\s+", para.strip()) if s]
        prev_job = None
        for k, s in enumerate(sents):
            prev_i = k > 0 and sents[k - 1].split()[0] == "I"
            if s.startswith("I ") and prev_i:
                m = _TRAILING_ROLE.match(s)
                if m:
                    where = m.group("where")
                    s = f"{where[0].upper() + where[1:]}, I {m.group('body')}{m.group('end')}"
                elif prev_job and not job_named(s) and is_bullet_of(s, prev_job):
                    s = _same_job_frame(s, frame)
                    frame += 1
                sents[k] = s
            prev_job = job_named(s) or prev_job
        out_paras.append(" ".join(sents))
    return "\n\n".join(out_paras)


# past tense -> -ing, only for verbs whose -ing form is known (no guessing)
_GERUND = {"built": "building", "wrote": "writing", "developed": "developing",
           "designed": "designing", "created": "creating", "coordinated": "coordinating",
           "automated": "automating", "collaborated": "collaborating", "implemented":
           "implementing", "integrated": "integrating", "tested": "testing", "prepared":
           "preparing", "contributed": "contributing", "managed": "managing", "taught":
           "teaching", "debugged": "debugging", "achieved": "achieving", "reduced": "reducing",
           "demonstrated": "demonstrating", "included": "including", "worked": "working"}


def _same_job_frame(sentence: str, n: int) -> str:
    """'I prepared X.' about the job just named -> 'In that role, I prepared X.' /
    'My work there also included preparing X.' / 'There, I prepared X.'"""
    rest = sentence[2:]
    verb = rest.split()[0] if rest.split() else ""
    options = ["In that role, I " + rest]
    if verb.lower() in _GERUND:
        options.append(f"My work there also included {_GERUND[verb.lower()]}{rest[len(verb):]}")
    options.append("There, I " + rest)
    return options[n % len(options)]


def _multi_draft(drafters, make, call, check, facts, log, profile, forbidden, drafts):
    """Every model writes a cover-letter draft at the same time; each draft is
    fact-checked on its own; the checked drafts are merged in code. Returns
    (merged letter, removed sentences). Raises FactCheckFailed if no draft could be
    checked - unchecked text is never used."""
    log(f"Writing the cover letter with {len(drafters)} models in parallel: "
        f"{', '.join(drafters)}...")

    def one(job):
        # a different temperature per drafter: the same model listed twice still
        # gives two different drafts (the Self-MoA setup)
        i, model_name = job
        raw = _preclean(call(make(), max_tokens=1200, logprobs=True, use_model=model_name,
                             temperature=min(0.3 + 0.3 * i, 0.9)))
        tokens = last_logprobs()
        try:
            checked, removed = factcheck(raw, facts, call, lambda m: None, profile, forbidden)
        except FactCheckFailed:
            return {"model": model_name, "raw": raw, "text": "", "removed": [], "tokens": tokens,
                    "failed": True}
        return {"model": model_name, "raw": raw, "text": checked, "removed": removed,
                "tokens": tokens, "failed": False}

    with ThreadPoolExecutor(len(drafters)) as ex:
        results = list(ex.map(one, enumerate(drafters)))
    if len(set(drafters)) < len(drafters):      # same model twice: tell the drafts apart
        for i, r in enumerate(results):
            r["model"] = f"{r['model']} #{i + 1}"
    for r in results:
        drafts.append(r["tokens"])
        total = len([s for s in re.split(r"(?<=[.!?])\s+", r["raw"]) if s.strip()])
        kept = len([s for s in re.split(r"(?<=[.!?])\s+", r["text"]) if s.strip()])
        log(f"  {r['model']}: " + ("fact-check failed, draft not used" if r["failed"] else
                                  f"{kept} of {total} sentences survived the fact-check"))
    usable = [r for r in results if r["text"].strip()]
    if not usable:
        raise FactCheckFailed("no draft could be fact-checked")
    fact_lines = [l.lstrip("- ") for l in facts.splitlines() if l.strip()]
    merged, used = _merge_drafts(usable, check, profile, fact_lines, forbidden)
    log("Merged letter: " + ", ".join(f"{n} sentence(s) from {m}" for m, n in used.items() if n))
    removed = [f"{x} [{r['model']}]" for r in results for x in r["removed"]]
    return merged, removed


# Sentences that lean on the one before them change meaning when moved.
_ANAPHOR = re.compile(r"^\s*(this|these|that|those|it|there|they|such|in that role|"
                      r"my work there|additionally|also|furthermore|moreover)\b", re.I)


def _merge_drafts(results: list, check, profile: dict, fact_lines: list, forbidden) -> tuple:
    """The best-shaped checked draft is the base; true sentences from the other
    drafts that add facts the base lacks are appended to the paragraph they fit
    best. Near-duplicates, sentences that depend on their neighbour, and anything a
    code check objects to in the merged text are left out."""
    words = lambda t: len(t.split())
    base = max(results, key=lambda r: (not check(r["text"]), words(r["text"])))
    paras = [[s for s in re.split(r"(?<=[.!?])\s+", p.strip()) if s]
             for p in re.split(r"\n\s*\n", base["text"].strip()) if p.strip()]
    used = {r["model"]: 0 for r in results}
    used[base["model"]] = sum(len(p) for p in paras)
    seen = [checks._content_stems(s) for p in paras for s in p]
    covered = set().union(*seen) if seen else set()
    for r in results:
        if r is base:
            continue
        for s in (x for x in re.split(r"(?<=[.!?])\s+", r["text"].strip()) if x):
            stems = checks._content_stems(s)
            if len(stems) < 4 or _ANAPHOR.match(s):
                continue
            if any(len(stems & o) >= 0.6 * min(len(stems), len(o)) for o in seen if o):
                continue                                   # says what the base says
            if len(stems - covered) < 3:
                continue                                   # adds too little
            if words("\n\n".join(" ".join(p) for p in paras)) + words(s) > 340:
                break
            # never into the opening paragraph: a merged "I also worked on UAV flight
            # testing" landed between the role and the studies
            body = range(1, len(paras)) if len(paras) > 1 else range(len(paras))
            target = max(body,
                         key=lambda i: len(stems & set().union(*[checks._content_stems(x)
                                                                 for x in paras[i]] or [set()])))
            trial = [list(p) for p in paras]
            trial[target].append(s)
            text = "\n\n".join(" ".join(p) for p in trial)
            if not _code_checks(text, profile, fact_lines, forbidden):
                paras, seen, covered = trial, seen + [stems], covered | stems
                used[r["model"]] += 1
    return "\n\n".join(" ".join(p) for p in paras), used


def write_texts(profile, job_description, lang, gaps, recruiter, call, log, forbidden=(),
                requirements=(), meta=None, drafters=None):
    """Optional cover letter + recruiter message: written from the proven facts only,
    shape-checked and retried once, then fact-checked (unsupported sentences are
    deleted). `forbidden` are unmet-requirement terms: mentioning one as a skill
    the candidate has fails the check."""
    _CURRENT_AD["text"] = job_description
    brief = job_brief(job_description, meta or checks.parse_header(job_description),
                      list(requirements))
    facts = candidate_facts(profile, list(requirements),
                            meta or checks.parse_header(job_description))
    results, issues, removed_all = {}, [], []
    for field, make, check, schema in (
            ("cover_letter",
             lambda: prompts.cover_only_prompt(profile, brief, lang, gaps, facts),
             checks.letter_problems, None),
            ("recruiter_message",
             lambda: prompts.recruiter_only_prompt(profile, brief, lang, recruiter),
             checks.recruiter_problems, prompts.RECRUITER_SCHEMA)):
        best, best_problems, drafts, built = None, [], [], False
        multi = field == "cover_letter" and drafters and len(drafters) > 1
        for attempt in (() if multi else (1, 2)):
            log(f"Writing the {field.replace('_', ' ')} (attempt {attempt})...")
            raw = _preclean(call(make(), max_tokens=1200, schema=schema, logprobs=True))
            drafts.append(last_logprobs())
            text = prompts.join_recruiter(raw) if schema else raw
            problems = check(text) + checks.gap_claims(text, forbidden)
            if best is None or len(problems) < len(best_problems):
                best, best_problems = text, problems
            if not problems:
                break
        try:
            if multi:
                best, removed = _multi_draft(drafters, make, call, check, facts, log, profile,
                                             forbidden, drafts)
            else:
                best, removed = factcheck(best or "", facts, call, log, profile, forbidden)
        except FactCheckFailed:
            # Unchecked text is never used: the letter is built from the profile
            # below, the recruiter message from code.
            log(f"The {field.replace('_', ' ')} could not be fact-checked; "
                "building it from your profile instead.")
            issues.append(f"{field.replace('_', ' ')}: the model's fact-check failed, so it "
                          "was built from profile.json - true but plain")
            best, removed, best_problems = "", [], check("")
        removed_all += [f"{field.replace('_', ' ')}: {r}" for r in removed]
        if removed:
            best_problems = check(best) + checks.gap_claims(best, forbidden)
        if removed and best_problems:
            # Too short after deleting unsupported claims: one rewrite that is told
            # exactly which claims not to make, then fact-checked again.
            log(f"Rewriting the {field.replace('_', ' ')} without the removed claims...")
            avoid = "; ".join(r.split(" (")[0] for r in removed)[:900]
            text = _preclean(call(make() + f"\n\nDo NOT write these unsupported claims: {avoid}",
                                  max_tokens=1200, schema=schema, logprobs=True))
            drafts.append(last_logprobs())
            text = prompts.join_recruiter(text) if schema else text
            try:
                text, removed2 = factcheck(text, facts, call, log, profile, forbidden,
                                           known=[r.split(" (")[0] for r in removed])
                problems2 = check(text) + checks.gap_claims(text, forbidden)
            except FactCheckFailed:
                log("The rewrite could not be fact-checked; keeping the checked first draft.")
                text, removed2, problems2 = "", [], None
            if problems2 is not None and len(problems2) <= len(best_problems):
                best, best_problems = text, problems2
                removed_all += [f"{field.replace('_', ' ')} (rewrite): {r}" for r in removed2]
        if field == "cover_letter" and best and best_problems:
            # Add the true opening and closing BEFORE judging the length: 7 checked
            # body sentences were thrown away as "too short" when the opening and
            # close, added later, would have made a full letter. The model's own
            # text must still carry the letter (3+ sentences, 70+ words).
            own = [s for s in re.split(r"(?<=[.!?])\s+", best) if s.strip()]
            if len(own) >= 3 and len(best.split()) >= 70:
                framed = _frame_letter(best, profile, meta or checks.parse_header(job_description),
                                       log)
                framed_problems = check(framed) + checks.gap_claims(framed, forbidden)
                if len(framed_problems) < len(best_problems):
                    best, best_problems = framed, framed_problems
        if field == "cover_letter" and lang == "en" and (
                not (best or "").strip() or any(p.startswith("cover letter has")
                                                for p in best_problems)):
            # Only true sentences survived, but too few for a letter: a short or
            # one-paragraph letter is a broken letter, so build it from the profile.
            log("Too little of the cover letter survived the fact-check; assembling it "
                "from your profile instead.")
            best = profile_letter(profile, meta or checks.parse_header(job_description),
                                  list(requirements), [g for g in gaps.split(", ") if g])
            built = True
            best_problems = check(best) + checks.gap_claims(best, forbidden)
            if not any(i.startswith("cover letter: the model's fact-check failed") for i in issues):
                issues.append("cover letter: assembled from profile.json because the model's "
                              "draft lost too much to the fact-check - it is true but plain; "
                              "personalise it before sending")
        if field == "recruiter_message":
            # also when nothing usable is left: intro, a real bullet and a question
            best = _complete_recruiter(best or "", profile, list(requirements),
                                       (meta or {}).get("role", ""))
            best = checks.fix_recruiter_question(best)
            best_problems = check(best) + checks.gap_claims(best, forbidden)
        if field == "cover_letter" and best and not built:
            best = _style_pass(best, drafts, facts, call, log, profile, forbidden,
                               [r.split(" (")[0] for r in removed], check)
            # code-only rewording, undone if any code check objects to the result
            fact_lines = [l.lstrip("- ") for l in facts.splitlines() if l.strip()]
            varied = _vary_openings(best, profile)
            if varied != best and not _code_checks(varied, profile, fact_lines, forbidden):
                best = varied
            best = _frame_letter(best, profile, meta or checks.parse_header(job_description), log)
        style = checks.predictability(best or "", _sentence_logprobs(best or "", drafts))
        style["problems"] = checks.style_problems(best or "")
        style["built_from_profile"] = built
        results[f"{field}_style"] = style
        # last, after every check: prose conventions ("one new feature", not "1 new ...")
        results[field] = _polish_prose(best) if best else best
        issues += best_problems
    results["factcheck_removed"] = removed_all
    return results, issues


def _sentence_logprobs(text: str, drafts: list) -> dict:
    """{sentence: [token log-probabilities]} for the sentences of `text` that come
    from a model draft generated with logprobs (kept sentences are cut, unchanged,
    from a draft). Sentences built in code have no entry."""
    out = {}
    for sentence in (s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()):
        probe = sentence.rstrip(".!?")[:60]
        for tokens in drafts:
            joined, starts = "", []
            for tok, lp in tokens:
                starts.append((len(joined), lp))
                joined += tok
            i = joined.find(probe)
            if i >= 0:
                end = i + len(sentence.rstrip(".!?"))
                lps = [lp for pos, lp in starts if i <= pos < end]
                if lps:
                    out[sentence] = lps
                    break
    return out


def _style_pass(text, drafts, facts, call, log, profile, forbidden, known, check):
    """One rewrite of a fact-checked letter for style only (the "write like a
    human" rules), kept only if it passes the fact-check without losing anything,
    still has letter shape, and reads less machine-like."""
    problems = checks.style_problems(text)
    lps = _sentence_logprobs(text, drafts)
    before = checks.predictability(text, lps)
    if not problems and before["label"] == "low":
        return text
    flat = sorted(lps.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))[:2]
    predictable = [s for s, lp in flat if 2.718281828 ** (-sum(lp) / len(lp)) < 2.5]
    log(f"Revising the cover letter's wording (predictability {before['score']}/100, "
        f"{len(problems)} style note(s))...")
    raw = _preclean(call(prompts.style_fix_prompt(text, problems or ["read less predictable"],
                                                  predictable), max_tokens=1200, logprobs=True))
    drafts.append(last_logprobs())
    try:
        fixed, removed = factcheck(raw, facts, call, log, profile, forbidden, known=known)
    except FactCheckFailed:
        log("The style revision could not be fact-checked; keeping the checked letter.")
        return text
    after = checks.predictability(fixed, _sentence_logprobs(fixed, drafts))
    worse_shape = len(check(fixed) + checks.gap_claims(fixed, forbidden)) > len(check(text))
    better = (len(checks.style_problems(fixed)) < len(problems)
              or after["score"] < before["score"])
    if removed or worse_shape or not better:
        why = ("it made claims the fact-check removed" if removed else
               "it broke the letter's shape" if worse_shape else "it did not read better")
        log(f"Style revision dropped ({why}); keeping the checked letter.")
        return text
    log(f"Style revision kept: predictability {before['score']} -> {after['score']}/100.")
    return fixed


# ---------------------------------------------------------------------- report

def build_report(pkg: dict, region) -> str:
    m, kw = pkg["meta"], pkg["keywords"]
    L = [f"# Application report - {m['role'] or 'Target role'}"
         f"{' @ ' + m['company'] if m['company'] else ''}",
         "",
         f"**Candidate:** {m['candidate']}  ",
         f"**Region profile:** {region.label} ({region.code})  ",
         f"**Document language:** {prompts.LANGUAGE_NAMES.get(pkg['lang'], 'English')}  ",
         f"**Generated:** {m['date']}",
         "",
         "## Keyword fit",
         f"**Score:** {kw['score']}/100 (profile vs job ad, computed without the model)",
         ""]
    reqs = pkg.get("requirements") or []
    if reqs:
        cov = planning.coverage(reqs)
        L += [f"**Requirements met:** {cov['required_have']}/{cov['required']} required"
              + (f" (+{cov['required_likely']} possibly)" if cov["required_likely"] else "")
              + f", {cov['preferred_have']}/{cov['preferred']} preferred", "",
              "| Requirement | Priority | Status | Proof in your profile |", "|---|---|---|---|"]
        for r in sorted(reqs, key=lambda r: (r["priority"] != "required", r["status"] != "gap")):
            status = {"have": "✓ have", "alternative": "✓ either/or", "likely": "? check",
                      "gap": "✗ gap"}[r["status"]]
            proof = (r["evidence"][0][:90] if r["evidence"] else "—").replace("|", "/")
            L.append(f"| {r['skill']} | {r['priority']} | {status} | {proof} |")
        L += ["", "Gaps are NOT added to your CV or letter. \"? check\" means the model "
              "linked a profile line - judge it yourself; it is not used in the CV.", ""]
    L += [
         "**Job keywords you have** (only these, where a bullet already contains them, "
         "are brought forward):",
         ", ".join(kw["matched"]) or "none found",
         "",
         "**Job-ad terms not in your profile** (do not claim these; learn them or skip the role):",
         ", ".join(kw["missing"]) or "none",
         ""]
    if kw["reasons"]:
        L += ["**Watch out:** " + "; ".join(kw["reasons"]), ""]
    L += ["## What was generated", *[f"- {n}" for n in pkg["experience_notes"]]]
    L += ["- Everything else (contact, summary, skills, projects, education, languages) "
          "is taken unchanged from profile.json.", ""]
    styles = [(f, pkg.get(f"{f}_style")) for f in ("cover_letter", "recruiter_message")]
    if any(s for _f, s in styles):
        L += ["## Writing style",
              "Predictability 0-100: how machine-like the text reads (sentence rhythm, "
              "repeated openings, stock AI words, formula patterns, and - for sentences the "
              "model wrote - its real perplexity). An estimate, not an AI-detector result.", ""]
        for field, s in styles:
            if not s:
                continue
            name = field.replace("_", " ").capitalize()
            L.append(f"**{name}:** {s['score']}/100 ({s['label']})"
                     + (f", perplexity {s['perplexity']}" if s.get("perplexity") else
                        " - built from your profile, no model perplexity" if s.get("built_from_profile")
                        else "") + f", sentence-length variation {s['sentence_length_variation']}")
            L += [f"- {p}" for p in s.get("problems") or []]
            L += [f"- {n}" for n in s.get("notes") or []]
            L.append("")
    if pkg.get("factcheck_removed"):
        L += ["## Removed by the fact-check (not supported by your profile)",
              *[f"- {r}" for r in pkg["factcheck_removed"]], ""]
    if pkg.get("issues"):
        L += ["## Checks that still fail", *[f"- ⚠ {i}" for i in pkg["issues"]], ""]
    if pkg.get("recruiter_message"):
        L += ["## Recruiter message", pkg["recruiter_message"], ""]
    return "\n".join(L)


# ----------------------------------------------------------------------- output

def slugify(*parts, keep_last: bool = False) -> str:
    """File-name-safe name, at most 60 characters. With keep_last the final part
    (the date) is never cut: "..._ground_robotics_m_f_d_2026_0" lost its date, so
    every run for a job wrote into - and replaced - the same folder."""
    clean = lambda s: re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_").lower()
    parts = [re.sub(r"\((?:[mwfdx]\s*/\s*){2,3}[mwfdx]\)", "", p or "") for p in parts]
    if keep_last and len(parts) > 1:
        tail = clean(parts[-1])
        head = clean("_".join(p for p in parts[:-1] if p))[:60 - len(tail) - 1].strip("_")
        return "_".join(x for x in (head, tail) if x) or "application"
    return (clean("_".join(p for p in parts if p))[:60] or "application")


def write_outputs(pkg, region, profile, outputs_root: Path, style: str = "both",
                  compile_pdf: bool = False) -> Path:
    m = pkg["meta"]
    out = Path(outputs_root) / slugify(m.get("company"), m.get("role"), m["date"], keep_last=True)
    out.mkdir(parents=True, exist_ok=True)

    (out / "report.md").write_text(build_report(pkg, region), encoding="utf-8")
    (out / "tailored_experience.json").write_text(json.dumps(
        {"experience": pkg["resume"]["experience"], "notes": pkg["experience_notes"],
         "keywords": pkg["keywords"]}, indent=2, ensure_ascii=False), encoding="utf-8")
    if pkg.get("recruiter_message"):
        (out / "recruiter_message.txt").write_text(pkg["recruiter_message"], encoding="utf-8")

    lang = pkg["lang"]
    letter = bool(pkg.get("cover_letter"))
    written = []
    if style in ("ats", "both"):
        (out / "cv_ats.tex").write_text(
            latex.render_cv_ats(pkg, region, profile, lang), encoding="utf-8")
        written.append("cv_ats.tex")
        if letter:
            (out / "cover_letter_ats.tex").write_text(
                latex.render_cover_letter_ats(pkg, profile, m["date"], lang), encoding="utf-8")
            written.append("cover_letter_ats.tex")
    if style in ("styled", "both"):
        # Copy first, so the .tex only references a photo/signature that exists.
        found = latex.copy_assets(assets_dir(), out, want_photo=region.photo)
        (out / "cv_styled.tex").write_text(
            latex.render_cv_styled(pkg, region, profile, lang, has_photo=found["photo"]),
            encoding="utf-8")
        written.append("cv_styled.tex")
        if letter:
            main, info, body = latex.render_cover_letter_styled(
                pkg, profile, lang, has_signature=found["signature"])
            (out / "cover_letter_styled.tex").write_text(main, encoding="utf-8")
            (out / "info.tex").write_text(info, encoding="utf-8")
            (out / "body.tex").write_text(body, encoding="utf-8")
            written.append("cover_letter_styled.tex")

    if compile_pdf:
        compile_all(out, written)
    return out


def assets_dir() -> Path:
    """Where profile_pic.png / sig.png live: the user's data folder first
    (%APPDATA%\\EUJobSearch\\assets for the .exe), then next to the code."""
    from linkedin_jobs import data_dir
    for d in (data_dir() / "assets", ROOT / "assets"):
        if d.is_dir():
            return d
    return ROOT / "assets"


def compile_all(out: Path, tex_files):
    engine = shutil.which("pdflatex")
    if not engine:
        print("  pdflatex not found on PATH - skipping PDF compilation.")
        return
    for name in tex_files:
        if not (out / name).exists():
            continue
        try:
            subprocess.run([engine, "-interaction=nonstopmode", "-halt-on-error", name],
                           cwd=out, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=180, check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            print(f"  compiled {name[:-4]}.pdf")
            # LaTeX's helper files only clutter the folder once the PDF exists
            # (the .log is kept when compiling fails, to show why)
            for ext in (".aux", ".out", ".log"):
                (out / (name[:-4] + ext)).unlink(missing_ok=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print(f"  could not compile {name} - see the .log file in the output folder")


# ------------------------------------------------------------------------- run

def load_profile(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _first_chat_model(base_url):
    try:
        models = list_local_models(base_url)
    except PipelineError:
        return ""
    return models[0] if models else ""


def prepare(job_description: str, region_code: str, profile_path: Path = None,
            recruiter: str = "", notes: str = "", model: str = DEFAULT_MODEL,
            api_key: str = None, backend: str = DEFAULT_BACKEND, base_url: str = None,
            max_tokens: int = 1500, log=print) -> dict:
    """Stages 1-4: check the profile, read the job, analyse gaps, plan the edits.
    Nothing is written yet; the returned plan can be reviewed and edited."""
    if not job_description.strip():
        raise PipelineError("Job description is empty.")
    profile = load_profile(profile_path or ROOT / "profile.json")
    errors, warnings = planning.validate_profile(profile)
    for w in warnings:
        log(f"profile.json: {w}")
    if errors:
        raise PipelineError("profile.json has problems:\n- " + "\n- ".join(errors))
    if notes.strip():
        profile = dict(profile, additional_context=notes.strip())
    # "modelA, modelB": the first model judges (requirements, fact-check, bullets,
    # recruiter message); every listed model writes a cover-letter draft, in parallel.
    drafters = [m.strip() for m in (model or "").split(",") if m.strip()]
    model = drafters[0] if drafters else ""
    if drafters:
        # named models are loaded with the right context; no lms commands to type
        ensure_models_loaded(drafters, base_url, log)
    if not model:
        model = _first_chat_model(base_url)
        if model:
            log(f"Using the loaded model: {model}")
    drafters = drafters or [model]
    if len(drafters) > 1:
        log(f"Judge model: {model}; cover-letter drafts by: {', '.join(drafters)}")
    region = get_region(region_code)
    lang, jd_lang = checks.choose_language(job_description, profile)
    meta = checks.parse_header(job_description)
    kw = checks.keyword_report(profile, job_description, meta["role"])
    degree = checks.degree_warning(job_description, profile)
    if degree:
        kw["reasons"] = list(kw["reasons"]) + [degree]

    log(f"Region: {region.label}")
    log(f"Document language: {prompts.LANGUAGE_NAMES[lang]}"
        + (f" (job ad is {prompts.LANGUAGE_NAMES.get(jd_lang, jd_lang)}; profile German level "
           "is below B2)" if jd_lang != lang else ""))
    log(f"Backend: {backend} @ {base_url or DEFAULT_BASE_URL}")

    def call(prompt, max_tokens=max_tokens, schema=None, temperature=0.3, logprobs=False,
             use_model=None):
        return call_model(prompt, model=use_model or model, api_key=api_key, backend=backend,
                          base_url=base_url, max_tokens=max_tokens, schema=schema,
                          temperature=temperature, logprobs=logprobs)

    if degree:
        log(f"Watch out: {degree}.")
    log("Reading the job requirements...")
    if not (meta["role"] and meta["company"]):
        found = planning.job_identity(job_description, call)
        meta = {k: meta[k] or found.get(k, "") for k in meta}
        if meta["role"] or meta["company"]:
            log(f"Read from the ad: {meta['role'] or '(no title)'} at "
                f"{meta['company'] or '(no company)'}")
    # the job's own title and company are not skills to learn ("quantum-systems")
    own = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", " ".join(meta.values()))}
    kw["missing"] = [t for t in kw["missing"]
                     if not all(part in own for part in re.split(r"[^a-z0-9]+", t.lower()) if part)]
    reqs = planning.extract_requirements(job_description, call, log)
    reqs = planning.analyse_gaps(profile, reqs, call, log, job_description,
                                 embed_url=base_url or DEFAULT_BASE_URL) if reqs else []
    plan = planning.make_plan(profile, job_description, reqs, lang, jd_lang)
    log(f"Plan: {sum(p['edit'] for p in plan)} of {len(plan)} bullets contain keywords "
        "the job asks for; the rest stay as written.")
    return {"profile": profile, "region": region, "lang": lang, "jd_lang": jd_lang,
            "meta": meta, "keywords": kw, "requirements": reqs, "plan": plan,
            "job_description": job_description, "recruiter": recruiter, "call": call,
            "drafters": drafters}


def run(job_description: str, region_code: str, profile_path: Path = None,
        outputs_root: Path = None, style: str = "both", recruiter: str = "",
        notes: str = "", model: str = DEFAULT_MODEL, compile_pdf: bool = False,
        api_key: str = None, backend: str = DEFAULT_BACKEND, base_url: str = None,
        max_tokens: int = 1500, cover: bool = True, approve=None, log=print):
    """Tailor the CV (and optionally a cover letter) for one job.

    The CV comes from profile.json; the model only rewords experience bullets,
    and only around keywords each bullet already contains. `approve(prepared)`
    may review the plan: return it (edited) to continue, or None to cancel.
    `notes` is extra truthful context for the cover letter.
    """
    prepared = prepare(job_description, region_code, profile_path, recruiter, notes, model,
                       api_key, backend, base_url, max_tokens, log)
    if approve is not None:
        prepared = approve(prepared)
        if prepared is None:
            raise PipelineError("Cancelled - nothing was written.")
    profile, reqs, lang = prepared["profile"], prepared["requirements"], prepared["lang"]
    meta, kw, call = prepared["meta"], prepared["keywords"], prepared["call"]
    # Off-limits in everything written for you: words of unproven requirements, plus
    # every job-ad term the profile does not contain (the requirement list can miss
    # one - LiDAR was once dropped from it and then claimed in a recruiter message).
    forbidden = planning.gap_terms(reqs, profile) | checks._foreign_terms(
        job_description, checks._profile_blob(profile))

    bullets, exp_notes = tailor_experience(profile, job_description, prepared["plan"], lang,
                                           call, forbidden, log)
    pkg = {
        "meta": {"candidate": profile["personal"]["name"], "role": meta["role"],
                 "company": meta["company"], "city": meta["city"],
                 "recipient": recruiter or "Hiring Manager",
                 "date": date.today().isoformat()},
        "resume": build_resume(profile, bullets),
        "keywords": kw,
        "requirements": reqs,
        "experience_notes": exp_notes,
        "lang": lang,
        "issues": [n[2:] for n in exp_notes if n.startswith("⚠")],
        "cover_letter": "",
        "recruiter_message": "",
    }
    cov = planning.coverage(reqs) if reqs else None
    if cover and cov and cov["required"] >= 3 and cov["required_have"] + cov["preferred_have"] == 0:
        log("Skipping the cover letter and recruiter message: your profile meets none of "
            f"the {cov['required']} required skills, so there is nothing true to argue with.")
        pkg["issues"].append("no cover letter: none of the required skills are in your profile")
        cover = False
    if cover:
        # Extracted requirements only, required first. The keyword fallback put site
        # debris into letters ("I am keen to learn more about quantum-systems",
        # "I do not have experience with prototypes, technical ...").
        gap_names = sorted((r for r in reqs if r["status"] == "gap"),
                           key=lambda r: r["priority"] != "required")
        gap_names = [r["skill"] for r in gap_names][:3]
        texts, text_issues = write_texts(profile, job_description, lang, ", ".join(gap_names),
                                         recruiter, call, log, forbidden, reqs, meta,
                                         drafters=prepared.get("drafters"))
        pkg.update(texts)
        pkg["issues"] += text_issues
        for field in ("cover_letter", "recruiter_message"):
            s = texts.get(f"{field}_style") or {}
            if s:
                log(f"Writing style, {field.replace('_', ' ')}: predictability {s['score']}/100 "
                    f"({s['label']})" + (f", perplexity {s['perplexity']}" if s.get("perplexity")
                                         else "") + (f"; {len(s['problems'])} style note(s)"
                                                     if s.get("problems") else ""))
        for field in ("cover letter", "recruiter message"):
            n = sum(r.startswith(field) for r in texts.get("factcheck_removed", []))
            if n:
                # Deleting sentences keeps the text true but can gut it - say so, so a
                # thin letter is never reported as "all checks passed".
                pkg["issues"].append(f"{field}: the fact-check deleted {n} sentence(s) - reread "
                                     "it; the removed sentences are listed in report.md")
    degree = checks.degree_warning(job_description, profile)
    if degree:
        pkg["issues"].append(degree)

    for n in exp_notes:
        log(n)
    if pkg["issues"]:
        log(f"{len(pkg['issues'])} check(s) still failing - see report.md.")
    out = write_outputs(pkg, prepared["region"], profile, outputs_root or ROOT / "outputs",
                        style=style, compile_pdf=compile_pdf)
    log(f"Written to: {out}")
    return pkg, out
