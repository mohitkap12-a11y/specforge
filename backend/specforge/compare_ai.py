"""AI summary of a spec comparison (map-reduce, so it works with small local models).

The comparison itself is computed by code (compare.py). The model only *describes* it: for each chapter it receives a digest of
the most important detected changes, each with an id, and writes a headline plus a few points that cite those ids; a second call
turns the chapter headlines into an overall summary. Ids the model was not shown are dropped, so it cannot invent a change, and
the full change list stays the authoritative record.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Literal

from pydantic import BaseModel, Field

from . import compare, compare_entities, eventlog
from .llm.base import LLMError
from .llm.service import LLMService

PROMPT_VERSION = "compare-v1"
MAX_CHAPTERS = 30           # chapters summarised individually (the most affected first)
PER_CHAPTER = 25            # changes shown to the model per chapter
CHAPTER_CHARS = 9000
OVERALL_CHARS = 9000
ENTITY_PROMPT_VERSION = "compare-entities-v1"
ENTITY_GROUPS = ("commands_added", "commands_modified", "structures_added", "structures_modified")
ENTITY_ROWS = 30            # rows described per table (the most important first); the rest keep their factual line
ENTITY_CHARS = 9000

Impact = Literal["breaking", "new", "changed", "clarification"]


class Point(BaseModel):
    title: str = Field(description="what changed, in at most 12 words")
    detail: str = Field(description="1-3 sentences: what the new version says or does differently and why it matters to an implementer or test author")
    impact: Impact = "changed"
    refs: list[str] = Field(default=[], description="ids (C1, C2 ...) of the listed changes this point is based on")


class ChapterSummary(BaseModel):
    headline: str = Field(description="one sentence on what changed in this chapter")
    points: list[Point] = []


class OverallSummary(BaseModel):
    overview: str = Field(description="4-6 sentences: what the new version brings overall")
    highlights: list[Point] = []
    breaking: list[str] = Field(default=[], description="changes that can break existing implementations or tests, one line each")
    upgrade_notes: list[str] = Field(default=[], description="what an implementer or test author should review or update, one line each")


class EntityLine(BaseModel):
    id: str = Field(description="the row id (E1, E2 ...)")
    summary: str = Field(description="one or two sentences: what is new or what changed, with exact field names and values")


class EntityLines(BaseModel):
    rows: list[EntityLine] = []


CHAPTER_SYSTEM = """You compare two versions of a technical standard (storage/NVMe) and explain the differences for engineers.

You are given CHANGES: numbered detected differences (C1, C2 ...) between the old and new version of ONE chapter. Each says whether
it is NEW, REMOVED or MODIFIED, its impact (breaking / new / changed / clarification), and the exact text that changed.
'shall' means mandatory, 'should' recommended, 'may' optional.

Reply with one JSON object:
- "headline": one sentence on what changed in this chapter.
- "points": up to 6 items, most important first. Each has "title" (<= 12 words), "detail" (1-3 sentences: what is different and
  why it matters), "impact" (breaking | new | changed | clarification) and "refs" (the ids of the changes it is based on).

Rules: describe only what the CHANGES show; never invent a requirement, value or field. Quote names and values exactly. Merge
related changes into one point. Ignore editorial changes. Output JSON only."""

OVERALL_SYSTEM = """You write the release summary of a new version of a technical standard (storage/NVMe) for engineers.

You are given COUNTS and CHAPTERS: for each chapter, a headline and its main points, already derived from the detected differences.

Reply with one JSON object:
- "overview": 4-6 sentences on what the new version brings overall: new features, behaviour changes, removals.
- "highlights": up to 8 items, most important first; each with "title" (<= 12 words), "detail" (1-3 sentences), "impact"
  (breaking | new | changed | clarification) and "refs" (leave empty).
- "breaking": changes that can break existing implementations or tests, one line each (empty if none).
- "upgrade_notes": what an implementer or test author should review or update, one line each.

Rules: use only what CHAPTERS states; do not add facts. Output JSON only."""


ENTITY_SYSTEM = """You describe the commands and data structures that changed between two versions of a technical standard (storage/NVMe).

You are given ROWS: numbered items (E1, E2 ...), each a command or data structure, with the detected changes it is built from.
Reply with one JSON object: {"rows": [{"id": "E1", "summary": "..."}, ...]}, one entry per row you were given.
Each summary is 1-2 sentences for an implementer or test author: for a NEW item what it is for and its main fields; for a
MODIFIED item exactly what is different (fields added, removed, made reserved, values or requirement levels changed).

Rules: describe only what the changes show; never invent a field, value or opcode. Quote names and values exactly. Output JSON only."""


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _describe(c: dict) -> str:
    """One change as the text the model reads."""
    d, num = c.get("details", {}), c.get("number_to") or c.get("number_from") or ""
    head = f"{c['change'].upper()} {c['kind']} {num} '{c['title']}' [{c['impact']}]"
    lines = [head]
    if c["change"] == "renamed":
        lines.append(f"  caption/title: '{c.get('title_from') or ''}' -> '{c['title']}' (content unchanged)")
    elif c["change"] == "moved":
        lines.append(f"  number: {c.get('number_from')} -> {c.get('number_to')} (content unchanged)")
    elif c["kind"] == "section" and c["change"] == "modified":
        for s in d.get("removed", [])[:4]:
            lines.append(f"  - removed: {_clip(s['text'], 260)}")
        for s in d.get("added", [])[:4]:
            lines.append(f"  + added: {_clip(s['text'], 260)}")
        for m in d.get("modified", [])[:4]:
            lines.append(f"  ~ before: {_clip(m['before'], 220)}")
            lines.append(f"    after:  {_clip(m['after'], 220)}")
    elif c["kind"] == "section":
        for s in d.get("excerpt", [])[:2]:
            lines.append(f"  {_clip(s, 260)}")
    elif c["change"] == "modified":
        if d.get("header_changed"):
            lines.append(f"  ~ columns: {_clip(' | '.join(d.get('header_before', [])), 160)} -> {_clip(' | '.join(d.get('header_after', [])), 160)}")
        for r in d.get("rows_added", [])[:5]:
            lines.append(f"  + row: {_clip(' | '.join(r), 220)}")
        for r in d.get("rows_removed", [])[:5]:
            lines.append(f"  - row: {_clip(' | '.join(r), 220)}")
        for r in d.get("rows_changed", [])[:5]:
            for cell in r["cells"][:3]:
                lines.append(f"  ~ row {r['key']} {cell['column']}: {_clip(cell['before'], 140)} -> {_clip(cell['after'], 140)}")
    else:
        hdr = " | ".join(d.get("header", []))
        lines.append(f"  columns: {_clip(hdr, 160)}; {d.get('row_count', 0)} row(s)")
        for r in d.get("rows", [])[:4]:
            lines.append(f"  row: {_clip(' | '.join(r), 200)}")
    return "\n".join(lines)


def _chapter_digest(items: list[dict]) -> tuple[str, list[dict]]:
    """The CHANGES text for a chapter and the changes that fit in it (those the model may cite)."""
    shown, out, total = [], [], 0
    for c in items:
        txt = f"C{len(shown) + 1}: " + _describe(c)
        if total + len(txt) > CHAPTER_CHARS and shown:
            break
        shown.append(c)
        out.append(txt)
        total += len(txt)
    return "\n".join(out), shown


def summarize(conn: sqlite3.Connection, cid: int, svc: LLMService, redo: bool = False, progress=None) -> dict:
    comp = compare.get_comparison(conn, cid)
    if comp is None:
        raise KeyError(cid)
    if comp["ai"] and not redo:
        return comp["ai"]["stats"]
    s = comp["summary"]
    cfg = svc.task_cfg("generate")
    chapters = [c for c in s["chapters"] if c["total"] - c["editorial"] > 0][:MAX_CHAPTERS]
    stats = {"chapters": len(chapters), "summarised": 0, "failed": 0, "dropped_refs": 0, "dropped_points": 0, "entities_described": 0}
    eventlog.emit("info", "agent", f"Compare summariser starting: {len(chapters)} chapter(s), model {cfg['provider']}/{cfg['model'] or '(unset)'}", compare_id=cid)
    done: list[dict] = []
    meta: dict = {}
    for n, ch in enumerate(chapters):
        if progress:
            progress(f"summarising chapter {ch['chapter']}", n, len(chapters) + 2)
        items = compare.query_changes(conn, cid, chapter=ch["chapter"], nested=True, sort="importance", limit=PER_CHAPTER * 3)["items"]
        items = [c for c in items if c["impact"] != "editorial"][:PER_CHAPTER]
        if not items:
            continue
        text, shown = _chapter_digest(items)
        user = f"CHAPTER {ch['chapter']} {ch['title']}\nOLD {s['from']['version']} -> NEW {s['to']['version']}\n\nCHANGES:\n{text}"
        try:
            out, meta = svc.generate_model("generate", [{"role": "system", "content": CHAPTER_SYSTEM}, {"role": "user", "content": user}],
                                           ChapterSummary, PROMPT_VERSION)
        except LLMError as e:
            stats["failed"] += 1
            eventlog.emit("error", "agent", f"chapter {ch['chapter']}: model failed: {str(e)[:200]}", compare_id=cid, error=str(e))
            continue
        by_id = {f"C{i}": c for i, c in enumerate(shown, 1)}
        points = []
        for p in out.points[:8]:
            refs = [r.strip().upper() for r in p.refs]
            good = [r for r in refs if r in by_id]
            stats["dropped_refs"] += len(refs) - len(good)
            if not good:        # a point that cites no change it was shown could be invented: it is not kept
                stats["dropped_points"] += 1
                continue
            points.append({"title": _clip(p.title, 160), "detail": _clip(p.detail, 700), "impact": p.impact,
                           "refs": [{"id": by_id[r]["id"], "kind": by_id[r]["kind"], "number": by_id[r].get("number_to") or by_id[r].get("number_from"),
                                     "title": by_id[r]["title"]} for r in good]})
        done.append({"chapter": ch["chapter"], "title": ch["title"], "headline": _clip(out.headline, 400), "points": points,
                     "changes": ch["total"]})
        stats["summarised"] += 1
    if progress:
        progress("writing the overall summary", len(chapters), len(chapters) + 2)
    overall = {"overview": "", "highlights": [], "breaking": [], "upgrade_notes": []}
    if done:
        lines = []
        for ch in done:
            lines.append(f"CHAPTER {ch['chapter']} {ch['title']} ({ch['changes']} changes): {ch['headline']}")
            lines += [f"  - [{p['impact']}] {p['title']}: {_clip(p['detail'], 240)}" for p in ch["points"][:5]]
        digest = _clip_block("\n".join(lines), OVERALL_CHARS)
        bi, nr = s["by_impact"], s["normative"]
        counts = (f"{s['total']} changes: {bi['breaking']} breaking, {bi['new']} new, {bi['changed']} changed, {bi['clarification']} clarifications, "
                  f"{bi['editorial']} editorial. Sections added {s['roots']['sections_added']}, removed {s['roots']['sections_removed']}; "
                  f"tables added {s['roots']['tables_added']}, removed {s['roots']['tables_removed']}. "
                  f"Normative statements: {nr['added']} added, {nr['removed']} removed, {nr['strength_changed']} with a changed level.")
        user = f"{s['to']['family']}: {s['from']['version']} -> {s['to']['version']}\nCOUNTS: {counts}\n\nCHAPTERS:\n{digest}"
        try:
            out, meta = svc.generate_model("generate", [{"role": "system", "content": OVERALL_SYSTEM}, {"role": "user", "content": user}],
                                           OverallSummary, PROMPT_VERSION)
            overall = {"overview": _clip(out.overview, 2500),
                       "highlights": [{"title": _clip(h.title, 160), "detail": _clip(h.detail, 700), "impact": h.impact} for h in out.highlights[:8]],
                       "breaking": [_clip(b, 400) for b in out.breaking[:12]], "upgrade_notes": [_clip(u, 400) for u in out.upgrade_notes[:12]]}
        except LLMError as e:
            stats["failed"] += 1
            eventlog.emit("error", "agent", f"overall summary: model failed: {str(e)[:200]}", compare_id=cid, error=str(e))
    if not chapters:       # nothing but renumbering or no change at all: there is nothing for a model to describe, and that is not a failure
        overall["overview"] = "No substantive changes were detected between these versions." if not s["total"] else \
            "The only differences are editorial (renumbering and renaming); nothing else changed."
    elif not done and not overall["overview"]:
        raise LLMError("the model produced no summary (see the Activity log for the errors)")
    if not overall["overview"]:     # the reduce step failed: fall back to the chapter headlines so the work is not lost
        overall["overview"] = " ".join(ch["headline"] for ch in done[:6])
    entities = _describe_entities(conn, cid, comp["summary"].get("entities") or {}, svc, stats,
                                  (lambda m, _i, _t: progress(m, len(chapters) + 1, len(chapters) + 2)) if progress else None)
    ai = {**overall, "chapters": done, "entities": entities, "meta": {**meta, "prompt_version": PROMPT_VERSION}, "stats": stats}
    conn.execute("UPDATE spec_compare SET ai_json=? WHERE id=?", (json.dumps(ai), cid))
    conn.commit()
    eventlog.emit("info" if not stats["failed"] else "warn", "agent", "Compare summariser finished: " + ", ".join(f"{v} {k}" for k, v in stats.items()), **stats)
    return stats


def _clip_block(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rsplit("\n", 1)[0]


def _entity_digest(group: str, items: list[dict], by_id: dict[int, dict]) -> tuple[str, list[dict]]:
    verb = "NEW" if group.endswith("_added") else "MODIFIED"
    out, shown, total = [], [], 0
    for e in items[:ENTITY_ROWS]:
        lines = [f"E{len(shown) + 1}: {verb} {'command' if group.startswith('commands') else 'structure'} '{e['name']}'"
                 + (f" (opcode {e['opcode']})" if e.get("opcode") else "") + f" [{e['impact']}]"]
        for ref in e["changes"][:4]:
            c = by_id.get(ref["id"])
            lines.append("  " + ref["text"])
            if c is not None and ref["text"] == compare_entities.describe(c):      # the change itself: show what it says
                lines += ["    " + ln for ln in _describe(c).split("\n")[1:6]]
        txt = "\n".join(lines)
        if total + len(txt) > ENTITY_CHARS and shown:
            break
        shown.append(e)
        out.append(txt)
        total += len(txt)
    return "\n".join(out), shown


def _describe_entities(conn: sqlite3.Connection, cid: int, entities: dict, svc: LLMService, stats: dict, progress=None) -> dict[str, dict[str, str]]:
    """{group: {row key: one-line description}} for the command / structure tables. A failure leaves the factual lines in place."""
    groups = [g for g in ENTITY_GROUPS if (entities.get(g) or {}).get("items")]
    if not groups:
        return {}
    want = {ref["id"] for g in groups for e in entities[g]["items"][:ENTITY_ROWS] for ref in e["changes"][:4]}
    by_id = {c["id"]: c for c in compare.query_changes(conn, cid, ids=sorted(want), limit=max(len(want), 1))["items"]} if want else {}
    out: dict[str, dict[str, str]] = {}
    for n, g in enumerate(groups):
        if progress:
            progress(f"describing {g.replace('_', ' ')}", n, len(groups))
        text, shown = _entity_digest(g, entities[g]["items"], by_id)
        try:
            res, _meta = svc.generate_model("generate", [{"role": "system", "content": ENTITY_SYSTEM}, {"role": "user", "content": f"ROWS:\n{text}"}],
                                            EntityLines, ENTITY_PROMPT_VERSION)
        except LLMError as e:
            stats["failed"] += 1
            eventlog.emit("error", "agent", f"{g.replace('_', ' ')}: model failed: {str(e)[:200]}", compare_id=cid, error=str(e))
            continue
        keys = {f"E{i}": e["key"] for i, e in enumerate(shown, 1)}
        got = {keys[r.id.strip().upper()]: _clip(r.summary, 600) for r in res.rows if r.id.strip().upper() in keys and r.summary.strip()}
        stats["dropped_refs"] += sum(1 for r in res.rows if r.id.strip().upper() not in keys)
        stats["entities_described"] += len(got)
        out[g] = got
    return out
