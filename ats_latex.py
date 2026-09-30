"""
ats_latex.py — write the ATS and styled CV / cover letter as LaTeX and compile to PDF.

Turn the parsed application package into LaTeX source files.

Two styles:
  ats     - single column, no photo, no icons, standard headings. This is what you
            upload to a portal. It is deliberately plain because that is what parses.
  styled  - keeps the look of the candidate's existing template (blue headings,
            photo where the region allows it). Use it for human eyes / email.
"""

import re
import shutil
from pathlib import Path

SPECIALS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def pretty_date(iso: str) -> str:
    """'2026-09-16' -> '16 September 2026'; passes anything else through."""
    try:
        from datetime import datetime
        return datetime.strptime(iso.strip(), "%Y-%m-%d").strftime("%d %B %Y").lstrip("0")
    except (ValueError, AttributeError):
        return iso


def esc(text: str) -> str:
    if text is None:
        return ""
    text = str(text)
    # Models sometimes escape entities inside CDATA, where they are literal.
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'")):
        text = text.replace(ent, ch)
    out = []
    for ch in text:
        out.append(SPECIALS.get(ch, ch))
    s = "".join(out)
    s = s.replace("–", "--").replace("—", "---").replace("’", "'")
    s = s.replace("“", "``").replace("”", "''")
    # "C++/Python" cannot be hyphenated and pushed a styled-letter line 15pt past
    # the margin: allow a line break after a slash between words (zero width).
    s = re.sub(r"(?<=[\w+])/(?=\w)", r"/\\allowbreak{}", s)
    return s


# ---------------------------------------------------------------------- language

# Fixed text the renderer adds around the model's content. Keeping it in the
# document language avoids a German letter that opens with "Dear" and closes
# with "Kind regards".
TEXT = {
    "en": {
        "summary": "Professional Summary", "skills": "Skills",
        "experience": "Professional Experience", "projects": "Projects",
        "education": "Education", "certifications": "Certifications",
        "languages": "Languages", "profile": "Personal Profile",
        "work": "Work Experience", "dob": "Date of Birth",
        "greeting": "Dear", "generic_recipient": "Hiring Manager",
        "generic_greeting": "Dear Hiring Manager", "closer": "Kind regards,",
    },
    "de": {
        "summary": "Profil", "skills": "Kenntnisse",
        "experience": "Berufserfahrung", "projects": "Projekte",
        "education": "Ausbildung", "certifications": "Zertifikate",
        "languages": "Sprachen", "profile": "Profil",
        "work": "Berufserfahrung", "dob": "Geburtsdatum",
        "greeting": "Sehr geehrte/r", "generic_recipient": "Personalabteilung",
        # German letters close without a comma.
        "generic_greeting": "Sehr geehrte Damen und Herren", "closer": "Mit freundlichen Grüßen",
    },
}

GENERIC_RECIPIENTS = {"", "hiring manager", "hiring team", "recruiter", "personalabteilung"}


def _t(lang: str) -> dict:
    return TEXT.get(lang, TEXT["en"])


def _salutation(recipient: str, lang: str) -> tuple[str, str]:
    """(recipient line, salutation line) for the letter header."""
    t = _t(lang)
    rec = (recipient or "").strip()
    if rec.lower() in GENERIC_RECIPIENTS:
        return t["generic_recipient"], t["generic_greeting"]
    return rec, f"{t['greeting']} {_address(rec, lang)}"


_TITLE = re.compile(r"^(prof\.?|professor|dr\.?(-ing\.?)?|dipl\.?-ing\.?|mr\.?|ms\.?|mrs\.?|"
                    r"herr|frau)$", re.I)


def _address(name: str, lang: str) -> str:
    """'Prof. Dr. Lukas Rosenberger Schmid' -> 'Prof. Rosenberger Schmid' (English
    letters use the highest title and the surname, never the first name; German
    ones keep every title: 'Prof. Dr. Rosenberger Schmid')."""
    parts = name.split()
    titles = []
    while parts and _TITLE.match(parts[0]):
        titles.append(parts.pop(0))
    if not titles or len(parts) < 2:
        return name                     # a plain name or a team: leave as given
    surname = " ".join(parts[1:])       # first token is the given name
    academic = [t for t in titles if t.lower().startswith(("prof", "dr"))]
    if lang == "en":
        top = next((t for t in academic if t.lower().startswith("prof")), None) or             (academic[0] if academic else titles[0])
        return f"{top} {surname}"
    return " ".join(academic or titles) + " " + surname


def languages_line(profile) -> str:
    """Languages always come from profile.json, the ground truth, never from
    the model (which tends to echo the prompt's '@ ... | ...' format)."""
    return ", ".join(f"{l['language']} ({l['level']})" for l in profile.get("languages", []))


# --------------------------------------------------------------------------- parsing

def parse_entries(block: str):
    """Parse the '@ header | fields' + '- bullet' mini-format into dicts."""
    entries = []
    for raw in (block or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("@"):
            fields = [f.strip() for f in line[1:].split("|")]
            fields += [""] * (4 - len(fields))
            entries.append({"title": fields[0], "org": fields[1],
                            "place": fields[2], "dates": fields[3], "bullets": []})
        elif line.startswith(("-", "*", "•")) and entries:
            entries[-1]["bullets"].append(line[1:].strip())
        elif entries:
            entries[-1]["bullets"].append(line)
    return entries


def parse_skills(block: str):
    rows = []
    for raw in (block or "").splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line:
            continue
        if ":" in line:
            label, items = line.split(":", 1)
            label, items = label.strip(), items.strip()
            # Small models copy the spec literally: "Category: Tools, ROS2, Git".
            # The real label is then the first item.
            if label.lower() in ("category", "kategorie") and "," in items:
                label, items = (x.strip() for x in items.split(",", 1))
            rows.append((label, items))
        else:
            rows.append(("", line))
    return rows


def _headline(e):
    bits = [b for b in (e["org"], e["place"]) if b]
    return " | ".join(bits)


# ----------------------------------------------------------------------- ATS CV

ATS_PREAMBLE = r"""% ATS-optimised CV -- single column, no tables, no images, no icons.
% Generated by ats_tailor. Compile with: pdflatex cv_ats.tex
\documentclass[11pt,a4paper]{article}
\usepackage[left=0.75in,right=0.75in,top=0.75in,bottom=0.75in]{geometry}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
% Latin Modern: outline fonts. The default T1 Computer Modern came out as bitmap fonts
% here, whose text layer broke words (no "fl"/"fi") and which microtype refuses.
\usepackage{lmodern}
% ATS parsers read the PDF's text layer: without these the "fl"/"fi"/"ff" ligatures
% came out as "workows", "dierent", "Articial" when the CV was copied or parsed.
\input{glyphtounicode}
\pdfgentounicode=1
\usepackage{microtype}
\DisableLigatures{encoding = *, family = *}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage{parskip}
\usepackage[hidelinks]{hyperref}
\pagestyle{empty}
\titleformat{\section}{\large\bfseries\uppercase}{}{0em}{}[\vspace{-0.6em}\rule{\linewidth}{0.6pt}]
\titlespacing*{\section}{0pt}{0.9em}{0.4em}
\setlist[itemize]{label=\textbullet, leftmargin=1.2em, noitemsep, topsep=2pt, parsep=0pt}
\begin{document}
"""


def contact(p: dict) -> list:
    """The candidate's contact details as [(kind, text)] in ONE order for all four
    documents: location, phone, email, LinkedIn. The styled letter used to show the
    phone as "49.155.104.19941" (profile.json's phone_dotted) and its own order,
    and the styled CV wrote "LinkedIn: /in/...": the same person looked different
    on each page."""
    handle = re.sub(r"^(https?://)?(www\.)?linkedin\.com/in/", "", str(p.get("linkedin", ""))).strip("/")
    items = [("location", p.get("location", "")), ("phone", p.get("phone", "")),
             ("email", p.get("email", "")),
             ("linkedin", f"linkedin.com/in/{handle}" if handle else "")]
    return [(k, str(v).strip()) for k, v in items if str(v).strip()]


_ICONS = {"location": r"\faMapMarker*", "phone": r"\faPhone", "email": r"\faEnvelope",
          "linkedin": r"\faLinkedinIn"}


def _contact_link(kind: str, text: str) -> str:
    """The escaped text, linked where a link makes sense."""
    if kind == "email":
        return r"\href{mailto:" + text + "}{" + esc(text) + "}"
    if kind == "phone":
        return r"\href{tel:" + re.sub(r"[^\d+]", "", text) + "}{" + esc(text) + "}"
    if kind == "linkedin":
        return r"\href{https://" + text + "}{" + esc(text) + "}"
    return esc(text)


def contact_icons(p: dict, sep: str = r"\quad ") -> str:
    """The styled documents' contact line: icon + text, in contact() order."""
    return sep.join(_ICONS[k] + r"\enspace " + _contact_link(k, v) for k, v in contact(p))


def render_cv_ats(pkg, region, profile, lang: str = "en") -> str:
    t = _t(lang)
    p = profile["personal"]
    r = pkg["resume"]
    out = [ATS_PREAMBLE]
    a = out.append

    a(r"\begin{center}")
    a(r"{\LARGE \textbf{" + esc(p["name"]) + r"}}\\[0.35em]")
    details = [text for _kind, text in contact(p)]
    if region.dob and p.get("date_of_birth"):
        details.append(f"{t['dob']}: " + p["date_of_birth"])
    if region.nationality and p.get("work_authorisation"):
        details.append(p["work_authorisation"])
    a(r" \textbar\ ".join(esc(c) for c in details))
    a(r"\end{center}")
    a("")

    if r.get("summary"):
        a(r"\section*{" + t["summary"] + "}")
        a(esc(r["summary"].strip()))
        a("")

    skills = parse_skills(r.get("skills", ""))
    if skills:
        a(r"\section*{" + t["skills"] + "}")
        a(r"\begingroup\hyphenpenalty=10000\exhyphenpenalty=10000")   # no "Solid-works"
        for label, items in skills:
            a((r"\textbf{" + esc(label) + ":} " if label else "") + esc(items) + r" \\")
        a(r"\par\endgroup")
        a("")

    for key in ("experience", "projects", "education"):
        entries = parse_entries(r.get(key, ""))
        if not entries:
            continue
        a(r"\section*{" + t[key] + "}")
        for e in entries:
            a(r"\textbf{" + esc(e["title"]) + r"} \hfill " + esc(e["dates"]) + r" \\")
            head = _headline(e)
            if head:
                a(r"\textit{" + esc(head) + r"}")
            if e["bullets"]:
                a(r"\begin{itemize}")
                for b in e["bullets"]:
                    a(r"  \item " + esc(b))
                a(r"\end{itemize}")
            a(r"\vspace{0.4em}")
        a("")

    if (r.get("certifications") or "").strip():
        a(r"\section*{" + t["certifications"] + "}")
        for line in r["certifications"].splitlines():
            line = line.strip().lstrip("-*• ").strip()
            if line:
                a(esc(line) + r" \\")
        a("")

    langs = languages_line(profile)
    if langs:
        a(r"\section*{" + t["languages"] + "}")
        a(esc(langs))
        a("")

    a(r"\end{document}")
    return "\n".join(out)


# --------------------------------------------------------------------- styled CV

STYLED_PREAMBLE = r"""% Styled CV -- keeps the candidate's original visual template.
% Compile with: pdflatex cv_styled.tex   (needs profile_pic.png alongside if photo=on)
\documentclass[11pt,a4paper]{article}
\usepackage[left=0.8in,top=0.8in,right=0.8in,bottom=0.8in]{geometry}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}   % outline fonts, not bitmaps (see the ATS CV)
% ligatures stay for looks, but the text layer reads "fl"/"fi" (not "workows")
\input{glyphtounicode}
\pdfgentounicode=1
\usepackage{xcolor}
\usepackage{titlesec}
\usepackage{enumitem}
\usepackage{parskip}
\usepackage{graphicx}
\usepackage{wrapfig}
\usepackage{fontawesome5}
\usepackage[hidelinks]{hyperref}
\setlength{\emergencystretch}{2em}
\definecolor{titleblue}{HTML}{00199e}
\definecolor{subtitleblue}{HTML}{2ec1e0}
\definecolor{darktext}{HTML}{222222}
\color{darktext}
\linespread{0.95}
\setlength{\parskip}{0.1em}
\titleformat{\section}{\Large\bfseries\color{titleblue}}{}{0em}{}
\titlespacing*{\section}{0pt}{0.7em}{0.15em}
\newcommand{\blueitem}[1]{\textcolor{subtitleblue}{\textbf{#1}}}
\setlist[itemize]{label=\textbullet, leftmargin=*, noitemsep, topsep=0pt, parsep=0pt}
\pagestyle{empty}
\begin{document}
"""


def render_cv_styled(pkg, region, profile, lang: str = "en", has_photo: bool = True) -> str:
    """has_photo=False leaves the photo out even where the region allows one,
    so the file still compiles when no profile_pic.png has been supplied."""
    t = _t(lang)
    p = profile["personal"]
    r = pkg["resume"]
    out = [STYLED_PREAMBLE]
    a = out.append

    if region.photo and has_photo:
        a(r"\begin{wrapfigure}{r}{0.5cm}")
        a(r"\hspace*{-2.5cm}\includegraphics[width=2.5cm]{profile_pic}")
        a(r"\end{wrapfigure}")
    a(r"{\Huge \textbf{\textcolor{titleblue}{" + esc(p["name"]) + r"}}}")
    a(r"\vspace{0.5em}")
    a("")
    # the same icons, values and order as the styled cover letter
    # one line across the text width, like the styled letter (at full size with
    # fixed gaps it ran 28pt past the right margin)
    a(r"\makebox[\linewidth][s]{\small " + contact_icons(p, r"\hfill ") + "}"
      + (r" \\" if region.dob and p.get("date_of_birth") else ""))
    if region.dob and p.get("date_of_birth"):
        a(f"{t['dob']}: " + esc(p["date_of_birth"]))
    a("")
    a(r"\rule{\linewidth}{0.4pt}")

    if r.get("summary"):
        a(r"\section*{" + t["profile"] + "}")
        a(esc(r["summary"].strip()))
        a("")  # end the paragraph, or the rule is set inline and overflows the margin
        a(r"\rule{\linewidth}{0.4pt}")

    skills = parse_skills(r.get("skills", ""))
    if skills:
        a(r"\section*{" + t["skills"] + "}")
        # tool names are never hyphenated ("Solid-works" split across lines); the
        # setting must hold where the paragraph ends, so it wraps the whole block
        a(r"\begingroup\hyphenpenalty=10000\exhyphenpenalty=10000")
        for label, items in skills:
            a((r"\textbf{" + esc(label) + ":} " if label else "") + esc(items) + r" \\")
        a(r"\par\endgroup")
        a(r"\rule{\linewidth}{0.4pt}")

    for heading, key in ((t["work"], "experience"),
                         (t["projects"], "projects"),
                         (t["education"], "education")):
        entries = parse_entries(r.get(key, ""))
        if not entries:
            continue
        a(r"\section*{" + heading + "}")
        for e in entries:
            a(r"\blueitem{" + esc(e["dates"]) + ": " + esc(e["title"]) + r"} \\")
            head = _headline(e)
            if head:
                a(r"\textit{" + esc(head) + r"}")
            if e["bullets"]:
                a(r"\begin{itemize}")
                for b in e["bullets"]:
                    a(r"  \item " + esc(b))
                a(r"\end{itemize}")
            a(r"\vspace{0.3em}")
        a(r"\rule{\linewidth}{0.4pt}")

    langs = languages_line(profile)
    if langs:
        a(r"\section*{" + t["languages"] + "}")
        a(esc(langs))

    a(r"\end{document}")
    return "\n".join(out)


# ----------------------------------------------------------------- cover letter

def fill(template: str, **values) -> str:
    """Token substitution with <<name>> markers - safe around LaTeX % and {}."""
    for key, val in values.items():
        template = template.replace(f"<<{key}>>", val)
    return template


def _paragraphs(text: str):
    text = (text or "").strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [esc(" ".join(p.split())) for p in paras]


COVER_ATS = r"""% ATS-safe cover letter -- plain, single column, no graphics.
\documentclass[11pt,a4paper]{article}
\usepackage[left=1in,right=1in,top=1in,bottom=1in]{geometry}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
% Latin Modern: outline fonts. The default T1 Computer Modern came out as bitmap fonts
% here, whose text layer broke words (no "fl"/"fi") and which microtype refuses.
\usepackage{lmodern}
% text layer without broken ligatures (see the ATS CV)
\input{glyphtounicode}
\pdfgentounicode=1
\usepackage{microtype}
\DisableLigatures{encoding = *, family = *}
\usepackage{parskip}
\usepackage[hidelinks]{hyperref}
\pagestyle{empty}
\begin{document}

\noindent\textbf{<<name>>}\\
<<contact>>

\vspace{1em}
\noindent <<date>>

\vspace{1em}
\noindent <<recipient>>\\
<<company>>\\
<<city>>

\vspace{1em}
\noindent <<salutation>>,

<<body>>

\vspace{1em}
\noindent <<closer>>\\[1.5em]
<<name>>
\end{document}
"""


def render_cover_letter_ats(pkg, profile, today: str, lang: str = "en") -> str:
    p = profile["personal"]
    m = pkg["meta"]
    body = "\n\n".join(_paragraphs(pkg.get("cover_letter", "")))
    recipient, salutation = _salutation(m.get("recipient"), lang)
    return fill(
        COVER_ATS,
        **{
        "name": esc(p["name"]), "contact": r"\\".join(esc(t) for _k, t in contact(p)),
        "date": esc(pretty_date(today)),
        "recipient": esc(recipient), "salutation": esc(salutation),
        "closer": esc(_t(lang)["closer"]),
        "company": esc(m.get("company") or ""), "city": esc(m.get("city") or ""),
        "body": body,
    })


COVER_STYLED_MAIN = r"""% Styled cover letter -- based on the candidate's existing template.
% Compile with: pdflatex cover_letter_styled.tex   (needs info.tex, body.tex, sig.png)
\documentclass[12pt]{letter}
\usepackage[utf8]{inputenc}
% ligatures stay for looks, but the text layer reads "fl"/"fi"
\input{glyphtounicode}
\pdfgentounicode=1
\usepackage[empty]{fullpage}
\usepackage[hidelinks]{hyperref}
\usepackage{graphicx}
\usepackage{fontawesome5}
\usepackage{eso-pic}
\usepackage{charter}

\addtolength{\topmargin}{-0.5in}
\addtolength{\textheight}{1.0in}
\definecolor{gr}{RGB}{225,225,225}
\setlength{\emergencystretch}{2em}

\input{info}

\begin{document}

\AddToShipoutPictureBG{%
\color{gr}
\AtPageUpperLeft{\rule[-1.3in]{\paperwidth}{1.3in}}
}

\begin{center}
{\fontsize{28}{0}\selectfont \myname}

% one line: at the letter's 12pt the four items wrapped LinkedIn onto a second line
\makebox[\linewidth][s]{\footnotesize \mycontact}
\end{center}

\vspace{0.2in}

\mydate\\

\vspace{-0.1in}\recipient\\
\company\\
\city\\

\vspace{-0.1in}\salutation,\\

\vspace{-0.1in}\setlength\parindent{24pt}
\noindent\input{body}

\vspace{0.1in}
\begin{flushleft}
\closer

<<signature>>

\myname\\
\mytitle
\end{flushleft}

\end{document}
"""

COVER_STYLED_INFO = r"""% Generated by ats_tailor -- edit freely.
\newcommand{\myname}{<<name>>}
\newcommand{\mytitle}{<<title>>}
\newcommand{\myemail}{<<email>>}
\newcommand{\mylinkedin}{<<linkedin>>}
\newcommand{\myphone}{<<phone>>}
\newcommand{\mylocation}{<<location>>}
% location, phone, email, LinkedIn - the same order and form as the CVs
\newcommand{\mycontact}{<<contact>>}
\newcommand{\mydate}{<<date>>}
\newcommand{\recipient}{<<recipient>>}
\newcommand{\salutation}{<<salutation>>}
\newcommand{\closer}{<<closer>>}
\newcommand{\company}{<<company>>}
\newcommand{\street}{}
\newcommand{\city}{<<city>>}
\newcommand{\state}{}
\newcommand{\zip}{}
"""


def render_cover_letter_styled(pkg, profile, lang: str = "en", has_signature: bool = True):
    """has_signature=False drops the signature image so the letter still
    compiles when no sig.png has been supplied."""
    p = profile["personal"]
    m = pkg["meta"]
    recipient, salutation = _salutation(m.get("recipient"), lang)
    main = fill(COVER_STYLED_MAIN, signature=(
        r"\vspace{0.1in}\includegraphics[width=1.5in]{sig.png}\vspace{0.1in}"
        if has_signature else r"\vspace{0.4in}"))
    info = fill(
        COVER_STYLED_INFO,
        **{
        "name": esc(p["name"]), "title": esc(p.get("title", "")),
        "email": esc(p["email"]), "linkedin": esc(dict(contact(p)).get("linkedin", "")),
        "phone": esc(p["phone"]), "location": esc(p["location"]),
        "contact": contact_icons(p, r"\hfill "),
        # the ATS letter's date format (\today printed "September 29, 2026")
        "date": esc(pretty_date(m.get("date", ""))),
        "recipient": esc(recipient), "salutation": esc(salutation),
        "closer": esc(_t(lang)["closer"]),
        "company": esc(m.get("company") or ""), "city": esc(m.get("city") or ""),
    })
    body = "\n\n".join(_paragraphs(pkg.get("cover_letter", "")))
    return main, info, body


# ------------------------------------------------------------------------ assets

def copy_assets(assets_dir: Path, out_dir: Path, want_photo: bool) -> dict:
    """Copy photo/signature if present. Returns {"photo": bool, "signature": bool}
    saying which ones actually exist, so the renderer can leave out the rest."""
    found = {}
    for key, name, wanted in (("photo", "profile_pic.png", want_photo),
                              ("signature", "sig.png", True)):
        src = assets_dir / name
        found[key] = bool(wanted and src.exists())
        if found[key]:
            shutil.copy(src, out_dir / name)
    return found
