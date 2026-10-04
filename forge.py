#!/usr/bin/env python3
"""
promptforge — field-structured prompt generator, checker, and fixer.
language: python 3.10+ | runtime: stdlib only | target: windows/linux terminal

commands:
  parse      read raw chat text -> template, slots, constraints (v2)
  compile    context parser + learned prefs -> finished prompt (v2)
  learn      diff generated vs corrected -> learn your patterns (v2)
  brain      show/forget/reset/export what has been learned (v2)
  gen      build a field-specific prompt from slots
  check    structural score of any prompt against a field's checklist
  fix      patch gaps in your prompt (diff + before/after score)
  compare  score two prompts side by side
  merge    union the strengths of two prompts for one field
  ensemble mutate + score search, returns top candidates
  lint     validate every field spec
  list/show/new

scores are STRUCTURAL (is the discipline present?), never a promise of
model output quality. verify winners against a real model response.
"""

import argparse
import difflib
import hashlib
import json
import random
import re
import sys
from pathlib import Path

SPEC_DIR = Path(__file__).resolve().parent / "specs"

EXIT_OK, EXIT_VALIDATION, EXIT_INPUT = 0, 1, 2
MAX_INPUT_BYTES = 1_000_000

BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)]|#{1,6}|\[[ xX]\])\s")
STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*+])\s")
PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}|<[A-Za-z_][A-Za-z0-9_]*>")

# heading keyword -> structural section key (first match wins)
SECTION_KEYS = [
    ("role", ["role", "system prompt", "you are", "persona"]),
    ("context", ["context", "background", "parameters"]),
    ("task", ["task", "objective", "goal", "brief", "instruction"]),
    ("methodology", ["methodology", "approach", "process", "procedure", "steps", "workflow"]),
    ("output", ["output", "deliverable", "format", "response", "contract"]),
    ("checklist", ["checklist", "self-verify", "quality"]),
    ("negatives", ["do not", "don't", "dont", "never", "constraints", "avoid"]),
]

STRUCT_REQUIRED = ["role", "task", "methodology", "output", "checklist", "negatives"]

STRUCT_LABELS = {
    "role": "role section present",
    "task": "task section present",
    "methodology": "methodology section with steps",
    "output": "output contract section present",
    "checklist": "checklist section present",
    "negatives": "do-not section present",
    "placeholders": "no unresolved placeholders",
}

CHECKLIST_RE = re.compile(r"^[a-z0-9_.-]+(?: [a-z0-9_.-]+)* \| .+$")


class MissingSlot(Exception):
    def __init__(self, name):
        self.name = name


class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


class FillDict(dict):
    """renders unknown {key} as `fill.format(key)` — used by fix/merge grafts"""

    def __init__(self, values, fill):
        super().__init__(values)
        self._fill = fill

    def __missing__(self, key):
        return self._fill.format(key)


# ---------------------------------------------------------------- text utils

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", s.lower())).strip()


CHECKBOX_RE = re.compile(r"^\[[ xX]\]\s*")
TAG_RE = re.compile(r"\[\s*([a-z0-9 _.-]+?)\s*\]")
STEPNUM_RE = re.compile(r"^\s*\d+[.)]\s*")
MIN_PROBE_EXTRA = 5


def probe_vocab(spec) -> set:
    """every word used in any probe — keyword-dumps recycle these, so they
    don't count as substance"""
    words = set()
    for raw in spec.get("checklist", []):
        probe = raw.split("|", 1)[0] if "|" in raw else raw
        words.update(normalize(probe).split())
    return words


def probe_hit(line: str, probe: str, vocab: set) -> bool:
    """probe counts only on a substantive line: >=MIN_PROBE_EXTRA words beyond
    the probe itself and beyond any other probe's vocabulary"""
    np = normalize(probe)
    if not np:
        return False
    cleaned = re.sub(r"^[\s\-*]+", "", line)
    cleaned = STEPNUM_RE.sub("", cleaned)
    cleaned = CHECKBOX_RE.sub("", cleaned)
    cleaned = TAG_RE.sub(r"\1", cleaned)
    n = normalize(cleaned)
    if np not in n:
        return False
    extra = [w for w in n.split() if w not in vocab and w not in np.split()]
    return len(extra) >= MIN_PROBE_EXTRA


def structural_lines(text: str):
    """(line, heading) for every bullet/step/heading line — where requirements count"""
    _, sections = split_sections(text)
    out = []
    for heading, body in sections:
        for line in body:
            if BULLET_RE.match(line):
                out.append((line, heading))
    for line in text.splitlines():
        if line.startswith("## ") and BULLET_RE.match(line):
            out.append((line, line[3:].strip()))
    return out


def heading_key(heading):
    norm = normalize(heading or "")
    for key, kws in SECTION_KEYS:
        if any(kw in norm for kw in kws):
            return key
    return None


def split_sections(text: str):
    """-> (preamble_lines, [(heading, body_lines)]) ; heading None for orphan body"""
    lines = text.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    preamble, rest = lines[:idx], lines[idx:]
    sections = []
    cur_h, cur_b = None, []
    for ln in rest:
        if ln.startswith("## "):
            if cur_h is not None or cur_b:
                sections.append((cur_h, cur_b))
            cur_h, cur_b = ln[3:].strip(), []
        else:
            cur_b.append(ln)
    if cur_h is not None or cur_b:
        sections.append((cur_h, cur_b))
    return preamble, sections


def join_sections(preamble, sections) -> str:
    lines = list(preamble)
    for heading, body in sections:
        lines.append(f"## {heading}")
        lines.extend(body)
    return "\n".join(lines).strip() + "\n"


def read_file(path: str) -> str:
    if path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    if len(text.encode("utf-8", errors="replace")) > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds {MAX_INPUT_BYTES} bytes")
    return text


# ---------------------------------------------------------------- spec loader

def load_specs(fail_fast: bool = False) -> dict:
    specs, errors = {}, []
    for path in sorted(SPEC_DIR.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            if fail_fast:
                errors.append(f"{path.name}: {exc}")
                continue
            print(f"[!] skipping {path.name}: {exc}", file=sys.stderr)
            continue
        spec["_file"] = path.name
        if "id" in spec:
            specs[spec["id"]] = spec
        elif fail_fast:
            errors.append(f"{path.name}: missing id")
    if fail_fast and errors:
        raise ValueError("; ".join(errors))
    if not specs:
        raise ValueError("no field specs found in ./specs")
    return specs


def get_spec(specs: dict, field: str) -> dict:
    if field in specs:
        return specs[field]
    close = [k for k in specs if field in k or k.endswith(field)]
    hint = f" did you mean: {', '.join(close)}" if close else " run: forge list"
    raise KeyError(f"unknown field '{field}'.{hint}")


# ---------------------------------------------------------------- rendering

_SLOT_REF = re.compile(r"\{([A-Za-z0-9_]+)\}")


def render(template: str, values: dict, fill: str = "<{0}>") -> str:
    """interpolate slots; drop whole sentences that interpolate an empty
    slot — a dangling 'within this scope: .' is worse than no sentence.
    keys absent from values keep the fill (fix/merge graft behavior)."""
    kept = [part for part in re.split(r"(?<=\.) ", template)
            if not any(values.get(m.group(1)) == ""
                       for m in _SLOT_REF.finditer(part))]
    return " ".join(kept).format_map(FillDict(values, fill))


def slot_values(spec, provided, interactive=False, assume_yes=False,
                missing_ok=False, fill_missing=""):
    values, missing = {}, []
    for slot in spec.get("slots", []):
        name = slot["name"]
        if name in provided:
            values[name] = provided[name]
        elif slot.get("default") is not None:
            values[name] = slot["default"]
        elif slot.get("required"):
            missing.append(slot)
        else:
            values[name] = ""
    warnings = []
    for slot in missing:
        if assume_yes or missing_ok:
            values[slot["name"]] = f"<{slot['name']}>" if assume_yes else fill_missing
            if missing_ok and not assume_yes:
                warnings.append(
                    f"slot '{slot['name']}' has no value — "
                    "omitted, listed under Unstated")
        elif interactive:
            hint = slot.get("hint", "")
            try:
                answer = input(
                    f"  {slot['name']}" + (f" ({hint})" if hint else "") + ": "
                ).strip()
            except EOFError:
                raise MissingSlot(slot["name"]) from None
            if not answer:
                raise MissingSlot(slot["name"])
            values[slot["name"]] = answer
        else:
            raise MissingSlot(slot["name"])
    return values, warnings


def build(spec, values, mode="full", fill="<{0}>") -> str:
    out = [f"# {spec['name']} — generated prompt", ""]
    if mode in ("full", "system"):
        role = render(spec["system"], values, fill).strip()
        if role:
            out = ["# " + spec["name"] + " — generated prompt",
                   "", "## Role", role, ""]
    if mode in ("full", "task"):
        context = [f"- **{s['name']}**: {values.get(s['name'], '')}"
                   for s in spec.get("slots", []) if values.get(s.get("name"))]
        unstated = [s["name"] for s in spec.get("slots", [])
                    if not values.get(s.get("name"))]
        if context or unstated:
            out += ["## Context", *context]
            if unstated:
                out.append(f"Unstated: {', '.join(unstated)}. Confirm before "
                           "acting; assume only the target given above.")
            out.append("")
        task = render(spec["task"], values, fill).strip()
        if task:
            out += ["## Task", task, ""]
        rendered = [s for s in (render(step, values, fill).strip()
                                for step in spec.get("methodology", [])) if s]
        if rendered:
            out += ["## Methodology"]
            out += [f"{i}. {s}" for i, s in enumerate(rendered, 1)]
            out.append("")
        out_items = [i for i in (render(item, values, fill).strip()
                                 for item in spec.get("output", [])) if i]
        if out_items:
            out += ["## Output contract"]
            out += [f"- {item}" for item in out_items]
            out.append("")
        checks = []
        for raw in spec.get("checklist", []):
            if "|" in raw:
                probe, item = raw.split("|", 1)
                body = render(item.strip(), values, fill).strip()
                line = f"- [ ] [{probe.strip()}] {body}" if body else ""
            else:
                body = render(raw.strip(), values, fill).strip()
                line = f"- [ ] {body}" if body else ""
            if line:
                checks.append(line)
        if checks:
            out += ["## Quality checklist (self-verify before delivering)"]
            out += checks
            out.append("")
        negatives = [i for i in (render(item, values, fill).strip()
                                 for item in spec.get("negative", [])) if i]
        if negatives:
            out += ["## Do not"]
            out += [f"- {item}" for item in negatives]
            out.append("")
    if mode == "system":
        out = out[:out.index("## Context")] if "## Context" in out else out
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- judge

def judge(spec, text: str) -> dict:
    """structural score: is the field's discipline stated as structure?"""
    items = []
    slines = structural_lines(text)
    vocab = probe_vocab(spec)
    for raw in spec.get("checklist", []):
        if "|" in raw:
            probe, item = raw.split("|", 1)
            probe, item = probe.strip(), item.strip()
        else:
            probe, item = "", raw.strip()
        hit = next((h for l, h in slines if probe_hit(l, probe, vocab)), None)
        items.append({
            "probe": probe, "text": item, "pass": bool(hit),
            "reason": f"stated in section '{hit}'" if hit
                      else "requirement not present as a structured line (bullet/step/heading)",
        })

    _, sections = split_sections(text)
    keys = {}
    for heading, body in sections:
        k = heading_key(heading)
        if k and k not in keys:
            keys[k] = body
    non_empty = lambda body: any(l.strip() for l in (body or []))
    has_steps = any(STEP_RE.match(l) for l in keys.get("methodology", []))

    structure = []
    structure.append({"key": "role", "pass": non_empty(keys.get("role"))})
    structure.append({"key": "task", "pass": non_empty(keys.get("task"))})
    structure.append({"key": "methodology",
                      "pass": non_empty(keys.get("methodology")) and has_steps})
    structure.append({"key": "output", "pass": non_empty(keys.get("output"))})
    structure.append({"key": "checklist", "pass": non_empty(keys.get("checklist"))})
    structure.append({"key": "negatives", "pass": non_empty(keys.get("negatives"))})
    placeholders = PLACEHOLDER_RE.findall(text)
    structure.append({"key": "placeholders", "pass": not placeholders, "detail": placeholders})

    # weights: checklist probes carry substance (3), structural sections are cheap (1)
    # -> structure-only filler (recipe with headings) stays under 50%
    PW, SW = 3, 1
    passed = PW * sum(1 for i in items if i["pass"]) + SW * sum(1 for s in structure if s["pass"])
    total = PW * len(items) + SW * len(structure)
    return {
        "score": int(round(100 * passed / total)) if total else 0,
        "passed": sum(1 for i in items if i["pass"]) + sum(1 for s in structure if s["pass"]),
        "total": len(items) + len(structure),
        "items": items, "structure": structure,
        "placeholders": placeholders,
    }


def print_judge(result, title, as_json=False):
    if as_json:
        print(json.dumps({"result": title, **result}, indent=2))
        return
    print(f"check: {title}")
    print(f"score: {result['score']}% ({result['passed']}/{result['total']})  "
          f"[structural — verify against a real model output]")
    print("checklist:")
    for i in result["items"]:
        mark = "x" if i["pass"] else " "
        print(f"  [{mark}] {i['probe']:<20} {i['reason']}")
    print("structure:")
    for s in result["structure"]:
        mark = "x" if s["pass"] else " "
        detail = ""
        if s["key"] == "placeholders" and s.get("detail"):
            detail = ": " + ", ".join(s["detail"])
        print(f"  [{mark}] {STRUCT_LABELS[s['key']]}{detail}")


# ---------------------------------------------------------------- fix

def spec_section_map(spec, values, fill="…"):
    """canonical target sections from a rendered spec: key -> (heading, [lines])"""
    text = build(spec, values, "full", fill=fill)
    _, sections = split_sections(text)
    out = {}
    for heading, body in sections:
        k = heading_key(heading)
        if k and k not in out:
            out[k] = (heading, body)
    return out


def probe_line_for(spec, values, probe, fill="…"):
    """first structural spec line that anchors this probe"""
    np = normalize(probe)
    target = spec_section_map(spec, values, fill)
    for key in ["methodology", "output", "checklist", "task", "role", "negatives"]:
        if key not in target:
            continue
        heading, body = target[key]
        for line in body:
            if BULLET_RE.match(line) and np in normalize(line):
                return key, line
    item = next((r.split("|", 1)[1].strip() for r in spec.get("checklist", [])
                 if "|" in r and r.split("|", 1)[0].strip() == probe), probe)
    return "checklist", f"- [ ] [{probe}] {render(item, values, fill)}"


def substitute(text: str, values: dict) -> str:
    for k, v in values.items():
        text = text.replace(f"{{{k}}}", str(v)).replace(f"<{k}>", str(v))
    return text


def fix_prompt(spec, text, values):
    """keep the user's wording, graft what's missing, return (fixed, report)"""
    report = {"substituted": [], "sections_added": [], "lines_added": [],
              "unresolved": []}
    for k, v in values.items():
        for token in (f"{{{k}}}", f"<{k}>"):
            if token in text:
                text = text.replace(token, str(v))
                report["substituted"].append(k)

    target = spec_section_map(spec, values, fill="…")
    preamble, sections = split_sections(text)
    keys = {}
    for heading, body in sections:
        k = heading_key(heading)
        if k and k not in keys:
            keys[k] = [heading, body]

    for key in STRUCT_REQUIRED:
        if key in target and key not in keys:
            heading, body = target[key]
            keys[key] = [heading, list(body)]
            report["sections_added"].append(heading)

    for key in STRUCT_REQUIRED:
        if key in keys:
            heading, body = keys[key]
            text_so_far = join_sections([], [(heading, body)])
            for raw in spec.get("checklist", []):
                if "|" not in raw:
                    continue
                probe = raw.split("|", 1)[0].strip()
                np = normalize(probe)
                if any(np in normalize(l) for l, _ in structural_lines(text_so_far)):
                    continue
                gk, gline = probe_line_for(spec, values, probe, fill="…")
                if gk != key:
                    continue
                if not any(normalize(gline) == normalize(l) for l in body):
                    body.append(gline)
                    report["lines_added"].append(gline)

    represented = {id(b) for _, b in keys.values()}
    ordered = [(h, b) for k, (h, b) in keys.items() if k in STRUCT_REQUIRED]
    extras = [(h, b) for h, b in sections if id(b) not in represented]
    fixed = join_sections(preamble, ordered + extras)
    report["unresolved"] = PLACEHOLDER_RE.findall(fixed)
    return fixed, report


# ---------------------------------------------------------------- merge

def section_coverage(probes, lines):
    return sum(1 for p in probes
               if any(p in normalize(l) for l in lines if BULLET_RE.match(l)))


def merge_prompts(spec, text_a, text_b, values):
    probes = [normalize(r.split("|", 1)[0]) for r in spec.get("checklist", [])
              if "|" in r]
    pre_a, sec_a = split_sections(text_a)
    pre_b, sec_b = split_sections(text_b)
    pre = pre_a if judge(spec, text_a)["score"] >= judge(spec, text_b)["score"] else pre_b

    def bucket(sections):
        order, by_key = [], {}
        for heading, body in sections:
            k = heading_key(heading) or f"raw::{normalize(heading)}"
            if k not in by_key:
                by_key[k] = []
                order.append(k)
            by_key[k].append((heading, body))
        return order, by_key

    order_a, bag_a = bucket(sec_a)
    order_b, bag_b = bucket(sec_b)
    order = order_a + [k for k in order_b if k not in order_a]

    merged_sections, took_from = [], {"a": 0, "b": 0}
    for k in order:
        cands = []
        for side, bag in (("a", bag_a), ("b", bag_b)):
            for heading, body in bag.get(k, []):
                cands.append((section_coverage(probes, body), side, heading, body))
        best = max(cands, key=lambda t: (t[0], t[1] == "a"))
        merged_sections.append((best[2], best[3]))
        took_from[best[1]] += 1

    merged = join_sections(pre, merged_sections)
    fixed, rep = fix_prompt(spec, merged, values)
    return fixed, {"took_from": took_from, "fix": rep}


# ---------------------------------------------------------------- ensemble

def _mutation_graft(spec, text, values, rng, failed):
    if not failed:
        return None, text
    probe = rng.choice(failed)
    key, line = probe_line_for(spec, values, probe, fill="…")
    preamble, sections = split_sections(text)
    for i, (heading, body) in enumerate(sections):
        if heading_key(heading) == key:
            if any(normalize(line) == normalize(l) for l in body):
                continue
            body.append(line)
            return f"graft [{probe}] -> '{heading}'", join_sections(preamble, sections)
    # section absent entirely — create it so sectionless prompts can improve
    target = spec_section_map(spec, values, fill="…")
    heading = target[key][0] if key in target else key.title()
    sections.append((heading, [line, ""]))
    return f"graft [{probe}] -> new '{heading}'", join_sections(preamble, sections)


def _mutation_swap(text, rng):
    preamble, sections = split_sections(text)
    cands = [i for i, (h, b) in enumerate(sections)
             if sum(1 for l in b if STEP_RE.match(l)) >= 2
             or sum(1 for l in b if BULLET_RE.match(l)) >= 2]
    if not cands:
        return None, text
    i = rng.choice(cands)
    heading, body = sections[i]
    idx = [j for j, l in enumerate(body) if BULLET_RE.match(l)]
    a, b = rng.sample(idx, 2)
    body[a], body[b] = body[b], body[a]
    return f"reorder lines in '{heading}'", join_sections(preamble, sections)


def _mutation_prune(text, probes, rng):
    preamble, sections = split_sections(text)
    cands = []
    for i, (heading, body) in enumerate(sections):
        for j, line in enumerate(body):
            if BULLET_RE.match(line) and not any(
                    p in normalize(line) for p in probes):
                cands.append((i, j))
    if not cands:
        return None, text
    i, j = rng.choice(cands)
    heading, body = sections[i]
    removed = body.pop(j)
    return f"prune: {removed.strip()[:60]}", join_sections(preamble, sections)


def ensemble(spec, base_text, values, attempts=12, seed=None, topk=3):
    rng = random.Random(seed)
    probes = [normalize(r.split("|", 1)[0]) for r in spec.get("checklist", [])
              if "|" in r]
    seen, log = set(), []
    best_text, best_score = base_text, judge(spec, base_text)["score"]
    candidates = [(best_score, base_text)]
    log.append(f"start score {best_score}%")

    for n in range(1, attempts + 1):
        failed = [i["probe"] for i in judge(spec, best_text)["items"] if not i["pass"]]
        ops = []
        if failed:
            ops.append(_mutation_graft)
        ops += [_mutation_swap, _mutation_prune]
        op = rng.choice(ops)
        if op is _mutation_graft:
            desc, cand = _mutation_graft(spec, best_text, values, rng, failed)
        elif op is _mutation_swap:
            desc, cand = _mutation_swap(best_text, rng)
        else:
            desc, cand = _mutation_prune(best_text, probes, rng)
        if desc is None:
            log.append(f"attempt {n}: no-op")
            continue
        score = judge(spec, cand)["score"]
        log.append(f"attempt {n}: {desc} -> {score}%")
        key = hash(cand)
        if key not in seen:
            seen.add(key)
            candidates.append((score, cand))
        if score > best_score:
            best_text, best_score = cand, score

    ranked, seen_keys = [], set()
    for score, text in sorted(candidates, key=lambda t: -t[0]):
        k = hash(text)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        ranked.append((score, text))
        if len(ranked) >= topk:
            break
    return ranked, log


# ---------------------------------------------------------------- lint

def lint_specs(specs):
    errors, warnings = [], []
    for fid, spec in specs.items():
        f = spec.get("_file", fid)
        for key in ["id", "domain", "name", "system", "task", "slots",
                    "methodology", "output", "checklist", "negative"]:
            if key not in spec:
                errors.append(f"{f}: missing required key '{key}'")
        expect = fid.replace(".", "_") + ".json"
        if spec.get("_file") != expect:
            errors.append(f"{f}: id '{fid}' does not match filename convention ({expect})")
        if len(spec.get("slots", [])) < 1:
            errors.append(f"{f}: needs >=1 slot")
        if len(spec.get("methodology", [])) < 3:
            errors.append(f"{f}: needs >=3 methodology steps (got {len(spec.get('methodology', []))})")
        if len(spec.get("checklist", [])) < 3:
            errors.append(f"{f}: needs >=3 checklist items")
        if len(spec.get("negative", [])) < 1:
            errors.append(f"{f}: needs >=1 negative")
        slot_names = {s["name"] for s in spec.get("slots", [])}
        probes = []
        for i, raw in enumerate(spec.get("checklist", [])):
            if "|" not in raw or not CHECKLIST_RE.match(raw):
                errors.append(f"{f}: checklist[{i}] must be 'probe | item' "
                              f"(lowercase probe): {raw!r}")
            else:
                probe = raw.split("|", 1)[0]
                if probe in probes:
                    errors.append(f"{f}: duplicate probe '{probe}'")
                probes.append(probe)
        templates = ([spec.get("system", ""), spec.get("task", "")]
                     + list(spec.get("methodology", []))
                     + list(spec.get("output", []))
                     + list(spec.get("negative", [])))
        for tpl in templates:
            for m in PLACEHOLDER_RE.finditer(tpl):
                name = m.group(0)[1:-1]
                if name not in slot_names:
                    errors.append(f"{f}: orphan placeholder {m.group(0)} in template")
        if not spec.get("spec_version"):
            warnings.append(f"{f}: no spec_version")
        if not spec.get("last_reviewed"):
            warnings.append(f"{f}: no last_reviewed date")
    return errors, warnings


# ---------------------------------------------------------------- v2 pipeline

def infer_spec_id(specs, text):
    """match '# <spec name> — generated prompt' title line"""
    first = (text or "").splitlines()
    first = first[0] if first else ""
    for sid, spec in specs.items():
        name = spec.get("name", "")
        if name and name in first:
            return sid
    return None


def universal_spec(label="General", slug="general"):
    """field-agnostic build used when no structure matches — any field works"""
    if slug == "general":
        system = ("You are a senior multi-disciplinary practitioner. You work "
                  "from the stated requirements only: you state assumptions "
                  "instead of inventing requirements, deliver exactly what was "
                  "asked with no filler, and verify your own output against "
                  "the request before returning it.")
    else:
        system = (f"You are a senior {label} specialist. You work from the "
                  "stated requirements only: you state assumptions instead of "
                  "inventing requirements, deliver exactly what was asked "
                  "with no filler, and verify your own output against the "
                  "request before returning it.")
    return {
        "id": slug,
        "name": f"{label} Prompt",
        "domain": "general",
        "summary": "field-agnostic build used when no structure matches",
        "spec_version": "2.0",
        "last_reviewed": "2026-10-04",
        "slots": [{"name": "request", "required": True,
                   "hint": "the original request"}],
        "system": system,
        "task": ("Complete the request below. Treat it as the full "
                 "specification: deliver exactly what it asks, in the format "
                 "it implies, at the length it implies.\n\n{request}"),
        "methodology": [
            "Restate the goal, constraints, and required format in one pass "
            "before producing anything.",
            "If the request is ambiguous in a way that changes the outcome, "
            "ask the fewest questions needed; otherwise state assumptions and "
            "proceed.",
            "Produce the deliverable directly — no preamble, no "
            "meta-commentary, no summaries of your own process.",
            "Self-review: check every requirement in the request against "
            "your output and fix gaps before returning.",
        ],
        "output": [
            "the deliverable matches every requirement of the original "
            "request — format, length, scope",
            "assumptions (if any) are stated explicitly with the deliverable",
            "nothing outside the requested scope is added",
        ],
        "checklist": [
            "request|every requirement from the original request is addressed",
            "assumptions|assumptions are stated, not hidden",
            "scope|nothing outside the requested scope is added",
            "format|the output follows the format the request implies",
        ],
        "negative": [
            "Do not ask for permission to start when the request is clear",
            "Do not pad the response with summaries of what you are about "
            "to do",
        ],
    }


def apply_constraints(prompt, constraints):
    """append parsed rules under a Constraints section — explicit text always
    wins over learned edits; returns (prompt, n_applied)"""
    cons, seen = [], set()
    for c in constraints or []:
        t = (c.get("text") or "").strip()
        if not t:
            continue
        key = t.lower().rstrip(".!?")
        if key in seen:
            continue
        seen.add(key)
        if t.lower() in prompt.lower():
            continue
        cons.append(t)
    if not cons:
        return prompt, 0
    lines = prompt.rstrip("\n").split("\n")
    heading = "## Constraints (from your request)"
    bullets = [f"- {t}" for t in cons]

    def insert_before(marker):
        for i, l in enumerate(lines):
            if l.strip() == marker:
                lines[i:i] = [heading, ""] + bullets + [""]
                return True
        return False

    existing = next((i for i, l in enumerate(lines) if l.strip() == heading),
                    None)
    if existing is None:
        if not insert_before(
                "## Quality checklist (self-verify before delivering)") \
                and not insert_before("## Do not"):
            lines += ["", heading] + bullets
    else:
        end = existing + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        lines[end:end] = bullets
    return "\n".join(lines).rstrip("\n") + "\n", len(cons)


def specs_hash(specs=None):
    """sha256 of the canonical spec set (sorted keys, compact JSON) —
    configuration provenance: same input + same specs == same artifact"""
    specs = specs if specs is not None else load_specs()
    blob = json.dumps(specs, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def merge_prefs(spec_id, provided, use_brain, as_of=None):
    """ONE seam for effective slot resolution: learned prefs sit UNDER
    caller-provided slots, explicit always dominates; use_brain=False
    never touches the brain DB. CLI gen, api_gen and compile_prompt
    all resolve through here — no second copy of the merge order."""
    import brain
    if not use_brain:
        return dict(provided)
    return {**brain.slot_prefs(spec_id, as_of=as_of), **dict(provided)}


def brain_meta(use_brain, as_of=None, specs=None):
    """provenance for an artifact: brain state, decay day, spec-set identity.
    metadata only — lives in reports/JSON/stderr, never in the prompt body."""
    import brain
    return {"use_brain": bool(use_brain),
            "as_of": brain.as_of_day(as_of).isoformat(),
            "brain_hash": brain.payload_hash(as_of=as_of,
                                             applied=bool(use_brain)),
            "spec_hash": specs_hash(specs)}


def compile_prompt(text, spec_id=None, explicit=None, *, use_brain,
                   mode="full", specs=None, as_of=None):
    """context parse -> slot merge -> build -> brain apply;
    no template match -> universal build (any field).
    use_brain: keyword-only, NO default — every caller must state it; a
    missing value is a TypeError, not a silent fallback (chokepoint).
    as_of: day-granular decay date (YYYY-MM-DD / date); default = today."""
    import brain
    from context_parser import parse, guess_field
    specs = specs if specs is not None else load_specs()
    ctx = parse(text, specs=specs, spec_id=spec_id)
    chosen = spec_id or ctx["spec_id"]
    warnings = []
    if chosen:
        spec = get_spec(specs, chosen)
        parsed = {k: v.get("value", "")
                  for k, v in ctx.get("slots", {}).items()}
        slots = merge_prefs(spec["id"], {**parsed, **(explicit or {})},
                            use_brain, as_of)
        values, warnings = slot_values(spec, slots, missing_ok=True)
    else:
        slug, label = guess_field(text)
        spec = universal_spec(label, slug)
        request = " ".join((text or "").split())
        if len(request) > 1400:
            request = request[:1400].rstrip() + " …"
        values = {"request": request}
        values.update(explicit or {})
    prompt = build(spec, values, mode)
    applied = {"suppressed": [], "added": [], "considered": 0}
    if use_brain:
        prompt, applied = brain.apply(spec["id"], prompt, as_of=as_of)
    prompt, n_cons = apply_constraints(prompt, ctx.get("constraints"))
    score = judge(spec, prompt)["score"]
    nm = spec.get("name") or spec["id"]
    label = nm[:-7] if nm.endswith(" Prompt") else nm
    report = {
        "schema": 1,
        "spec_id": spec["id"], "field": label,
        "confidence": ctx["confidence"],
        "candidates": ctx["candidates"], "inferred": ctx["slots"],
        "constraints": ctx["constraints"], "constraints_applied": n_cons,
        "facts": ctx["facts"],
        "learned": applied, "warnings": warnings, "mode": mode,
        "learned_on": bool(use_brain),
        **brain_meta(use_brain, as_of, specs),
    }
    return {"prompt": prompt, "values": values, "score": score, "report": report}


# ---------------------------------------------------------------- commands

def cmd_list(specs, domain=None):
    for spec in specs.values():
        if domain and spec.get("domain") != domain:
            continue
        print(f"{spec['id']:<20} {spec.get('domain', '?'):<7} {spec['name']}")


def cmd_show(spec):
    print(f"{spec['id']} — {spec['name']}")
    print(f"domain: {spec.get('domain')}  version: {spec.get('spec_version', 'n/a')}"
          f"  reviewed: {spec.get('last_reviewed', 'n/a')}")
    print(f"summary: {spec.get('summary', '')}")
    print("\nslots:")
    for s in spec.get("slots", []):
        req = "required" if s.get("required") else "optional"
        default = f"  default={s['default']}" if s.get("default") is not None else ""
        print(f"  {s['name']:<18} {req:<9}{default}")
        if s.get("hint"):
            print(f"    hint: {s['hint']}")
    if spec.get("methodology"):
        print("\nmethodology:")
        for i, step in enumerate(spec["methodology"], 1):
            print(f"  {i}. {step}")


def parse_set(pairs):
    values = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"-s expects key=value, got: {pair}")
        k, _, v = pair.partition("=")
        values[k.strip()] = v.strip()
    return values


def gen_payload(spec, values, mode, prompt):
    return {"schema": 1, "field": spec["id"],
            "spec_version": spec.get("spec_version"),
            "mode": mode, "slots": values, "prompt": prompt}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(
        prog="forge",
        description="Field-structured prompt generator, checker, fixer. Offline, free.")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("list", help="list available fields")
    sp.add_argument("--domain", choices=["cyber", "aiml"])

    sp = sub.add_parser("show", help="show a field's slots and methodology")
    sp.add_argument("field")

    sp = sub.add_parser("gen", help="generate a field-specific prompt")
    sp.add_argument("field")
    sp.add_argument("-s", "--set", action="append", metavar="KEY=VALUE")
    sp.add_argument("--mode", choices=["full", "system", "task"], default="full")
    sp.add_argument("-o", "--out")
    sp.add_argument("--strict", action="store_true",
                    help="fail if any required slot is empty/placeholder")
    sp.add_argument("--yes", action="store_true",
                    help="non-interactive: placeholders for missing slots")
    sp.add_argument("--no-brain", action="store_true",
                    help="ignore learned slot preferences (deterministic gen)")
    sp.add_argument("--as-of", metavar="YYYY-MM-DD", default=None,
                    help="decay date for brain hash/prefs (default: today)")
    sp.add_argument("--format", choices=["text", "json"], default="text")

    sp = sub.add_parser("check", help="score a prompt against a field's checklist")
    sp.add_argument("field")
    sp.add_argument("file", help="prompt file, or - for stdin")
    sp.add_argument("--format", choices=["text", "json"], default="text")

    sp = sub.add_parser("fix", help="patch gaps: graft missing structure, keep your wording")
    sp.add_argument("field")
    sp.add_argument("file", help="prompt file, or - for stdin")
    sp.add_argument("-s", "--set", action="append", metavar="KEY=VALUE")
    sp.add_argument("-o", "--out", help="write fixed prompt here (report goes to stdout)")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.add_argument("--yes", action="store_true")

    sp = sub.add_parser("compare", help="score two prompts side by side")
    sp.add_argument("field")
    sp.add_argument("file_a")
    sp.add_argument("file_b")
    sp.add_argument("--format", choices=["text", "json"], default="text")

    sp = sub.add_parser("merge", help="union the strengths of two prompts")
    sp.add_argument("field")
    sp.add_argument("file_a")
    sp.add_argument("file_b")
    sp.add_argument("-s", "--set", action="append", metavar="KEY=VALUE")
    sp.add_argument("-o", "--out")
    sp.add_argument("--format", choices=["text", "json"], default="text")

    sp = sub.add_parser("ensemble", help="mutate + score search; returns top candidates")
    sp.add_argument("field")
    sp.add_argument("file", help="base prompt, or - for stdin")
    sp.add_argument("-s", "--set", action="append", metavar="KEY=VALUE")
    sp.add_argument("--attempts", type=int, default=12)
    sp.add_argument("--seed", type=int, default=None)
    sp.add_argument("--top", type=int, default=3)
    sp.add_argument("-o", "--out", help="write best candidate here")
    sp.add_argument("--format", choices=["text", "json"], default="text")

    sp = sub.add_parser("lint", help="validate all field specs")
    sp.add_argument("--stale-days", type=int, default=None,
                    help="warn when last_reviewed is older than N days")

    sp = sub.add_parser("new", help="scaffold a new field spec")
    sp.add_argument("domain", choices=["cyber", "aiml"])
    sp.add_argument("field_id")

    sp = sub.add_parser("parse", help="read raw chat text: template, slots, constraints")
    sp.add_argument("file", help="chat text file, or - for stdin")
    sp.add_argument("--spec", help="force template id instead of auto-pick")
    sp.add_argument("--format", choices=["text", "json"], default="text")

    sp = sub.add_parser("compile", help="parse text -> finished prompt with learned prefs")
    sp.add_argument("file", help="chat text file, or - for stdin")
    sp.add_argument("--spec", help="force template id instead of auto-pick")
    sp.add_argument("-s", "--set", action="append", metavar="KEY=VALUE")
    sp.add_argument("--mode", choices=["full", "system", "task"], default="full")
    sp.add_argument("--no-brain", "--no-learned", action="store_true",
                    dest="no_brain",
                    help="ignore learned brain state for this compile "
                         "(fully deterministic; --no-learned kept as alias)")
    sp.add_argument("--as-of", metavar="YYYY-MM-DD", default=None,
                    help="decay date for brain apply/hash (default: today)")
    sp.add_argument("-o", "--out")
    sp.add_argument("--format", choices=["text", "json"], default="text")

    sp = sub.add_parser("learn", help="diff generated vs corrected prompt and learn")
    sp.add_argument("generated", help="what PromptForge produced")
    sp.add_argument("corrected", help="your edited version")
    sp.add_argument("--spec", help="template id (auto-inferred from title if omitted)")
    sp.add_argument("--format", choices=["text", "json"], default="text")

    sp = sub.add_parser("brain", help="show/forget/reset/export learned patterns")
    sp.add_argument("action", nargs="?", default="show",
                    choices=["show", "status", "forget", "reset", "export"])
    sp.add_argument("pattern_id", nargs="?", help="pattern id (for forget)")
    sp.add_argument("--spec", help="filter by template id")
    sp.add_argument("--yes", action="store_true", help="confirm reset")
    sp.add_argument("-o", "--out", help="export target file")
    sp.add_argument("--format", choices=["text", "json"], default="text")

    args = p.parse_args(argv)
    fmt_json = getattr(args, "format", "text") == "json"

    try:
        if args.command == "new":
            path = SPEC_DIR / f"{args.field_id.replace('.', '_')}.json"
            if path.exists():
                raise FileExistsError(f"{path.name} already exists")
            scaffold = {
                "id": args.field_id, "domain": args.domain, "name": "TODO name",
                "summary": "TODO one line", "spec_version": "1.0",
                "last_reviewed": "2026-10-03", "target_models": ["any"],
                "slots": [{"name": "target", "label": "Target", "required": True,
                           "default": None, "hint": "what this prompt operates on"}],
                "system": f"You are a senior {args.domain} specialist.",
                "task": "Work on: {target}",
                "methodology": ["Step one on {target}", "Step two on {target}",
                                "Step three on {target}"],
                "output": ["Structured result for {target}"],
                "checklist": ["target | result names {target} explicitly"],
                "negative": ["Do not invent facts"],
            }
            path.write_text(json.dumps(scaffold, indent=2) + "\n", encoding="utf-8")
            print(f"[+] scaffolded {path.name} — fill the TODOs, then: forge lint")
            return EXIT_OK

        specs = load_specs(fail_fast=(args.command == "lint"))

        if args.command == "lint":
            errors, warnings = lint_specs(specs)
            if args.stale_days is not None:
                from datetime import date
                cutoff_days = args.stale_days
                for fid, spec in specs.items():
                    d = spec.get("last_reviewed")
                    if d:
                        try:
                            age = (date.today() - date.fromisoformat(d)).days
                            if age > cutoff_days:
                                warnings.append(
                                    f"{spec.get('_file', fid)}: last_reviewed {d} "
                                    f"is {age} days old (> {cutoff_days})")
                        except ValueError:
                            warnings.append(f"{fid}: bad last_reviewed date {d!r}")
            for w in warnings:
                print(f"[warn] {w}")
            for e in errors:
                print(f"[FAIL] {e}")
            print(f"lint: {len(specs)} specs, {len(errors)} errors, {len(warnings)} warnings")
            return EXIT_VALIDATION if errors else EXIT_OK

        if args.command == "list":
            cmd_list(specs, args.domain)
            return EXIT_OK

        if args.command == "parse":
            import context_parser
            ctx = context_parser.parse(read_file(args.file), specs=specs,
                                       spec_id=args.spec)
            if fmt_json:
                print(json.dumps(ctx, indent=2, ensure_ascii=False))
                return EXIT_OK
            print(f"parse: {ctx['chars']} chars")
            if ctx["spec_id"]:
                print(f"template: {ctx['spec_id']}  "
                      f"(confidence {ctx['confidence']})")
            else:
                print(f"template: none detected — field: {ctx['field']} "
                      f"(universal build)")
            if ctx["candidates"]:
                print("candidates: " + ", ".join(
                    f"{c['id']} ({c['score']})" for c in ctx["candidates"]))
            if ctx["slots"]:
                print("slots detected:")
                for name, s in ctx["slots"].items():
                    print(f"  {name:<16} = {s['value']}  [{s['how']}]")
            if ctx["constraints"]:
                neg = sum(1 for c in ctx["constraints"]
                          if c["polarity"] == "negative")
                print(f"constraints: {len(ctx['constraints'])} "
                      f"({neg} negative)")
            if ctx["facts"]:
                print("facts: " + ", ".join(
                    f"{f['kind']}={f['value']}" for f in ctx["facts"]))
            return EXIT_OK

        if args.command == "compile":
            text = read_file(args.file)
            provided = parse_set(getattr(args, "set", None))
            res = compile_prompt(text, spec_id=args.spec, explicit=provided,
                                 use_brain=not args.no_brain, mode=args.mode,
                                 specs=specs, as_of=args.as_of)
            rep = res["report"]
            print(f"brain: hash={rep['brain_hash']} as_of={rep['as_of']} "
                  f"use_brain={rep['use_brain']}", file=sys.stderr)
            if fmt_json:
                print(json.dumps({"field": rep["spec_id"],
                                  "score": res["score"], "prompt": res["prompt"],
                                  "values": res["values"], "report": rep},
                                 indent=2, ensure_ascii=False))
            else:
                print(f"compile: {rep['spec_id']} · {rep['field']}  "
                      f"(confidence {rep['confidence']})")
                print(f"score: {res['score']}%  "
                      f"[structural — verify against a real model output]")
                for name, s in rep["inferred"].items():
                    print(f"  inferred {name} = {s['value']}  [{s['how']}]")
                for c in rep["constraints"][:5]:
                    print(f"  {c['polarity']:<9} {c['text'][:70]}")
                for line in rep["learned"]["suppressed"]:
                    print(f"  [learned] suppressed: {line[:70]}")
                for line in rep["learned"]["added"]:
                    print(f"  [learned] added: {line[:70]}")
                for w in rep["warnings"]:
                    print(f"[warn] {w}")
            if args.out:
                Path(args.out).write_text(res["prompt"], encoding="utf-8")
                print(f"[+] wrote {args.out} ({len(res['prompt'])} bytes)")
            elif not fmt_json:
                print("\n--- prompt ---")
                print(res["prompt"])
            return EXIT_OK

        if args.command == "learn":
            import brain
            generated = read_file(args.generated)
            corrected = read_file(args.corrected)
            sid = args.spec or infer_spec_id(specs, generated)
            if not sid:
                raise KeyError("couldn't infer the template from the title line "
                               "— pass --spec <id>")
            get_spec(specs, sid)
            summary = brain.learn_from_diff(sid, generated, corrected)
            if fmt_json:
                print(json.dumps(summary, indent=2, ensure_ascii=False))
            else:
                if not summary["learned"]:
                    print("brain: nothing learned — no meaningful changes detected")
                for item in summary["learned"]:
                    label = {"boilerplate_removed": "you removed (will suppress)",
                             "constraint_added": "you added (will apply)",
                             "slot_override": "you changed (preference)"}[item["kind"]]
                    print(f"  {label}: {item['sample'][:90]}")
                if summary["ignored"]:
                    print(f"  ignored {summary['ignored']} unrelated change(s)")
                print(f"brain: {len(summary['learned'])} pattern(s) recorded "
                      f"for {sid} — status: forge brain")
            return EXIT_OK

        if args.command == "brain":
            import brain
            if args.action == "reset":
                if not args.yes:
                    print("[!] reset deletes every learned pattern — "
                          "confirm with --yes", file=sys.stderr)
                    return EXIT_INPUT
                n = brain.reset()
                print(f"[+] brain reset — {n} pattern(s) deleted")
                return EXIT_OK
            if args.action == "forget":
                if not args.pattern_id:
                    print("[!] forget needs a pattern id: forge brain show",
                          file=sys.stderr)
                    return EXIT_INPUT
                n = brain.forget(args.pattern_id)
                if not n:
                    raise KeyError(f"no pattern with id {args.pattern_id}")
                print(f"[+] forgot pattern {args.pattern_id}")
                return EXIT_OK
            if args.action == "export":
                data = brain.export_json()
                if args.out:
                    Path(args.out).write_text(data, encoding="utf-8")
                    print(f"[+] exported {args.out} "
                          f"({len(data)} bytes)")
                else:
                    print(data)
                return EXIT_OK
            st = brain.status()
            patterns = brain.suggest(args.spec)
            if fmt_json:
                print(json.dumps({"status": st, "patterns": patterns},
                                 indent=2, ensure_ascii=False))
                return EXIT_OK
            print(f"brain: {st['patterns']} pattern(s), "
                  f"{st['auto_active']} auto-applying "
                  f"(threshold {st['auto_threshold']}, "
                  f"half-life {st['half_life_days']}d)")
            if st["by_kind"]:
                for kind, info in sorted(st["by_kind"].items()):
                    print(f"  {kind:<22} {info['patterns']} patterns, "
                          f"{info['hits']} events")
            if not patterns:
                print("  (nothing learned yet — edit a compiled prompt and "
                      "run: forge learn generated.txt corrected.txt)")
                return EXIT_OK
            print(f"{'id':<5} {'status':<9} {'x':<4} {'kind':<21} sample")
            for p in patterns:
                print(f"{p['id']:<5} {p['status']:<9} {p['count']:<4} "
                      f"{p['kind']:<21} {p['sample'][:64]}")
            return EXIT_OK

        spec = get_spec(specs, args.field)
        provided = parse_set(getattr(args, "set", None))
        unknown = [k for k in provided
                   if k not in {s["name"] for s in spec.get("slots", [])}]
        if unknown:
            print(f"[warn] ignoring unknown slot(s): {', '.join(sorted(unknown))}",
                  file=sys.stderr)

        if args.command == "show":
            cmd_show(spec)
            return EXIT_OK

        if args.command == "gen":
            interactive = sys.stdin.isatty() and not args.yes
            slots = merge_prefs(spec["id"], provided,
                                not args.no_brain, args.as_of)
            values, _ = slot_values(spec, slots, interactive=interactive,
                                    assume_yes=args.yes)
            if args.strict:
                bad = [s["name"] for s in spec.get("slots", [])
                       if s.get("required")
                       and (not str(values.get(s["name"], "")).strip()
                            or PLACEHOLDER_RE.fullmatch(str(values.get(s["name"], ""))))]
                if bad:
                    print(f"[!] strict: required slots unresolved: {', '.join(bad)}",
                          file=sys.stderr)
                    return EXIT_INPUT
            prompt = build(spec, values, args.mode)
            meta = brain_meta(not args.no_brain, args.as_of, specs)
            if fmt_json:
                payload = gen_payload(spec, values, args.mode, prompt)
                payload.update(meta)
                print(json.dumps(payload, indent=2))
                if args.out:
                    Path(args.out).write_text(prompt, encoding="utf-8")
                    print(f"[+] wrote {args.out} ({len(prompt)} bytes)", file=sys.stderr)
            elif args.out:
                Path(args.out).write_text(prompt, encoding="utf-8")
                print(f"[+] wrote {args.out} ({len(prompt)} bytes)")
            else:
                print(prompt)
            print(f"brain: hash={meta['brain_hash']} as_of={meta['as_of']} "
                  f"use_brain={meta['use_brain']}", file=sys.stderr)
            return EXIT_OK

        if args.command == "check":
            text = read_file(args.file)
            result = judge(spec, text)
            print_judge(result, f"{spec['id']} / {args.file}", as_json=fmt_json)
            return EXIT_OK

        if args.command == "fix":
            text = read_file(args.file)
            values, warnings = slot_values(spec, provided, missing_ok=True)
            before = judge(spec, text)
            fixed, rep = fix_prompt(spec, text, values)
            after = judge(spec, fixed)
            diff = "\n".join(difflib.unified_diff(
                text.splitlines(), fixed.splitlines(),
                fromfile=args.file, tofile="fixed", lineterm=""))
            if fmt_json:
                print(json.dumps({"field": spec["id"], "before": before["score"],
                                  "after": after["score"], "report": rep,
                                  "diff": diff, "prompt": fixed}, indent=2))
                if args.out:
                    Path(args.out).write_text(fixed, encoding="utf-8")
                    print(f"[+] wrote fixed prompt to {args.out}", file=sys.stderr)
                return EXIT_OK
            print(f"fix: {spec['id']} / {args.file}")
            for w in warnings:
                print(f"[warn] {w}")
            print(f"score: {before['score']}% -> {after['score']}%  "
                  f"[structural — verify against a real model output]")
            if rep["substituted"]:
                print(f"substituted slots: {', '.join(sorted(set(rep['substituted'])))}")
            if rep["sections_added"]:
                print(f"sections grafted: {', '.join(rep['sections_added'])}")
            if rep["lines_added"]:
                print(f"lines grafted ({len(rep['lines_added'])}):")
                for l in rep["lines_added"]:
                    print(f"  + {l.strip()}")
            if rep["unresolved"]:
                print(f"[warn] unresolved placeholders remain: "
                      f"{', '.join(rep['unresolved'])} — pass -s key=value")
            print("\n--- diff ---")
            print(diff if diff else "(no changes)")
            if args.out:
                Path(args.out).write_text(fixed, encoding="utf-8")
                print(f"[+] wrote fixed prompt to {args.out}")
            else:
                print("\n--- fixed prompt ---")
                print(fixed)
            return EXIT_OK

        if args.command == "compare":
            ta, tb = read_file(args.file_a), read_file(args.file_b)
            ra, rb = judge(spec, ta), judge(spec, tb)
            if fmt_json:
                print(json.dumps({"field": spec["id"], "a": ra, "b": rb}, indent=2))
                return EXIT_OK
            print(f"compare: {spec['id']}")
            print(f"{'':<24} A ({args.file_a})   B ({args.file_b})")
            print(f"{'score':<24} {ra['score']:>6}%       {rb['score']:>6}%")
            print("checklist:")
            for ia, ib in zip(ra["items"], rb["items"]):
                ma, mb = ("x" if ia["pass"] else " "), ("x" if ib["pass"] else " ")
                win = " A" if ia["pass"] and not ib["pass"] else \
                      " B" if ib["pass"] and not ia["pass"] else ""
                print(f"  [{ma}] / [{mb}] {ia['probe']:<18}{win}")
            print("structure:")
            for sa, sb in zip(ra["structure"], rb["structure"]):
                ma = "x" if sa["pass"] else " "
                mb = "x" if sb["pass"] else " "
                print(f"  [{ma}] / [{mb}] {STRUCT_LABELS[sa['key']]}")
            winner = ("A" if ra["score"] > rb["score"]
                      else "B" if rb["score"] > ra["score"] else "tie")
            print(f"winner: {winner}  "
                  f"[merge them to union strengths: forge merge {spec['id']} a b]")
            return EXIT_OK

        if args.command == "merge":
            ta, tb = read_file(args.file_a), read_file(args.file_b)
            values, warnings = slot_values(spec, provided, missing_ok=True)
            merged, meta = merge_prompts(spec, ta, tb, values)
            ra, rb, rm = judge(spec, ta), judge(spec, tb), judge(spec, merged)
            if fmt_json:
                print(json.dumps({"field": spec["id"], "a": ra["score"],
                                  "b": rb["score"], "merged": rm["score"],
                                  "meta": meta, "prompt": merged}, indent=2))
                if args.out:
                    Path(args.out).write_text(merged, encoding="utf-8")
                    print(f"[+] wrote {args.out}", file=sys.stderr)
                return EXIT_OK
            print(f"merge: {spec['id']}")
            for w in warnings:
                print(f"[warn] {w}")
            print(f"A {ra['score']}%  B {rb['score']}%  ->  merged {rm['score']}%  "
                  f"[structural — verify against a real model output]")
            print(f"sections taken: A {meta['took_from']['a']}, B {meta['took_from']['b']}")
            if args.out:
                Path(args.out).write_text(merged, encoding="utf-8")
                print(f"[+] wrote {args.out}")
            else:
                print()
                print(merged)
            return EXIT_OK

        if args.command == "ensemble":
            if args.top < 1:
                print("[!] --top must be >= 1", file=sys.stderr)
                return EXIT_INPUT
            text = read_file(args.file)
            values, warnings = slot_values(spec, provided, missing_ok=True)
            ranked, log = ensemble(spec, text, values, attempts=args.attempts,
                                   seed=args.seed, topk=args.top)
            if fmt_json:
                print(json.dumps({"field": spec["id"], "log": log,
                                  "top": [{"score": s, "prompt": t}
                                          for s, t in ranked]}, indent=2))
                if args.out:
                    Path(args.out).write_text(ranked[0][1], encoding="utf-8")
                    print(f"[+] wrote best candidate to {args.out}", file=sys.stderr)
                return EXIT_OK
            print(f"ensemble: {spec['id']}  attempts={args.attempts}")
            for w in warnings:
                print(f"[warn] {w}")
            for line in log:
                print(f"  {line}")
            print("top candidates:")
            for i, (score, _) in enumerate(ranked, 1):
                print(f"  #{i}: {score}%")
            print("[scores are structural — paste the winner into a real model "
                  "and read the answer before trusting it]")
            if args.out:
                Path(args.out).write_text(ranked[0][1], encoding="utf-8")
                print(f"[+] wrote best candidate to {args.out}")
            else:
                print(f"\n--- best candidate ({ranked[0][0]}%) ---")
                print(ranked[0][1])
            return EXIT_OK

    except MissingSlot as exc:
        print(f"[!] missing required slot '{exc.name}' — pass -s {exc.name}=... "
              f"(or --yes for placeholders)", file=sys.stderr)
        return EXIT_INPUT
    except KeyError as exc:
        print(f"[!] {exc.args[0]}", file=sys.stderr)
        return EXIT_VALIDATION
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return EXIT_VALIDATION

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
