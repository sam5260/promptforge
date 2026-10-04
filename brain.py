#!/usr/bin/env python3
"""brain — local learning store: feedback events, patterns, apply layer.
language: python 3.10+ | runtime: stdlib only (sqlite3) | target: promptforge v2

weights are deterministic: count x recency decay (half-life 30 days).
auto-apply threshold: effective weight >= 3. everything stays on this machine.
"""

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import forge

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "brain.db"

AUTO_APPLY = 3.0
HALF_LIFE_DAYS = 30
HEADING_RE = re.compile(r"^#{1,6}\s")
CHECKBOX_ITEM_RE = re.compile(r"^\s*- \[[ xX]\]")

NEG = ("don't", "do not", "dont", "never", "avoid", "refrain", "must not",
       "mustn't", "don’t", "no brute", "exclude", "out of scope")
POS = ("always", "must ", "ensure ", "keep ", "prefer ", "only ")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    spec_id TEXT NOT NULL DEFAULT '',
    norm TEXT NOT NULL,
    sample TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    created_ts INTEGER NOT NULL,
    last_seen_ts INTEGER NOT NULL,
    UNIQUE(kind, spec_id, norm)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def set_db(path):
    global DB_PATH
    DB_PATH = Path(path)


@contextmanager
def _db():
    """open-commit-close per call: threading-safe, no lingering file locks"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


def _norm(line):
    return forge.normalize(line)


def _decayed(count, last_seen_ts, now=None):
    now = now or time.time()
    days = max(0.0, (now - last_seen_ts) / 86400.0)
    return round(count * (0.5 ** (days / HALF_LIFE_DAYS)), 2)


def record(kind, spec_id, norm, sample):
    """upsert one feedback event — bumps count + last_seen on repeats"""
    spec_id = spec_id or ""
    norm = _norm(norm)[:500]
    sample = re.sub(r"\s+", " ", str(sample)).strip()[:400]
    if not norm or not sample:
        return None
    now = int(time.time())
    with _db() as con:
        row = con.execute(
            "SELECT id, count FROM events WHERE kind=? AND spec_id=? AND norm=?",
            (kind, spec_id, norm)).fetchone()
        if row:
            con.execute("UPDATE events SET count=count+1, last_seen_ts=?, "
                        "sample=? WHERE id=?", (now, sample, row["id"]))
            return row["id"]
        cur = con.execute(
            "INSERT INTO events(kind, spec_id, norm, sample, count, "
            "created_ts, last_seen_ts) VALUES(?,?,?,?,1,?,?)",
            (kind, spec_id, norm, sample, now, now))
        return cur.lastrowid


def _looks_like_constraint(sent):
    low = " " + sent.lower() + " "
    if any(m in low for m in NEG):
        return "negative"
    if any(m in low for m in POS):
        return "positive"
    return None


def learn_from_diff(spec_id, generated, corrected):
    """diff two prompts -> boilerplate deletions, added constraints, slot edits"""
    gen_lines = [l for l in (generated or "").splitlines() if l.strip()]
    cor_lines = [l for l in (corrected or "").splitlines() if l.strip()]
    gen_set = {_norm(l) for l in gen_lines}
    cor_set = {_norm(l) for l in cor_lines}
    learned, ignored = [], 0
    slot_re = re.compile(r"^\s*[-*]\s*\*\*([^*]+)\*\*\s*:\s*(.+)$")

    for line in gen_lines:
        n = _norm(line)
        if n in cor_set or not n:
            continue
        if HEADING_RE.match(line.strip()) or CHECKBOX_ITEM_RE.match(line):
            ignored += 1
            continue
        if slot_re.match(line):
            ignored += 1  # context-slot edits are handled as slot_override
            continue
        record("boilerplate_removed", spec_id, n, line)
        learned.append({"kind": "boilerplate_removed", "sample": line.strip()})

    gen_slots = {}
    for line in gen_lines:
        m = slot_re.match(line)
        if m:
            gen_slots[forge.normalize(m.group(1))] = m.group(2).strip()

    for line in cor_lines:
        n = _norm(line)
        if n in gen_set or not n:
            continue
        m = slot_re.match(line)
        if m:
            key = forge.normalize(m.group(1))
            old = gen_slots.get(key)
            if old is not None and _norm(old) != _norm(m.group(2)):
                record("slot_override", spec_id, key, m.group(2).strip())
                learned.append({"kind": "slot_override",
                                "sample": f"{m.group(1)}: {m.group(2).strip()}"})
                continue
            ignored += 1
            continue
        polarity = _looks_like_constraint(line)
        if polarity:
            record("constraint_added", spec_id, n, line.strip())
            learned.append({"kind": "constraint_added", "sample": line.strip()})
        else:
            ignored += 1
    return {"learned": learned, "ignored": ignored, "spec_id": spec_id or ""}


def suggest(spec_id=None, min_effective=0.0):
    """all patterns with effective weight + auto/suggest status"""
    now = time.time()
    q = "SELECT * FROM events"
    args = ()
    if spec_id:
        q += " WHERE spec_id=?"
        args = (spec_id,)
    q += " ORDER BY last_seen_ts DESC"
    out = []
    with _db() as con:
        for row in con.execute(q, args):
            eff = _decayed(row["count"], row["last_seen_ts"], now)
            if eff < min_effective:
                continue
            out.append({
                "id": row["id"], "kind": row["kind"], "spec_id": row["spec_id"],
                "sample": row["sample"], "count": row["count"],
                "effective": eff,
                "status": "auto" if eff >= AUTO_APPLY else "suggest",
                "age_days": round((now - row["last_seen_ts"]) / 86400.0, 1),
            })
    return out


def forget(pattern_id):
    with _db() as con:
        cur = con.execute("DELETE FROM events WHERE id=?", (int(pattern_id),))
        return cur.rowcount


def reset():
    with _db() as con:
        cur = con.execute("DELETE FROM events")
        return cur.rowcount


def status():
    now = time.time()
    with _db() as con:
        rows = con.execute("SELECT kind, COUNT(*) AS n, SUM(count) AS hits "
                           "FROM events GROUP BY kind").fetchall()
        auto = 0
        n_patterns = 0
        for row in con.execute("SELECT count, last_seen_ts FROM events"):
            n_patterns += 1
            if _decayed(row["count"], row["last_seen_ts"], now) >= AUTO_APPLY:
                auto += 1
    return {
        "db": str(DB_PATH), "patterns": n_patterns,
        "auto_active": auto,
        "by_kind": {r["kind"]: {"patterns": r["n"], "hits": r["hits"] or 0}
                    for r in rows},
        "auto_threshold": AUTO_APPLY, "half_life_days": HALF_LIFE_DAYS,
    }


def export_json():
    with _db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id, kind, spec_id, sample, count, created_ts, last_seen_ts "
            "FROM events ORDER BY id")]
    return json.dumps({"format": "promptforge-brain-v1", "exported": int(time.time()),
                       "patterns": rows}, indent=2, ensure_ascii=False)


def slot_prefs(spec_id):
    """effective slot overrides -> {slot: value} (auto tier only)"""
    out = {}
    with _db() as con:
        now = time.time()
        for row in con.execute(
                "SELECT norm, sample, count, last_seen_ts FROM events "
                "WHERE kind='slot_override' AND spec_id=?", (spec_id,)):
            if _decayed(row["count"], row["last_seen_ts"], now) >= AUTO_APPLY:
                out[row["norm"]] = row["sample"]
    return {k: v for k, v in out.items() if v}


def _strip_previous_prefs(text):
    idx = text.find("## Your preferences")
    return text[:idx].rstrip() if idx != -1 else text


def apply(spec_id, prompt, use_learned=True):
    """post-build: strip learned boilerplate, append auto-tier preferences"""
    report = {"suppressed": [], "added": [], "considered": 0}
    if not use_learned:
        return prompt, report
    patterns = suggest(spec_id)
    boiler = [p for p in patterns
              if p["kind"] == "boilerplate_removed" and p["status"] == "auto"]
    prefs = [p for p in patterns
             if p["kind"] == "constraint_added" and p["status"] == "auto"]
    if not boiler and not prefs:
        return _strip_previous_prefs(prompt).rstrip() + "\n", report

    kept = []
    body = _strip_previous_prefs(prompt).splitlines()
    if boiler:
        report["considered"] = sum(
            1 for l in body
            if l.strip() and not HEADING_RE.match(l.strip())
            and not CHECKBOX_ITEM_RE.match(l))
    for line in body:
        n = _norm(line)
        drop = False
        if n and boiler and not HEADING_RE.match(line.strip()) \
                and not CHECKBOX_ITEM_RE.match(line):
            for p in boiler:
                pn = forge.normalize(p["sample"])
                if pn and (pn in n or n in pn):
                    report["suppressed"].append(p["sample"])
                    drop = True
                    break
        if not drop:
            kept.append(line)

    additions, seen = [], set()
    kept_norms = [forge.normalize(l) for l in kept if l.strip()]
    for p in prefs:
        n = forge.normalize(p["sample"])
        if not n or n in seen:
            continue
        if any(n in kn or kn in n for kn in kept_norms):
            continue
        seen.add(n)
        additions.append(p["sample"])
    if additions:
        while kept and not kept[-1].strip():
            kept.pop()
        kept.append("")
        kept.append("## Your preferences")
        for a in additions:
            kept.append(f"- [learned] {a}")
        report["added"] = additions
    out = "\n".join(kept).rstrip() + "\n"
    return out, report
