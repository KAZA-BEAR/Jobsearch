#!/usr/bin/env python3
"""
job_gui.py — desktop front-end for the EU robotics / mechatronics job search.

Run:  python job_gui.py

Needs linkedin_jobs.py, eu_student_jobs.py and robotics_track.py in the same
folder. Tkinter ships with Python; nothing else to install beyond `requests`.
"""

from __future__ import annotations

import csv
import json
import queue
import re
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from linkedin_jobs import Store, Job, STATUSES, age_hours, data_dir, parse_posted
from eu_student_jobs import MARKETS, KINDS, EuresProvider, classify, is_student_suitable
from ats_pipeline import (
    DEFAULT_BACKEND, DEFAULT_BASE_URL, DEFAULT_LOCAL_MODELS, DEFAULT_MODEL,
    PipelineError, list_local_models, run as ats_run,
)
from ats_regions import DEFAULT_REGION, REGIONS as ATS_REGIONS
from company_radar import (
    FormDScanner,
    BoardProber,
    RadarStore,
    emerging_employers,
    FUNDING_KEYWORDS,
)
from us_asia_jobs import (
    US_ASIA_BOARDS,
    COUNTRY_NAMES as US_ASIA_COUNTRY_NAMES,
)
from robotics_track import (
    BOARDS,
    ATSBoards,
    PhdProvider,
    InternshipProvider,
    PHD_TERMS,
    ROBOTICS_FIELDS,
    FUNDING_MARKERS,
    GRAD_MARKERS,
    MECHATRONICS_MARKERS,
    SENIOR_MARKERS,
    COUNTRY_NAMES as EU_COUNTRY_NAMES,
)
from jobspy_provider import JobSpyProvider, DEFAULT_SITES, INDEED_COUNTRIES
from ba_jobsuche import BAJobsucheProvider, OFFER_TYPES as BA_OFFER_TYPES
from personio_jobs import COMPANIES as PERSONIO_COMPANIES, PersonioCompany, PersonioFeeds
from workday_jobs import SITES as WORKDAY_SITES, WorkdayBoards, parse_url as parse_workday_url
from research_jobs import SOURCES as RESEARCH_SOURCES, ResearchFeeds
from fit_score import (FitScorer, default_profile_path, is_bundled_copy, load_default_scorer,
                       remember_profile_path, rescore)
import daily_sweep

# board.country ("DE", "US", "JP", ...) -> jobspy's expected country_indeed spelling
JOBSPY_COUNTRY_NAMES = {**EU_COUNTRY_NAMES, **US_ASIA_COUNTRY_NAMES}


def _jobspy_fallback_for(store: Store, boards, w: "Worker", strict: bool) -> None:
    """Search LinkedIn/Indeed by company name for boards that returned nothing.

    Shared by the Graduate roles, US and Asia-Pacific tabs, so an employer
    whose own ATS board is dead or empty still turns up results the same way
    the CLI's --jobspy-fallback does.
    """
    if not boards:
        return
    try:
        provider = JobSpyProvider(site_name=["indeed", "linkedin"])
    except SystemExit as exc:
        w.log(f"JobSpy fallback unavailable: {exc}")
        return
    w.log(f"{len(boards)} employer board(s) returned nothing directly; "
          "falling back to LinkedIn/Indeed search for those companies…")
    fb_seen = fb_new = 0
    for board in boards:
        if w.cancelled.is_set():
            return
        location = JOBSPY_COUNTRY_NAMES.get(board.country, board.country)
        try:
            jobs = list(provider.search(
                f'"{board.company}" graduate OR junior OR entry level', location, 1, False,
            ))
        except Exception as exc:
            w.log(f"  {board.company}: {exc}")
            continue
        wanted_name = re.sub(r"[^a-z0-9]", "", board.company.lower())
        for job in jobs:
            fb_seen += 1
            got_name = re.sub(r"[^a-z0-9]", "", job.company.lower())
            if not got_name or (wanted_name not in got_name and got_name not in wanted_name):
                continue
            blob = f"{job.title} {job.description[:800]}"
            if not MECHATRONICS_MARKERS.search(blob) or SENIOR_MARKERS.search(job.title):
                continue
            is_grad = bool(GRAD_MARKERS.search(blob))
            if strict and not is_grad:
                continue
            job.employment_type = "graduate" if is_grad else "entry-candidate"
            if store.upsert(job):
                fb_new += 1
                w.found(job)
    w.log(f"fallback: {fb_seen} scanned · {fb_new} new")

# Kept under the old name; the implementation moved to linkedin_jobs so the
# headless daily sweep shares the same location.
_data_dir = data_dir


DB = str(_data_dir() / "robotics_jobs.db")


# --------------------------------------------------------------------------
# Background worker
# --------------------------------------------------------------------------

class Worker(threading.Thread):
    """Runs a search off the UI thread and reports back through a queue."""

    def __init__(self, fn, outbox: queue.Queue):
        super().__init__(daemon=True)
        self.fn = fn
        self.outbox = outbox
        self.cancelled = threading.Event()

    def log(self, msg: str) -> None:
        self.outbox.put(("log", msg))

    def found(self, job: Job, tag: str = "") -> None:
        self.outbox.put(("job", (job, tag)))

    def run(self) -> None:
        try:
            self.fn(self)
        except Exception:
            self.outbox.put(("log", "ERROR:\n" + traceback.format_exc()))
        finally:
            self.outbox.put(("done", None))


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("EU Robotics & Mechatronics Job Search")
        self.geometry("1100x720")
        self.minsize(900, 600)

        self.scorer: FitScorer | None = load_default_scorer()
        self.store = Store(DB, scorer=self.scorer.score if self.scorer else None)
        if self.scorer:
            rescore(self.store, self.scorer, only_missing=True)
        self.events: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self.rows: dict[str, Job] = {}

        self._build_style()
        self._build_layout()
        self.after(120, self._drain)
        self.load_saved()
        self.after(1500, self._maybe_auto_sweep)

    # ---------------- styling ----------------

    def _build_style(self) -> None:
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("Treeview", rowheight=26, font=("TkDefaultFont", 10))
        s.configure("Treeview.Heading", font=("TkDefaultFont", 10, "bold"))
        s.configure("Run.TButton", font=("TkDefaultFont", 10, "bold"), padding=6)
        s.configure("Seg.Toolbutton", padding=(12, 5), relief="flat", background="#e4e4e4")
        s.map("Seg.Toolbutton",
              background=[("selected", "#3b6ea5"), ("active", "#d0d8e4")],
              foreground=[("selected", "white")])

    # ---------------- layout ----------------

    def _build_layout(self) -> None:
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)

        panes = ttk.PanedWindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True)
        self.panes = panes

        top = ttk.Frame(panes)
        panes.add(top, weight=0)
        self.tabs = ttk.Notebook(top)
        self.tabs.pack(fill="x")
        self._tab_switcher("  Graduate & PhD  ", [
            ("PhD positions", self._panel_phd),
            ("Graduate employer boards", self._panel_grad),
            ("Research institutes", self._panel_research),
        ])
        self._tab_switcher("  Job boards  ", [
            ("Arbeitsagentur (DE)", self._panel_ba),
            ("EURES internships / Werkstudent", self._panel_student),
            ("Personio", self._panel_personio),
            ("Workday", self._panel_workday),
            ("LinkedIn / Indeed", self._panel_jobspy),
        ])
        self._tab_radar()
        self._tab_apps()
        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        mid = ttk.Frame(panes)
        panes.add(mid, weight=3)
        self._results(mid)

        bot = ttk.Frame(panes)
        panes.add(bot, weight=1)
        self._console(bot)

        self._statusbar(outer)
        self.after(50, self._fit_tabs)

    def _fit_tabs(self) -> None:
        """Size the tab area to the tab being shown.

        ttk.Notebook otherwise reserves the height of its tallest tab, which
        left short tabs with a large empty gap and squeezed the results table.
        """
        self.update_idletasks()
        current = self.nametowidget(self.tabs.select())
        self.tabs.configure(height=current.winfo_reqheight())
        self.update_idletasks()
        top = self.tabs.winfo_reqheight()
        self.panes.sashpos(0, top)
        # Keep the activity log compact so the results table gets the room.
        total = self.panes.winfo_height()
        if total > 1:
            self.panes.sashpos(1, max(top + 160, total - 110))

    def _tab_switcher(self, title: str, panels) -> None:
        """One notebook tab holding several search panels, picked with a
        segmented button row instead of a tab each."""
        tab = ttk.Frame(self.tabs, padding=(10, 8))
        self.tabs.add(tab, text=title)
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)
        var = tk.StringVar(value=panels[0][0])
        frames: dict[str, ttk.Frame] = {}

        def show() -> None:
            for fr in frames.values():
                fr.pack_forget()
            frames[var.get()].pack(fill="both", expand=True)
            self._fit_tabs()

        for label, build in panels:
            fr = ttk.Frame(body, padding=(0, 10, 0, 0))
            build(fr)
            frames[label] = fr
            ttk.Radiobutton(bar, text=label, value=label, variable=var, style="Seg.Toolbutton",
                            command=show).pack(side="left", padx=(0, 4))
        frames[panels[0][0]].pack(fill="both", expand=True)

    # ---------------- panel: PhD ----------------

    def _panel_phd(self, f) -> None:

        ttk.Label(f, text="Research field").grid(row=0, column=0, sticky="w", pady=3)
        self.phd_field = ttk.Combobox(f, values=list(ROBOTICS_FIELDS), width=32)
        self.phd_field.set("robotics")
        self.phd_field.grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(f, text="Pages per query").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.phd_pages = tk.IntVar(value=2)
        ttk.Spinbox(f, from_=1, to=10, textvariable=self.phd_pages, width=5).grid(
            row=0, column=3, sticky="w", padx=6)

        self.phd_funded = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Funded positions only (salary / TV-L E13 / MSCA)",
                        variable=self.phd_funded).grid(row=1, column=1, columnspan=3,
                                                       sticky="w", pady=4)

        ttk.Label(f, text="Countries").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        self.phd_countries = self._country_grid(f, row=2, col=1,
                                                codes=list(PHD_TERMS),
                                                preset={"DE", "NL", "SE", "CH"})

        ttk.Button(f, text="Search EURAXESS", style="Run.TButton",
                   command=self.run_phd).grid(row=3, column=1, sticky="w", pady=(12, 0))

    # ---------------- tab: graduate ----------------

    def _panel_grad(self, f) -> None:

        ttk.Label(f, text="Employer group").grid(row=0, column=0, sticky="w", pady=3)
        self.grad_tier = ttk.Combobox(
            f, width=20, state="readonly",
            values=["all", "robotics", "automation", "mobility", "semiconductor"])
        self.grad_tier.set("all")
        self.grad_tier.grid(row=0, column=1, sticky="w", padx=6)
        self.grad_tier.bind("<<ComboboxSelected>>", lambda _e: self._refresh_companies())

        ttk.Label(f, text="Region").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.grad_region = ttk.Combobox(f, width=16, state="readonly",
                                        values=list(self.GRAD_REGIONS))
        self.grad_region.set("Europe")
        self.grad_region.grid(row=0, column=3, sticky="w", padx=6)
        self.grad_region.bind("<<ComboboxSelected>>", lambda _e: self._refresh_companies())

        self.grad_strict = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Only explicit graduate / junior / entry-level titles",
                        variable=self.grad_strict).grid(row=2, column=1, columnspan=3,
                                                        sticky="w", pady=(4, 0))

        ttk.Label(f, text="Employers").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        box = ttk.Frame(f)
        box.grid(row=1, column=1, columnspan=3, sticky="w", pady=(8, 0))
        self.company_list = tk.Listbox(box, selectmode="extended", height=6, width=52,
                                       exportselection=False)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.company_list.yview)
        self.company_list.configure(yscrollcommand=sb.set)
        self.company_list.pack(side="left")
        sb.pack(side="left", fill="y")
        self._refresh_companies()

        self.grad_jobspy_fallback = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="If a board is dead/empty, search LinkedIn/Indeed for that company",
                        variable=self.grad_jobspy_fallback).grid(
            row=3, column=1, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(f, foreground="#666",
                  text="Nothing selected = search every employer shown.").grid(
            row=1, column=4, sticky="nw", padx=12, pady=(8, 0))

        ttk.Button(f, text="Poll employer boards", style="Run.TButton",
                   command=self.run_grad).grid(row=4, column=1, sticky="w", pady=(12, 0))

    GRAD_REGIONS = {
        "Europe": lambda b: b in BOARDS,
        "United States": lambda b: b.country == "US",
        "Asia-Pacific": lambda b: b in US_ASIA_BOARDS and b.country != "US",
        "All regions": lambda b: True,
    }

    def _refresh_companies(self) -> None:
        tier = self.grad_tier.get()
        in_region = self.GRAD_REGIONS[self.grad_region.get()]
        self.company_list.delete(0, "end")
        self._companies = [b for b in BOARDS + US_ASIA_BOARDS
                           if in_region(b) and (tier == "all" or b.tier == tier)]
        for b in self._companies:
            src = b.ats if b.ats != "none" else "no public board (name search)"
            self.company_list.insert("end", f"{b.company}  ·  {b.country}  ·  {src}")

    # ---------------- tab: student roles ----------------

    def _panel_student(self, f) -> None:

        ttk.Label(f, text="Field").grid(row=0, column=0, sticky="w", pady=3)
        self.stu_field = ttk.Entry(f, width=34)
        self.stu_field.insert(0, "mechatronics")
        self.stu_field.grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(f, text="Role type").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.stu_kind = ttk.Combobox(f, values=["(all)"] + list(KINDS),
                                     state="readonly", width=16)
        self.stu_kind.set("(all)")
        self.stu_kind.grid(row=0, column=3, sticky="w", padx=6)

        ttk.Label(f, text="Countries").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        self.stu_countries = self._country_grid(f, row=1, col=1,
                                                codes=list(MARKETS),
                                                preset={"DE", "NL", "SE"})

        ttk.Button(f, text="Search EURES", style="Run.TButton",
                   command=self.run_student).grid(row=2, column=1, sticky="w", pady=(12, 0))

    # ---------------- tab: Bundesagentur für Arbeit ----------------

    def _panel_ba(self, f) -> None:

        ttk.Label(f, text="Search").grid(row=0, column=0, sticky="w", pady=3)
        self.ba_query = ttk.Combobox(f, width=32, values=[
            "Werkstudent Robotik", "Werkstudent Mechatronik", "Praktikum Robotik",
            "Masterarbeit Robotik", "Werkstudent ROS", "Junior Robotik Ingenieur",
            "Robotik", "Mechatronik Ingenieur", "Embedded Werkstudent"])
        self.ba_query.set("Werkstudent Robotik")
        self.ba_query.grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(f, text="Near").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.ba_where = ttk.Entry(f, width=18)
        self.ba_where.insert(0, "Straubing")
        self.ba_where.grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(f, text="Radius km").grid(row=0, column=4, sticky="w")
        self.ba_radius = tk.IntVar(value=150)
        ttk.Spinbox(f, from_=0, to=200, increment=25, textvariable=self.ba_radius,
                    width=5).grid(row=0, column=5, sticky="w", padx=6)

        ttk.Label(f, text="Offer type").grid(row=1, column=0, sticky="w", pady=3)
        self.ba_offer = ttk.Combobox(f, state="readonly", width=20,
                                     values=["(any)"] + list(BA_OFFER_TYPES))
        self.ba_offer.set("(any)")
        self.ba_offer.grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(f, text="Posted within days").grid(row=1, column=2, sticky="w", padx=(18, 0))
        self.ba_days = tk.IntVar(value=7)
        ttk.Spinbox(f, from_=0, to=100, textvariable=self.ba_days, width=5).grid(
            row=1, column=3, sticky="w", padx=6)

        self.ba_part_time = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Part-time only (typical for Werkstudent)",
                        variable=self.ba_part_time).grid(row=2, column=1, columnspan=3,
                                                         sticky="w", pady=4)
        ttk.Label(f, foreground="#555", wraplength=720, justify="left",
                  text="Germany's largest job database (Bundesagentur für Arbeit). Leave 'Near' "
                       "empty for nationwide. 'Posted within days' = 1 gives the last 24 hours. "
                       "Full descriptions are fetched for the first 40 hits so the fit score "
                       "and German-language check work.").grid(
            row=3, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Button(f, text="Search Arbeitsagentur", style="Run.TButton",
                   command=self.run_ba).grid(row=4, column=1, sticky="w", pady=(12, 0))

    def run_ba(self) -> None:
        query = self.ba_query.get().strip()
        if not query:
            messagebox.showwarning("No search term", "Type something to search for.")
            return
        where = self.ba_where.get().strip()
        radius = self.ba_radius.get()
        offer = "" if self.ba_offer.get() == "(any)" else self.ba_offer.get()
        days = self.ba_days.get()
        work_time = "teilzeit" if self.ba_part_time.get() else ""

        def task(w: Worker) -> None:
            jobs = BAJobsucheProvider().search(query, where, radius, offer, work_time, days,
                                               pages=3, on_log=w.log, cancelled=w.cancelled,
                                               cached=self._cached_description)
            new = 0
            for job in jobs:
                if self.store.upsert(job):
                    new += 1
                    w.found(job)
            w.log(f"{len(jobs)} returned, {new} new")

        self.start(f"Arbeitsagentur · {query}", task)

    def _cached_description(self, job_id: str) -> str | None:
        r = self.store.get(job_id)
        return r["description"] if r is not None and len(r["description"] or "") > 200 else None

    # ---------------- tab: Personio ----------------

    def _panel_personio(self, f) -> None:

        ttk.Label(f, text="Employers").grid(row=0, column=0, sticky="nw", pady=3)
        box = ttk.Frame(f)
        box.grid(row=0, column=1, sticky="w")
        self.personio_list = tk.Listbox(box, selectmode="extended", height=7, width=40,
                                        exportselection=False)
        for c in PERSONIO_COMPANIES:
            self.personio_list.insert("end", f"{c.company}  ·  {c.slug}")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.personio_list.yview)
        self.personio_list.configure(yscrollcommand=sb.set)
        self.personio_list.pack(side="left")
        sb.pack(side="left", fill="y")

        ttk.Label(f, text="Extra slugs").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.personio_extra = ttk.Entry(f, width=42)
        self.personio_extra.grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(f, foreground="#555", wraplength=720, justify="left",
                  text="Many German startups and Mittelstand firms recruit via Personio. Find the "
                       "slug in a company's careers link (xyz.jobs.personio.de → xyz) and add it, "
                       "comma-separated. Nothing selected = all listed employers.").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(f, text="Fetch Personio jobs", style="Run.TButton",
                   command=self.run_personio).grid(row=3, column=1, sticky="w", pady=(12, 0))

    def run_personio(self) -> None:
        picked = self.personio_list.curselection()
        companies = ([PERSONIO_COMPANIES[i] for i in picked] if picked
                     else list(PERSONIO_COMPANIES))
        extra = [s.strip() for s in self.personio_extra.get().split(",") if s.strip()]
        companies += [PersonioCompany(s, s) for s in extra]

        def task(w: Worker) -> None:
            feeds = PersonioFeeds()
            for c in companies:
                if w.cancelled.is_set():
                    return
                jobs = feeds.fetch(c)
                if jobs is None:
                    w.log(f"{c.company}: no Personio feed at '{c.slug}'")
                    continue
                new = 0
                for job in jobs:
                    if self.store.upsert(job):
                        new += 1
                        w.found(job)
                w.log(f"{c.company}: {len(jobs)} roles, {new} new")

        self.start(f"Personio · {len(companies)} employers", task)

    # ---------------- tab: Workday ----------------

    def _panel_workday(self, f) -> None:

        ttk.Label(f, text="Search").grid(row=0, column=0, sticky="w", pady=3)
        self.wd_query = ttk.Combobox(f, width=28, values=[
            "Werkstudent", "working student robotics", "Praktikum", "internship",
            "thesis", "robotics", "mechatronics", "graduate"])
        self.wd_query.set("Werkstudent")
        self.wd_query.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(f, text="Country").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.wd_country = ttk.Entry(f, width=16)
        self.wd_country.insert(0, "Germany")
        self.wd_country.grid(row=0, column=3, sticky="w", padx=6)

        ttk.Label(f, text="Employers").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        box = ttk.Frame(f)
        box.grid(row=1, column=1, columnspan=3, sticky="w", pady=(8, 0))
        self.wd_list = tk.Listbox(box, selectmode="extended", height=6, width=40,
                                  exportselection=False)
        for s in WORKDAY_SITES:
            self.wd_list.insert("end", s.company)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.wd_list.yview)
        self.wd_list.configure(yscrollcommand=sb.set)
        self.wd_list.pack(side="left")
        sb.pack(side="left", fill="y")

        ttk.Label(f, text="Add careers URL").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.wd_url = ttk.Entry(f, width=60)
        self.wd_url.grid(row=2, column=1, columnspan=3, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(f, foreground="#555", wraplength=720, justify="left",
                  text="Big manufacturers (ZEISS, Airbus, ASML, NXP…) recruit on Workday. To add "
                       "another, open its careers page and paste any link containing "
                       "'myworkdayjobs.com'. Nothing selected = all employers.").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(f, text="Search Workday", style="Run.TButton",
                   command=self.run_workday).grid(row=4, column=1, sticky="w", pady=(12, 0))

    def run_workday(self) -> None:
        picked = self.wd_list.curselection()
        sites = [WORKDAY_SITES[i] for i in picked] if picked else list(WORKDAY_SITES)
        url = self.wd_url.get().strip()
        if url:
            try:
                sites.append(parse_workday_url(url))
            except ValueError as exc:
                messagebox.showerror("Not a Workday link", str(exc))
                return
        text = self.wd_query.get().strip()
        country = self.wd_country.get().strip()

        def task(w: Worker) -> None:
            wb = WorkdayBoards()
            for site in sites:
                if w.cancelled.is_set():
                    return
                try:
                    jobs = wb.search(site, text, country, max_jobs=100, details=30,
                                     on_log=w.log, cancelled=w.cancelled,
                                     cached=self._cached_description)
                except Exception as exc:
                    w.log(f"{site.company}: FAILED — {exc}")
                    continue
                new = 0
                for job in jobs:
                    if self.store.upsert(job):
                        new += 1
                        w.found(job)
                w.log(f"{site.company}: {len(jobs)} roles, {new} new")

        self.start(f"Workday · {text or 'all'} · {len(sites)} employers", task)

    # ---------------- tab: research institutes ----------------

    def _panel_research(self, f) -> None:

        ttk.Label(f, text="Keywords").grid(row=0, column=0, sticky="w", pady=3)
        self.res_query = ttk.Combobox(f, width=28, values=[
            "robotik", "mechatronik", "autonom", "robotics", "Hilfskraft", "Masterarbeit",
            "Doktorand", ""])
        self.res_query.set("robotik")
        self.res_query.grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(f, text="Sources").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        box = ttk.Frame(f)
        box.grid(row=1, column=1, columnspan=3, sticky="w", pady=(8, 0))
        self.res_sources: dict[str, tk.BooleanVar] = {}
        for i, (code, label) in enumerate(RESEARCH_SOURCES.items()):
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(box, text=label, variable=v).grid(row=i // 2, column=i % 2,
                                                              sticky="w", padx=(0, 18))
            self.res_sources[code] = v
        ttk.Label(f, foreground="#555", wraplength=720, justify="left",
                  text="HiWi (studentische Hilfskraft), thesis, internship and PhD roles straight "
                       "from Fraunhofer, DLR (incl. the Institute of Robotics and Mechatronics), "
                       "Max Planck and TH Deggendorf. Empty keywords = everything.").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(f, text="Search institutes", style="Run.TButton",
                   command=self.run_research).grid(row=3, column=1, sticky="w", pady=(12, 0))

    def run_research(self) -> None:
        sources = [c for c, v in self.res_sources.items() if v.get()]
        if not sources:
            messagebox.showwarning("No sources", "Pick at least one institute.")
            return
        kw = self.res_query.get().strip()

        def task(w: Worker) -> None:
            feeds = ResearchFeeds()
            for src in sources:
                if w.cancelled.is_set():
                    return
                try:
                    jobs = feeds.search(src, kw, on_log=w.log)
                except Exception as exc:
                    w.log(f"{RESEARCH_SOURCES[src]}: FAILED — {exc}")
                    continue
                new = 0
                for job in jobs:
                    if self.store.upsert(job):
                        new += 1
                        w.found(job)
                w.log(f"{RESEARCH_SOURCES[src]}: {len(jobs)} roles, {new} new")

        self.start(f"Research · {kw or 'all'}", task)

    # ---------------- tab: application tracker ----------------

    TRACK_COLS = ("status", "fit", "title", "company", "applied", "follow_up", "notes")

    def _tab_apps(self) -> None:
        """Daily sweep bar on top; tracker and CV tailor side by side below."""
        tab = ttk.Frame(self.tabs, padding=(10, 8))
        self.tabs.add(tab, text="  Applications  ")
        self._apps_tab = tab

        sweep = ttk.Frame(tab)
        sweep.pack(fill="x", pady=(0, 8))
        ttk.Label(sweep, text="Daily sweep", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.sw_status = tk.StringVar(value="")
        ttk.Label(sweep, textvariable=self.sw_status, foreground="#226622").pack(
            side="left", padx=10)
        ttk.Button(sweep, text="Settings…", command=self._open_sweep_settings).pack(side="right")
        ttk.Button(sweep, text="Run sweep now", style="Run.TButton",
                   command=self.run_sweep_now).pack(side="right", padx=6)

        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.LabelFrame(panes, text=" My applications ", padding=8)
        right = ttk.LabelFrame(panes, text=" Tailor CV & cover letter ", padding=8)
        panes.add(left, weight=1)
        panes.add(right, weight=1)
        self._panel_tracker(left)
        self._panel_ats(right)
        self._sweep_refresh_status()

    def _panel_tracker(self, f) -> None:

        self.tracker_summary = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.tracker_summary,
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w")

        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True, pady=(4, 6))
        self.track_tree = ttk.Treeview(wrap, columns=self.TRACK_COLS, show="headings",
                                       height=10, selectmode="browse")
        widths = {"status": 70, "fit": 34, "title": 220, "company": 110,
                  "applied": 80, "follow_up": 80, "notes": 120}
        for c in self.TRACK_COLS:
            self.track_tree.heading(c, text=c.replace("_", "-").title())
            self.track_tree.column(c, width=widths[c], anchor="w",
                                   stretch=c in ("title", "notes"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.track_tree.yview)
        self.track_tree.configure(yscrollcommand=vs.set)
        self.track_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="left", fill="y")
        self.track_tree.tag_configure("due", foreground="#b00020")
        self.track_tree.bind("<<TreeviewSelect>>", self._on_track_select)
        self.track_tree.bind("<Double-1>", lambda _e: self._track_open())

        ed = ttk.Frame(f)
        ed.pack(fill="x")
        ttk.Label(ed, text="Status").grid(row=0, column=0, sticky="w")
        self.tr_status = ttk.Combobox(ed, values=list(STATUSES), state="readonly", width=11)
        self.tr_status.grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(ed, text="Applied").grid(row=0, column=2, sticky="w")
        self.tr_applied = ttk.Entry(ed, width=11)
        self.tr_applied.grid(row=0, column=3, sticky="w", padx=(4, 12))
        ttk.Label(ed, text="Follow up").grid(row=0, column=4, sticky="w")
        self.tr_follow = ttk.Entry(ed, width=11)
        self.tr_follow.grid(row=0, column=5, sticky="w", padx=(4, 12))
        ttk.Label(ed, text="Notes").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.tr_notes = ttk.Entry(ed)
        self.tr_notes.grid(row=1, column=1, columnspan=5, sticky="we", padx=(4, 0), pady=(4, 0))
        ed.columnconfigure(5, weight=1)

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="Save changes", style="Run.TButton",
                   command=self._track_save).pack(side="left")
        ttk.Button(bar, text="Open posting", command=self._track_open).pack(side="left", padx=6)
        ttk.Button(bar, text="Tailor CV →", command=self._track_tailor).pack(side="left")
        ttk.Label(f, foreground="#555", wraplength=460, justify="left",
                  text="Right-click jobs in the results to track them. Dates are YYYY-MM-DD; "
                       "'applied' sets a follow-up 7 days out. Red = follow-up due.").pack(
            anchor="w", pady=(6, 0))
        self.refresh_tracker()

    def refresh_tracker(self) -> None:
        if not hasattr(self, "track_tree"):
            return
        self.track_tree.delete(*self.track_tree.get_children())
        today = datetime.now().date().isoformat()
        counts: dict[str, int] = {}
        due = 0
        for r in self.store.tracked():
            job = Job.from_row(r)
            counts[job.status] = counts.get(job.status, 0) + 1
            is_due = bool(job.follow_up) and job.follow_up <= today
            due += is_due
            self.track_tree.insert("", "end", iid=job.job_id, tags=("due",) if is_due else (),
                                   values=(job.status, job.fit_score or "", job.title, job.company,
                                           job.applied_at, job.follow_up, job.notes))
        parts = [f"{counts[s]} {s}" for s in STATUSES if counts.get(s)]
        self.tracker_summary.set(
            (" · ".join(parts) or "Nothing tracked yet — right-click a job below and pick a status.")
            + (f"   ⚠ {due} follow-up(s) due" if due else ""))
        self.tabs.tab(self._apps_tab, text=f"  Applications{f' ({due} due)' if due else ''}  ")

    def _track_current(self) -> Job | None:
        sel = self.track_tree.selection()
        if not sel:
            return None
        r = self.store.get(sel[0])
        return Job.from_row(r) if r else None

    def _on_track_select(self, _e=None) -> None:
        job = self._track_current()
        if not job:
            return
        self.tr_status.set(job.status)
        for entry, val in ((self.tr_applied, job.applied_at), (self.tr_follow, job.follow_up),
                           (self.tr_notes, job.notes)):
            entry.delete(0, "end")
            entry.insert(0, val or "")

    def _track_save(self) -> None:
        job = self._track_current()
        if not job:
            messagebox.showinfo("Nothing selected", "Select an application first.")
            return
        for label, val in (("Applied", self.tr_applied.get().strip()),
                           ("Follow up", self.tr_follow.get().strip())):
            if val and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
                messagebox.showerror("Bad date", f"{label} must look like 2026-09-30.")
                return
        if self.tr_status.get() and self.tr_status.get() != job.status:
            self.store.set_status(job.job_id, self.tr_status.get())
        self.store.set_tracking(job.job_id, notes=self.tr_notes.get().strip(),
                                applied_at=self.tr_applied.get().strip(),
                                follow_up=self.tr_follow.get().strip())
        r = self.store.get(job.job_id)
        if r is not None and job.job_id in self.rows:
            self.rows[job.job_id] = Job.from_row(r)
            if self.tree.exists(job.job_id):
                self.tree.item(job.job_id, values=self._values(self.rows[job.job_id]))
        self.refresh_tracker()
        if self.track_tree.exists(job.job_id):
            self.track_tree.selection_set(job.job_id)
        self.log(f"saved: {job.title}")

    def _track_open(self) -> None:
        job = self._track_current()
        if job and job.url:
            webbrowser.open(job.url)

    def _track_tailor(self) -> None:
        job = self._track_current()
        if job:
            self.prefill_ats(job)

    def _on_tab_changed(self, _e=None) -> None:
        if hasattr(self, "panes"):
            self._fit_tabs()
        if self.tabs.select() == str(getattr(self, "_apps_tab", "")):
            self.refresh_tracker()
            self._sweep_refresh_status()

    # ---------------- tab: daily sweep ----------------

    SWEEP_SOURCES = (("ba", "Arbeitsagentur"), ("personio", "Personio"),
                     ("workday", "Workday"), ("research", "Research institutes"),
                     ("boards", "Employer boards (Graduate tab list)"), ("eures", "EURES"))

    def _open_sweep_settings(self) -> None:
        dlg = getattr(self, "_sweep_dlg", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            return
        dlg = tk.Toplevel(self)
        dlg.title("Daily sweep settings")
        dlg.transient(self)
        self._sweep_dlg = dlg
        f = ttk.Frame(dlg, padding=12)
        f.pack(fill="both", expand=True)
        cfg = daily_sweep.load_config()

        left = ttk.Frame(f)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Sources", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, sticky="w")
        self.sw_sources: dict[str, tk.BooleanVar] = {}
        for i, (code, label) in enumerate(self.SWEEP_SOURCES):
            v = tk.BooleanVar(value=bool(cfg["sources"].get(code)))
            ttk.Checkbutton(left, text=label, variable=v).grid(row=1 + i, column=0, sticky="w")
            self.sw_sources[code] = v

        opts = ttk.Frame(left)
        opts.grid(row=10, column=0, sticky="w", pady=(10, 0))
        ttk.Label(opts, text="Alert when fit ≥").grid(row=0, column=0, sticky="w")
        self.sw_min_fit = tk.IntVar(value=int(cfg.get("min_fit", 50)))
        ttk.Spinbox(opts, from_=0, to=100, increment=5, textvariable=self.sw_min_fit,
                    width=4).grid(row=0, column=1, sticky="w", padx=4)
        self.sw_notify = tk.BooleanVar(value=bool(cfg.get("notify", True)))
        ttk.Checkbutton(opts, text="Windows notification", variable=self.sw_notify).grid(
            row=1, column=0, columnspan=2, sticky="w")
        self.sw_on_start = tk.BooleanVar(value=bool(cfg.get("run_on_app_start", True)))
        ttk.Checkbutton(opts, text="Run when the app opens (if not run today)",
                        variable=self.sw_on_start).grid(row=2, column=0, columnspan=2, sticky="w")

        mid = ttk.Frame(f)
        mid.pack(side="left", fill="both", expand=True, padx=(18, 0))
        ttk.Label(mid, text="Arbeitsagentur searches  (one per line:  query | near | radius km)").pack(anchor="w")
        self.sw_ba = tk.Text(mid, height=5, width=52, wrap="none", font=("TkFixedFont", 9))
        self.sw_ba.pack(fill="x")
        self.sw_ba.insert("1.0", "\n".join(
            f"{s['query']} | {s.get('where', '')} | {s.get('radius', 0)}"
            for s in cfg["ba"]["searches"]))
        row = ttk.Frame(mid)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Workday searches").pack(side="left")
        self.sw_wd = ttk.Entry(row, width=36)
        self.sw_wd.insert(0, ", ".join(cfg["workday"]["queries"]))
        self.sw_wd.pack(side="left", padx=4)
        row2 = ttk.Frame(mid)
        row2.pack(fill="x", pady=(4, 0))
        ttk.Label(row2, text="Research keywords").pack(side="left")
        self.sw_res = ttk.Entry(row2, width=36)
        self.sw_res.insert(0, ", ".join(cfg["research"]["keywords"]))
        self.sw_res.pack(side="left", padx=4)

        task = ttk.Frame(mid)
        task.pack(fill="x", pady=(10, 0))
        ttk.Label(task, text="Windows task: run daily at").pack(side="left")
        self.sw_time = ttk.Entry(task, width=6)
        self.sw_time.insert(0, "09:00")
        self.sw_time.pack(side="left", padx=4)
        ttk.Button(task, text="Install", command=self._sweep_install).pack(side="left", padx=4)
        ttk.Button(task, text="Remove", command=self._sweep_uninstall).pack(side="left")

        btns = ttk.Frame(mid)
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="Close", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="Save", style="Run.TButton",
                   command=lambda: (self._sweep_save(), dlg.destroy())).pack(side="right", padx=6)

    def _sweep_dialog_open(self) -> bool:
        dlg = getattr(self, "_sweep_dlg", None)
        return dlg is not None and dlg.winfo_exists()

    def _sweep_collect(self) -> dict:
        cfg = daily_sweep.load_config()
        if not self._sweep_dialog_open():
            return cfg
        cfg["sources"] = {k: v.get() for k, v in self.sw_sources.items()}
        cfg["min_fit"] = int(self.sw_min_fit.get())
        cfg["notify"] = self.sw_notify.get()
        cfg["run_on_app_start"] = self.sw_on_start.get()
        searches = []
        for line in self.sw_ba.get("1.0", "end").splitlines():
            parts = [p.strip() for p in line.split("|")]
            if not parts or not parts[0]:
                continue
            radius = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 50
            searches.append({"query": parts[0], "where": parts[1] if len(parts) > 1 else "",
                             "radius": radius})
        cfg["ba"]["searches"] = searches
        cfg["workday"]["queries"] = [q.strip() for q in self.sw_wd.get().split(",") if q.strip()]
        cfg["research"]["keywords"] = [q.strip() for q in self.sw_res.get().split(",") if q.strip()]
        return cfg

    def _sweep_save(self) -> None:
        daily_sweep.save_config(self._sweep_collect())
        self.log(f"sweep settings saved → {daily_sweep.CONFIG_PATH}")
        self._sweep_refresh_status()

    def _sweep_refresh_status(self) -> None:
        state = daily_sweep.load_state()
        parts = []
        if state.get("last_run_at"):
            s = state.get("last_summary") or {}
            parts.append(f"last run {state['last_run_at'].replace('T', ' ')[:16]}: "
                         f"{s.get('new_total', 0)} new, {s.get('new_good_fit', 0)} good fit")
        else:
            parts.append("never run")
        parts.append("done for today" if daily_sweep.ran_today(state) else "not run today")
        try:
            nxt = daily_sweep.task_installed()
        except OSError:
            nxt = ""
        parts.append(f"Windows task next {nxt}" if nxt else "no Windows task")
        self.sw_status.set("  ·  ".join(parts))

    def _sweep_task(self, force: bool):
        def task(w: Worker) -> None:
            daily_sweep.run_sweep(force=force, log=w.log, cancelled=w.cancelled,
                                  on_job=w.found, store=self.store)
            self._ui(self._sweep_refresh_status)
        return task

    def run_sweep_now(self) -> None:
        if self._sweep_dialog_open():
            daily_sweep.save_config(self._sweep_collect())
        force = False
        if daily_sweep.ran_today():
            if not messagebox.askyesno(
                    "Already ran today",
                    "The daily sweep already ran today. It's limited to once per day to "
                    "stay polite to the job sites.\n\nRun it again anyway?"):
                return
            force = True
        self.start("Daily sweep", self._sweep_task(force))

    def _maybe_auto_sweep(self) -> None:
        cfg = daily_sweep.load_config()
        if not cfg.get("run_on_app_start") or daily_sweep.ran_today():
            return
        if self.worker and self.worker.is_alive():
            return
        self.log("Daily sweep hasn't run today — starting it now (turn off on the Daily sweep tab).")
        self.start("Daily sweep", self._sweep_task(False))

    def _sweep_install(self) -> None:
        at = self.sw_time.get().strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
            messagebox.showerror("Bad time", "Use 24h HH:MM, e.g. 09:00.")
            return
        if not messagebox.askyesno(
                "Install daily task", parent=self._sweep_dlg, message=
                f"Create a Windows Task Scheduler entry '{daily_sweep.TASK_NAME}' that runs "
                f"the sweep every day at {at} for your user account?\n\n"
                f"Command: {daily_sweep._task_command()}"):
            return
        try:
            self.log(daily_sweep.install_task(at))
        except (RuntimeError, OSError) as exc:
            messagebox.showerror("Could not create task", str(exc))
        self._sweep_refresh_status()

    def _sweep_uninstall(self) -> None:
        try:
            self.log(daily_sweep.uninstall_task())
        except (RuntimeError, OSError) as exc:
            messagebox.showerror("Could not remove task", str(exc))
        self._sweep_refresh_status()

    # ---------------- tab: web search (JobSpy) ----------------

    def _panel_jobspy(self, f) -> None:

        ttk.Label(f, text="Search term").grid(row=0, column=0, sticky="w", pady=3)
        self.jobspy_query = ttk.Entry(f, width=34)
        self.jobspy_query.insert(0, "robotics graduate engineer")
        self.jobspy_query.grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(f, text="Location").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.jobspy_location = ttk.Entry(f, width=24)
        self.jobspy_location.insert(0, "Germany")
        self.jobspy_location.grid(row=0, column=3, sticky="w", padx=6)

        ttk.Label(f, text="Country (Indeed / Glassdoor)").grid(row=1, column=0, sticky="w", pady=3)
        self.jobspy_country = ttk.Combobox(f, width=22,
                                           values=sorted(c.title() for c in INDEED_COUNTRIES))
        self.jobspy_country.set("Germany")
        self.jobspy_country.grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(f, text="Pages (~20 results each)").grid(row=1, column=2, sticky="w", padx=(18, 0))
        self.jobspy_pages = tk.IntVar(value=1)
        ttk.Spinbox(f, from_=1, to=10, textvariable=self.jobspy_pages, width=5).grid(
            row=1, column=3, sticky="w", padx=6)

        sites_box = ttk.Frame(f)
        sites_box.grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(sites_box, text="Sites:").pack(side="left", padx=(0, 8))
        self.jobspy_sites: dict[str, tk.BooleanVar] = {}
        for site in DEFAULT_SITES:
            var = tk.BooleanVar(value=site in ("indeed", "linkedin"))
            self.jobspy_sites[site] = var
            ttk.Checkbutton(sites_box, text=site, variable=var).pack(side="left", padx=6)

        self.jobspy_remote = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Remote only", variable=self.jobspy_remote).grid(
            row=3, column=1, sticky="w", pady=(8, 0))

        hint = ttk.Label(f, foreground="#666", wraplength=680, justify="left",
                         text="Searches LinkedIn / Indeed / Glassdoor / ZipRecruiter by keyword "
                              "and location, in any country. Use it when an employer has no public "
                              "board of its own. Needs python-jobspy installed (see the install "
                              "note in requirements.txt).")
        hint.grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))

        ttk.Button(f, text="Search", style="Run.TButton",
                   command=self.run_jobspy).grid(row=5, column=1, sticky="w", pady=(12, 0))

    def run_jobspy(self) -> None:
        query = self.jobspy_query.get().strip()
        if not query:
            messagebox.showwarning("No search term", "Type something to search for.")
            return
        location = self.jobspy_location.get().strip()
        country = self.jobspy_country.get().strip()
        sites = [site for site, var in self.jobspy_sites.items() if var.get()]
        if not sites:
            messagebox.showwarning("No sites", "Pick at least one job board to search.")
            return
        pages = self.jobspy_pages.get()
        remote = self.jobspy_remote.get()

        def task(w: Worker) -> None:
            try:
                provider = JobSpyProvider(site_name=sites, country_indeed=country)
            except SystemExit as exc:
                w.log(str(exc))
                return
            try:
                jobs = list(provider.search(query, location, pages, remote))
            except Exception as exc:
                w.log(f"FAILED: {exc}")
                return
            new = 0
            for job in jobs:
                if self.store.upsert(job):
                    new += 1
                    w.found(job)
            w.log(f"{len(jobs)} returned, {new} new")

        self.start(f"JobSpy · {query}", task)

    # ---------------- tab: company radar ----------------

    def _tab_radar(self) -> None:
        f = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(f, text="  New companies  ")
        # In the data folder like the jobs DB: a bare "company_radar.db" landed
        # wherever the .exe was started from (e.g. next to it in dist\).
        self.radar = RadarStore(str(_data_dir() / "company_radar.db"))

        ttk.Label(f, text="Field").grid(row=0, column=0, sticky="w", pady=3)
        self.radar_field = ttk.Combobox(f, values=list(FUNDING_KEYWORDS), width=26)
        self.radar_field.set("robotics")
        self.radar_field.grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(f, text="Look back").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.radar_months = tk.IntVar(value=6)
        ttk.Spinbox(f, from_=1, to=24, textvariable=self.radar_months, width=5).grid(
            row=0, column=3, sticky="w", padx=6)
        ttk.Label(f, text="months").grid(row=0, column=4, sticky="w")

        btns = ttk.Frame(f)
        btns.grid(row=1, column=0, columnspan=5, sticky="w", pady=(12, 0))
        ttk.Button(btns, text="1 · US funding (SEC Form D)", style="Run.TButton",
                   command=self.run_funding).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="2 · New employers in my results",
                   command=self.run_emerging).pack(side="left", padx=8)
        ttk.Button(btns, text="3 · Probe for job boards",
                   command=self.run_probe).pack(side="left", padx=8)

        ttk.Label(f, foreground="#555", wraplength=680, justify="left",
                  text=("Form D is the notice every US company files when it raises private "
                        "capital — official, keyless SEC data. 'New employers' scans the jobs "
                        "you've already collected for companies not on the monitored list, which "
                        "works for every market. 'Probe' checks each one for a live job board.")
                  ).grid(row=2, column=0, columnspan=5, sticky="w", pady=(10, 0))

        wrap = ttk.Frame(f)
        wrap.grid(row=3, column=0, columnspan=5, sticky="nsew", pady=(10, 0))
        cols = ("name", "market", "signal", "detail", "board")
        self.radar_tree = ttk.Treeview(wrap, columns=cols, show="headings", height=9)
        for c, wd in zip(cols, (230, 60, 80, 270, 150)):
            self.radar_tree.heading(c, text=c.title())
            self.radar_tree.column(c, width=wd, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.radar_tree.yview)
        self.radar_tree.configure(yscrollcommand=sb.set)
        self.radar_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.radar_tree.bind("<Double-1>", lambda _e: self.open_radar_row())
        self.radar_tree.tag_configure("hiring", background="#eef7ee")

        ttk.Button(f, text="Export watchlist CSV",
                   command=self.export_radar).grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.load_radar()

    def load_radar(self) -> None:
        self.radar_tree.delete(*self.radar_tree.get_children())
        self.radar_rows = {}
        for r in self.radar.all():
            board = f"{r['ats']}:{r['ats_slug']}" if r["ats"] else ""
            self.radar_rows[r["key"]] = dict(r)
            self.radar_tree.insert("", "end", iid=r["key"],
                                   tags=("hiring",) if r["ats"] else (),
                                   values=(r["name"], r["market"], r["signal"],
                                           r["detail"], board))

    def open_radar_row(self) -> None:
        for iid in self.radar_tree.selection():
            row = self.radar_rows.get(iid) or {}
            url = row.get("board_url") or row.get("source_url")
            if url:
                webbrowser.open(url)

    def export_radar(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        rows = list(self.radar_rows.values())
        with open(path, "w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(["name", "market", "signal", "detail", "ats", "slug",
                         "open_roles", "url"])
            for r in rows:
                wr.writerow([r["name"], r["market"], r["signal"], r["detail"],
                             r["ats"], r["ats_slug"], r["open_roles"],
                             r["board_url"] or r["source_url"]])
        self.log(f"exported {len(rows)} companies -> {path}")

    def run_funding(self) -> None:
        field = self.radar_field.get().strip() or "robotics"
        months = self.radar_months.get()

        def task(w: Worker) -> None:
            scanner = FormDScanner()
            try:
                found = scanner.scan(field, months, on_log=w.log)
            except Exception as exc:
                w.log(f"SEC scan FAILED: {exc}")
                return
            new = sum(1 for c in found if self.radar.upsert(c))
            w.log(f"{len(found)} filings matched, {new} companies new to the radar")
            if not found:
                w.log("Nothing matched. Try a broader term or a longer look-back.")
            self._ui(self.load_radar)

        self.start(f"SEC Form D · {field}", task)

    def run_emerging(self) -> None:
        def task(w: Worker) -> None:
            try:
                found = emerging_employers(DB, days=90, on_log=w.log)
            except Exception as exc:
                w.log(f"FAILED: {exc}")
                return
            new = sum(1 for c in found if self.radar.upsert(c))
            w.log(f"{len(found)} employers seen, {new} new to the radar")
            if not found:
                w.log("No unrecognised employers yet — collect some jobs first.")
            self._ui(self.load_radar)

        self.start("New employers in collected jobs", task)

    def run_probe(self) -> None:
        pending = [r["name"] for r in self.radar.all() if not r["ats"]][:30]
        if not pending:
            messagebox.showinfo("Nothing to probe",
                                "Every tracked company already has a board, or the "
                                "radar is empty. Run step 1 or 2 first.")
            return

        def task(w: Worker) -> None:
            prober = BoardProber()
            hits = 0
            for name in pending:
                if w.cancelled.is_set():
                    return
                c = prober.probe(name, on_log=w.log)
                if c.ats:
                    hits += 1
                self.radar.upsert(c)
            w.log(f"{hits} of {len(pending)} have a public job board")
            self._ui(self.load_radar)

        self.start(f"Probing {len(pending)} companies", task)

    # ------------------------------------- CV & cover-letter generator (Applications tab)

    # Region auto-map: job source / country code → ATS region code
    _ATS_REGION_MAP = {
        "DE": "DE", "AT": "DE", "CH": "DE",
        "NL": "EU", "FR": "EU", "BE": "EU", "SE": "EU", "DK": "EU",
        "FI": "EU", "IT": "EU", "ES": "EU", "PT": "EU", "PL": "EU",
        "IE": "EU", "CZ": "EU", "NO": "EU",
        "GB": "UK", "UK": "UK",
        "US": "US", "CA": "US",
        "SG": "ASIA", "IN": "ASIA", "MY": "ASIA", "PH": "ASIA",
        "JP": "JP", "KR": "JP",
        "CN": "ASIA", "TW": "ASIA", "AU": "ASIA",
        # LinkedIn writes names, not codes ("Greater Munich Metropolitan Area")
        "GERMANY": "DE", "DEUTSCHLAND": "DE", "AUSTRIA": "DE", "SWITZERLAND": "DE",
        "MUNICH": "DE", "BERLIN": "DE", "HAMBURG": "DE", "STUTTGART": "DE",
        "FRANKFURT": "DE", "COLOGNE": "DE", "VIENNA": "DE", "ZURICH": "DE",
        "NETHERLANDS": "EU", "FRANCE": "EU", "BELGIUM": "EU", "SWEDEN": "EU",
        "DENMARK": "EU", "FINLAND": "EU", "ITALY": "EU", "SPAIN": "EU",
        "PORTUGAL": "EU", "POLAND": "EU", "IRELAND": "EU", "NORWAY": "EU",
        "PARIS": "EU", "AMSTERDAM": "EU", "EINDHOVEN": "EU", "MILAN": "EU", "ROME": "EU",
        "MADRID": "EU", "BARCELONA": "EU", "STOCKHOLM": "EU", "COPENHAGEN": "EU",
        "UNITED KINGDOM": "UK", "ENGLAND": "UK", "LONDON": "UK",
        "UNITED STATES": "US", "CANADA": "US",
        "SINGAPORE": "ASIA", "INDIA": "ASIA", "JAPAN": "JP",
    }

    def _panel_ats(self, f) -> None:
        # ── controls, laid out for half the window width ──────────────────
        ctl = ttk.Frame(f)
        ctl.pack(fill="x", pady=(0, 6))
        pad = dict(sticky="w", pady=2)

        ttk.Label(ctl, text="Region").grid(row=0, column=0, **pad)
        self.ats_region = tk.StringVar(value=DEFAULT_REGION)
        ttk.Combobox(ctl, textvariable=self.ats_region, values=sorted(ATS_REGIONS), width=7,
                     state="readonly").grid(row=0, column=1, padx=(4, 12), **pad)
        ttk.Label(ctl, text="Style").grid(row=0, column=2, **pad)
        self.ats_style = tk.StringVar(value="both")
        ttk.Combobox(ctl, textvariable=self.ats_style, values=["ats", "styled", "both"],
                     width=7, state="readonly").grid(row=0, column=3, padx=(4, 12), **pad)
        self.ats_review = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Review plan", variable=self.ats_review).grid(
            row=1, column=6, sticky="w", pady=2)
        self.ats_cover = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Cover letter", variable=self.ats_cover).grid(
            row=0, column=5, **pad)
        self.ats_compile = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Compile PDF", variable=self.ats_compile).grid(
            row=0, column=4, **pad)

        ttk.Label(ctl, text="Backend").grid(row=1, column=0, **pad)
        self.ats_backend = tk.StringVar(value="lmstudio")
        ttk.Combobox(ctl, textvariable=self.ats_backend, values=["anthropic", "lmstudio"],
                     width=10, state="readonly").grid(row=1, column=1, padx=(4, 12), **pad)
        ttk.Label(ctl, text="Model").grid(row=1, column=2, **pad)
        # the default pair; the pipeline loads it in LM Studio by itself
        self.ats_model = tk.StringVar(value=DEFAULT_LOCAL_MODELS)
        ttk.Entry(ctl, textvariable=self.ats_model, width=20).grid(
            row=1, column=3, columnspan=2, padx=4, **pad)
        ttk.Button(ctl, text="Detect local", command=self._ats_detect).grid(
            row=1, column=5, **pad)

        ttk.Label(ctl, text="Server").grid(row=2, column=0, **pad)
        self.ats_base_url = tk.StringVar(value="http://localhost:1234/v1")
        ttk.Entry(ctl, textvariable=self.ats_base_url, width=24).grid(
            row=2, column=1, columnspan=2, padx=4, **pad)
        ttk.Label(ctl, text="Recruiter").grid(row=2, column=3, **pad)
        self.ats_recruiter = tk.StringVar()
        ttk.Entry(ctl, textvariable=self.ats_recruiter, width=18).grid(
            row=2, column=4, columnspan=2, padx=4, **pad)

        ttk.Label(ctl, text="Profile").grid(row=3, column=0, **pad)
        # The shared lookup: the file picked last time, then the data dir and the
        # .exe's folder. "Next to __file__" inside the .exe is the temporary bundle,
        # which only ever holds the profile as it was at build time.
        self.ats_profile_path = tk.StringVar(value=str(default_profile_path()))
        ttk.Entry(ctl, textvariable=self.ats_profile_path, width=40).grid(
            row=3, column=1, columnspan=4, padx=4, **pad)
        ttk.Button(ctl, text="Browse…", command=self._ats_browse_profile).grid(
            row=3, column=5, **pad)

        # ── job description ───────────────────────────────────────────────
        ttk.Label(f, text="Job description — paste it, or double-click a job in the results").pack(anchor="w")
        self.ats_jd = tk.Text(f, height=8, wrap="word", undo=True,
                               font=("TkDefaultFont", 10))
        self.ats_jd.pack(fill="both", expand=True, pady=(2, 6))

        # ── extra notes ───────────────────────────────────────────────────
        ttk.Label(f, text="Extra truthful context for the cover letter (optional)").pack(anchor="w")
        self.ats_notes = tk.Text(f, height=2, wrap="word")
        self.ats_notes.pack(fill="x", pady=(2, 6))

        # ── action bar ────────────────────────────────────────────────────
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Button(bar, text="Load JD…",
                   command=self._ats_load_jd).pack(side="left")
        ttk.Button(bar, text="Outputs folder",
                   command=self._ats_open_outputs).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear JD",
                   command=lambda: self.ats_jd.delete("1.0", "end")).pack(side="left")
        self.ats_go = ttk.Button(bar, text="Generate",
                                  style="Run.TButton", command=self._ats_generate)
        self.ats_go.pack(side="right")

        # ── last result summary ───────────────────────────────────────────
        self.ats_result_var = tk.StringVar(value="No output yet.")
        ttk.Label(f, textvariable=self.ats_result_var,
                  foreground="#226622").pack(anchor="w", pady=(2, 0))

    # ── ATS helpers ──────────────────────────────────────────────────────────

    def _approve_plan(self, prepared):
        """Called on the worker thread between planning and writing: shows the
        plan on the Tk thread and blocks until the user approves or cancels."""
        done = threading.Event()
        box = {"result": None}
        self._ui(lambda: self._show_plan_dialog(prepared, box, done))
        while not done.wait(0.2):
            if self.worker and self.worker.cancelled.is_set():
                self._ui(lambda: getattr(self, "_plan_dlg", None) and self._plan_dlg.destroy())
                return None
        return box["result"]

    def _show_plan_dialog(self, prepared, box, done) -> None:
        dlg = tk.Toplevel(self)
        self._plan_dlg = dlg
        dlg.title("Review the tailoring plan")
        dlg.transient(self)
        dlg.geometry("820x620")
        f = ttk.Frame(dlg, padding=12)
        f.pack(fill="both", expand=True)
        # buttons first, pinned to the bottom, so a small window never hides them
        btns = ttk.Frame(f)
        btns.pack(side="bottom", fill="x", pady=(8, 0))

        reqs = prepared["requirements"]
        if reqs:
            req = [r for r in reqs if r["priority"] == "required"]
            head = (f"Required skills met: {sum(r['status'] in ('have', 'alternative') for r in req)}/{len(req)}"
                    f"   ·   gaps are listed, never added to your CV")
        else:
            head = "Job requirements could not be read; using keyword matching only."
        ttk.Label(f, text=head, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")

        cols = ("status", "priority", "skill", "proof")
        tree = ttk.Treeview(f, columns=cols, show="headings", height=9)
        for c, w in zip(cols, (90, 75, 200, 410)):
            tree.heading(c, text=c.title())
            tree.column(c, width=w, anchor="w", stretch=c == "proof")
        order = {"gap": 0, "likely": 1, "alternative": 2, "have": 3}
        for r in sorted(reqs, key=lambda r: (r["priority"] != "required", order[r["status"]])):
            status = {"have": "✓ have", "alternative": "✓ either/or", "likely": "? check",
                      "gap": "✗ gap"}[r["status"]]
            tree.insert("", "end", values=(status, r["priority"], r["skill"],
                                           (r["evidence"][0] if r["evidence"] else "—")[:110]))
        tree.pack(fill="x", pady=(6, 10))

        ttk.Label(f, text="Bullets the model may rephrase (only around keywords they already "
                          "contain - untick to keep a bullet word for word):").pack(anchor="w")
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True, pady=(4, 8))
        canvas = tk.Canvas(wrap, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        vars_ = []
        last_job = None
        for item in prepared["plan"]:
            if item["job"] != last_job:
                ttk.Label(inner, text=f"{item['title']} @ {item['employer']}",
                          font=("TkDefaultFont", 9, "bold")).pack(anchor="w", pady=(6, 0))
                last_job = item["job"]
            v = tk.BooleanVar(value=item["edit"])
            words = ", ".join(item["keep"] + [a for _o, a in item["pairs"]])
            text = item["text"] if len(item["text"]) < 110 else item["text"][:107] + "..."
            cb = ttk.Checkbutton(inner, variable=v,
                                 text=f"{text}" + (f"   [{words}]" if words else "   (no job keywords)"))
            if not item["edit"]:
                cb.state(["disabled"])
            cb.pack(anchor="w")
            vars_.append((item, v))

        def finish(ok: bool) -> None:
            if ok:
                for item, v in vars_:
                    item["edit"] = item["edit"] and v.get()
                box["result"] = prepared
            done.set()
            dlg.destroy()

        ttk.Button(btns, text="Cancel", command=lambda: finish(False)).pack(side="right")
        ttk.Button(btns, text="Write CV", style="Run.TButton",
                   command=lambda: finish(True)).pack(side="right", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(False))

    def _ats_detect(self) -> None:
        try:
            base = self.ats_base_url.get().strip() or DEFAULT_BASE_URL
            models = list_local_models(base)
        except PipelineError as e:
            messagebox.showerror("No local server", str(e))
            return
        if models:
            self.ats_backend.set("lmstudio")
            if not self.ats_model.get().strip():     # keep a model list already entered
                self.ats_model.set(models[0])
            self.log(f"[ATS] Local models: {', '.join(models)}")
        else:
            self.log("[ATS] Server reachable but no model loaded.")

    def _ats_browse_profile(self) -> None:
        path = filedialog.askopenfilename(
            title="Select profile.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.ats_profile_path.set(path)
            self._use_profile(path)

    def _use_profile(self, path: str) -> None:
        """Remember the profile for the next start and score jobs against it too."""
        remember_profile_path(path)
        scorer = load_default_scorer()
        if scorer is not None:
            self.scorer = scorer
            self.store.scorer = scorer.score
        self.log(f"[ATS] Profile set to {path} (remembered for next time)")

    def _ats_load_jd(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("Text / Markdown", "*.txt *.md"), ("All files", "*.*")])
        if path:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            self.ats_jd.delete("1.0", "end")
            self.ats_jd.insert("1.0", text)

    def _ats_open_outputs(self) -> None:
        folder = getattr(self, "_ats_last_out", None) or (_data_dir() / "ats_outputs")
        Path(folder).mkdir(parents=True, exist_ok=True)
        import os
        if os.name == "nt":
            os.startfile(str(folder))
        else:
            os.system(f'xdg-open "{folder}" >/dev/null 2>&1 &')

    def prefill_ats(self, job: Job) -> None:
        """Called when the user double-clicks a job row.

        Switches to the Applications tab, fills the job description box with whatever
        description we have, and auto-selects the best region.
        """
        # Build a JD text from what we have stored
        parts = []
        if job.title:
            parts.append(f"Position: {job.title}")
        if job.company:
            parts.append(f"Company:  {job.company}")
        if job.location:
            parts.append(f"Location: {job.location}")
        if job.employment_type:
            parts.append(f"Type:     {job.employment_type}")
        if job.url:
            parts.append(f"Apply:    {job.url}")
        if job.description and len(job.description.strip()) > 20:
            parts.append("")
            parts.append(job.description.strip())
        else:
            parts.append("")
            parts.append(
                "[No full description stored - open the link above, copy the full"
                " job description and paste it here before generating.]")
        jd_text = "\n".join(parts)

        self.ats_jd.delete("1.0", "end")
        self.ats_jd.insert("1.0", jd_text)

        # Auto-select region from job country / source
        region = self._infer_ats_region(job)
        self.ats_region.set(region)

        self.tabs.select(self._apps_tab)
        self.log(f"[ATS] Pre-filled from: {job.title} @ {job.company} → region {region}")

    def _infer_ats_region(self, job: Job) -> str:
        """Best-guess region from job location and source."""
        loc = (job.location or "").upper()
        src = (job.source or "").lower()
        # source-based shortcut
        if "usajobs" in src or "adzuna-us" in src:
            return "US"
        if "mcf" in src or "adzuna-sg" in src:
            return "ASIA"
        if "adzuna-in" in src:
            return "ASIA"
        if "adzuna-jp" in src:
            return "JP"
        # location-based, whole words only: "AT" once matched inside "GREATER" and
        # "IT" inside "METROPOLITAN" ("Greater Munich Metropolitan Area")
        for token, code in self._ATS_REGION_MAP.items():
            if re.search(rf"\b{re.escape(token)}\b", loc):
                return code
        # fall back to stored region if available
        return DEFAULT_REGION

    # ── ATS generation ───────────────────────────────────────────────────────

    def _ats_generate(self) -> None:
        jd = self.ats_jd.get("1.0", "end").strip()
        if not jd:
            messagebox.showwarning("Missing input", "Paste a job description first.")
            return
        profile_path = self.ats_profile_path.get().strip()
        if not Path(profile_path).exists():
            messagebox.showerror(
                "Profile not found",
                f"Cannot find profile.json at:\n{profile_path}\n\n"
                "Browse to your profile.json file above.")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A search is already running. Wait for it to finish.")
            return
        if is_bundled_copy(profile_path):
            self.log("[ATS] WARNING: using the profile.json packed into the .exe at build time - "
                     "your edits to your own file are not in it. Use Browse… to pick your file.")
        else:
            if Path(profile_path).resolve() != default_profile_path().resolve():
                self._use_profile(profile_path)        # typed in by hand: keep it too
            self.log(f"[ATS] Profile: {profile_path}")

        self.ats_go.configure(state="disabled")
        self.ats_result_var.set("Generating…")

        region    = self.ats_region.get()
        style     = self.ats_style.get()
        backend   = self.ats_backend.get()
        model     = self.ats_model.get().strip()
        recruiter = self.ats_recruiter.get().strip()
        notes     = self.ats_notes.get("1.0", "end").strip()
        compile_  = self.ats_compile.get()
        cover     = self.ats_cover.get()
        review    = self.ats_review.get()
        base_url  = self.ats_base_url.get().strip() or None   # read here, not in the worker
        out_root  = _data_dir() / "ats_outputs"

        def task(w: Worker) -> None:
            try:
                w.log(f"[ATS] Region={region}  Backend={backend}  Style={style}")
                pkg, out = ats_run(
                    jd, region,
                    profile_path=Path(profile_path),
                    outputs_root=out_root,
                    style=style,
                    recruiter=recruiter,
                    notes=notes,
                    # the local pair in the field is not a Claude model name
                    model=(DEFAULT_MODEL if backend == "anthropic" and
                           (not model or model == DEFAULT_LOCAL_MODELS) else model),
                    backend=backend,
                    base_url=base_url,
                    compile_pdf=compile_,
                    cover=cover,
                    approve=self._approve_plan if review else None,
                    log=lambda m: w.log(f"[ATS] {m}"),
                )
                score = pkg["keywords"]["score"]
                reqs = pkg.get("requirements") or []
                if reqs:
                    req = [r for r in reqs if r["priority"] == "required"]
                    score = (f"{sum(r['status'] in ('have', 'alternative') for r in req)}/{len(req)} required met, "
                             f"keyword {score}")
                n_issues = len(pkg.get("issues") or [])
                checks_s = ("all checks passed" if not n_issues else
                            f"⚠ {n_issues} check(s) failed, see report.md")
                self._ui(lambda: self.ats_result_var.set(
                    f"Fit: {score}/100 · {checks_s} · {out}"))
                self._ui(lambda: setattr(self, "_ats_last_out", out))
            except PipelineError as e:
                w.log(f"[ATS] ERROR: {e}")
                # Bind the text now: Python unbinds `e` when the except block ends,
                # so a lambda reading it later would raise NameError.
                err = str(e)
                self._ui(lambda: self.ats_result_var.set("Failed — see activity log"))
                self._ui(lambda: messagebox.showerror("ATS pipeline failed", err))
            finally:
                self._ui(lambda: self.ats_go.configure(state="normal"))

        # Reuse the existing Worker / thread infrastructure
        self.worker = Worker(task, self.events)
        self.worker.start()
        self.progress.start(12)
        self.cancel_btn.configure(state="normal")
        self.status.set("ATS Tailor running…")
        self.log("[ATS] Generation started — the model only rewords your experience bullets"
                 + (" and writes the cover letter" if cover else "")
                 + "; expect 1-3 minutes on a 7B model. The window will not freeze.")

    def _country_grid(self, parent, row: int, col: int, codes, preset) -> dict:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=col, columnspan=3, sticky="w", pady=(8, 0))
        vars_: dict[str, tk.BooleanVar] = {}
        for i, code in enumerate(sorted(codes)):
            v = tk.BooleanVar(value=code in preset)
            name = MARKETS[code].name if code in MARKETS else code
            ttk.Checkbutton(frame, text=f"{code} {name}", variable=v).grid(
                row=i // 5, column=i % 5, sticky="w", padx=(0, 14))
            vars_[code] = v
        return vars_

    @staticmethod
    def _selected(vars_: dict) -> list[str]:
        return [c for c, v in vars_.items() if v.get()]

    # ---------------- results table ----------------

    COLS = ("fit", "title", "company", "location", "type", "posted", "status")

    def _results(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(8, 4))
        ttk.Label(bar, text="Filter").pack(side="left")
        self.filter_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.filter_var, width=24)
        e.pack(side="left", padx=6)
        e.bind("<KeyRelease>", lambda _e: self.apply_filter())

        self.fresh_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Posted < 24h", variable=self.fresh_only,
                        command=self.apply_filter).pack(side="left", padx=(6, 2))
        ttk.Label(bar, text="Min fit").pack(side="left", padx=(8, 2))
        self.min_fit = tk.IntVar(value=0)
        sb = ttk.Spinbox(bar, from_=0, to=100, increment=10, width=4,
                         textvariable=self.min_fit, command=self.apply_filter)
        sb.pack(side="left")
        sb.bind("<KeyRelease>", lambda _e: self.apply_filter())

        track = ttk.Menubutton(bar, text="Track selected ▾")
        menu = tk.Menu(track, tearoff=False)
        for st in STATUSES:
            menu.add_command(label=st.title(), command=lambda s=st: self.set_selected_status(s))
        menu.add_separator()
        menu.add_command(label="Stop tracking", command=lambda: self.set_selected_status(""))
        track["menu"] = menu
        track.pack(side="left", padx=(12, 4))

        ttk.Button(bar, text="Open", command=self.open_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="Export", command=self.export).pack(side="left", padx=4)
        ttk.Button(bar, text="Reload", command=self.load_saved).pack(side="left", padx=4)
        ttk.Button(bar, text="Re-score", command=self.rescore_all).pack(side="left", padx=4)
        ttk.Button(bar, text="Clear untracked", command=self.clear_db).pack(side="right")

        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=self.COLS, show="headings", selectmode="extended")
        widths = {"fit": 46, "title": 360, "company": 170, "location": 160,
                  "type": 110, "posted": 92, "status": 80}
        for c in self.COLS:
            self.tree.heading(c, text=c.title(), command=lambda cc=c: self.sort_by(cc))
            self.tree.column(c, width=widths[c], anchor="center" if c == "fit" else "w",
                             stretch=c in ("title", "company", "location"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self._on_result_double_click)
        self.tree.bind("<Button-3>", self._on_result_right_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_result_select)
        self.tree.tag_configure("new", background="#eef7ee")
        self.tree.tag_configure("fresh", foreground="#0b6b2e")
        self.tree.tag_configure("tracked", background="#eef2fb")

        self.fit_detail = tk.StringVar(value="Select a job to see why it scored what it did.")
        ttk.Label(parent, textvariable=self.fit_detail, foreground="#555").pack(anchor="w", pady=(2, 0))

        self.ctx_menu = tk.Menu(self, tearoff=False)
        self.ctx_menu.add_command(label="Open in browser", command=self.open_selected)
        self.ctx_menu.add_command(label="Tailor CV for this job", command=self._ctx_tailor)
        self.ctx_menu.add_separator()
        for st in STATUSES:
            self.ctx_menu.add_command(label=f"Mark {st}", command=lambda s=st: self.set_selected_status(s))
        self.ctx_menu.add_command(label="Stop tracking", command=lambda: self.set_selected_status(""))

    # ---------------- results helpers ----------------

    @staticmethod
    def _posted_label(job: Job) -> str:
        dt = parse_posted(job.posted_at)
        return dt.astimezone().strftime("%Y-%m-%d") if dt else ""

    def _values(self, job: Job) -> tuple:
        fit = "" if job.fit_score is None else job.fit_score
        return (fit, job.title, job.company, job.location, job.employment_type,
                self._posted_label(job), job.status)

    def _tags(self, job: Job, new: bool) -> tuple:
        tags = []
        if new:
            tags.append("new")
        elif job.status:
            tags.append("tracked")
        a = age_hours(job)
        if a is not None and a <= 24:
            tags.append("fresh")
        return tuple(tags)

    def _passes(self, job: Job) -> bool:
        needle = self.filter_var.get().lower().strip()
        if needle:
            hay = f"{job.title} {job.company} {job.location} {job.employment_type} {job.status}".lower()
            if needle not in hay:
                return False
        if self.fresh_only.get():
            a = age_hours(job)
            if a is None or a > 24:
                return False
        try:
            floor = int(self.min_fit.get())
        except (tk.TclError, ValueError):
            floor = 0
        if floor and (job.fit_score or 0) < floor:
            return False
        return True

    # ---------------- console ----------------

    def _console(self, parent) -> None:
        ttk.Label(parent, text="Activity").pack(anchor="w", pady=(6, 2))
        self.console = tk.Text(parent, height=8, wrap="word", font=("TkFixedFont", 9))
        cs = ttk.Scrollbar(parent, orient="vertical", command=self.console.yview)
        self.console.configure(yscrollcommand=cs.set, state="disabled")
        self.console.pack(side="left", fill="both", expand=True)
        cs.pack(side="left", fill="y")

    def _statusbar(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(6, 0))
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=180)
        self.progress.pack(side="right")
        self.cancel_btn = ttk.Button(bar, text="Stop", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="right", padx=8)

    # ---------------- plumbing ----------------

    def log(self, msg: str) -> None:
        self.console.configure(state="normal")
        self.console.insert("end", f"{datetime.now():%H:%M:%S}  {msg}\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "job":
                    job, tag = payload
                    self._insert(job, new=True)
                elif kind == "call":
                    payload()
                elif kind == "done":
                    self._finish()
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _ui(self, fn) -> None:
        """Run fn on the Tk main thread. Safe to call from a worker thread,
        where touching Tk directly (even self.after) is not."""
        self.events.put(("call", fn))

    def _insert(self, job: Job, new: bool = False) -> None:
        iid = job.job_id
        if iid in self.rows:
            return
        self.rows[iid] = job
        if self._passes(job):
            self.tree.insert("", "end", iid=iid, tags=self._tags(job, new),
                             values=self._values(job))
        self.status.set(f"{len(self.rows)} roles")

    def start(self, label: str, fn) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A search is already running.")
            return
        self.log(f"--- {label} ---")
        self.status.set(label)
        self.progress.start(12)
        self.cancel_btn.configure(state="normal")
        self.worker = Worker(fn, self.events)
        self.worker.start()

    def _finish(self) -> None:
        self.progress.stop()
        self.cancel_btn.configure(state="disabled")
        self.status.set(f"Done — {len(self.rows)} roles")
        self.log("finished")

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancelled.set()
            self.log("stopping after current request…")

    # ---------------- searches ----------------

    def run_phd(self) -> None:
        codes = self._selected(self.phd_countries)
        if not codes:
            messagebox.showwarning("No countries", "Pick at least one country.")
            return
        field = self.phd_field.get().strip() or "robotics"
        pages = self.phd_pages.get()
        funded = self.phd_funded.get()

        def task(w: Worker) -> None:
            provider = PhdProvider()
            if provider.ping(on_log=w.log) == 0:
                w.log("EURES reports 0 vacancies portal-wide — likely a temporary portal issue.")
            grand_total = kept = 0
            for code in codes:
                if w.cancelled.is_set():
                    return
                for term in PHD_TERMS.get(code, ("PhD position",))[:3]:
                    if w.cancelled.is_set():
                        return
                    query = f"{term} {field}"
                    try:
                        jobs = provider.search_phd(query, code, pages, on_log=w.log)
                    except Exception as exc:
                        w.log(f"  {code} '{query}' FAILED: {exc}")
                        continue
                    grand_total += len(jobs)
                    for job in jobs:
                        if funded and not FUNDING_MARKERS.search(job.title + job.description):
                            continue
                        if SENIOR_MARKERS.search(job.title):
                            continue
                        kept += 1
                        if self.store.upsert(job):
                            w.found(job)
            w.log(f"{grand_total} returned by EURES, {kept} passed filters")
            if grand_total and not kept:
                w.log("Everything was filtered out — try unticking 'Funded positions only'.")
            elif not grand_total:
                w.log("EURES returned nothing. Try a broader field, or more countries.")

        self.start(f"PhD · {field}", task)

    def run_grad(self) -> None:
        picked = self.company_list.curselection()
        boards = [self._companies[i] for i in picked] if picked else list(self._companies)
        strict = self.grad_strict.get()
        jobspy_fallback = self.grad_jobspy_fallback.get()

        def task(w: Worker) -> None:
            fetcher = ATSBoards()
            total = 0
            dead_boards = []
            for b in boards:
                if w.cancelled.is_set():
                    return
                try:
                    postings = fetcher.fetch(b)
                except Exception as exc:
                    w.log(f"{b.company}: FAILED — {exc}")
                    dead_boards.append(b)
                    continue
                if not postings:
                    dead_boards.append(b)
                w.log(f"{b.company} ({b.ats}): {len(postings)} postings")
                total += len(postings)
                for job in postings:
                    blob = f"{job.title} {job.description[:800]}"
                    if not MECHATRONICS_MARKERS.search(blob):
                        continue
                    if SENIOR_MARKERS.search(job.title):
                        continue
                    grad = bool(GRAD_MARKERS.search(blob))
                    if strict and not grad:
                        continue
                    job.employment_type = "graduate" if grad else "entry-candidate"
                    if self.store.upsert(job):
                        w.found(job)
            w.log(f"{total} postings scanned across {len(boards)} employers")
            if jobspy_fallback and not w.cancelled.is_set():
                _jobspy_fallback_for(self.store, dead_boards, w, strict)

        self.start(f"Employer boards · {len(boards)} companies", task)

    def run_student(self) -> None:
        codes = self._selected(self.stu_countries)
        if not codes:
            messagebox.showwarning("No countries", "Pick at least one country.")
            return
        field = self.stu_field.get().strip()
        kind = self.stu_kind.get()
        kinds = list(KINDS) if kind == "(all)" else [kind]

        def task(w: Worker) -> None:
            provider = EuresProvider()
            provider.ping(on_log=w.log)
            intern = InternshipProvider()
            attr = {"internship": "internship", "working_student": "working_student",
                    "werkstudent": "working_student",
                    "graduate": "graduate", "thesis": "thesis"}
            grand_total = kept = 0
            for code in codes:
                if w.cancelled.is_set():
                    return
                market = MARKETS[code]
                terms: list[str] = []
                for k in kinds:
                    terms.extend(getattr(market, attr[k]))
                for term in terms[:6]:
                    if w.cancelled.is_set():
                        return
                    query = f"{term} {field}".strip()
                    try:
                        # Internships also get the structured offering-code filter.
                        client = intern if "internship" in kinds and len(kinds) == 1 else provider
                        if client is intern:
                            jobs = client.search_intern(query, code, 1, on_log=w.log)
                        else:
                            jobs = client.search(query, "", 1, False, country=code, on_log=w.log)
                    except Exception as exc:
                        w.log(f"  {code} '{query}' FAILED: {exc}")
                        continue
                    grand_total += len(jobs)
                    for job in jobs:
                        ok, _ = is_student_suitable(job)
                        if not ok:
                            continue
                        label = classify(job, market)
                        job.employment_type = label or job.employment_type
                        kept += 1
                        if self.store.upsert(job):
                            w.found(job)
            w.log(f"{grand_total} returned by EURES, {kept} passed filters")
            if grand_total and not kept:
                w.log("Everything was filtered out — try a broader field term.")
            elif not grand_total:
                w.log("EURES returned nothing for these terms.")

        self.start(f"Student roles · {field}", task)

    # ---------------- table actions ----------------

    def load_saved(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        for r in self.store.all():
            self._insert(Job.from_row(r))
        self._sort("fit", reverse=True)
        fresh = sum(1 for j in self.rows.values() if (a := age_hours(j)) is not None and a <= 24)
        self.log(f"loaded {len(self.rows)} saved roles · {fresh} posted in the last 24h")

    def apply_filter(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for iid, job in self.rows.items():
            if self._passes(job):
                self.tree.insert("", "end", iid=iid, values=self._values(job),
                                 tags=self._tags(job, False))
        self._sort(*getattr(self, "_last_sort", ("fit", True)))
        self.status.set(f"{len(self.tree.get_children(''))} of {len(self.rows)} roles shown")

    def _sort(self, col: str, reverse: bool) -> None:
        def key(k):
            v = self.tree.set(k, col)
            if col == "fit":
                return int(v) if str(v).isdigit() else -1
            return str(v).lower()
        items = sorted(self.tree.get_children(""), key=key, reverse=reverse)
        for pos, k in enumerate(items):
            self.tree.move(k, "", pos)
        self._last_sort = (col, reverse)

    def sort_by(self, col: str) -> None:
        # Numbers and dates read best largest/newest first on the first click.
        default = col in ("fit", "posted")
        last_col, last_rev = getattr(self, "_last_sort", (None, False))
        reverse = (not last_rev) if col == last_col else default
        self._sort(col, reverse)

    def _selected_jobs(self) -> list[Job]:
        return [self.rows[i] for i in self.tree.selection() if i in self.rows]

    def _on_result_select(self, _event=None) -> None:
        jobs = self._selected_jobs()
        if len(jobs) != 1 or self.scorer is None:
            return
        job = jobs[0]
        res = self.scorer.explain(job)
        age = age_hours(job)
        age_s = "" if age is None else (f" · posted {age:.0f}h ago" if age < 48 else f" · posted {age / 24:.0f}d ago")
        self.fit_detail.set(f"Fit {res.summary()}{age_s} · source: {job.source}")

    def _on_result_right_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.ctx_menu.tk_popup(event.x_root, event.y_root)

    def _ctx_tailor(self) -> None:
        jobs = self._selected_jobs()
        if jobs:
            self.prefill_ats(jobs[0])

    def set_selected_status(self, status: str) -> None:
        jobs = self._selected_jobs()
        if not jobs:
            messagebox.showinfo("Nothing selected", "Select one or more jobs first.")
            return
        for job in jobs:
            self.store.set_status(job.job_id, status)
            r = self.store.get(job.job_id)
            if r is not None:
                fresh = Job.from_row(r)
                self.rows[job.job_id] = fresh
                if self.tree.exists(job.job_id):
                    self.tree.item(job.job_id, values=self._values(fresh),
                                   tags=self._tags(fresh, False))
        self.log(f"{len(jobs)} job(s) → {status or 'untracked'}")
        self.refresh_tracker()

    def rescore_all(self) -> None:
        self.scorer = load_default_scorer()
        if self.scorer is None:
            messagebox.showerror("No profile", f"Could not read {default_profile_path()}")
            return
        self.store.scorer = self.scorer.score
        n = rescore(self.store, self.scorer)
        self.log(f"re-scored {n} jobs against {default_profile_path()}")
        self.load_saved()

    def _on_result_double_click(self, event) -> None:
        """First double-click: pre-fill ATS tab. Ctrl+double-click: open URL."""
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        job = self.rows.get(iid)
        if not job:
            return
        # Ctrl held → open URL in browser (power-user shortcut)
        if event.state & 0x4:
            if job.url:
                webbrowser.open(job.url)
        else:
            self.prefill_ats(job)

    def open_selected(self) -> None:
        for job in self._selected_jobs():
            if job.url:
                webbrowser.open(job.url)

    def export(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("JSON", "*.json")])
        if not path:
            return
        visible = [self.rows[i] for i in self.tree.get_children("")]
        if path.endswith(".json"):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([vars(j) for j in visible], fh, indent=2, ensure_ascii=False)
        else:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                wr = csv.writer(fh)
                wr.writerow(["fit", "title", "company", "location", "type", "posted",
                             "status", "applied", "follow_up", "notes", "url"])
                for j in visible:
                    wr.writerow([j.fit_score, j.title, j.company, j.location, j.employment_type,
                                 j.posted_at, j.status, j.applied_at, j.follow_up, j.notes, j.url])
        self.log(f"exported {len(visible)} rows → {path}")

    def clear_db(self) -> None:
        if messagebox.askyesno("Clear untracked jobs",
                               "Delete all saved roles you are NOT tracking?\n\n"
                               "Jobs with a status (saved, applied, interview…) are kept."):
            n = self.store.clear()
            self.log(f"deleted {n} untracked roles")
            self.load_saved()


def ats_headless(argv: list) -> int:
    """`EUJobSearch.exe --ats JOB.txt [--region DE]`: the CV tab's Generate
    without the window, with the tab's defaults (both CV styles, cover letter,
    no PDF compile, plan accepted as proposed) and the tab's profile lookup.
    The .exe has no console, so the log also goes to ats_headless.log in the
    data folder."""
    import argparse
    p = argparse.ArgumentParser(prog="EUJobSearch.exe --ats")
    p.add_argument("--ats", required=True, metavar="JOB_TEXT_FILE")
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--profile", default=None)
    # "modelA,modelB": the first judges, every listed model drafts the cover letter
    p.add_argument("--model", default=DEFAULT_LOCAL_MODELS)
    args, _ = p.parse_known_args(argv)
    log_file = _data_dir() / "ats_headless.log"
    with open(log_file, "w", encoding="utf-8") as fh:
        def log(m: str) -> None:
            fh.write(m + "\n")
            fh.flush()
        try:
            profile = Path(args.profile) if args.profile else default_profile_path()
            log(f"[ATS] Profile: {profile}" + ("  (WARNING: the copy packed into the .exe)"
                                               if is_bundled_copy(profile) else ""))
            jd = Path(args.ats).read_text(encoding="utf-8")
            pkg, out = ats_run(jd, args.region, profile_path=profile,
                               outputs_root=_data_dir() / "ats_outputs", style="both",
                               cover=True, compile_pdf=False, backend=DEFAULT_BACKEND,
                               model=args.model,
                               log=lambda m: log(f"[ATS] {m}"))
            log(f"[ATS] Done: {out}")
            return 0
        except Exception:  # noqa: BLE001 - report everything in the log file
            log(traceback.format_exc())
            return 1


if __name__ == "__main__":
    # The Windows scheduled task runs "EUJobSearch.exe --sweep": do the daily
    # sweep headless (no window) and exit.
    if "--sweep" in sys.argv:
        sys.exit(daily_sweep.headless(sys.argv[1:]))
    if "--ats" in sys.argv:
        sys.exit(ats_headless(sys.argv[1:]))
    App().mainloop()
