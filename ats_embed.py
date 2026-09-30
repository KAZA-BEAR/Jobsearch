"""
ats_embed.py — match job requirements to profile lines by meaning (nomic embeddings).

Meaning-based matching of job requirements to profile lines.

Word banks miss paraphrases ("control engineering" vs "Control Systems",
"cross-domain" vs "across different domains"). Here both sides are turned into
vectors by an embedding model and compared by cosine similarity, so texts with
the same meaning match even when they share no words.

The model is nomic-embed-text-v1.5 served by LM Studio's OpenAI-compatible
/v1/embeddings endpoint, called over urllib like the chat models (no new
dependencies). Nomic expects task prefixes: "search_query: " for what is looked
for (the requirement) and "search_document: " for what is searched (profile
lines). Vectors are cached on disk, so profile lines are embedded once.

Similarity says two texts are ABOUT the same thing, not that one proves the
other, and nomic's scores are too close together for a cut-off (true matches
0.57-0.84, unmet requirements up to 0.65). So similarity only RANKS profile
lines; ats_plan has a verifier decide whether the best ones prove the
requirement. Research interests are left out: they are never proof.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.request
from pathlib import Path

EMBED_MODEL = os.environ.get("ATS_EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
_cache: dict | None = None


def _cache_file() -> Path:
    """In the app's data folder: next to the code is a temporary folder in the .exe."""
    try:
        from linkedin_jobs import data_dir
        return data_dir() / "embed_cache.json"
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent / "embed_cache.json"


class EmbedError(RuntimeError):
    pass


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_cache_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        _cache_file().write_text(json.dumps(_cache), encoding="utf-8")
    except OSError:
        pass                    # a read-only install just re-embeds next time


def embed(texts: list, base_url: str, model: str = EMBED_MODEL, timeout: int = 300) -> list:
    """One unit-length vector per text (texts carry their nomic prefix)."""
    cache = _load_cache()
    key = lambda t: hashlib.sha1(f"{model}\n{t}".encode("utf-8")).hexdigest()
    todo = [t for t in dict.fromkeys(texts) if key(t) not in cache]
    for i in range(0, len(todo), 64):
        batch = todo[i:i + 64]
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/embeddings",
            data=json.dumps({"model": model, "input": batch}).encode("utf-8"),
            headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))["data"]
        except Exception as e:  # noqa: BLE001 - any failure means "no embeddings"
            raise EmbedError(f"embedding model '{model}' not available at {base_url}: {e}")
        for item in data:
            v = item["embedding"]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            cache[key(batch[item["index"]])] = [x / n for x in v]
    if todo:
        _save_cache()
    return [cache[key(t)] for t in texts]


def cosine(a: list, b: list) -> float:
    return sum(x * y for x, y in zip(a, b))       # vectors are unit length


# ------------------------------------------------------------ profile items

def profile_items(profile: dict) -> list:
    """(text to embed, evidence line as ats_plan labels it) for every piece
    of the profile that can prove a skill. List lines (skills, coursework) are
    split into single items: "Gazebo" compared against a 13-tool line scored
    low because the other 12 tools dilute it."""
    items = []
    for e in profile.get("experience") or []:
        for b in e.get("bullets") or []:
            items.append((b, f"{e['title']} @ {e['employer']}: {b}"))
    for p in profile.get("projects") or []:
        # the bullet alone: with the project name in front, "XR Simulation of
        # Factory" pulled unrelated requirements ("robot simulation") to its bullets
        for b in p.get("bullets") or []:
            items.append((b, f"Project: {p['name']}: {b}"))
    for cat, values in (profile.get("skills") or {}).items():
        line = f"Skills: {cat}: {', '.join(values)}"
        items += [(f"{v} ({cat})", line) for v in values]
    for ed in profile.get("education") or []:
        # Courses only: a research interest is not proof, and on this line it
        # would read as coursework in the letter built from the profile.
        courses = ed.get("coursework") or []
        line = f"Education: {ed['degree']}: {', '.join(courses)}"
        items += [(f"{c} (course, {ed['degree']})", line) for c in courses]
        if ed.get("thesis"):
            items.append((ed["thesis"], f"Thesis: {ed['degree']}: {ed['thesis']}"))
    return items


def rank(requirements: list, profile: dict, base_url: str, top: int = 3,
         terms: list = None) -> list:
    """For each requirement, the `top` best profile items as
    [(score, evidence line, item text)], best first, one entry per line.
    `terms[i]` are concrete terms for requirement i ("serial communication
    buses" -> UART, I2C, SPI); they pull in lines that name an instance of the
    skill rather than the skill itself. Each item scores the higher of the skill
    name alone and the name with its terms: with the terms only, "motor control"
    pushed a PCB bullet above the course "Control Systems" for "control
    engineering". (The ad's quote, "Good knowledge of ...", only added noise.)"""
    items = profile_items(profile)
    terms = terms or [[] for _ in requirements]
    plain = [f"search_query: {r['skill']}" for r in requirements]
    wide = [f"search_query: {r['skill']}: {', '.join(t)}" for r, t in zip(requirements, terms) if t]
    vecs = embed(plain + wide + [f"search_document: {t}" for t, _l in items], base_url)
    iv = vecs[len(plain) + len(wide):]
    wide_vecs = iter(vecs[len(plain):len(plain) + len(wide)])
    out = []
    for q, t in zip(vecs[:len(plain)], terms):
        qs = [q, next(wide_vecs)] if t else [q]
        best = {}
        for (text, line), v in zip(items, iv):
            s = max(cosine(x, v) for x in qs)
            if line not in best or s > best[line][0]:
                best[line] = (s, line, text)
        out.append(sorted(best.values(), reverse=True)[:top])
    return out

