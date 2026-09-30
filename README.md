# EU Robotics & Mechatronics Job Search

A Windows desktop app for finding robotics, mechatronics and engineering jobs across
Europe (plus US/Asia from the command line), ranking them against your own profile,
tracking applications, and generating a tailored CV and cover letter for a single
job with a **local** language model.

The CV and letter generator is built around one rule: **nothing is ever added that
your `profile.json` does not say.** Requirements you do not meet are reported as
gaps, never written in. Every model-written sentence is fact-checked by the model
and by deterministic code checks before it reaches a document.

---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Local models (LM Studio)](#local-models-lm-studio)
- [Your profile (`profile.json`)](#your-profile-profilejson)
- [Using the app](#using-the-app)
- [Command line](#command-line)
- [How CV and letter generation works](#how-cv-and-letter-generation-works)
- [Output files](#output-files)
- [Configuration](#configuration)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Privacy](#privacy)
- [License](#license)

---

## Features

**Job search** (keyless sources unless noted)

| Source | What it covers |
|---|---|
| Bundesagentur für Arbeit | Germany's largest job database (public API) |
| EURES | EU-wide jobs from the European Commission |
| EURAXESS | PhD positions across Europe |
| Employer ATS boards | Greenhouse, Lever, SmartRecruiters, Ashby, Recruitee, Workday, Personio |
| Research institutes | Fraunhofer, DLR and other institute feeds (HiWi, thesis, PhD) |
| New companies | Funding signals and new employers per market (`company_radar.py`) |
| LinkedIn / Indeed (optional) | via `python-jobspy`, or licensed APIs (JSearch / Apify, key needed) |
| US / Asia (CLI only) | USAJOBS and Adzuna (free keys), employer ATS boards |

**Ranking and tracking**
- **Fit score 0–100** for every job against `profile.json` — instant, no model call.
- **Daily sweep** of your saved searches, limited to **one run per calendar day**,
  optionally scheduled as a Windows task.
- **Application tracker** with status, applied date and follow-up reminders.

**CV and cover-letter tailoring**
- CV built from `profile.json` in code; the model only rewords bullets around keywords
  they already contain.
- Optional cover letter and three-sentence recruiter message.
- Requirement extraction, meaning-based matching and a gap report.
- Two LaTeX styles (plain ATS version and a styled version) for CV and letter.
- Writing-style check with a predictability score ("does this read machine-written?").

---

## Requirements

- **Windows 10/11** (the app also runs from source on Linux/macOS; the scheduled
  daily task is Windows-only)
- **Python 3.10+** with Tkinter (only to run from source or build the `.exe`)
- **[LM Studio](https://lmstudio.ai)** for the local models (CV/letter generation only)
- **A LaTeX distribution** such as [MiKTeX](https://miktex.org) if you want PDFs
  generated automatically (otherwise you get `.tex` files)
- A GPU with **6 GB VRAM** is enough for the default model pair

---

## Installation

### Option A — build the `.exe`

```bat
build.bat
```

`build.bat` creates a `.venv`, installs the dependencies, compiles and import-checks
every module, and builds `dist\EUJobSearch.exe` with PyInstaller. Flags:

| Flag | Effect |
|---|---|
| `/nopause` | no prompts (for scripts) |
| `/nojobspy` | skip the optional LinkedIn/Indeed engine (smaller `.exe`) |
| `/fresh` | delete and recreate `.venv` first |

The `.exe` runs on any Windows PC without Python.

### Option B — run from source

```bash
pip install -r requirements.txt
python job_gui.py
```

Optional LinkedIn/Indeed search (`python-jobspy` pins an old numpy that often fails to
build, so it has its own installer):

```bash
python install_jobspy.py
```

---

## Local models (LM Studio)

Install LM Studio and download these models (search for them in LM Studio):

| Role | Model | File |
|---|---|---|
| Judge + drafter | `qwen/qwen3-4b-2507` (Qwen3-4B-Instruct-2507) | Q4_K_M, ~2.5 GB |
| Drafter | `llama-3.2-3b-instruct` | Q4_K_M, ~2.0 GB |
| Embeddings | `text-embedding-nomic-embed-text-v1.5` | ~0.1 GB |

Start LM Studio's local server (Developer tab → Start Server, default
`http://localhost:1234/v1`). **You do not need to load the models yourself:** before
each run the app loads the model pair with LM Studio's `lms` tool, with a 6144-token
context each, and unloads other chat models to make room on the GPU. The embedding
model loads on first use.

Notes from testing on an RTX 3050 (6 GB):
- Qwen3-4B ~50 tokens/s, Llama 3.2 3B ~60 tokens/s; a full run takes about 75–100 s.
- Avoid **Qwen3.5**-architecture models (e.g. Qwen3.5 4B): they ran at under 2 tokens/s
  in LM Studio on this GPU.
- Larger models (e.g. `prism-ml/bonsai-27b`) can be used by typing their name into the
  Model field; they are slower but write more natural letters.

---

## Your profile (`profile.json`)

`profile.json` is the **only source of facts** for your CV and letters. Keep it
accurate and complete — anything missing here cannot appear in a document.

```json
{
  "personal": {
    "name": "Jane Doe",
    "title": "Mechatronics Student",
    "email": "jane.doe@example.com",
    "phone": "+49 123 456 7890",
    "location": "Munich, Germany",
    "linkedin": "janedoe",
    "date_of_birth": "01/01/2000",
    "nationality": "",
    "work_authorisation": "EU citizen"
  },
  "profile_summary": "Aspiring Mechatronics Engineer focused on robotics. Bachelor's degree in electrical engineering. Currently pursuing an M.Sc in Robotics at TU Example.",
  "education": [
    {"degree": "M.Sc Robotics", "institution": "TU Example", "location": "Germany",
     "start": "Oct 2025", "end": "Present", "status": "in progress",
     "coursework": ["Control Systems", "Computer Vision"],
     "research_interests": ["SLAM"],
     "thesis": ""}
  ],
  "experience": [
    {"title": "Student Assistant", "employer": "Robotics Lab, TU Example",
     "location": "Germany", "start": "Jan 2026", "end": "Present",
     "bullets": ["Built and tested a 6-DOF arm in ROS2 and Gazebo."], "tools": ["ROS2"]}
  ],
  "projects": [
    {"name": "Line-following robot", "context": "University project", "year": "2024",
     "bullets": ["Designed the PCB and PID controller."]}
  ],
  "skills": {"Programming Languages": ["Python", "C++"], "Software Tools": ["ROS2", "Gazebo"]},
  "languages": [{"language": "English", "level": "Fluent"}, {"language": "German", "level": "B1"}],
  "certifications": [],
  "publications": []
}
```

Tips:
- Dates like `May 2025` or `Present`. An unfinished degree: add `(unfinished)` to the
  degree name or `"status": "unfinished"` — letters then say so.
- **Research interests are never presented as experience or coursework.**
- Write the summary as short résumé fragments with varied sentence length; letters turn
  them into first-person sentences ("I am an aspiring…", "My bachelor's degree is in…").

**Where the app looks for it**, in this order: the file you last picked with
**Browse…** (remembered) → `%APPDATA%\EUJobSearch\profile.json` → next to the `.exe` or
one folder above it → the copy bundled into the `.exe` (last resort; the log warns).

---

## Using the app

The window has four tabs:

| Tab | Purpose |
|---|---|
| **Graduate & PhD** | EURAXESS PhD positions, graduate employer boards, research institutes |
| **Job boards** | Bundesagentur, EURES, Personio, Workday, LinkedIn/Indeed |
| **New companies** | companies and startups newly hiring |
| **Applications** | daily sweep, application tracker, and the CV & cover-letter generator |

**Generating a CV and cover letter**
1. Open **Applications**. Paste a job ad into *Job description*, or double-click a job
   in the results list to fill it in.
2. Check *Region* (sets photo / date of birth / nationality conventions), *Style*
   (`ats`, `styled` or `both`), *Cover letter* and *Compile PDF*.
3. *Model* defaults to `qwen/qwen3-4b-2507,llama-3.2-3b-instruct` — leave it.
4. Press **Generate**. With *Review plan* ticked, a window shows which requirements you
   meet and which bullets may be reworded; untick bullets to keep them word for word,
   then **Write CV**.
5. The status line shows the fit and any warnings; **Outputs folder** opens the files.
   Read `report.md` — it lists every sentence the fact-check removed.

Right-click jobs in the results to track them (saved, applied, interview…).

---

## Command line

```bash
# CV + letter for one job, headless (same pipeline and defaults as the Generate button)
EUJobSearch.exe --ats job.txt --region DE
EUJobSearch.exe --ats job.txt --model "prism-ml/bonsai-27b"

# Daily sweep (what the scheduled Windows task runs)
EUJobSearch.exe --sweep
```

From source, the same with `python job_gui.py --ats job.txt`. `--ats` writes its log to
`%APPDATA%\EUJobSearch\ats_headless.log`.

```bash
python daily_sweep.py --force              # ignore the once-per-day limit
python daily_sweep.py --install 08:00      # register a daily Windows task
python daily_sweep.py --uninstall

python robotics_track.py phd --field robotics --countries DE,NL,SE,CH
python us_asia_jobs.py --help              # US / Asia-Pacific sources
python linkedin_jobs.py search "robotics engineer" --location Berlin --pages 2
python linkedin_jobs.py export jobs.csv
```

---

## How CV and letter generation works

```
job ad ──► 1  requirements (quote-verified) + job title/company
           2  gap analysis: vocabulary → meaning-based matching (embeddings)
              → model verifies the top-3 profile lines ("yes / partly / no")
           3  rewrite plan (you can review it)
           4  CV bullets reworded one at a time, each checked
           5  cover letter: every model drafts in parallel
              → each draft fact-checked (model + code) → merged in code
              → opening/closing added → style-only revision (fact-checked again)
              → too little true text left? letter built from profile.json instead
           6  recruiter message (3 sentences), fact-checked the same way
           7  LaTeX → PDF, report.md
```

**What the checks guard against** (all in `ats_checks.py`, run in code):
- claiming a requirement you do not meet, or a skill/tool not in your profile
- inflation ("led", "expertise", "extensive"), made-up numbers, "1 feature" → "features"
- merging two bullets into one claim, or attaching work to the wrong employer/project
- research interests presented as experience, coursework or "focus"
- an unfinished degree mentioned without saying so
- "keen to learn X" when X is already in your profile
- text copied from the job ad or addressed to the reader ("you will…")
- CV bullets that drop a number, tool or action verb, add buzzwords, or repeat words

**Writing style.** Prompts follow a "write like a person" style guide (plain words,
varied sentence length, no stock AI phrases, no lists of three). `report.md` includes a
**predictability score** (0–100) from sentence rhythm, repeated openings, stock phrases
and — for model-written sentences — their real perplexity from LM Studio. It is an
estimate, not an AI-detector result.

---

## Output files

Each run writes to `ats_outputs\<company>_<role>_<date>\` in the data folder:

| File | Content |
|---|---|
| `cv_ats.tex/.pdf` | plain one-column CV for upload to job portals |
| `cv_styled.tex/.pdf` | styled CV for email / people |
| `cover_letter_ats.tex/.pdf` | plain cover letter |
| `cover_letter_styled.tex/.pdf` (+ `info.tex`, `body.tex`) | styled cover letter |
| `recruiter_message.txt` | short message for LinkedIn / email |
| `report.md` | fit, requirements, gaps, removed sentences, writing style, warnings |
| `tailored_experience.json` | the reworded experience section and notes |

For a signature and photo, put `sig.png` and `profile_pic.png` in
`%APPDATA%\EUJobSearch\assets\` (the photo is used only where the region expects one).

---

## Configuration

**Data folder:** `%APPDATA%\EUJobSearch` for the `.exe`; the project folder when run from
source. Contains the job database (`robotics_jobs.db`), `company_radar.db`,
`sweep_config.json`, `sweep_state.json`, `settings.json` (remembered profile path),
`embed_cache.json` and `ats_outputs\`.

**Environment variables** (all optional):

| Variable | Purpose |
|---|---|
| `ATS_MODELS` | default model list, e.g. `qwen/qwen3-4b-2507,llama-3.2-3b-instruct` |
| `ATS_BACKEND` | `lmstudio` (default) or `anthropic` |
| `ATS_BASE_URL` | OpenAI-compatible server, default `http://localhost:1234/v1` |
| `ATS_MODEL` | model used when the Model field is empty (Anthropic backend) |
| `ATS_EMBED_MODEL` | embedding model, default `text-embedding-nomic-embed-text-v1.5` |
| `ANTHROPIC_API_KEY` | only for the `anthropic` backend |
| `JOBSEARCH_DATA_DIR` | override the data folder |
| `JOBS_DB` | override the job database path |
| `JSEARCH_API_KEY`, `APIFY_TOKEN` | licensed LinkedIn job APIs |
| `USAJOBS_API_KEY`, `USAJOBS_EMAIL`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | US / Asia sources |

---

## Project structure

| File | Role |
|---|---|
| `job_gui.py` | desktop app (Tkinter); `--ats` and `--sweep` headless modes |
| `ats_pipeline.py` | CV/letter pipeline: model calls, drafting, merging, fact-check, outputs |
| `ats_plan.py` | profile check, requirement extraction, gap analysis, rewrite plan |
| `ats_checks.py` | all deterministic checks, keyword and style analysis |
| `ats_prompts.py` | prompts and JSON schemas for the model |
| `ats_embed.py` | meaning-based requirement matching via LM Studio embeddings |
| `ats_latex.py` | LaTeX templates for CVs and letters |
| `ats_regions.py` | per-market conventions (photo, date of birth, nationality) |
| `fit_score.py` | 0–100 fit score and profile lookup |
| `daily_sweep.py` | once-a-day sweep and Windows task |
| `linkedin_jobs.py` | job model, SQLite store, shared HTTP helpers, LinkedIn APIs |
| `eu_student_jobs.py` | EURES and student-job classification |
| `robotics_track.py` | EURAXESS PhDs and employer ATS boards |
| `ba_jobsuche.py`, `personio_jobs.py`, `workday_jobs.py`, `research_jobs.py` | further sources |
| `company_radar.py` | newly hiring companies |
| `us_asia_jobs.py` | US / Asia-Pacific sources (CLI) |
| `jobspy_provider.py`, `install_jobspy.py` | optional LinkedIn/Indeed engine |
| `build.bat`, `jobsearch.spec` | Windows build |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Could not reach http://localhost:1234" | start LM Studio's server (Developer tab → Start Server) |
| "Not downloaded in LM Studio: …" | download the named model in LM Studio |
| "lms tool was not found" | install LM Studio's CLI (`~\.lmstudio\bin\lms.exe`) or load the models manually |
| A model loads but is extremely slow | it is probably a Qwen3.5-architecture model — use the defaults |
| "answer ran past its … token limit" | raise the model's context in LM Studio to 8k+ |
| Cover letter says "built from your profile" | the drafts lost too much to the fact-check; the letter is true but plain — personalise it, or try a larger model |
| No PDFs | install MiKTeX/TeX Live and tick *Compile PDF*; missing packages install on first use |
| "EUJobSearch.exe is running" during build | close the app first |

---

## Privacy

Everything runs locally: job searches call public job APIs, and the CV/letter models run
in LM Studio on your machine. Nothing from your profile is sent to an online AI service
unless you switch the backend to `anthropic`.

**Before pushing this project to GitHub**, keep your personal data out of the repository.
The included `.gitignore` already excludes `profile.json` and its backups, generated
PDFs and outputs, the local databases and caches, and the build folders. Share a
placeholder profile such as the example above instead of your real one.

---

## License

This project is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).
You may use, modify and share it for personal, research, educational and other
**noncommercial** purposes. **Commercial use is not permitted.** For a commercial
license, contact the author.

The third-party libraries it uses keep their own licenses (Apache-2.0, MIT, BSD,
MPL-2.0 and others). They are listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
