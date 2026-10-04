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
# The judge (first model listed). Before the separate letter writer (DEFAULT_WRITER)
# this was the pair "qwen/qwen3-4b-2507,llama-3.2-3b-instruct", both drafting letters.
DEFAULT_LOCAL_MODELS = os.environ.get("ATS_MODELS", "qwen/qwen3-4b-2507")
# Context each model is loaded with. Loaded on demand, LM Studio gave Llama its
# maximum (131072), whose working memory filled the GPU so Qwen could not load.
# (LM Studio rounds a requested 6000 up to 6144.)
MODEL_CONTEXT = {"qwen/qwen3-4b-2507": 6144, "llama-3.2-3b-instruct": 6144,
                 "prism-ml/bonsai-27b": 6144}
# Who writes the cover letter and recruiter message, separate from the judge (the
# first model above), which keeps requirements, the fact-check and the CV edits. The
# 3-4B judges invented 5-17 letter sentences per job for the fact-check to delete
# (2026-10-03), so a larger model writes. Comma-separate several to draft in parallel.
DEFAULT_WRITER = os.environ.get("ATS_WRITER", "prism-ml/bonsai-27b")
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
    raw = _at_word(raw)
    return _protect_abbrev(raw).strip()


# Abbreviations in the current job's title and employer ("Stud.", "H.", "W.") and
# the stand-in dot used for them while sentences are split (see write_texts).
_ABBREV = {"toks": []}
# "Stud.", "H." and lower case with inner dots: "Engineer /in, o.ä. (f/m/x)" (DLR)
_ABBREV_TOKEN = re.compile(r"(?<![\w.])(?:[A-Z][a-z]{0,4}|[a-zäöü](?:\.[a-zäöü])+|"
                           r"bzw|ggf|inkl|ca|evtl|usw|etc)\.(?=\s)")
_ABBREV_DOT = "․"


def _protect_abbrev(text: str) -> str:
    for tok in _ABBREV["toks"]:
        text = re.sub(rf"(?<![\w.]){re.escape(tok)}(?=\s)", tok[:-1] + _ABBREV_DOT, text or "")
    return text


def _unprotect(value):
    """Puts the real dots back, in strings, lists and dicts."""
    if isinstance(value, str):
        return value.replace(_ABBREV_DOT, ".")
    if isinstance(value, list):
        return [_unprotect(v) for v in value]
    if isinstance(value, dict):
        return {k: _unprotect(v) for k, v in value.items()}
    return value


def _at_word(text: str) -> str:
    """"Student Assistant (SHK) @ Robotics Lab" -> "... (SHK) at Robotics Lab"; the
    closing bracket kept the "@" in recruiter messages (2026-10-04)."""
    return re.sub(r"(?<=[\w)]) @ (?=\w)", " at ", text or "")


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
                          r"not new information|repeats (an? |the )?(application|stated|known) fact|"
                          # qwen3-4b judge, 2026-10-03: "Stated in facts" (true UART sentence),
                          # "Repeats a fact, not new" (true C++ and ROS2 sentences)
                          r"^\s*(stated|listed|supported|present|found) (in|by) (the )?facts\W*$|"
                          r"repeats (an? |the )?fact\b)",
                          re.I)


def _repeats(sentence: str, known: list) -> bool:
    """Whether a sentence restates most of a claim already removed as unsupported."""
    own = planning._stems(sentence)
    return any(k and len(own & k) >= 0.7 * len(k) for k in known)


# The job ad of the run in progress, for the check that finds ad text copied into a
# letter. Set once per run in write_texts: the app runs one generation at a time,
# and the checks are called from many places, some in parallel draft threads.
_CURRENT_AD = {"text": ""}


def _applying_to() -> str:
    """Employer and role of the current ad: naming them is no invented detail."""
    head = checks.parse_header(_CURRENT_AD["text"] or "") if _CURRENT_AD.get("text") else {}
    return f"{head.get('company', '')} {head.get('role', '')}"


def _code_checks(text: str, profile: dict, fact_lines: list, forbidden=()) -> list:
    """(sentence, why) from every deterministic check."""
    # Deterministic: a sentence naming one organisation but describing another's work
    # (the 8B checker missed "at TH Deggendorf ... UAV for traffic observation").
    return (checks.reader_or_ad_copy(text, _CURRENT_AD["text"])
            + checks.misattributions(text, profile)
            + checks.coursework_mixups(text, profile)
            + checks.invented_details(text, profile, _applying_to())
            + checks.false_denials(text, profile)
            + checks.interest_claims(text, profile)
            + checks.mixed_details(text, profile)
            + checks.merged_claims(text, profile)
            + checks.blended_bullets(text, profile)
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
    # Four content words are enough when every one comes from the profile: "I added one
    # new feature and test case to the computation engine" was deleted by the judge
    # ("not in the facts") although it is the SOCO bullet nearly word for word.
    short = len(checks._content_stems(sentence)) < 5
    if len(checks._content_stems(sentence)) < 4 or _code_checks(sentence, profile,
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
        need = 0.85 if len(stems) >= 3 and not short else 1.0
        share, best = max(((len(stems & l) / len(stems), i) for i, l in enumerate(lines)),
                          default=(0, -1))
        if share < need:
            return False
        # "led", "managed" are verbs, so not counted above: "I led a team to create a
        # robot platform ..." matched the 'Coordinating with teams ...' bullet 100%.
        if _LEAD.search(clause) and not _LEAD.search(fact_lines[best]):
            return False
    return True


def _interests_only(profile: dict) -> set:
    """Research interests that are not also coursework."""
    out = set()
    for ed in profile.get("education") or []:
        courses = " ".join(ed.get("coursework") or []).lower()
        out |= {r for r in ed.get("research_interests") or [] if r.lower() not in courses}
    return out


# The letter writer drafts at a low temperature: prompt-level grounding "achieves zero
# detected hallucinations at low temperature" (Grounded Optimization, 2026); at 0.3
# Bonsai added purposes and results to true bullets.
WRITER_TEMPERATURE = 0.1


def _numbered_facts(facts: str) -> tuple[str, dict]:
    """The fact list with IDs for the writer ("F7: ..."), research interests split
    into their own line ("I1: RESEARCH INTEREST ONLY ..."), and {id: fact}."""
    lines = []
    for line in (l.lstrip("- ").strip() for l in facts.splitlines() if l.strip()):
        if "; research interests: " in line:
            base, rest = line.split("; research interests: ", 1)
            interests, _, thesis = rest.partition("; thesis: ")
            lines.append(("F", base + (f"; thesis: {thesis}" if thesis else "")))
            lines.append(("I", "RESEARCH INTEREST ONLY (not coursework, not experience, "
                               f"not a skill): {interests}"))
        else:
            lines.append(("F", line))
    ids, n = {}, {"F": 0, "I": 0}
    for kind, line in lines:
        n[kind] += 1
        ids[f"{kind}{n[kind]}"] = line
    return "\n".join(f"{k}: {v}" for k, v in ids.items()), ids


def _parse_cited(raw: str) -> list:
    """[[(sentence, {ids})] per paragraph] from a draft with "[F3, F9]" tags. A tag
    belongs to the sentence before it, whether it stands before or after the full
    stop; an untagged sentence gets no IDs."""
    out = []
    for para in re.split(r"\n\s*\n", (raw or "").strip()):
        # "... engine [F9]." -> "... engine. [F9]" so the tag follows its sentence
        para = re.sub(r"(\s*\[[FIJ0-9 ,]+\])\s*([.!?])", r"\2\1", para)
        sents, buf = [], ""
        for part in re.split(r"(\[[FIJ0-9 ,]+\])", para):
            if re.fullmatch(r"\[[FIJ0-9 ,]+\]", part.strip()):
                ids = {i.strip() for i in part.strip("[] ").split(",") if i.strip()}
                pieces = [s for s in re.split(r"(?<=[.!?])\s+", buf.strip()) if s.strip()]
                sents += [(s, set()) for s in pieces[:-1]]
                if pieces:
                    sents.append((pieces[-1], ids))
                buf = ""
            else:
                buf += part
        sents += [(s, set()) for s in re.split(r"(?<=[.!?])\s+", buf.strip()) if s.strip()]
        if sents:
            out.append(sents)
    return out


def _entails(sentence: str, support: list, call) -> bool:
    """One sentence against the few facts it draws on (see prompts.ENTAIL)."""
    if not support:
        return False
    try:
        raw = call(prompts.entail_prompt(support, sentence), max_tokens=200,
                   schema=prompts.ENTAIL_SCHEMA, temperature=0)
        return bool(json.loads(raw).get("supported"))
    except (PipelineError, ValueError, AttributeError):
        return False


def _place_phrase(line: str, profile: dict) -> str:
    """"At SOCO Engineers GmbH" / "In my Autonomous UAV ... project" for a work or
    project fact line, else ""."""
    m = _FACT_LINE.match(line or "")
    if not m:
        return ""
    if m.group("org"):
        org = m.group("org").strip()
        the = "the " if org.split()[0].lower() in _PLACE_ARTICLE else ""
        return f"At {the}{org}"
    return f"In my {m.group('proj').strip()} project"


def _added_tools(sentence: str, facts: list, profile: dict) -> list:
    """Profile tools a sentence names that none of its facts mention: "I automated
    workflows in ANSYS CAD design and simulation using Python" cited only the ANSYS
    bullet. Python is in the profile, so no word check caught it, and the judge
    passed it (KLA, 2026-10-04)."""
    names = set()
    for vals in (profile.get("skills") or {}).values():
        names |= {re.sub(r"\s*\(.*?\)", "", v).strip() for v in vals}
    for e in profile.get("experience") or []:
        names |= set(e.get("tools") or [])
    hay = " ".join(facts)

    def found(name, text):
        return re.search(rf"(?<![\w+]){re.escape(name)}(?![\w+])", text, re.I)
    return sorted(n for n in names if len(n) > 1 and found(n, sentence) and not found(n, hay))


MAX_WORK_FACTS = 4
# a sentence that leans on the one before: "This reduced ...", "The automation cut ..."
_ANAPHOR = re.compile(r"^(?:This|These|That|Those|It|Such|The (?:automation|change|changes|work|"
                      r"result|system|project|effort|tool|script|code))\b", re.I)
_CAPPED = "more than %d work examples" % MAX_WORK_FACTS
_STUDY_TALK = re.compile(r"\b(pursu\w*|stud\w*|degree|course\w*|M\.?\s?Eng|M\.?\s?Sc|B\.?\s?Sc|"
                         r"bachelor\w*|master\w*|thesis|Hochschule|Universit\w*|enrolled)\b", re.I)


_COMPANY_WORDS = r"[A-Z][\w&'․-]*(?:\s+[A-Z0-9&][\w&'.․-]*){0,4}"


def _company_pat(company: str = "") -> str:
    """The employer as the ad names it (also "sewts", lower case), else a few
    capitalised words."""
    return (re.escape(company) + "|" if company else "") + _COMPANY_WORDS


def _role_phrase(text: str, company: str = "") -> str:
    """"... about the NVIDIA 2027 Internships: Autonomous Vehicles and Robotics at
    NVIDIA role." -> "... Robotics role at NVIDIA." (recruiter intro, 2026-10-04)."""
    # The employer is a few capitalised words, nothing else: "[^.,]*" let it run from
    # "(M.Eng ... at Technische Hochschule Deggendorf) writing about the ... (w/m/x)"
    # to "role", which then landed inside the bracket (ZEISS, Magazino 2026-10-04).
    # The ad's own name too: "... | Munich | Graz at sewts role." (sewts).
    return re.sub(rf"\b(the\s+)?([^.]*?\S)\s+at\s+({_company_pat(company)})"
                  r"\s+(role|position)\b",
                  lambda m: f"{m.group(1) or ''}{m.group(2)} {m.group(4)} at {m.group(3)}",
                  text or "", count=1)


def _intro_stop(text: str, company: str = "") -> str:
    """"I am applying for ... at Fraunhofer As an Artificial Intelligence Engineer
    ..." -> "... at Fraunhofer. As an ...": the writer left out the full stop and the
    message counted two sentences, not three (Fraunhofer, Magazino 2026-10-04)."""
    return re.sub(rf"(\b(?:applying for|apply for|writing about|interested in)\b[^.!?]*?"
                  rf"\bat\s+(?:{_company_pat(company)}))(?<![.!?,;:])\s+"
                  r"(?=(?:As an?|At|I|In|During|While)\s)",
                  r"\1. ", text or "", count=1)


def _cited_check(raw: str, ids: dict, call, profile: dict, fact_lines: list,
                 log, proven=frozenset(), req_stems=frozenset()) -> tuple[str, list]:
    """Check each sentence of a cited draft against the facts it cites. A sentence
    that fails is checked once more against its best-matching facts (the writer
    can cite the wrong ID: "I am currently pursuing an M.Eng ..." was cut for citing
    a work bullet, NVIDIA 2026-10-04); if that fails too it is replaced with its
    cited fact in the profile's own words, or removed. An uncited ([J] or untagged)
    sentence that still says what the candidate did or has gets the best-match check.

    Then, in code: a work sentence that names no employer, lab or project gets the
    place of the fact it cites ("At SOCO Engineers GmbH, I wrote ..." - the writer
    dropped every employer, 2026-10-04), and at most MAX_WORK_FACTS work or project
    facts are kept, those proving a job requirement first (it used eight)."""
    paras, removed, used = _parse_cited(raw), [], set()

    def best_facts(flat):
        stems = checks._content_stems(flat)
        ranked = sorted(fact_lines, key=lambda l: -len(stems & checks._content_stems(l)))[:3]
        return [l for l in ranked if stems & checks._content_stems(l)]

    entries = []            # per paragraph: [(sentence, [work fact lines])]
    for para in paras:
        keep = []
        for sentence, cited in para:
            flat = " ".join(sentence.split())
            if flat.endswith("?") or checks._LEARNING.search(flat) or _ENTHUSIASM.search(flat):
                keep.append((flat, []))
                continue
            facts = [ids[i] for i in sorted(cited) if i in ids]
            work = [f for f in facts if _FACT_LINE.match(f)]
            if facts:
                extra = _added_tools(flat, facts, profile)
                if not extra and _entails(flat, facts, call):
                    keep.append((flat, work))
                    continue
                # Cited the wrong ID: the facts it really draws on decide - and give
                # its place, never the wrongly cited one ("At SOCO Engineers GmbH, I am
                # currently pursuing an M.Eng ..." came from a wrong citation).
                support = best_facts(flat)
                if not _added_tools(flat, support, profile) and _entails(flat, support, call):
                    keep.append((flat, [f for f in support[:1] if _FACT_LINE.match(f)]))
                    continue
                why = (f"adds {', '.join(extra)} to a fact that does not mention it" if extra
                       else f"says more than its cited fact(s) {', '.join(sorted(cited))}")
            else:
                if not checks._OWN_WIDE.search(flat):
                    keep.append((flat, []))     # about the job or motivation only
                    continue
                support = best_facts(flat)
                if not _added_tools(flat, support, profile) and _entails(flat, support, call):
                    keep.append((flat, [f for f in support[:1] if _FACT_LINE.match(f)]))
                    continue
                why = "says something about you without citing a fact"
            # repair from the cited bullet, in the profile's own words
            # (a repeat of a line stated elsewhere is dropped later by the clean-up)
            fixed, line_used = "", ""
            for i in sorted(cited):
                line = ids.get(i, "")
                if line and line not in used and _fact_sentence(line, profile):
                    fixed, line_used = _fact_sentence(line, profile), line
                    used.add(line)
                    break
            if fixed:
                keep.append((fixed, [line_used]))
                removed.append(f"{flat} ({why}; replaced with your profile's own line: \"{fixed}\")")
            else:
                removed.append(f"{flat} ({why})")
        entries.append(keep)

    # At most MAX_WORK_FACTS work/project facts: proven ones first, then in order.
    order = []
    for keep in entries:
        for _s, work in keep:
            order += [w for w in work if w not in order]
    # Most relevant first: a requirement's proof counts most, then each requirement
    # word the fact contains. "Proof only" ranked the ANSYS bullet over the C++/Python
    # code bullet for a software role whose C++ proof was the skills line (Ubica,
    # 2026-10-04). Stable: ties keep the writer's order.
    order.sort(key=lambda w: -(3 * (w in proven) + len(checks._content_stems(w) & req_stems)))
    chosen = set(order[:MAX_WORK_FACTS])
    out, prev_place, prev_work, said = [], "", "", set()
    for keep in entries:
        sents = []
        for k, (s, work) in enumerate(keep):
            if work and not (set(work) & chosen):
                removed.append(f"{s} (more than {MAX_WORK_FACTS} work examples; kept the ones "
                               "that best match the job)")
                continue
            # "This reduced test downtime ..." after a sentence about another fact, or
            # opening a paragraph: "This" points at the wrong work (ZEISS, 2026-10-04,
            # the ANSYS result after a UAV sentence). Say the whole fact instead, or
            # drop it when that fact is in the letter already.
            if work and _ANAPHOR.match(s) and (not k or work[0] != prev_work):
                whole = _fact_sentence(work[0], profile)
                if whole and work[0] not in said:
                    removed.append(f"{s} (\"this\" pointed at a different example; "
                                   f"replaced with your profile's own line: \"{whole}\")")
                    s = whole
                else:
                    removed.append(f"{s} (\"this\" pointed at a different example)")
                    continue
            if work:
                prev_work = work[0]
                said.add(work[0])
            place = _place_phrase(work[0], profile) if work else ""
            # name where it happened, unless the sentence or the one before already
            # does - and never on a sentence about studies or a school
            if place and s.startswith("I ") and not _names_place(s, profile) \
                    and place != prev_place and not _STUDY_TALK.search(s):
                s = f"{place}, {s}"
            if work:
                prev_place = place
            sents.append(s)
        if sents:
            out.append(" ".join(sents))
    if removed:
        log(f"Citation check: {len(removed)} sentence(s) removed or repaired.")
    return "\n\n".join(out), removed


def _double_check(texts: dict, profile: dict, requirements: list, meta: dict) -> list:
    """Sentences about the candidate that the model phrased itself - not a near-copy
    of one or two profile lines - for the human to read before sending. Every check
    can pass while a letter still overstates ("hands-on experience building robot
    platforms"); this makes those sentences visible instead of silent."""
    fact_lines = [l.lstrip("- ") for l in candidate_facts(profile, list(requirements),
                                                           meta).splitlines() if l.strip()]
    out = []
    for field, label in (("cover_letter", "cover letter"), ("recruiter_message", "recruiter message")):
        for s in re.split(r"(?<=[.!?])\s+", texts.get(field) or ""):
            s = " ".join(s.split())
            if not s or s.endswith("?") or s in _CLOSING or not re.search(r"\b(I|my|me)\b", s):
                continue
            if checks._LEARNING.search(s) or _ENTHUSIASM.search(s) or _APPLYING.search(s):
                continue
            if len(checks._content_stems(s)) >= 3 and _profile_share(s, fact_lines) < 0.8:
                out.append(f"{label}: {s}")
    return out


# Code findings that only compare words; an entailment check may overrule them.
_SOFT_FINDING = re.compile(r"(adds |no profile line backs)")
def _new_words(finding: str) -> int:
    """How many words a code finding says are new: 'adds components, ensuring,
    proper - not in your profile' -> 3; 'no profile line backs this claim (a, b)' -> 2."""
    m = re.match(r"adds (.+?) - not in your profile", finding) or \
        re.search(r"backs this claim \((.+?)\)", finding)
    return len([w for w in m.group(1).split(",") if w.strip()]) if m else 99


# "Role @ Employer: bullet" and "Project: Name: bullet" lines of candidate_facts().
_FACT_LINE = re.compile(r"^(?:(?P<role>[^@:]+?) @ (?P<org>[^:]+)|Project: (?P<proj>[^:]+)): "
                        r"(?P<bullet>.+)$")


def _fact_sentence(line: str, profile: dict) -> str:
    """A profile line as a first-person sentence, in the profile's own words:
    'At SOCO Engineers GmbH, I wrote and debugged C++/Python code, ...'. Only
    past-tense bullets ("Coordinating with teams ..." would need new words)."""
    m = _FACT_LINE.match(line)
    if not m:
        return ""
    bullet = m.group("bullet").strip().rstrip(".")
    first = bullet.split()[0].lower() if bullet.split() else ""
    if not (first.endswith("ed") or first in checks._IRREGULAR):
        return ""
    body = f"I {bullet[0].lower()}{bullet[1:]}."
    if m.group("org"):
        org = m.group("org").strip()
        the = "the " if org.split()[0].lower() in _PLACE_ARTICLE else ""
        return f"At {the}{org}, {body}"
    return f"In my {m.group('proj').strip()} project, {body}"


_TAIL_TALK = re.compile(r"\b(interest\w*|keen|eager|languages?|native|fluent\w*|German|English|"
                        r"Urdu|learn\w*|welcome|thank\w*)\b", re.I)
_INTRO_TALK = re.compile(r"\b(apply\w*|application|position|role)\b", re.I)


def _add_studies(letter: str, profile: dict) -> str:
    """A paragraph with the current degree's courses and the languages, in the
    profile's own words, before the closing - for a true letter that is short.
    Parts the letter already says are skipped."""
    paras = [p for p in re.split(r"\n\s*\n", letter) if p.strip()]
    if not paras:
        return letter
    flat = " ".join(paras).lower()
    extra = []
    current = next((ed for ed in profile.get("education") or []
                    if (ed.get("status") or "").lower() == "in progress"), None)
    courses = [c for c in (current or {}).get("coursework") or [] if c.lower() not in flat]
    if courses:
        degree = current["degree"].split(" ")[0]
        pairs = _in_pairs(courses)
        extra.append(f"My {degree} coursework includes {pairs[0]}."
                     + (f" It also covers {', as well as '.join(pairs[1:])}." if pairs[1:] else ""))
    if not _LANGUAGE_TALK.search(flat):
        extra += _languages_sentences(profile.get("languages") or [])
    if extra:
        closed = bool(_HAS_CLOSING.search(paras[-1]))
        at = len(paras) - 1 if closed else len(paras)
        if len(paras) + (0 if closed else 1) + 1 > 4:
            paras[at - 1] = paras[at - 1].rstrip() + " " + " ".join(extra)   # at most 4 paragraphs
        else:
            paras.insert(at, " ".join(extra))
    # the closing counts towards the length; framing adds it later anyway
    if not _HAS_CLOSING.search(paras[-1]):
        paras.append(_CLOSING)
    return "\n\n".join(paras)


def _reparagraph(letter: str) -> str:
    """A letter written as one or two blocks -> opening (applying, studies), the work
    examples (split in two when long), and interests/languages/closing."""
    if len([p for p in re.split(r"\n\s*\n", letter) if p.strip()]) >= 3:
        return letter
    sents = [s for s in re.split(r"(?<=[.!?])\s+", " ".join(letter.split())) if s]
    intro = []
    # "At the Robotics Lab, Technische Hochschule Deggendorf, I built ..." names the
    # school but is a work example
    while sents and len(intro) < 3 and not re.match(r"(?:At|As|During|While|In)\b", sents[0]) \
            and (_INTRO_TALK.search(sents[0]) or _STUDY_TALK.search(sents[0])):
        intro.append(sents.pop(0))
    tail = []
    while sents and _TAIL_TALK.search(sents[-1]) and not _STUDY_TALK.search(sents[-1]):
        tail.insert(0, sents.pop())
    half = (len(sents) + 1) // 2 if len(sents) > 4 else len(sents)
    paras = [intro, sents[:half], sents[half:], tail]
    return "\n\n".join(" ".join(p) for p in paras if p)


def _top_up(letter: str, profile: dict, requirements: list, words: int = 150) -> str:
    """Add up to two profile bullets the letter does not use yet, as first-person
    sentences, to the paragraph before the closing one - most relevant first (a
    bullet proving a requirement), then the most recent roles."""
    paras = [p for p in letter.split("\n\n") if p.strip()]
    if len(paras) < 2:
        return letter
    have = checks._content_stems(letter)
    proven = " ".join(e for r in requirements if r.get("status") == "have"
                      for e in r.get("evidence") or [])
    lines = []
    for e in profile.get("experience") or []:
        lines += [f"{e['title']} @ {e['employer']}: {b}" for b in e.get("bullets") or []]
    for p in profile.get("projects") or []:
        lines += [f"Project: {p['name']}: {b}" for b in p.get("bullets") or []]
    lines.sort(key=lambda l: l.split(": ", 1)[-1] not in proven)    # stable: recency kept
    added = []
    for line in lines:
        if len(letter.split()) + sum(len(a.split()) for a in added) >= words or len(added) == 2:
            break
        sentence = _fact_sentence(line, profile)
        own = checks._content_stems(sentence)
        if sentence and own and len(own & have) / len(own) < 0.5:
            added.append(sentence)
            have |= own
    if not added:
        return letter
    # The experience paragraph (the one after the opening), not the last body
    # paragraph, which holds courses, languages and "keen to learn".
    target = 1 if len(paras) >= 3 else len(paras) - 1
    paras[target] = paras[target].rstrip() + " " + " ".join(added)
    return "\n\n".join(paras)


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
    used_facts: set = set()
    verdicts: dict = {}

    def entailed(sentence: str) -> bool:
        """Second, focused opinion: does the sentence follow from the few profile
        lines it draws on? One sentence against 3 lines, not the whole letter
        against the whole profile, where the judge cut true sentences."""
        if sentence in verdicts:
            return verdicts[sentence]
        stems = checks._content_stems(sentence)
        ranked = sorted(fact_lines, key=lambda l: -len(stems & checks._content_stems(l)))
        support = [l for l in ranked[:3] if stems & checks._content_stems(l)]
        ok = False
        if support:
            try:
                raw = call(prompts.entail_prompt(support, sentence), max_tokens=200,
                           schema=prompts.ENTAIL_SCHEMA, temperature=0)
                ok = bool(json.loads(raw).get("supported"))
            except (PipelineError, ValueError, AttributeError):
                ok = False          # no usable answer: the first verdict stands
        verdicts[sentence] = ok
        return ok

    def repair(sentence: str, para_text: str) -> tuple[str, str]:
        """(sentence, fact) rebuilt from the profile line the cut sentence was about,
        or ('', '') when no line matches well or the letter already says it.
        RARR-style repair: a deleted sentence used to leave a hole, and three
        holes sent the letter to the plain profile fallback (DLR, 2026-10-03)."""
        stems = checks._content_stems(sentence)
        if len(stems) < 3:
            return "", ""
        best, share = "", 0.0
        for line in fact_lines:
            m = _FACT_LINE.match(line)
            if not m or line in used_facts:
                continue
            # Matched on what the work was, not where: naming SOCO swapped a made-up
            # "led a team ... in Rust at SOCO" for the unrelated ANSYS bullet.
            # A project's name says what the work was ("Autonomous UAV for Traffic
            # Observation"), so it counts; a job title and employer do not.
            where = checks._content_stems(f"{m.group('role') or ''} {m.group('org') or ''}")
            what = m.group("bullet") + (f" {m.group('proj')}" if m.group("proj") else "")
            work = stems - where
            if len(work) < 2:
                continue
            common = len(work & checks._content_stems(what))
            if common >= 3 and common / len(work) > share:
                best, share = line, common / len(work)
        if share < 0.4:
            return "", ""
        rebuilt = _fact_sentence(best, profile or {})
        if not rebuilt:
            return "", ""
        # Already in the letter: a repair would repeat it.
        have = checks._content_stems(para_text)
        own = checks._content_stems(rebuilt)
        if own and len(own & have) / len(own) >= 0.7:
            return "", ""
        used_facts.add(best)
        return rebuilt, best

    def cut(para, k, sentence, why):
        """Remove a flagged sentence, keeping its first part when only a trailing
        clause overreaches ("I built the manipulator ..., proving my ability to
        run experiments on real robots"): whole-sentence deletion cut a Bonsai
        letter to 16 words, mostly true facts lost with their add-ons. Failing that,
        the sentence is rebuilt from the profile line it was about."""
        head = _salvage(sentence, profile or {}, fact_lines, forbidden)
        if head:
            para[k] = head
            removed.append(f"…{sentence[len(head) - 1:].strip(' ,.')} ({why}; the first part "
                           "of the sentence was kept)")
            return
        letter = " ".join(s for p in paragraphs for s in p if s and s != sentence)
        fixed, fact = repair(sentence, letter)
        para[k] = fixed
        if fixed:
            removed.append(f"{sentence} ({why}; replaced with your profile's own line: "
                           f"\"{fixed}\")")
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
                    # The whole-letter verdict is checked once more, sentence by
                    # sentence; hard code rules below still apply to what it keeps.
                    if entailed(flat):
                        overruled.add(flat)
                        continue
                    cut(para, k, flat, reason)
    # Deterministic: a sentence naming one organisation but describing another's work
    # (the 8B checker missed "at TH Deggendorf ... UAV for traffic observation").
    known_stems = [planning._stems(k) for k in known]
    for para in paragraphs:
        for k, s in enumerate(para):
            if s and known_stems and _repeats(s, known_stems):
                flat = " ".join(s.split())
                # A first-draft deletion can itself be the judge's mistake: a rewrite
                # that restates the profile ("My work as Student Assistant (SHK)
                # involved building and testing a 4-DOF manipulator ...") stays.
                if _near_copy(flat, profile or {}, fact_lines, forbidden):
                    overruled.add(flat)
                    continue
                cut(para, k, flat, "repeats a claim already removed from the first draft")
    hits: dict = {}
    for sentence, why in _code_checks(text, profile or {}, fact_lines, forbidden):
        hits.setdefault(re.sub(r"\s+", " ", sentence), []).append(why)
    for target, whys in hits.items():
        # Word-level findings ("adds covers", "no profile line backs this") only
        # see new words, so a paraphrase trips them; the entailment check decides.
        # The other rules (wrong organisation, interest as coursework, inflated
        # counts or roles, claimed gaps) are never overruled.
        # Only a small difference: one new word ("adds covers"). With more, the 4B
        # judge said "supported" to "... and ensuring proper integration of hardware
        # components" (adds components, ensuring, proper; ZEISS 2026-10-03).
        # A claimed outcome ("adds improv… reliability") is never a paraphrase: "adding
        # one new feature and test case to improve system reliability" was overruled
        # as one new word (DLR, 2026-10-04).
        if all(_SOFT_FINDING.match(w) and "…" not in w and _new_words(w) <= 1 for w in whys) \
                and entailed(target):
            overruled.add(target)
            continue
        for para in paragraphs:
            for k, s in enumerate(para):
                if s and re.sub(r"\s+", " ", s).strip() == target:
                    cut(para, k, target, whys[0])
    if overruled:
        log(f"Kept {len(overruled)} flagged sentence(s) that your profile supports.")
    if any("replaced with your profile's own line" in r for r in removed):
        log(f"Repaired {sum('replaced with' in r for r in removed)} sentence(s) from your "
            "profile instead of deleting them.")
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
    # A bullet that proves a requirement; else the most recent past-tense bullet. With
    # only a language proven (BMW PhD), no bullet qualified and the message went out
    # with no evidence sentence: "has 2 sentence(s) (needs exactly 3)".
    fallback = ""
    for e in profile.get("experience") or []:
        for b in e.get("bullets") or []:
            first = b.split()[0].lower() if b.split() else ""
            past = first.endswith("ed") or first in checks._IRREGULAR
            the = "the " if str(e["employer"]).split()[0].lower() in _PLACE_ARTICLE else ""
            sentence = f"At {the}{e['employer']}, I {b[0].lower()}{b[1:].rstrip('.')}."
            if past and b in proven:
                return sentence
            if past and not fallback:
                fallback = sentence
    return fallback


def _intro_sentence(profile: dict, role: str) -> str:
    """'I am a Mechatronics Student (M.Eng ... at ...) writing about the X role.'"""
    title = (profile.get("personal") or {}).get("title") or "candidate"
    current = next((e for e in profile.get("education") or []
                    if str(e.get("end", "")).lower() == "present"), None)
    study = f" ({current['degree']} at {current['institution']})" if current else ""
    about = f"the {role} role" if role else "this role"
    article = "an" if title[:1].lower() in "aeiou" else "a"
    return f"I am {article} {title}{study} writing about {about}."


# A fact line pasted as the recruiter's evidence: "I have B.Sc Electrical Engineering:
# Power Electronics, Circuit Design, ... from GIK ..." (Ubica, 2026-10-04). The
# evidence must be a piece of work, so the code-built one from a real bullet replaces it.
_PASTED_FACT = re.compile(r"\b(?:B\.?\s?Sc|M\.?\s?Eng|M\.?\s?Sc|Skills|Education|Programming "
                          r"Languages|Software Tools|Hardware Tools|coursework|Languages)\b"
                          r"[^.]{0,60}:\s", re.I)


def _complete_recruiter(text: str, profile: dict, requirements: list, role: str = "") -> str:
    """Rebuild intro + evidence + question from what the fact-check left, filling
    a missing part in code: the intro from the profile, the evidence from a real
    bullet, the question from a plain template. (Bonsai once left only the
    question; always inserting evidence once repeated the manipulator bullet.)"""
    parts = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    plain = lambda t: re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", t)).strip().lower()
    if len(parts) == 3 and parts[2].endswith("?") \
            and re.search(r"\b(i|i'm|i am|my)\b", parts[0], re.I) \
            and re.search(r"\b(i|my)\b", parts[1], re.I) \
            and (not role or plain(role) in plain(parts[0])) \
            and len(planning._stems(parts[1]) & planning._stems(parts[0])) < \
            0.6 * max(1, len(planning._stems(parts[1]))) \
            and not _PASTED_FACT.search(parts[1]):             # evidence, not the intro again
        return text
    question = next((s for s in reversed(parts) if s.endswith("?")),
                    "Could you tell me more about the team's current work?")
    intro = next((s for s in parts if re.match(r"\s*(i am|i'm)\b", s, re.I)),
                 _intro_sentence(profile, role))
    # The model's intro must name the role as the ad titles it: it wrote "Doctorand/in
    # elektrischer Antriebe - mit Fokus auf innovative Regelungsverfahren ..." for the
    # BMW title despite the prompt (2026-10-03). Compared without "(w/m/x)" notes.
    if role and plain(role) not in plain(intro):
        intro = _intro_sentence(profile, role)
    # A kept sentence without "I"/"my" ("Developed and tested ...") is a broken
    # sentence: the code-built evidence replaces it.
    # Never another intro: when the code's intro replaced the model's (title not exact),
    # the model's "I am ..." sentence was taken as the evidence, dropped as a repeat of
    # the intro, and the message went out as intro + question (Fraunhofer, Thales,
    # 2026-10-03: "has 2 sentence(s) (needs exactly 3)").
    rest = [s for s in parts if s not in (intro, question) and re.search(r"\b(i|my)\b", s, re.I)
            and not re.match(r"\s*(i am|i'm)\b", s, re.I)]
    repeats = lambda e: len(planning._stems(e) & planning._stems(intro)) >= \
        0.6 * max(1, len(planning._stems(e)))
    usable = lambda e: not repeats(e) and not _PASTED_FACT.search(e)
    evidence = rest[0] if rest and usable(rest[0]) else _evidence_sentence(profile, requirements)
    if not evidence or repeats(evidence):
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


# What an experience "did" for the candidate, past and present tense.
_GAVE_ME = (r"(strengthen(?:s|ed)|taught|teaches|prepare[sd]|give[sn]|gave|provide[sd]|"
            r"equip(?:s|ped)|help(?:s|ed)|enable[sd]|allow(?:s|ed)|hone[sd]|sharpen(?:s|ed)|"
            r"deepen(?:s|ed)|improve[sd]|shape[sd]|la(?:id|ys)|instill(?:s|ed)|reinforce[sd]|"
            r"solidifie[sd]|broaden(?:s|ed)|enhance[sd]|foster(?:s|ed)|cultivate[sd]|"
            r"ground(?:s|ed)|demonstrates|showcases|shows|shown|showed|proves|proven|proved|"
            r"supports|underpins|"
            # plural subjects: "These projects demonstrate my ability ..." (BMW, 2026-10-03)
            r"demonstrate|showcase|show|prove|highlight|highlights|reflect|reflects|"
            r"illustrate|illustrates|underline|underlines|"
            r"built|builds|developed|develops)\b")
# Sentences that fill space without saying anything about the candidate. From the
# DLR letter: "I'm familiar with XML documentation ..., which could be useful for
# this role", "I am keen to learn about transferable skills such as ...".
_PADDING = re.compile(
    r"\b(which (could|would|might|may) be (useful|helpful|valuable|beneficial)|"
    r"transferable skills|in addition to the above|as mentioned (above|before|earlier))\b"
    # Inference about past work, which no profile line states and the fact-check,
    # reading first-person claims, let through: "These tasks required understanding
    # of real-time processing and system reliability." (DLR letter)
    r"|^(these|this|those|such|both)\s+(tasks?|experiences?|projects?|roles?|work|positions?)\s+"
    r"(required|involved|demanded|needed|called for|relied on)\b"
    # Recap of a sentence already written: "My experience as an Artificial Intelligence
    # Engineer at SOCO Engineers GmbH has given me hands-on experience with C++ and Python"
    r"|^my (experience|role|time|work|position)s?\s+(as|at|in)\b.*\b(has|have)\s+"
    r"(given|provided|taught|equipped|allowed)\b"
    # What a past role, project or study did FOR the candidate is the model's
    # conclusion, never a profile fact: "These experiences strengthened my ability to
    # implement control algorithms" (BMW), "my studies ... have provided a solid
    # foundation for designing real-time systems", "This experience taught me the
    # importance of effective communication" (DLR, 2026-09-30).
    r"|\b(this|these|that|those|such|both|my|the|each|all)\s+(?:[\w'’-]+\s+){0,6}?"
    r"(experiences?|work|roles?|projects?|tasks?|studies|coursework|courses|background|"
    r"education|degree|programs?|programmes?|time|positions?|internships?|jobs?|"
    # "My research interests in SLAM ... have equipped me with the skills" (BMW)
    r"interests?|skills?|knowledge|expertise)\b"
    # Up to 160 characters between the noun and the verb, and present tense too: "My
    # coursework in Autonomous Systems, AR/VR Human Machine Interface, Additive
    # Manufacturing ... has given me" (DLR) and "My background in control systems ...
    # provides a foundation" (BMW) both got past a 60-character, past-tense rule.
    # ... but not across an "I": in "In my role as a student assistant ..., I built"
    # the one who built is the candidate, which is a claim the fact-check reads.
    r"(?:(?!\bI\b)[^.!?]){0,160}?\b(?:has\s+|have\s+|had\s+)?(?:also\s+|further\s+)?" + _GAVE_ME
    # A trailing clause doing the same: "... coursework in Control Systems, which
    # provided a foundation in the regulation of electric drives" (BMW recruiter)
    + r"|,\s*(which|that|and this|this)\s+(?:has\s+|have\s+|also\s+)*" + _GAVE_ME
    + r"\s+(me|my|a|an|the|strong|solid)\b"
    # "I am confident in my ability to work with C++ programming, as evident from my
    # experience ..." (DLR, 2026-10-03)
    r"|\bi am confident (in|that) my\b|\bas (is )?evident(ced)? (from|by|in) my\b"
    # Future tense: "I believe that this knowledge will enable me to make a significant
    # contribution" (DLR, 2026-10-03)
    r"|\b(this|that|these|those|such|my|the)\s+(?:[\w'’-]+\s+){0,3}?(knowledge|skills?|skill set|"
    # "this role will enable me to contribute ..." (BMW, 2026-10-03)
    r"expertise|understanding|experience|background|training|learning|role|position|"
    r"opportunity|program|programme|work)\s+(will|would|should|"
    r"can|could|may|might)\s+(?:also\s+)?(enable|help|allow|prepare|equip|make|let)\b"
    # "My studies ... have focused on control systems, which align with the core
    # technical challenges of electric drive development" (BMW, 2026-10-03)
    r"|,\s*(?:which|that|and this|this)\s+(?:also\s+|directly\s+|closely\s+)?(align|aligns|match|"
    r"matches|fit|fits|relate|relates|connect|connects|correspond|corresponds)\s+"
    r"(?:well\s+|closely\s+|directly\s+|perfectly\s+)?(with|to)\b"
    # High-fit letters (2026-10-03): "..., which provides foundational knowledge in image
    # processing", "..., which demonstrates direct experience with C++", "..., which are
    # essential tools in the field", "my experience ... aligns well with this role".
    r"|,\s*(?:which|that|and this|this)\s+(?:also\s+|directly\s+)?(provides?|demonstrates?|"
    r"shows?|proves?|enables?|allows?|supports?|equips?|gives?|reflects?|highlights?|"
    r"underlines?)\b"
    r"|,\s*which\s+are\s+(essential|important|key|relevant|crucial|vital|valuable)\b"
    r"|\b(aligns?|fits?)\s+(?:very\s+)?(?:well|perfectly|closely|directly|nicely)?\s*with\s+"
    r"(this|the)\s+(role|position|job|requirements)\b"
    # "My experience writing C++ code aligns with the requirement for C++ programming
    # knowledge" (Fraunhofer, 2026-10-03): kept once the profile overrule stopped the
    # judge deleting true sentences, so the matching talk is code's to drop.
    r"|\b(aligns?|fits?|matches|meets|fulfil?ls?|satisf(?:y|ies))\s+(?:also\s+)?(?:with\s+)?"
    r"(?:the|this|your|its|their)\s+(?:\w+\s+)?(requirements?|criteri(?:on|a))\b"
    # "... position at BMW AG, as it aligns with my studies in ..." (BMW, 2026-10-03)
    r"|,\s*(?:as|since|because)\s+(?:it|this|the role|the position)\s+(?:closely\s+|directly\s+)?"
    r"(aligns|fits|matches|relates|corresponds|connects)\b"
    # "..., which will enable me to work effectively in this position" (BMW, 2026-10-03)
    r"|,\s*which\s+(?:will|would|could|can|should)\s+(?:also\s+)?(enable|help|allow|prepare|"
    r"equip|let)\s+me\b"
    # "..., which I believe would be an excellent addition to my skill set" (BMW)
    r"|,\s*which\s+(?:i\s+believe\s+)?(?:would|will|could|can)\s+be\s+(?:an?\s+)?"
    r"(excellent|great|valuable|useful|good|important|welcome)\s+(addition|asset)\b"
    # "This background supports my ability to work on robotics hardware platforms"
    r"|\b(supports?|underpins?)\s+my\s+(ability|capacity|readiness|suitability)\b"
    # "... make me an ideal candidate for this position" (BMW, 2026-10-03)
    r"|\bmakes?\s+me\s+(an?\s+)?(ideal|strong|perfect|excellent|great|good|suitable|"
    r"well-suited|right)\s+(candidate|fit|match)\b", re.I)
# Saying which role again, mid-letter: "I am applying for the doctoral position in
# direct flux control ..." after an opening that named the German title (BMW).
_APPLYING = re.compile(r"\b(i am|i'm)\s+applying\b|\bplease consider my application\b|"
                       r"\b(to|i)\s+apply\s+(for|to)\b|\bas i apply\b", re.I)
# A sentence about the work rather than by the candidate ("The change was consistent
# across all cycles and did not require rework.", "This work was part of a larger
# system update." - DLR, 2026-10-03). The fact-check reads "I ..." claims, so these
# passed; they stay only when a profile line backs most of their words.
_THIRD_PERSON = re.compile(r"^(this|that|the|these|those|it|they|such)\b", re.I)
# A closing summary says nothing new: "In summary, I am excited about the opportunity
# to join BMW AG and contribute my skills ..." (BMW, 2026-10-03)
_SUMMARY = re.compile(r"^(in summary|in conclusion|overall|to sum up|to summari[sz]e|"
                      r"ultimately|in short|all in all|in closing)\b", re.I)
# Leadership the profile may not show: "co-led a team to develop a robot platform"
# for "Coordinating with teams across different domains" (DLR, 2026-10-03).
_LEAD = re.compile(r"\b(co-led|co-lead|led|lead|leading|managed|managing|headed|spearheaded|"
                   r"directed|supervised|oversaw|orchestrated)\b", re.I)


def _honest_lead(sentence: str, profile: dict) -> str | None:
    """The sentence when the profile line it describes shows leadership too; with
    "worked with" when it does not and the object is a team; else None."""
    if not _LEAD.search(sentence):
        return sentence
    # Any related profile line that shows leadership too: the best word match for "I
    # managed teams for future competitions" was a different competition bullet.
    # It must be the line the sentence is about, though: matching at least as well as
    # any other line, or "co-led a team ... robot platform" passed on the one word
    # "robot" it shares with the competition bullet.
    stems = checks._content_stems(sentence)
    scores = [(len(stems & checks._content_stems(f"{label} {text}")), bool(_LEAD.search(text)))
              for label, text in planning.evidence_units(profile)]
    best_other = max((n for n, lead in scores if not lead), default=0)
    if any(lead and n >= max(2, best_other) for n, lead in scores):
        return sentence                      # "managed teams for future competitions"
    fixed = re.sub(r"\b(co-led|co-lead|led|managed|headed|spearheaded|directed|supervised|"
                   r"oversaw|orchestrated)\s+(?=(a|the|cross-domain|multidisciplinary|"
                   r"interdisciplinary)?\s*(team|teams)\b)", "worked with ", sentence, flags=re.I)
    return None if _LEAD.search(fixed) else fixed


_COORD_TEAMS = re.compile(r"\b(coordinat(?:ed|ing|e|es))\s+(?=(?:the\s+|multiple\s+|several\s+)?"
                          r"(?:cross-domain\s+|multidisciplinary\s+)?teams?\b)", re.I)


def _with_teams(sentence: str, profile: dict) -> str:
    """'I coordinated teams across different domains' -> 'I coordinated with teams
    ...' when the profile says "Coordinating with teams": without "with" it reads as
    leading them (4 of 5 letters, 2026-10-03). Left alone if the profile itself
    says the candidate coordinated teams."""
    if not _COORD_TEAMS.search(sentence):
        return sentence
    lines = " ".join(t for _l, t in planning.evidence_units(profile))
    if _COORD_TEAMS.search(lines) or not re.search(r"\bcoordinat\w*\s+with\s+teams?\b", lines, re.I):
        return sentence
    return _COORD_TEAMS.sub(lambda m: f"{m.group(1)} with ", sentence)


def _backed_by_profile(sentence: str, profile: dict) -> bool:
    stems = checks._content_stems(sentence)
    if len(stems) < 3:
        return True                     # too short to judge ("This took a month.")
    lines = [f"{label} {line}" for label, line in planning.evidence_units(profile)]
    best = max((len(stems & checks._content_stems(l)) for l in lines), default=0)
    return best >= 0.5 * len(stems)
# Office software is no gap worth a sentence: "I am keen to learn Microsoft Office."
_TRIVIAL_GAP = re.compile(r"\b(microsoft office|ms office|excel|powerpoint|outlook|"
                          r"word processing)\b", re.I)
# Soft skills the models add ("gained experience in collaboration, communication,
# and project management"): kept only when the profile itself names them.
_SOFT_SKILL = re.compile(r"\b(project management|leadership|stakeholder management|"
                         r"time management|people management|communication skills?|"
                         # "I possess strong analytical and problem-solving abilities" (ZEISS)
                         r"analytical)\b", re.I)
# Talk about spoken languages. The models turned "German: Beginner" into "I am keen
# to learn German language, but I have already demonstrated proficiency in English"
# (BMW letter); the profile's own plain sentence replaces it.
_LANGUAGE_TALK = re.compile(
    r"\b(german|english|deutsch|englisch|french|spanish|urdu|mother tongue|native speaker)\b"
    # "As a strong background holder of both German and English languages, I am
    # confident in effectively communicating ..." (BMW, 2026-10-03: German is beginner)
    r"[^.]*\b(learn\w*|proficien\w*|fluen\w*|demonstrat\w*|level|speak\w*|skills?|languages?|"
    r"communicat\w*|command|holder|confident|background|knowledge|master\w*)\b"
    r"|\b(learn\w*|proficien\w*|fluen\w*|speak\w*)\b[^.]*\b(german|english|deutsch|englisch)\b",
    re.I)


# An outcome clause: ", which improved team understanding of data flow" was added to
# the XML documentation bullet (DLR, 2026-10-03); ", which reduced test downtime by
# three minutes per cycle" is the profile's own result and stays.
_OUTCOME = re.compile(r",\s*(?:which|that|and this|this)\s+(?:also\s+|further\s+|greatly\s+|"
                      r"significantly\s+)?[a-z]+ed\b", re.I)


def _cut_unbacked_outcome(sentence: str, profile: dict) -> str:
    m = _OUTCOME.search(sentence)
    if not m:
        return sentence
    # Judged even when short: "which improved team understanding of data flow" has
    # just two content words, and the general rule leaves under three alone.
    stems = checks._content_stems(sentence[m.start() + 1:])
    lines = [f"{label} {line}" for label, line in planning.evidence_units(profile)]
    best = max((len(stems & checks._content_stems(l)) for l in lines), default=0)
    if not stems or best >= max(1, 0.5 * len(stems)):
        return sentence
    head = sentence[:m.start()].rstrip(" ,")
    return head + "." if len(head.split()) >= 5 else sentence


def _strip_inference(sentence: str) -> str | None:
    """The sentence without its padding or inference, or None to drop it. A trailing
    clause is cut and the true first half kept: "I completed a Bachelor's degree in
    Electrical Engineering with coursework in Control Systems, which provided a
    foundation in the regulation of electric drives." -> "... in Control Systems."
    (BMW recruiter message, 2026-10-03)."""
    m = _PADDING.search(sentence)
    if not m:
        return sentence
    cut = max(sentence.rfind(", ", 0, m.start()), sentence.rfind("; ", 0, m.start()),
              sentence.rfind(" - ", 0, m.start()),
              m.start() if sentence[m.start()] == "," else -1)     # ", which provided ..."
    head = sentence[:cut].rstrip(" ,;-") if cut > 0 else ""
    # Cut only at a side clause ("..., which ...", "..., as it ..."). At the main verb,
    # "My work as a Student Assistant at the Robotics Lab, Technische Hochschule
    # Deggendorf, has given me ..." left a verbless fragment (ZEISS, 2026-10-03).
    tail = sentence[cut:].lstrip(" ,;-") if cut > 0 else ""
    if not re.match(r"(which|that|and this|this|as|since|because|so)\b", tail, re.I):
        return None
    # A leading "As I apply for ..., my studies have provided ..." leaves "As I apply
    # for the ... position at BMW AG." - a fragment, not a sentence (2026-10-03).
    if re.match(r"(as|while|since|because|when|although|though|if|whereas|after|before|"
                r"given that|now that)\b", head, re.I):
        return None
    if len(head.split()) >= 6 and not _PADDING.search(head):
        return head + "."
    return None


# "Here, I successfully automated workflows in ANSYS" after a Robotics Lab sentence
# put SOCO's work at the lab; "There, I coordinated ..." after a SOCO sentence put
# the lab's work at SOCO (BMW letters, 2026-10-03).
_DEICTIC = re.compile(r"^(?:here|there|in (?:this|that|the same) (?:role|position|job|lab|company|"
                      r"team)|in (?:these|those|both) (?:roles|positions|jobs)|"
                      r"at the same (?:place|company|lab))\s*,?\s*", re.I)
_PLACE_ARTICLE = ("robotics", "research", "institute", "lab", "laboratory", "university",
                  "department", "chair", "centre", "center")


def _learns_what_profile_has(sentence: str, profile: dict) -> bool:
    """'I am keen to learn more about working in a team to create XML documentation
    for different variables' - what follows "learn" is mostly a profile line
    (DLR, 2026-10-03); checks.learning_known reads only the first words after it."""
    m = re.search(r"\b(?:keen|eager|excited|want|hope|looking forward|interested)\s+(?:to\s+)?"
                  r"learn\w*\s+(.+)", sentence, re.I)
    if not m:
        return False
    stems = checks._content_stems(m.group(1))
    if len(stems) < 2:
        return False
    best = max((len(stems & checks._content_stems(line))
                for _label, line in planning.evidence_units(profile)), default=0)
    return best >= max(2, 0.5 * len(stems))


def _names_place(text: str, profile: dict) -> bool:
    """The text names an employer or project itself ("at SOCO Engineers GmbH")."""
    low = text.lower()
    names = [str(e.get("employer", "")).split(",")[0] for e in profile.get("experience") or []]
    names += [str(p.get("name", "")) for p in profile.get("projects") or []]
    return any(n and n.lower() in low for n in names)


def _place_of(text: str, profile: dict, strict: bool = False) -> str:
    """Where the work in `text` happened, as a phrase ("At SOCO Engineers GmbH", "In
    the XR Simulation of Factory project"), or "" if unclear: an employer named in
    the text, else the experience or project line sharing the most words with it."""
    def phrase(label: str) -> str:
        if " @ " in label:
            employer = label.split(" @ ", 1)[1]
            the = "the " if employer.split()[0].lower() in _PLACE_ARTICLE else ""
            return f"At {the}{employer}"
        if label.startswith("Project: "):
            return f"In the {label[len('Project: '):]} project"
        return ""
    low = text.lower()
    for e in profile.get("experience") or []:
        name = str(e.get("employer", "")).split(",")[0].strip().lower()
        if name and name in low:
            return phrase(f"{e['title']} @ {e['employer']}")
    stems = checks._content_stems(text)
    # strict: adding a place nobody wrote needs a clear match (3+ shared words)
    best, label = (2 if strict else 1), ""
    for lab, line in planning.evidence_units(profile):
        if " @ " not in lab and not lab.startswith("Project: "):
            continue
        n = len(stems & checks._content_stems(line))
        if n > best:
            best, label = n, lab
    return phrase(label) if label else ""


def _fix_place(sentence: str, previous: str, profile: dict) -> str:
    """_place_fix, unless the place it adds makes the sentence misattributed: "I also
    prepared safety documentation for operations and helped mentor students ..."
    mixed POF and Robotics Lab work, and became "At the Robotics Lab, ..., I also
    prepared safety documentation ..." after every check had run (ZEISS, 2026-10-03)."""
    fixed = _place_fix(sentence, previous, profile)
    if fixed != sentence and checks.misattributions(fixed, profile):
        return sentence
    return fixed


def _place_fix(sentence: str, previous: str, profile: dict) -> str:
    """Replace "Here," / "There," / "In this role," with the real employer or project
    when the sentence's work is not where the previous sentence was."""
    m = _DEICTIC.match(sentence)
    if not m:
        # "I also automated ANSYS simulation workflows" right after two Robotics Lab
        # sentences read as lab work; it was SOCO's (DLR, 2026-10-03). Only when the
        # sentence names no place, matches one profile line clearly, and the previous
        # sentence names a different place.
        also = re.match(r"(I (?:also|additionally|further|then)\b)", sentence)
        if not also or not previous or _names_place(sentence, profile):
            return sentence
        here, before = _place_of(sentence, profile, strict=True), _place_of(previous, profile)
        if here and before and here != before:
            return f"{here}, {sentence}"
        return sentence
    rest = sentence[m.end():]
    # Also when the previous sentence names no place (or was removed): "In these
    # roles, I built and tested a 4-DOF manipulator ..." pointed at nothing.
    here, before = _place_of(rest, profile), _place_of(previous, profile) if previous else ""
    if not here or here == before:
        return sentence
    rest = rest if rest.startswith("I ") else rest[:1].lower() + rest[1:]
    return f"{here}, {rest}"


def _tidy_recruiter(text: str, profile: dict) -> str:
    """The letter's inference and place rules for the recruiter message, sentence by
    sentence; _complete_recruiter refills a part this leaves empty."""
    out, prev = [], ""
    for s in (x for x in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if x):
        s = _strip_inference(s) if not s.rstrip().endswith("?") else s
        if not s:
            continue
        s = _with_teams(_fix_place(s, prev, profile), profile)
        out.append(s)
        prev = s
    return " ".join(out)


# German pasted from the ad into an English letter: "... the company's expertise in
# Premium-Finanz- und Mobilitätsdienstleistungen, particularly in innovative
# Regelungsverfahren für Synchronmotoren" (BMW, 2026-10-03).
_GERMAN_WORD = re.compile(r"\b(und|für|der|die|das|mit|von|im|zur|zum|bei|oder|sowie)\b|"
                          r"\b\w*[äöüß]\w*\b", re.I)


# Spelled the same in German and English ads, so not a sign of copied German.
_SHARED_WORDS = set("""position positions innovative interesse projekte projects
mentoring onboarding software hardware simulation prototyping company research
""".split())


def _copies_german(sentence: str, allowed: set) -> bool:
    """German words in the sentence that are not part of the job title or the
    profile (an institution such as "Technische Hochschule" is fine)."""
    return any(m.group(0).lower() not in allowed for m in _GERMAN_WORD.finditer(sentence))


def _tidy_letter(text: str, profile: dict, role: str = "", ad: str = "",
                 skills: tuple = ()) -> str:
    """Code-only, after every model step: drop padding and sentences that repeat an
    earlier one (the DLR letter told the robot-platform story twice), give spoken
    languages the profile's plain wording once, and write "at" for "@" in prose.
    Only removes or swaps in profile wording, so it adds no claim. Keeps the text as
    it was when fewer than three sentences would remain."""
    langs = _languages_sentences(profile.get("languages") or [])
    blob = checks._profile_blob(profile)
    english = checks.text_language(text or "") == "en"
    allowed = {w.lower() for w in re.findall(r"\w+", f"{role} {blob} {' '.join(skills)}")}
    # Words of a German ad that an English letter may not borrow: "Regelungsverfahren",
    # "Steuerungskonzepte", "Antriebstechnologie" (BMW, 2026-10-03) carry no umlaut.
    # Tool names stay usable: they are in the requirement names ("Matlab Simulink").
    summary = re.sub(r"\s+", " ", str(profile.get("profile_summary", ""))).lower()
    known_learn = {sent for sent, _why in checks.learning_known(text or "", profile)}
    # The ad body only: the "Position:" header line made "position" count as German.
    body = checks.jd_excerpt(ad or "", limit=20_000)
    ad_words = ({w.lower() for w in re.findall(r"[A-Za-zÄÖÜäöüß-]{7,}", body)} - allowed
                - _SHARED_WORDS if english and ad and checks.text_language(ad) == "de" else set())
    seen, lang_done, applied, out, kept, prev = [], False, False, [], 0, ""
    exact: set = set()
    for p_no, para in enumerate(re.split(r"\n\s*\n", (text or "").strip())):
        sents = []
        for s in (x for x in re.split(r"(?<=[.!?])\s+", para.strip()) if x):
            s = _keen_not_lack(_strip_inference(s))
            if not s or _SUMMARY.match(s):
                continue
            # "C++ is required for this internship." says nothing about the candidate
            # (NVIDIA letter: three of these, 2026-10-04).
            if _JOB_ONLY.search(s) and not re.search(r"\b(I|my|me)\b", s):
                continue
            s = _trim_tools(s, profile, skills)
            if not s:
                continue
            # Word for word again: "I added one new feature and test case to that
            # engine." twice (Fraunhofer, 2026-10-04) - 3 content words, under the
            # 4 the "mostly said already" rule below needs.
            key = re.sub(r"\W+", " ", s.lower()).strip()
            if key in exact:
                continue
            exact.add(key)
            s = _honest_lead(s, profile)
            if not s:
                continue
            s = _with_teams(s, profile)
            s = _fix_place(s, prev, profile)
            if _APPLYING.search(s):
                # Once, and only in the opening paragraph: the standard opening is
                # added there afterwards when the letter has none.
                if applied or p_no > 0:
                    continue
                applied = True
            if _TRIVIAL_GAP.search(s) and re.search(r"\b(learn\w*|keen|eager)\b", s, re.I):
                continue
            if _THIRD_PERSON.match(s) and not _backed_by_profile(s, profile):
                continue
            # A résumé fragment pasted from the profile summary: "Aspiring Mechatronics
            # Engineer focused on robotics, AI and autonomous systems with a strong
            # educational background ..." (BMW, 2026-10-03) - no verb, not a sentence.
            if summary and not re.search(r"\b(i|my|me)\b", s, re.I) \
                    and s.lower()[:40].rstrip(" .") in summary:
                continue
            # "keen to learn ... XML documentation", which the profile already shows
            if s in known_learn or _learns_what_profile_has(s, profile) or _VAGUE_LEARN.search(s):
                continue
            if english and (_copies_german(s, allowed) or any(
                    w.lower() in ad_words for w in re.findall(r"[A-Za-zÄÖÜäöüß-]{7,}", s))):
                continue
            s = _cut_unbacked_outcome(s, profile)
            if any(m.group(0).lower() not in blob for m in _SOFT_SKILL.finditer(s)):
                continue
            if _LANGUAGE_TALK.search(s):
                if not lang_done and langs:
                    sents += langs
                    lang_done = True
                continue
            stems = checks._content_stems(s)
            # Mostly said already (70% of its words appear in earlier sentences).
            # Measured on the new sentence: compared with the shorter one, the whole
            # coursework sentence went because it shares "electrical engineering"
            # with "My bachelor's degree is in electrical engineering".
            said = set().union(*seen) if seen else set()
            if len(stems) >= 4 and len(stems & said) >= 0.7 * len(stems):
                continue
            seen.append(stems)
            sents.append(re.sub(r"\s@\s", " at ", s))
            prev = s
        if sents:
            out.append(" ".join(sents))
            kept += len(sents)
    return _place_flow("\n\n".join(out), profile) if kept >= 3 else text


def _with_article(text: str, profile: dict) -> str:
    """"As Student Assistant (SHK) at ..." -> "As a Student Assistant (SHK) at ..."."""
    for e in profile.get("experience") or []:
        t = (e.get("title") or "").strip()
        if t:
            art = "an" if t[0].lower() in "aeiou" else "a"
            text = re.sub(rf"\b([Aa]s) (?={re.escape(t)})", rf"\1 {art} ", text)
    return text


def _place_flow(text: str, profile: dict) -> str:
    """Names each place once instead of opening every sentence with "As a <title> at
    <employer>,": the same employer as the sentence before becomes "There, I also"
    / "I also", an employer named earlier keeps only "At <employer>,", and a third
    "As ..." opener in a row moves the role to the end. Also drops "at the same
    employer" after "In that role," (said twice). sewts, ZEISS, Magazino, RobCo
    letters, 2026-10-04."""
    text = _with_article(text, profile)
    orgs = sorted({e.get("employer", "").strip() for e in profile.get("experience") or []
                   if e.get("employer")}, key=len, reverse=True)
    if not orgs:
        return text
    alts = "|".join(re.escape(o) for o in orgs)
    lead = re.compile(rf"^(?:As (?:an? |the )?(?P<title>[^,]+?) at (?:the )?|At (?:the )?)"
                      rf"(?P<org>{alts})(?:, where I (?:worked|work) as an? [^,]+)?, I (?P<body>.+)$")
    named = re.compile(alts)
    # a trailing place: the employer by name ("org"), or "the same lab", "there" ...
    trail = re.compile(rf",?\s+(?:(?:while working|during my time|while I worked|in my role)\s+)?"
                       rf"(?:as an? [^,.]+?\s+)?(?:(?:at|in|with)\s+(?:the\s+)?(?:(?P<org>{alts})|"
                       r"(?:the same|that) (?:lab|employer|company|team|organi[sz]ation|role|job))"
                       r"|there|in that role)(?P<end>[.!?])$")

    def at(org):
        return ("the " if org.split()[0].lower() in _PLACE_ARTICLE else "") + org

    out, last, seen, as_run, n = [], None, set(), 0, 0
    for para in re.split(r"\n\s*\n", text.strip()):
        sents = [s for s in re.split(r"(?<=[.!?])\s+", para.strip()) if s]
        para_last = None
        for k, s in enumerate(sents):
            m = lead.match(s)
            if m:
                org, body = m.group("org"), re.sub(r"^also\s+", "", m.group("body"))
                if org == last:
                    if k:
                        s = ("There, I also " if n % 2 == 0 else "I also ") + body
                        n += 1
                    else:
                        s = f"At {at(org)}, I also {body}"
                elif org in seen:
                    s = f"At {at(org)}, I {body}"
                elif as_run >= 2 and m.group("title") and body[-1:] in ".!?":
                    title = m.group("title")
                    if not re.match(r"(?:an?|the)\s", title, re.I):
                        title = ("an " if title[0].lower() in "aeiou" else "a ") + title
                    s = f"I {body[:-1]} as {title} at {at(org)}{body[-1]}"
            # The place said again at the end: "... while working as a Student
            # Assistant (SHK) at the Robotics Lab ..." twice in a row, or "In that
            # role, ... during my time at the same lab" (NXP, sewts 2026-10-04).
            t = trail.search(s)
            # only right after a sentence of the same paragraph that named it
            if t and k and para_last and (t.group("org") == para_last or not t.group("org")) \
                    and (s.startswith("I ") or re.match(r"(?:In that role|There),", s)):
                s = s[:t.start()] + t.group("end")
                if re.match(r"I (?:have|had) ", s):
                    s = re.sub(r"^I (have|had) ", r"I \1 also ", s)
                elif s.startswith("I ") and not s.startswith("I also "):
                    s = "I also " + s[2:]
            as_run = as_run + 1 if s.startswith("As ") else 0
            hit = named.search(s)
            if hit:
                last = para_last = hit.group(0)
                seen.add(last)
            sents[k] = s
        out.append(" ".join(sents))
    return "\n\n".join(out)


_LACK = re.compile(r"^I (?:lack|do not have|don't have|am missing|am not yet familiar with)\s+"
                   r"(?:(?:hands-on\s+|practical\s+|direct\s+)?(?:experience|knowledge|skills?)\s+"
                   r"(?:with|in|of)\s+)?(?P<x>[^,.;]+?)\s*(?:[,;].*)?[.!]$", re.I)


def _keen_not_lack(sentence: str) -> str:
    """"I lack foundation models, so I want to learn them soon." -> "I am keen to
    learn foundation models." - the letter's own way of naming a gap (Fraunhofer,
    2026-10-04)."""
    m = _LACK.match(sentence or "")
    if not m or len(m.group("x").split()) > 6 or m.group("x").split()[0].lower() in (
            "experience", "knowledge", "skills", "skill", "a", "any", "the", "much", "enough"):
        return sentence
    return f"I am keen to learn {m.group('x').strip()}."


# "I am keen to learn about the specific requirements for this role at Thales" -
# says nothing (2026-10-04)
_VAGUE_LEARN = re.compile(r"\b(?:keen|eager|happy|excited)\s+to\s+learn\s+(?:more\s+)?about\s+"
                          r"(?:the\s+|your\s+)?(?:specific\s+|exact\s+)?(?:requirements|needs|"
                          r"expectations|details|role|position|team|company|work)\b(?!['’])", re.I)


_JOB_ONLY = re.compile(
    r"^(?:the\s+)?[\w/+#.&() -]{2,60}?\s+(?:is|are)\s+(?:also\s+)?(?:a\s+|an\s+)?"
    r"(?:key\s+|core\s+|main\s+|central\s+|important\s+)?(?:required|needed|essential|"
    r"important|requirement|prerequisite|expected|preferred)\b[^.]*\b(?:role|position|"
    r"internship|job|thesis|project|team)\b"
    r"|^(?:the|this)\s+(?:role|position|job|internship|thesis)\s+(?:also\s+)?(?:requires|"
    r"asks for|mentions|needs|calls for|lists)\b", re.I)


def _trim_tools(sentence: str, profile: dict, wanted: tuple = ()) -> str:
    """A bulk tool list ("I possess skills in ROS2, Gazebo, MoveIt2, Nav2, OpenCV, ...")
    cut to the tools the job asks for, at most three; dropped if the job names none
    of them. The prompt says "do NOT list skills or tools in bulk", but citing facts
    made the writer cite the whole skills line (ZEISS, Ubica 2026-10-04)."""
    items = []
    for values in (profile.get("skills") or {}).values():
        for v in values:
            name = re.sub(r"\s*\(.*?\)", "", v).strip()
            if name and re.search(rf"(?<![\w+]){re.escape(name)}(?![\w+])", sentence, re.I):
                items.append(name)
    if len(items) < 5:
        return sentence
    keep = [t for t in items if any(t.lower() in w.lower() or w.lower() in t.lower()
                                    for w in wanted if w)][:3]
    if not keep:
        return ""
    return "My tools include " + (keep[0] if len(keep) == 1 else
                                  ", ".join(keep[:-1]) + " and " + keep[-1]) + "."


def _true_names(text: str, profile: dict) -> str:
    """Schools as the profile writes them: the writer translated "Technische
    Hochschule Deggendorf" into "Technical University Deggendorf" (DLR, Fraunhofer
    2026-10-04), which is a different kind of school and not its name."""
    for ed in profile.get("education") or []:
        name = str(ed.get("institution", "")).strip()
        if not re.search(r"\b(Hochschule|Universität|Fachhochschule|Technische|Akademie)\b", name):
            continue
        city = re.escape(name.split()[-1])
        kinds = (r"(?:Technical|Technological|Applied Sciences?)\s+(?:University|College|Institute)|"
                 r"University of Applied Sciences|Institute of Technology|Technical Institute|"
                 r"Technical School|University")
        text = re.sub(rf"\b(?:the\s+)?(?:{kinds})(?:\s+of)?\s+{city}\b", name, text or "")
        text = re.sub(rf"\b{city}\s+(?:{kinds})\b", name, text)
    return text


def _true_role(text: str, role: str) -> str:
    """The job title's words as the ad spells them: the writer turned "Praktikum"
    into "Practikum" (ZEISS, 2026-10-04). Only near-misses of a title word
    (one or two letters off) are put back."""
    words = {w for w in re.findall(r"[A-Za-zÄÖÜäöüß]{6,}", role or "")}
    if not words:
        return text

    def fix(m):
        w = m.group(0)
        for t in words:
            # same length, same first letter, one or two letters swapped - never a
            # case change or a plural ("testing" stays, "manipulator" stays)
            if len(t) == len(w) and t[0].lower() == w[0].lower() and t.lower() != w.lower() \
                    and sum(a != b for a, b in zip(t.lower(), w.lower())) <= (1 if len(w) < 9 else 2):
                return t
        return w
    return re.sub(r"[A-Za-zÄÖÜäöüß]{6,}", fix, text or "")


def _polish_prose(text: str) -> str:
    """Letter prose conventions: small numbers as words ("one new feature"), but
    not in terms like "4-DOF", "2-layer", "15-20%" or "3.5"."""
    def word(m):
        # a version after a name stays a digit: "ROS 2 and" became "ROS two and" (ZEISS)
        before = re.search(r"(\S+)\s*$", (text or "")[:m.start()])
        if before and before.group(1)[:1].isupper():
            return m.group(1)
        return _NUMBER_WORDS[m.group(1)]
    text = re.sub(r"(?<![\w.,/-])([1-9])(?=\s+[a-z])", word, text or "")
    # The facts' "Project: <name>" label copied into prose: "as part of Project: XR
    # Simulation of Factory" -> "as part of the XR Simulation of Factory project"
    # (Wandelbots, 2026-10-04)
    text = re.sub(r"(?:\b[Tt]he\s+|\bmy\s+)?\bProject:\s+([^.,;:]+?)(?:\s+project)?(?=[.,;]|$)",
                  r"the \1 project", text)
    # "Technologies , where" (a list joined in front of a comma, ZEISS 2026-10-04)
    return re.sub(r"[ \t]+([,.;:])", r"\1", text)


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

    # Languages are stated in their own sentence below: as a met requirement they
    # came out as "The role asks for English language. My work as a Languages
    # included english (Fluent)."
    have = sorted((r for r in requirements if r["status"] == "have" and r["evidence"]
                   and r.get("source") != "languages"),
                  key=lambda r: r["priority"] != "required")
    work, used, listed = [], set(), []
    for r in have:
        line = next((e for e in r["evidence"] if not e.startswith(_LISTED)), "")
        if line and line not in used and len(work) < 3:
            used.add(line)
            work.append(_work_sentence(line, len(work)))
        elif not line:
            listed.append(r)
    # At least three examples: with one proven bullet the DLR letter came out at 133
    # words, below the 150 the check asks for (2026-10-03). The next ones are the
    # first bullets of the most recent roles not used yet.
    for e in profile.get("experience") or []:
        if len(work) >= 3:
            break
        b = (e.get("bullets") or [""])[0]
        line = f"{e['title']} @ {e['employer']}: {b}"
        if b and not any(e["employer"] in u for u in used):
            used.add(line)
            work.append(_work_sentence(line, len(work)))
    skills = [r["skill"] for r in have]
    # A statement about the job, not a claim: "which my experience covers" also
    # claimed experience for coursework-only requirements. Two items, not a list
    # of three.
    # "electronics and wiring as well as Python/Linux", not "... and wiring and Python"
    joiner = " as well as " if any(" and " in s for s in skills[:2]) else " and "
    listing = joiner.join(skills[:2]) + (", among other skills" if len(skills) > 2 else "")
    p2 = (f"The role asks for {listing}. " if skills else "") + " ".join(work)

    p3, more_skills = [], []
    for r in listed:
        ev = r["evidence"][0]
        if ev.startswith("Education: "):
            # the courses themselves: the ad's word ("control engineering") is not
            # what the transcript says ("Control Systems")
            degree, _, courses = ev[len("Education: "):].partition(": ")
            pairs = _in_pairs([c.strip() for c in courses.rstrip(".").split(",") if c.strip()])
            sentence = (f"My {degree} coursework included {pairs[0]}."
                        + (f" It also covered {', as well as '.join(pairs[1:])}." if pairs[1:] else ""))
            if sentence not in p3:
                p3.append(sentence)
        elif r["skill"] not in more_skills:
            more_skills.append(r["skill"])
    # One sentence: "My skills also include OpenCV. My skills also include Linux."
    # read like a form (Ubica, 2026-10-03).
    if more_skills:
        p3.append("My skills also include " + (" and ".join(more_skills) if len(more_skills) <= 2
                  else ", ".join(more_skills[:-1]) + " and " + more_skills[-1]) + ".")
    # Only extracted requirements: the keyword fallback in gap_names once made this
    # "I am keen to learn similar in practice" (from "or similar simulation tools").
    real_gaps = [g for g in gap_names
                 if any(r["skill"] == g and r["status"] == "gap" for r in requirements)
                 and not _TRIVIAL_GAP.search(g)]
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
        opening = _opening_paragraph(profile, meta)
        # Body sentences that only repeat the added opening go: "I have also completed
        # a bachelor's degree in electrical engineering and am currently pursuing an
        # M.Eng ..." stood right under the same facts (ZEISS, 2026-10-03).
        said = checks._content_stems(opening)
        # School names do not count: "I completed my Bachelor's degree ... at GIK
        # Institute of Engineering Sciences and Technologies and now pursue an M.Eng
        # ..." repeated the opening but its school's name kept it under 80%
        # (Fraunhofer, 2026-10-04).
        schools = set().union(*[checks._content_stems(ed.get("institution", ""))
                                for ed in profile.get("education") or []] or [set()]) - said

        def repeats(x):
            own = checks._content_stems(x) - schools
            return own and len(own & said) / len(own) >= 0.8
        kept = []
        for p in paras:
            sents = [x for x in re.split(r"(?<=[.!?])\s+", p) if x.strip()]
            sents = [x for x in sents if not repeats(x)]
            if sents:
                kept.append(" ".join(sents))
        paras = [opening] + kept
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
    # Dots inside the job title and employer name ("Stud. Assistant", "H. & W.
    # Quadflieg GmbH") are not sentence ends: protected while the text is checked,
    # put back at the end (Fraunhofer, Quadflieg 2026-10-04).
    meta = dict(meta or checks.parse_header(job_description))
    _ABBREV["toks"] = sorted({t for v in (meta.get("role"), meta.get("company")) if v
                              for t in _ABBREV_TOKEN.findall(v)}, key=len, reverse=True)
    for k in ("role", "company"):
        if meta.get(k):
            meta[k] = _protect_abbrev(meta[k])
    log = (lambda msg, _log=log: _log(_unprotect(msg)))
    # Spoken languages are stated by the profile's own sentence, never as proof: as
    # "requirement 1" the recruiter message said "English as Fluent ... meets the
    # requirement of 'Sehr gute Deutsch- und Englischkenntnisse'" (German unmet).
    requirements = [r for r in requirements if r.get("source") != "languages"]
    brief = job_brief(job_description, meta or checks.parse_header(job_description),
                      list(requirements))
    facts = candidate_facts(profile, list(requirements),
                            meta or checks.parse_header(job_description))
    results, issues, removed_all = {}, [], []
    # One letter writer that is not the judge: it writes, rewrites and restyles; the
    # judge (`call`) still fact-checks everything it writes.
    writer = drafters[0] if drafters and len(drafters) == 1 else None
    write = lambda prompt, **kw: call(prompt, use_model=writer,
                                      **{"temperature": WRITER_TEMPERATURE, **kw})
    # The letter writer sees the facts with IDs and cites them per sentence; each
    # sentence is then checked against its own cited facts (_cited_check).
    numbered, fact_ids = _numbered_facts(facts)
    # The recruiter message is not cited: the same facts without IDs, so no "[F7]"
    # can end up in it.
    recruiter_facts = "\n".join(f"- {v}" for v in fact_ids.values())
    fact_lines = [l.lstrip("- ") for l in facts.splitlines() if l.strip()]

    # Facts that prove a job requirement: kept first when the letter has too many.
    proven = {e for r in requirements if r.get("status") == "have" for e in r.get("evidence") or []}
    req_stems = checks._content_stems(" ".join(f"{r.get('skill', '')} {r.get('quote', '')}"
                                               for r in requirements))

    def uncite(field: str, raw: str) -> tuple[str, list]:
        if field != "cover_letter":
            return raw, []
        return _cited_check(raw, fact_ids, call, profile, fact_lines, log, proven, req_stems)

    for field, make, check, schema in (
            ("cover_letter",
             lambda: prompts.cover_only_prompt(profile, brief, lang, gaps, numbered, cited=True),
             checks.letter_problems, None),
            ("recruiter_message",
             lambda: prompts.recruiter_only_prompt(profile, brief, lang, recruiter,
                                                   facts=recruiter_facts),
             checks.recruiter_problems, prompts.RECRUITER_SCHEMA)):
        best, best_problems, drafts, built = None, [], [], False
        cite_removed = []
        multi = field == "cover_letter" and drafters and len(drafters) > 1
        for attempt in (() if multi else (1, 2)):
            log(f"Writing the {field.replace('_', ' ')} (attempt {attempt})...")
            raw = _preclean(write(make(), max_tokens=1200, schema=schema, logprobs=True))
            drafts.append(last_logprobs())
            text = prompts.join_recruiter(raw) if schema else raw
            text, cut_here = uncite(field, text)
            problems = check(text) + checks.gap_claims(text, forbidden)
            if best is None or len(problems) < len(best_problems):
                best, best_problems, cite_removed = text, problems, cut_here
            if not problems:
                break
        removed_all += [f"{field.replace('_', ' ')} (citation check): {r}" for r in cite_removed]
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
            text = _preclean(write(make() + f"\n\nDo NOT write these unsupported claims: {avoid}",
                                  max_tokens=1200, schema=schema, logprobs=True))
            drafts.append(last_logprobs())
            text = prompts.join_recruiter(text) if schema else text
            text, cut2 = uncite(field, text)
            removed_all += [f"{field.replace('_', ' ')} (rewrite, citation check): {r}" for r in cut2]
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
        if field == "cover_letter" and lang == "en" and (best or "").strip() and best_problems \
                and all(p.startswith("cover letter has") for p in best_problems):
            # True but in one block or a little short: give it paragraphs and the
            # candidate's own unused bullets before throwing it away (DLR: a true
            # 137-word, one-paragraph draft went to the profile fallback, 2026-10-04).
            # Studies and languages first: a draft that already has its four work
            # examples cannot take more (Magazino: 106 true words, 2026-10-04).
            mended = _add_studies(_reparagraph(best), profile)
            again = check(mended) + checks.gap_claims(mended, forbidden)
            if any(p.startswith("cover letter has") for p in again):
                mended = _top_up(mended, profile, list(requirements))
                again = check(mended) + checks.gap_claims(mended, forbidden)
            if not any(p.startswith("cover letter has") for p in again):
                log("The cover letter was one block or a little short; split it into "
                    "paragraphs and added your own unused profile lines.")
                best, best_problems = mended, again
        if field == "cover_letter" and lang == "en" and (
                not (best or "").strip() or any(p.startswith("cover letter has")
                                                for p in best_problems)):
            # Only true sentences survived, but too few for a letter: a short or
            # one-paragraph letter is a broken letter, so build it from the profile.
            log("Too little of the cover letter survived the fact-check; assembling it "
                "from your profile instead.")
            # Why, for report.md: Fraunhofer fell back with only two sentences cut and
            # no way to see what was wrong with the draft (2026-10-04).
            results["fallback_reason"] = "; ".join(
                p for p in best_problems if p.startswith("cover letter has")) or "no usable text left"
            results["fallback_draft"] = best or ""
            best = profile_letter(profile, meta or checks.parse_header(job_description),
                                  list(requirements), [g for g in gaps.split(", ") if g])
            built = True
            best_problems = check(best) + checks.gap_claims(best, forbidden)
            if not any(i.startswith("cover letter: the model's fact-check failed") for i in issues):
                issues.append("cover letter: assembled from profile.json because the model's "
                              f"draft lost too much to the fact-check ({results['fallback_reason']}) "
                              "- it is true but plain; "
                              "personalise it before sending")
        if field == "recruiter_message":
            # also when nothing usable is left: intro, a real bullet and a question
            company = (meta or {}).get("company", "")
            best = _tidy_recruiter(_intro_stop(best or "", company), profile)
            best = _complete_recruiter(best or "", profile, list(requirements),
                                       (meta or {}).get("role", ""))
            best = checks.fix_recruiter_question(best)
            best = _with_article(_at_word(_role_phrase(best, company)), profile)
            best_problems = check(best) + checks.gap_claims(best, forbidden)
        if field == "cover_letter" and best and not built:
            best = _style_pass(best, drafts, facts, call, log, profile, forbidden,
                               [r.split(" (")[0] for r in removed], check, write=write)
            # code-only rewording, undone if any code check objects to the result
            fact_lines = [l.lstrip("- ") for l in facts.splitlines() if l.strip()]
            varied = _vary_openings(best, profile)
            if varied != best and not _code_checks(varied, profile, fact_lines, forbidden):
                best = varied
            # the clean-up turns "@" into "at"; that is not a removal (sewts)
            before_tidy = _at_word(re.sub(r"\s@\s", " at ", best or ""))
            best = _tidy_letter(best, profile,
                                (meta or checks.parse_header(job_description)).get("role", ""),
                                job_description, tuple(r["skill"] for r in requirements))
            # What the clean-up dropped, for report.md: two letters went to the
            # profile fallback with only the fact-check's deletions listed, so the
            # reason could not be seen (Ubica, NVIDIA 2026-10-03).
            kept_flat = re.sub(r"\s+", " ", best or "")
            # Reworded is not removed: "As a Student Assistant ... I coordinated ..."
            # became "I also coordinated ..." and was listed as removed (2026-10-04).
            def core(s):        # without its "As a <title> at <place>, " opener
                return checks._content_stems(re.sub(r"^[^.]*?,\s+(?=I\s)", "", s))
            kept_stems = [core(k) for k in re.split(r"(?<=[.!?])\s+", kept_flat) if k.strip()]

            def still_there(s):
                own = core(s)
                return re.sub(r"\s+", " ", s) in kept_flat or (
                    own and any(len(own & k) >= 0.8 * len(own) for k in kept_stems))
            results["cleanup_removed"] = [
                s for s in (x.strip() for x in re.split(r"(?<=[.!?])\s+", before_tidy or ""))
                if s and not still_there(s)]
            best = _frame_letter(best, profile, meta or checks.parse_header(job_description), log)
            # Checked again after the clean-up: it ran after the checks, and a BMW
            # letter came out as one paragraph with no opening ("I am eager to apply
            # these skills ...") with no warning (2026-10-03).
            best_problems = check(best) + checks.gap_claims(best, forbidden)
            short = [p for p in best_problems if p.startswith("cover letter has")]
            if lang == "en" and short and len(best.split()) >= 100:
                # A few words short: add the candidate's own unused bullets rather than
                # throw the letter away (ZEISS: 144 of 150 words went to the profile
                # fallback, 2026-10-03).
                topped = _top_up(best, profile, list(requirements))
                if topped != best:
                    again = check(topped) + checks.gap_claims(topped, forbidden)
                    if not any(p.startswith("cover letter has") for p in again):
                        log("The cover letter was a little short; added "
                            f"{len(topped.split()) - len(best.split())} words from your profile.")
                        best, best_problems = topped, again
                        short = []
            if lang == "en" and short:
                results["fallback_reason"] = "; ".join(short)
                results["fallback_draft"] = best or ""
                log("After removing unsupported sentences the cover letter was too short; "
                    "assembling it from your profile instead.")
                best = profile_letter(profile, meta or checks.parse_header(job_description),
                                      list(requirements), [g for g in gaps.split(", ") if g])
                built = True
                best_problems = check(best) + checks.gap_claims(best, forbidden)
                issues.append("cover letter: assembled from profile.json because too little "
                              "survived the fact-check and clean-up "
                              f"({results['fallback_reason']}) - it is true but plain; "
                              "personalise it before sending")
        style = checks.predictability(best or "", _sentence_logprobs(best or "", drafts))
        style["problems"] = checks.style_problems(best or "")
        style["built_from_profile"] = built
        results[f"{field}_style"] = style
        # last, after every check: prose conventions ("one new feature", not "1 new ...")
        results[field] = _polish_prose(_true_role(_true_names(best, profile),
                                                  (meta or {}).get("role", ""))) if best else best
        issues += best_problems
    results["factcheck_removed"] = removed_all
    results, issues = _unprotect(results), _unprotect(issues)
    _ABBREV["toks"] = []
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


def _style_pass(text, drafts, facts, call, log, profile, forbidden, known, check, write=None):
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
    raw = _preclean((write or call)(prompts.style_fix_prompt(text, problems or ["read less predictable"],
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
    if kw.get("hard"):
        # Above the skill table: either one can rule the application out on its own.
        L += [f"**⚠ Hard requirements you do not meet ({len(kw['hard'])}):**",
              *[f"- {w}" for w in kw["hard"]], ""]
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
    # Facts cut only to keep the letter short are true: listed apart, not under "not
    # supported by your profile" (2026-10-04)
    capped = [r for r in pkg.get("factcheck_removed") or [] if _CAPPED in r]
    wrong = [r for r in pkg.get("factcheck_removed") or [] if _CAPPED not in r]
    if wrong:
        L += ["## Removed by the fact-check (not supported by your profile)",
              *[f"- {r}" for r in wrong], ""]
    if capped:
        L += [f"## Left out to keep the letter to {MAX_WORK_FACTS} examples (true, but less relevant)",
              *["- " + re.sub(r" \(more than \d+ work examples[^)]*\)$", "",
                              r.replace(" (citation check)", "").replace(" (rewrite, citation check)",
                                                                         " (rewrite)"))
                for r in capped], ""]
    if pkg.get("fallback_draft"):
        L += ["## The model's draft that was replaced",
              f"Replaced by the letter built from your profile because: {pkg.get('fallback_reason', '')}.",
              "", *[f"> {p}" for p in pkg["fallback_draft"].split("\n\n") if p.strip()], ""]
    if pkg.get("cleanup_removed"):
        L += ["## Removed by the clean-up (padding, repeats, job-matching talk)",
              *[f"- {r}" for r in pkg["cleanup_removed"]], ""]
    if pkg.get("issues"):
        L += ["## Checks that still fail", *[f"- ⚠ {i}" for i in pkg["issues"]], ""]
    if pkg.get("double_check"):
        L += ["## Sentences to double-check (worded by the model, not copied from your profile)",
              "Read these before sending: they passed every check, but only you know if each "
              "says exactly what you did.",
              *[f"- {s}" for s in pkg["double_check"]], ""]
    if pkg.get("notes"):
        L += ["## Notes", *[f"- {i}" for i in pkg["notes"]], ""]
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
            max_tokens: int = 1500, log=print, writers: str = None) -> dict:
    """Stages 1-4: check the profile, read the job, analyse gaps, plan the edits.
    Nothing is written yet; the returned plan can be reviewed and edited.
    `writers`: the model(s) that write the cover letter and recruiter message; when
    empty, every model listed in `model` drafts, as before."""
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
    chosen = [m.strip() for m in (writers or "").split(",") if m.strip()]
    if chosen:
        drafters = chosen
    if model or drafters:
        # named models are loaded with the right context; no lms commands to type
        ensure_models_loaded(list(dict.fromkeys([model] + drafters)), base_url, log)
    if not model:
        model = _first_chat_model(base_url)
        if model:
            log(f"Using the loaded model: {model}")
    drafters = drafters or [model]
    if len(drafters) > 1 or drafters[0] != model:
        log(f"Judge model: {model}; letter written by: {', '.join(drafters)}")
    region = get_region(region_code)
    lang, jd_lang = checks.choose_language(job_description, profile)
    meta = checks.parse_header(job_description)
    kw = checks.keyword_report(profile, job_description, meta["role"])
    # Hard requirements the skill list never covers: a finished degree, a language.
    hard = [w for w in (checks.degree_warning(job_description, profile),
                        checks.language_warning(job_description, profile),
                        checks.experience_warning(job_description, profile)) if w]
    kw["hard"] = hard

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

    for w in hard:
        log(f"Watch out: {w}.")
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
        max_tokens: int = 1500, cover: bool = True, approve=None, log=print,
        writers: str = None):
    """Tailor the CV (and optionally a cover letter) for one job.

    The CV comes from profile.json; the model only rewords experience bullets,
    and only around keywords each bullet already contains. `approve(prepared)`
    may review the plan: return it (edited) to continue, or None to cancel.
    `notes` is extra truthful context for the cover letter.
    """
    prepared = prepare(job_description, region_code, profile_path, recruiter, notes, model,
                       api_key, backend, base_url, max_tokens, log, writers)
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
        pkg["double_check"] = _double_check(texts, profile, reqs, meta)
        for field in ("cover_letter", "recruiter_message"):
            s = texts.get(f"{field}_style") or {}
            if s:
                log(f"Writing style, {field.replace('_', ' ')}: predictability {s['score']}/100 "
                    f"({s['label']})" + (f", perplexity {s['perplexity']}" if s.get("perplexity")
                                         else "") + (f"; {len(s['problems'])} style note(s)"
                                                     if s.get("problems") else ""))
        for field in ("cover letter", "recruiter message"):
            n = sum(r.startswith(field) and _CAPPED not in r
                    for r in texts.get("factcheck_removed", []))
            if n:
                # Deleting sentences keeps the text true but can gut it - say so, so a
                # thin letter is never reported as "all checks passed". A failed check
                # only when it cost the letter its own text (the profile fallback);
                # otherwise a note: it fired in every report (2026-10-03), for one
                # correct deletion as much as for ten.
                msg = (f"{field}: the fact-check removed or repaired {n} sentence(s) - "
                       "they are listed above")
                fell_back = any(i.startswith(field) and "profile.json" in i
                                for i in pkg["issues"])
                (pkg["issues"] if fell_back else pkg.setdefault("notes", [])).append(msg)
    pkg["issues"] += prepared["keywords"].get("hard", [])

    for n in exp_notes:
        log(n)
    if pkg["issues"]:
        log(f"{len(pkg['issues'])} check(s) still failing - see report.md.")
    out = write_outputs(pkg, prepared["region"], profile, outputs_root or ROOT / "outputs",
                        style=style, compile_pdf=compile_pdf)
    log(f"Written to: {out}")
    return pkg, out
