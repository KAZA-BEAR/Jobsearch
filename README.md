# EU Robotics & Mechatronics Job Search

A Windows desktop app for finding robotics, mechatronics and engineering jobs across
Europe, the US and Asia-Pacific, ranking them against your own profile,
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
| Adzuna Europe | job aggregator for DE, AT, CH, NL, UK, FR, BE, IT, ES, PL (free key) |
| EURES | EU-wide jobs from the European Commission |
| EURAXESS | PhD positions across Europe |
| Employer ATS boards | Greenhouse, Lever, SmartRecruiters, Ashby, Recruitee, Workday, Personio |
| Research & new employers | Fraunhofer, DLR, Max Planck, TH Deggendorf feeds, plus open-ended discovery: thousands of startup and company job boards from Common Crawl's public index, career pages of research institutes and engineering companies (Wikidata), Y Combinator startups and Hacker News "Who is hiring?" (`discover_jobs.py`) |
| New companies | Funding signals and new employers per market (`company_radar.py`) |
| LinkedIn / Indeed (optional) | via `python-jobspy` (free, no key) |
| US / Asia | USAJOBS and Adzuna (free keys), MyCareersFuture (Singapore), employer ATS boards |

**Ranking and tracking**
- **Fit score 0–100** for every job against `profile.json` — instant, no model call.
- **Daily sweep** of your saved searches (Europe, and optionally the US and Asia-Pacific),
  limited to **one run per calendar day**, optionally scheduled as a Windows task.
- **Application tracker** with status, applied date and follow-up reminders.

**CV and cover-letter tailoring**
- CV built from `profile.json` in code; the model only rewords bullets around keywords
  they already contain.
- Optional cover letter and three-sentence recruiter message, written by a separate,
  larger "writer" model and checked by a small "judge" model plus code.
- **Cited letters:** every sentence the writer puts in a letter names the profile fact it is
  based on, and is checked against that fact alone. A sentence that says more is
  replaced with your profile's own wording.
- At most four work or project examples per letter, the ones that best match the job,
  each saying where it happened (employer or project).
- Requirement extraction, meaning-based matching and a gap report, with warnings for hard
  requirements you do not meet (language level, finished degree, years of experience).
- Two LaTeX styles (plain ATS version and a styled version) for CV and letter.
- Writing-style check with a predictability score ("does this read machine-written?"), and
  a list of model-worded sentences for you to double-check before sending.

---

## Requirements

- **Windows 10/11** (the app also runs from source on Linux/macOS; the scheduled
  daily task is Windows-only)
- **Python 3.10+** with Tkinter (only to run from source or build the `.exe`)
- **[LM Studio](https://lmstudio.ai)** for the local models (CV/letter generation only)
- **A LaTeX distribution** such as [MiKTeX](https://miktex.org) if you want PDFs
  generated automatically (otherwise you get `.tex` files)
- For the default models: a GPU with **6 GB VRAM** (tested on an RTX 3050 6 GB; LM
  Studio keeps part of the models in system RAM) and **16 GB RAM** (8 GB minimum).
  About 7 GB of disk for the three models.

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
| **Judge**: requirements, gap check, CV bullet edits, fact-check | `qwen/qwen3-4b-2507` (Qwen3-4B-Instruct-2507) | Q4_K_M, ~2.5 GB |
| **Letter writer**: cover letter and recruiter message | `prism-ml/bonsai-27b` (Bonsai-27B) | Q1_0, ~3.8 GB |
| **Embeddings**: meaning-based requirement matching | `text-embedding-nomic-embed-text-v1.5` | ~0.1 GB |

The two jobs are split on purpose: the small judge models invented several letter
sentences per job for the fact-check to delete, so a larger model writes and the small one
checks. The writer runs at a low temperature (0.1) so it sticks to the facts.

Start LM Studio's local server (Developer tab → Start Server, default
`http://localhost:1234/v1`). **You do not need to load the models yourself:** before
each run the app loads the judge and the writer with LM Studio's `lms` tool, with a
6144-token context each, and unloads other chat models to make room on the GPU. The
embedding model loads on first use. To use other models, change **Judge model** and
**Letter writer** under Settings for the session, or set `ATS_MODELS` / `ATS_WRITER`
(see [Configuration](#configuration)) to change the defaults; on the command line use
`--model` / `--writer`.

Notes from testing on an RTX 3050 (6 GB):
- One job takes about **5–10 minutes** from plan approval to finished files with the
  default models, most of it the 27B writer and the per-sentence fact-check.
- Bonsai-27B is a 1-bit (Q1_0) model on the Qwen3.5 architecture and runs fine. Other
  **Qwen3.5**-architecture models at normal quantisations (e.g. Qwen3.5 4B) ran at under
  2 tokens/s on this GPU — avoid them.
- A small writer such as `llama-3.2-3b-instruct` is much faster, but more of its letter
  sentences are cut by the fact-check, so letters more often fall back to the plain
  version built from your profile.

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

A sidebar on the left has five pages, in a dark theme that is easy on the eyes over long
sessions. Searches run in the background; the status bar at the bottom shows progress
and the latest message, and **Activity log** opens the full log.

| Page | Purpose |
|---|---|
| **Jobs** | every job found so far, best fit first: search, filter (All / New / Last 24h / Tracked, Fit ≥), tailor a CV, track |
| **Find jobs** | one source at a time, grouped: Germany (Arbeitsagentur, Personio, Workday), Europe (EURES internships, Adzuna, LinkedIn/Indeed), PhD & research (EURAXESS, research institutes and new employers, graduate employer boards), US & Asia (USAJOBS, Adzuna, MyCareersFuture, market notes). Each source shows only its search fields; its checkboxes (countries, sites, sources, "only graduate titles"…) are under **Advanced filters ▸**, which shows what is ticked while closed. Starting a search switches to Jobs, where new rows appear in green |
| **Applications** | application tracker and the CV & cover-letter generator |
| **New companies** | companies and startups newly hiring, and their job boards |
| **Settings** | CV models and server, profile, API keys, daily sweep, SEC email, health checks |

The **daily sweep** runs every enabled source in one go: **Run sweep now** at the bottom
of the sidebar, or schedule it under Settings → **Daily sweep…**.

**Generating a CV and cover letter**
1. Double-click a job under **Jobs** (or select it and press **Tailor CV**). Applications
   opens with the job description filled in; you can also paste one.
2. **Options ▸** holds *Region* (photo / date of birth / nationality conventions),
   *CV style* (`ats`, `styled` or `both`), *Cover letter*, *Review plan first*,
   *Compile PDF*, the recruiter's name and extra context for the letter.
3. The judge and letter-writer models are under **Settings** (*Judge model*,
   *Letter writer*) — leave the defaults unless you use other models in LM Studio.
4. Press **Generate**. With *Review plan first* ticked, a window shows which
   requirements you meet and which bullets may be reworded; untick bullets to keep them
   word for word, then **Write CV**.
5. The line under the buttons shows the fit and any warnings; **More ▾ → Open outputs
   folder** opens the files. Read `report.md` before sending anything — see
   [Output files](#output-files) for what each section means.

If the job's required skills are all missing from your profile, the app writes the CV
and the report but **no cover letter** ("no cover letter: none of the required skills are
in your profile"): a letter could only list unrelated experience.

To track a job, select it under **Jobs** and use **Track ▾** (or right-click it).
**More ▾** on the Jobs page exports the visible jobs, logs a summary by source, type,
market and company, re-scores against your profile, and deletes untracked jobs.
**Fit score** (Applications) scores pasted text without adding it to the list.

**Settings → API keys…** stores the free keys for USAJOBS and Adzuna in `settings.json`.
A key saved there overrides an environment variable of the same name. The US and Asia
employer boards are under Find jobs → Graduate employer boards → *Region*.

Everything the command-line scripts below can do is also in the app.

**Daily sweep** (Settings → Daily sweep…) shows only the fit alert and the Windows schedule; the sources, startup/notification options and search terms are in sections you open (**Sources ▸**, **Startup & notifications ▸**, **Search terms ▸**), each showing a summary while closed. It can also cover Adzuna Europe and the US and
Asia-Pacific: US & Asia employer boards, USAJOBS, Adzuna and MyCareersFuture. They are
off by default. Set their search terms, countries and posting age in the same window.
Adzuna Europe searches by role (e.g. `Werkstudent`) plus any of a list of field words
(e.g. `Mechatronik Robotik Automatisierung`), because Adzuna matches whole words and a
two-word search like `Werkstudent Mechatronik` misses most postings. Leave the roles
and field words empty to use each country's defaults in its own language. The scheduled
Windows task reads the keys saved under **API keys** too, and skips a keyed source whose
key is missing.

**Settings → Health checks** reports pass, warn, fail or skip for each check, and
selecting a row shows the full reason. **Copy report** puts the results on the clipboard.
- *APIs*: each job source answers a small real search. Keyed sources are skipped
  without a key. JobSpy is
  only checked as installed, because a test scrape of LinkedIn risks a temporary block.
- *LM Studio*: the app and its `lms` tool are installed, every model the CV generator
  uses is downloaded, and the local server answers.
- *Hardware*: 64-bit Python, AVX2, RAM, GPU memory compared with the models' size,
  free disk space, and `pdflatex` for *Compile PDF*.

The checks change nothing: no jobs are stored and no models are loaded.

---

## Command line

```bash
# CV + letter for one job, headless (same pipeline and defaults as the Generate button)
EUJobSearch.exe --ats job.txt --region DE
# other models: --model is the judge, --writer writes the letter
EUJobSearch.exe --ats job.txt --model "qwen/qwen3-4b-2507" --writer "llama-3.2-3b-instruct"

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
python jobspy_provider.py search "robotics engineer" --location Berlin --pages 2
python linkedin_jobs.py export jobs.csv
python discover_jobs.py crawl wikidata yc hn --region germany   # find new employers
python discover_jobs.py sites https://www.example-robotics.de   # read any company or lab site
```

---

## How CV and letter generation works

```
job ad ──► 1  requirements (quote-verified) + job title/company
           2  gap analysis: vocabulary → meaning-based matching (embeddings)
              → judge verifies the top-3 profile lines ("yes / partly / no");
              a job or project bullet naming the skill beats a skills-list line
           3  rewrite plan (you can review it)
           4  CV bullets reworded one at a time by the judge, each checked
           5  cover letter by the writer, from numbered facts (F1, F2, …):
              every sentence cites the fact(s) it uses
              → each sentence checked against its own cited facts (judge + code);
                one that says more is replaced with the profile's own line
              → at most 4 work/project examples, ranked by relevance to the job;
                each says where it happened
              → whole-letter fact-check (judge + code) → clean-up (padding,
                repeats, job-ad talk) → opening/closing added
              → short but true? split into paragraphs, add your courses and
                languages; still too little? letter built from profile.json instead
           6  recruiter message (3 sentences): intro, one real job or project
              example, one question; fact-checked the same way
           7  LaTeX → PDF, report.md
```

**What the checks guard against** (deterministic code in `ats_checks.py` and
`ats_pipeline.py`, on top of the judge model):
- claiming a requirement you do not meet, or a skill/tool not in your profile
- adding a tool to a fact that does not mention it ("…ANSYS workflows using Python")
- inflation ("led", "expertise", "extensive"), made-up numbers, "1 feature" → "features"
- merging two bullets into one claim, or attaching work to the wrong employer/project
- "This reduced downtime…" placed after a different example than the one it belongs to
- research interests presented as experience, coursework, skills or "focus"
- an unfinished degree mentioned without saying so; schools renamed or translated
- "keen to learn X" when X is already in your profile, and empty "keen to learn about the
  role" lines
- text copied from the job ad or addressed to the reader ("you will…"), restating
  the job's requirements, and tool lists longer than three items
- the same place named in every sentence ("As a … at …, I …" → "There, I also …")
- CV bullets that drop a number, tool or action verb, add buzzwords, or repeat words

Job titles and company names with abbreviations ("Stud. Assistant", "H. & W. … GmbH",
"o.ä.") are kept whole, so their dots are not read as sentence ends.

**Writing style.** Prompts follow a "write like a person" style guide (plain words,
varied sentence length, no stock AI phrases, no lists of three). `report.md` includes a
**predictability score** (0–100) from sentence rhythm, repeated openings, stock phrases
and — for model-written sentences — their real perplexity from LM Studio. It is an
estimate, not an AI-detector result.

---

## Output files

Each run writes to `ats_outputs\<company>_<role>_<date>\` in the **project folder** (the
folder above `dist\` for the `.exe`; an `.exe` copied elsewhere uses its own folder). The
folder is in `.gitignore`.

| File | Content |
|---|---|
| `cv_ats.tex/.pdf` | plain one-column CV for upload to job portals |
| `cv_styled.tex/.pdf` | styled CV for email / people |
| `cover_letter_ats.tex/.pdf` | plain cover letter |
| `cover_letter_styled.tex/.pdf` (+ `info.tex`, `body.tex`) | styled cover letter |
| `recruiter_message.txt` | short message for LinkedIn / email |
| `report.md` | what was generated and everything you should check (below) |
| `tailored_experience.json` | the reworded experience section and notes |

**`report.md` sections** (a section appears only when it has something to say):

| Section | Meaning |
|---|---|
| Keyword fit | score, requirements met / missed with the proof line for each, hard requirements you do not meet |
| What was generated | per job: bullets reworded, reordered or kept |
| Writing style | predictability score for the letter and the recruiter message |
| Removed by the fact-check | sentences cut as unsupported by your profile, and why; repaired ones show the replacement |
| Left out to keep the letter to 4 examples | true facts dropped as less relevant to this job |
| The model's draft that was replaced | when the letter fell back to the plain profile version: the reason and the draft |
| Removed by the clean-up | padding, repeats and job-ad talk taken out |
| Checks that still fail | warnings to act on (e.g. German level, unfinished degree, letter built from the profile) |
| Sentences to double-check | sentences the model worded itself; they passed every check, but only you know if each is exactly right |
| Notes | e.g. how many sentences the fact-check changed |

For a signature and photo, put `sig.png` and `profile_pic.png` in
`%APPDATA%\EUJobSearch\assets\` (the photo is used only where the region expects one).

---

## Configuration

**Data folder:** `%APPDATA%\EUJobSearch` for the `.exe`; the project folder when run from
source. Contains the job database (`robotics_jobs.db`), `company_radar.db`,
`sweep_config.json`, `sweep_state.json`, `settings.json` (remembered profile path, API
keys, SEC contact email, discovery options), `embed_cache.json` and `discovery_cache.json`.
Generated CVs and letters go to `ats_outputs\` in the project folder instead (see
[Output files](#output-files)).

**Environment variables** (all optional; the API keys can also be set in the app):

| Variable | Purpose |
|---|---|
| `ATS_MODELS` | judge model, default `qwen/qwen3-4b-2507` |
| `ATS_WRITER` | letter-writer model(s), default `prism-ml/bonsai-27b`; comma-separate several to draft in parallel |
| `ATS_BASE_URL` | OpenAI-compatible server, default `http://localhost:1234/v1` |
| `ATS_MODEL` | model used when the judge field is empty (default: the one LM Studio has loaded) |
| `ATS_EMBED_MODEL` | embedding model, default `text-embedding-nomic-embed-text-v1.5` |
| `JOBSEARCH_DATA_DIR` | override the data folder |
| `JOBS_DB` | override the job database path |
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
| `system_checks.py` | the health checks (Settings): API, LM Studio and hardware |
| `linkedin_jobs.py` | job model, SQLite store, shared HTTP helpers, LinkedIn APIs |
| `eu_student_jobs.py` | EURES and student-job classification |
| `robotics_track.py` | EURAXESS PhDs and employer ATS boards |
| `ba_jobsuche.py`, `personio_jobs.py`, `workday_jobs.py`, `research_jobs.py` | further sources |
| `company_radar.py` | newly hiring companies |
| `discover_jobs.py` | open-ended discovery: Common Crawl job boards, career-page detector, Wikidata, YC, HN |
| `us_asia_jobs.py` | US / Asia-Pacific sources |
| `jobspy_provider.py`, `install_jobspy.py` | optional LinkedIn/Indeed engine |
| `build.bat`, `jobsearch.spec` | Windows build |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Could not reach http://localhost:1234" | start LM Studio's server (Developer tab → Start Server) |
| "Not downloaded in LM Studio: …" | download the named model in LM Studio |
| "lms tool was not found" | install LM Studio's CLI (`~\.lmstudio\bin\lms.exe`) or load the models manually |
| A model loads but is extremely slow | it is probably a Qwen3.5-architecture model at a normal quantisation — use the defaults |
| A job takes 5–10 minutes | normal with the 27B writer on a 6 GB GPU; a smaller writer is faster but its letters lose more to the fact-check |
| "answer ran past its … token limit" | raise the model's context in LM Studio to 8k+ |
| Cover letter "assembled from profile.json" | the writer's draft lost too much to the fact-check; `report.md` shows the draft and the reason. The letter is true but plain — personalise it |
| "no cover letter: none of the required skills are in your profile" | intentional: the CV is still written; a letter would have nothing relevant to say |
| No PDFs | install MiKTeX/TeX Live and tick *Compile PDF*; missing packages install on first use |
| "EUJobSearch.exe is running" during build | close the app first |

---

## Privacy

Everything runs locally: job searches call public job APIs, and the CV/letter models run
in LM Studio on your machine. Nothing from your profile is sent to an online AI service.

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
