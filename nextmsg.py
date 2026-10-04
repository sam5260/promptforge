#!/usr/bin/env python3
"""nextmsg.py — PromptForge next-message engine.

Transcript -> State -> Next Action -> Next Message.

Hard product invariant (security property, not a metric):
    Infer action. Never invent decisions.
If the transcript lacks evidence for a decision, the output says
"your call" and never picks. PASS_BAR["invented_max"] == 0 is an
absolute blocker even when everything else scores well.

Offline and rule-based: no network, no model, stdlib only.
Limitations (documented): strong on "fix the error", "answer the
question", "continue the plan"; weak on strategy and "is this plan
good" — see README.

CLI:
    python nextmsg.py transcript.txt        # text card
    python nextmsg.py - < transcript.txt
    python nextmsg.py transcript.txt --json
    python nextmsg.py --score fixtures      # fixture eval vs PASSBAR.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PASS_BAR = {
    "intent_min": 7,          # of 10 fixtures: state == expected_state
    "constraints_min": 10,    # of 10: every expected clause carried
    "invented_max": 0,        # absolute blocker
    "beat_continue": True,    # intent count must exceed Continue. baseline
}

# report-only quality measures (never gate PASS): engine message vs the
# message actually sent. >EDIT_WORDS_MAX word edits = an edit-miss;
# "Continue." within the threshold = the tool added nothing that fixture.
EDIT_WORDS_MAX = 3

STATES = ("unresolved_error", "unverified_work", "open_question",
          "stated_next_step", "big_new_task", "continue")

# ----------------------------------------------------------------- splitting

ROLE_RE = re.compile(
    r"^\s*\[?(user|you|human|assistant|ai|claude|gpt|model)\]?\s*:\s*(.*)$",
    re.I)
_ROLE_MAP = {"you": "user", "human": "user", "ai": "assistant",
             "claude": "assistant", "gpt": "assistant", "model": "assistant"}


def split_turns(text: str) -> list[dict]:
    """role-marker splitter; ``` fences are opaque (colons inside code
    never start a turn). No markers at all -> one user turn."""
    turns: list[dict] = []
    in_fence = False
    role: str | None = None
    lines: list[str] = []

    def flush():
        nonlocal role, lines
        if role is not None:
            body = "\n".join(lines).strip()
            if body:
                turns.append({"role": role, "text": body})
        role, lines = None, []

    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            if role is not None:
                lines.append(line)
            continue
        m = None if in_fence else ROLE_RE.match(line)
        if m:
            flush()
            role = _ROLE_MAP.get(m.group(1).lower(), m.group(1).lower())
            if m.group(2):
                lines.append(m.group(2))
        else:
            if role is None:
                role = "user"
            lines.append(line)
    flush()
    if not turns:
        stripped = text.strip()
        if stripped:
            turns.append({"role": "user", "text": stripped})
    for i, t in enumerate(turns, 1):
        t["index"] = i
    return turns


# ------------------------------------------------------------------ signals

ERROR_RE = re.compile(
    r"Traceback \(most recent call last\)|ModuleNotFoundError|ImportError|"
    r"SyntaxError|NameError|KeyError|ValueError|TypeError|RuntimeError|"
    r"AssertionError|IndexError|AttributeError|"
    r"\berror:\s|\bfailed:|\bfailing\b|\bfailure\b|\bfails?\b|"
    r"\bcrash(?:ed|es|ing)?\b|\bstill failing\b|\bbroke again\b|"
    r"\bflaky\b|\bdoesn'?t work\b|\bnot working\b", re.I)

RESOLVE_RE = re.compile(
    r"\bworks? now\b|\bis working\b|\bfixed\b|\bpassing\b|\bgreen\b|"
    r"\bresolved\b|\bconfirmed\b|\bno longer (?:failing|broken)|"
    r"\b(?:all\s+)?(?:checks?|tests?) pass", re.I)

CLAIM_RE = re.compile(
    r"\b(?:added|updated|changed|fixed|implemented|created|wrote|"
    r"refactored|migrated|renamed|set up|pushed|applied|integrated|"
    r"tidied|moved|rewrote|completed|done)\b", re.I)

VERIFY_RE = re.compile(
    r"\btests? pass(?:ing|ed)?\b|\bpassing\b|\bgreen\b|\bverified\b|"
    r"\bconfirmed\b|\bchecked\b|\bworks? now\b|\bno errors\b|"
    r"\bruns clean\b|\bvalid(?:ated)?\b", re.I)

PENDING_RE = re.compile(
    r"\bqueued\b|\bready\b|\bwaiting\b|\bsay the word\b|"
    r"\bwhen you(?:'re| are) ready\b|\bon your go\b|\bstandby\b", re.I)

BIG_RE = re.compile(
    r"\bnew topic\b|\bdifferent project\b|\bseparate (?:project|topic|task)\b|"
    r"\bnew task\s*:|\bfresh (?:project|start) here\b", re.I)

CONTINUE_RE = re.compile(r"^\s*(?:please\s+)?continue\b|^\s*keep (?:it )?going\b",
                         re.I)

STEP_RE = re.compile(
    r"\bnext\s*:|"
    r"\bnext\s*[,:]?\s*(?:i(?:['’]ll| will)|we(?:['’]ll| will))\b|"
    r"\bthen\b|\bafter that\b|\bstep\s*\d+\b|\blet'?s\s+"
    r"(?:do|go with|use|run|start|move|switch)\b|\border of operations\b",
    re.I)

CONSTR_RE = re.compile(
    r"\b(?:don'?t|do not|never|must not|without|only|keep|make sure|avoid|"
    r"no)\b", re.I)

ASK_VERBS = ("run", "fix", "add", "check", "test", "verify", "show", "give",
             "paste", "send", "update", "write", "migrate", "remove",
             "rename", "review", "diff", "deploy", "restart", "delete",
             "create", "fetch", "install", "wire", "audit", "make sure")

GENERIC_ACTION_RE = re.compile(
    r"\b(?:updated|changed|modified|renamed|edited|moved|added|removed|"
    r"deleted|touched|refactored|rewrote|wrote|applied|tidied|switched)\b",
    re.I)

NEG_CLAUSE_RE = re.compile(
    r"(?:don'?t|do not|never|must not|without)\s+(?:also\s+)?(\w+)\s*(.*)",
    re.I)

_WEAK = {"the", "and", "for", "with", "that", "this", "when", "then", "your",
         "have", "will", "into", "from", "please", "want", "need", "make",
         "sure", "show", "give", "done", "please", "when", "youre", "it",
         "should", "would", "also", "just", "any", "all", "via", "per",
         "before", "after", "pasting", "continue"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9_./-]+", _norm(s))


def _stem5(w: str) -> str:
    """suffix-strip then prefix-5: caching->cach, cache->cache (prefix
    match covers both), inventory->inven."""
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[: -len(suf)]
            break
    return w[:5]


def _content(s: str) -> list[str]:
    """distinctive content tokens: len>=4, not weak, stemmed."""
    out = []
    for w in _words(s):
        if len(w) >= 4 and w not in _WEAK:
            out.append(_stem5(w))
    return out


def _sentences(text: str) -> list[str]:
    parts = re.split(r",\s+and\s+|(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------- constraint

def extract_constraints(turns: list[dict]) -> list[dict]:
    """rule clauses from user turns; 'And ' stripped; citable turn kept."""
    out = []
    for t in turns:
        if t["role"] != "user":
            continue
        for sent in _sentences(t["text"]):
            s = re.sub(r"^(?:and|also|plus)\s+", "", sent.strip(), flags=re.I)
            if CONSTR_RE.search(s) and len(s) > 8:
                out.append({"text": s.rstrip(" ."), "turn": t["index"]})
    return out


# ---------------------------------------------------------------- decisions

_CHOICE_DO_RE = re.compile(
    r"(?:actually\s+)?let'?s\s+(?:do|go with|use|take|pick|switch to)\s+"
    r"([A-Za-z][\w\-\.]{1,28})", re.I)


def _posing_sentence(turns: list[dict]) -> tuple[int, str] | None:
    """last question-sentence anywhere — the decision, if one was posed."""
    best = None
    for t in turns:
        for sent in _sentences(t["text"]):
            if sent.endswith("?") and len(sent) > 12:
                best = (t["index"], sent)
    return best


def _options_from(posing: str, turns: list[dict], posing_turn: int
                  ) -> list[str]:
    if " or " in posing.lower():
        bits = re.split(r",?\s+or\s+", posing, flags=re.I)
        opts = [_clean_opt(b) for b in bits[:2]]
        return [o for o in opts if o]
    # fall back: " or " in a neighbouring sentence of the same turn
    for t in turns:
        if t["index"] != posing_turn:
            continue
        for sent in _sentences(t["text"]):
            if " or " in sent.lower() and sent != posing:
                bits = re.split(r",?\s+or\s+", sent, flags=re.I)
                opts = [_clean_opt(b) for b in bits[:2]]
                opts = [o for o in opts if o]
                if opts:
                    return opts
    # last resort: proper nouns in the posing sentence + nearby offers
    opts = re.findall(r"\b([A-Z][a-zA-Z]{2,})\b", posing)
    return [o for o in opts if o not in ("Should", "Which", "What", "The")]


def _clean_opt(bit: str) -> str:
    b = bit.strip()
    b = re.sub(r"^(?:(?:two|three|both|a few|several|other)\s+)?"
               r"options?\s*[:：]\s*", "", b, flags=re.I)
    b = re.sub(r"^option\s+[a-z0-9]{1,2}\s*[:.)\-]\s*", "", b, flags=re.I)
    b = re.sub(r"^\d{1,2}\s*[.)\-]\s*", "", b)
    b = re.sub(r"^(?:should\s+i|would\s+you|do\s+you\s+want(?:\s+to)?|"
               r"which|or)\s+", "", b, flags=re.I)
    b = re.split(r"\.\s+(?=(?:which|what|should|do you|would|is|are|can|"
                 r"could)\b)", b, maxsplit=1, flags=re.I)[0]
    b = re.sub(r"[?,]+$", "", b)
    b = re.sub(r"\s+(?:instead|rather|then|about)\.?$", "", b, flags=re.I)
    b = b.split(",")[0].strip()
    b = re.sub(r"[.\s]+$", "", b)
    return b[:60]


def extract_decision(turns: list[dict]) -> dict:
    """posed question -> options; resolution = LAST user turn that names an
    option or says let's-do (recency: later 'actually B' beats earlier A)."""
    posed = _posing_sentence(turns)
    if not posed:
        return {"available": False, "posing_turn": None, "options": [],
                "resolved_by_turn": None, "choice": None}
    pturn, psent = posed
    options = _options_from(psent, turns, pturn)
    resolved_by, choice = None, None
    opt_tokens = {o.lower() for o in options}
    for t in turns:
        if t["role"] != "user":
            continue
        m = _CHOICE_DO_RE.search(t["text"])
        if m:
            resolved_by, choice = t["index"], m.group(1).strip(" .,-")
            continue
        tl = _norm(t["text"])
        for tok in opt_tokens:
            if len(tok) >= 3 and re.search(rf"\b{re.escape(tok)}\b", tl):
                resolved_by, choice = t["index"], options[
                    [o.lower() for o in options].index(tok)]
                break
    return {"available": True, "posing_turn": pturn, "options": options,
            "resolved_by_turn": resolved_by, "choice": choice}


# -------------------------------------------------------------- violations

def _last_user(turns: list[dict]) -> dict | None:
    for t in reversed(turns):
        if t["role"] == "user":
            return t
    return None


def _last_assistant(turns: list[dict]) -> dict | None:
    for t in reversed(turns):
        if t["role"] == "assistant":
            return t
    return None


def _ask_sentences(text: str) -> list[str]:
    out = []
    for sent in _sentences(text):
        s = re.sub(r"^(?:and|please|next\s*:)\s+", "", sent.strip(), flags=re.I)
        if not s:
            continue
        first = _words(s[:24])
        if first and first[0] in ASK_VERBS:
            out.append(s)
        elif "make sure" in _norm(s) and not NEG_CLAUSE_RE.search(s):
            # negated make-sure ("make sure you don't touch X") is a rule
            # clause — it lands in constraints/broke_rule, never in the
            # ask lane (double-counting it made the card lead wrong).
            out.append(s)
    return out


def _covered(ask: str, reply: str) -> bool:
    content = _content(ask)
    if not content:
        return True
    rl = _norm(reply)
    hits = sum(1 for c in content
               if re.search(rf"(?<![a-z0-9_]){re.escape(c)}", rl))
    if hits / len(content) >= 0.6:
        return True
    if hits and (VERIFY_RE.search(reply) or PENDING_RE.search(reply)):
        return True
    return False


def check_violations(turns: list[dict], constraints: list[dict],
                     state: str) -> list[dict]:
    """ignored_ask: standing user ask vs the agent's last reply.
    broke_rule: a negated rule whose object the last reply touched."""
    out = []
    lu, la = _last_user(turns), _last_assistant(turns)
    if lu is None or la is None or la["index"] < lu["index"]:
        return out
    err_fresh = (state == "unresolved_error"
                 and _error_turn(turns) == la["index"])
    if not err_fresh:
        for ask in _ask_sentences(lu["text"]):
            if not _covered(ask, la["text"]):
                out.append({"kind": "ignored_ask",
                            "detail": ask[:80], "object": None,
                            "rule_turn": lu["index"],
                            "evidence_turn": la["index"]})
    for c in constraints:
        m = NEG_CLAUSE_RE.search(c["text"])
        if not m:
            continue
        head, obj = m.group(1).lower(), m.group(2)
        obj = re.split(r"[,—.]| — ", obj)[0].strip()
        rl = _norm(la["text"])
        head_hit = len(head) >= 4 and head[:5] in rl
        obj_hit = bool(obj) and any(w[:5] in rl for w in _words(obj)
                                    if len(w) >= 4)
        if head_hit or (GENERIC_ACTION_RE.search(la["text"]) and obj_hit):
            out.append({"kind": "broke_rule", "detail": c["text"][:80],
                        "object": obj.split(",")[0][:60] or c["text"][:60],
                        "rule_turn": c["turn"],
                        "evidence_turn": la["index"]})
    return out


# ------------------------------------------------------------------ states

def _error_turn(turns: list[dict]) -> int | None:
    hit = None
    for t in turns:
        if ERROR_RE.search(t["text"]):
            hit = t["index"]
    return hit


def classify(turns: list[dict], decision: dict) -> str:
    """priority: big_new_task (user's latest redirection) then chef's order
    — unresolved error > unverified work > open question > stated next step,
    else continue."""
    if not turns:
        return "continue"
    lu, la = _last_user(turns), _last_assistant(turns)
    if lu is not None and BIG_RE.search(lu["text"]):
        return "big_new_task"
    e = _error_turn(turns)
    if e is not None:
        later = [t for t in turns if t["index"] > e]
        if not any(RESOLVE_RE.search(t["text"]) for t in later):
            return "unresolved_error"
    if la is not None and (not lu or la["index"] > lu["index"]):
        if CLAIM_RE.search(la["text"]) and not VERIFY_RE.search(la["text"]):
            return "unverified_work"
        last_line = [l for l in la["text"].splitlines() if l.strip()]
        if last_line and last_line[-1].strip().endswith("?"):
            return "open_question"
        if STEP_RE.search(la["text"]):
            # agent announced its own next step ("Next I'll run the
            # migration") — greenlight it, never fall to bare Continue.
            return "stated_next_step"
    if lu is not None:
        if decision.get("resolved_by_turn") == lu["index"]:
            return "stated_next_step"
        if CONTINUE_RE.search(lu["text"]):
            return "continue"
        if STEP_RE.search(lu["text"]):
            return "stated_next_step"
    return "continue"


# ----------------------------------------------------------------- renderer

def _error_excerpt(turns: list[dict], e: int) -> str:
    for t in turns:
        if t["index"] == e:
            for line in t["text"].splitlines():
                if ERROR_RE.search(line):
                    return re.sub(r"\s+", " ", line).strip()[:90]
            return re.sub(r"\s+", " ", t["text"]).strip()[:90]
    return "the error"


def _claim_object(text: str) -> str:
    m = re.search(
        r"\b(?:added|updated|changed|implemented|created|refactored|"
        r"migrated|renamed|set up|applied|integrated|tidied|moved|"
        r"completed)\s+(.{3,70}?)(?:\.|,|\s+while|\s+for\b)", text, re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else "the last change"


def _step_sentence(text: str) -> str | None:
    for sent in _sentences(text):
        if STEP_RE.search(sent):
            return re.sub(r"^(?:and\s+)?next\s*(?:[:,]\s*|\s+)"
                          r"(?:(?:i|we)(?:['’]ll| will)\s+)?", "", sent,
                          flags=re.I).strip()
    return None


def build_message(state, turns, constraints, decision, violations,
                  your_call: bool) -> str:
    lu = _last_user(turns)
    c0 = constraints[0]["text"] if constraints else None
    if violations:
        ign = next((v for v in violations if v["kind"] == "ignored_ask"), None)
        rule = next((v for v in violations if v["kind"] == "broke_rule"), None)
        msg = "You didn't do what I asked"
        if ign:
            msg += f": {ign['detail'].rstrip('.')}"
        msg += ". Fix that before moving on"
        if rule:
            msg += f", and don't change {rule['object']}."
        else:
            msg += "."
        return msg
    if state == "unresolved_error":
        e = _error_turn(turns) or 0
        msg = (f"The error is still open ({_error_excerpt(turns, e)}) — "
               "fix the root cause first.")
        if c0:
            msg += f" {c0}."
        if your_call and decision["options"]:
            a, b = decision["options"][0], (decision["options"][1]
                                            if len(decision["options"]) > 1
                                            else "the alternative")
            msg += (f" your call: {a} vs {b} — nothing else moves until "
                    "you pick.")
        return msg
    if state == "unverified_work":
        la = _last_assistant(turns)
        obj = _claim_object(la["text"]) if la else "the last change"
        msg = (f"Verify before we go further: run the checks for {obj} "
               "and paste the results.")
        if c0:
            msg += f" {c0}."
        return msg
    if state == "open_question" and your_call and decision["options"]:
        a = decision["options"][0]
        b = decision["options"][1] if len(decision["options"]) > 1 else "?"
        return f"your call — {a} or {b}. Nothing moves until you pick."
    if state == "stated_next_step":
        if decision.get("choice") and decision.get("resolved_by_turn"):
            msg = f"Go with {decision['choice']}."
            if c0:
                msg += f" {c0}."
            return msg
        la = _last_assistant(turns)
        if (la and lu and la["index"] > lu["index"]
                and STEP_RE.search(la["text"])):
            step = _step_sentence(la["text"])
            if step:
                msg = f"Go ahead — {step.rstrip('.')}."
                if c0:
                    msg += f" {c0}."
                return msg
        if lu:
            step = _step_sentence(lu["text"])
            if step:
                msg = f"{step.rstrip('.')}."
                if c0:
                    msg += f" {c0}."
                return msg
        if la:
            step = _step_sentence(la["text"])
            if step:
                msg = f"Go ahead — {step.rstrip('.')}."
                if c0:
                    msg += f" {c0}."
                return msg
        return "Proceed with the next step as agreed."
    if state == "big_new_task":
        gist = _sentences(lu["text"])[0][:90] if lu else "the new request"
        msg = ("Start fresh on this one — build a prompt for it instead "
               f"of continuing this thread: {gist}")
        if c0:
            msg += f" ({c0})"
        return msg + "."
    # continue
    return "Continue."


_WHY_WHY = {
    "unresolved_error": ("error signal with no later resolution", "error"),
    "unverified_work": ("work claimed without verification", "claim"),
    "open_question": ("agent asked; nobody answered", "question"),
    "stated_next_step": ("next step stated by you", "step"),
    "big_new_task": ("new workstream, not a continuation", "redirection"),
    "continue": ("mid-plan, nothing pending", "progress"),
}

_ALTS = {
    "unresolved_error": ["Ask for the full traceback if it is truncated.",
                         "Paste the error verbatim and let it retry."],
    "unverified_work": ["Ask what changed, file by file.",
                        "Continue now, verify at the next checkpoint."],
    "open_question": ["Answer the question directly — it is your call.",
                      "Delegate: 'you pick, document why.'"],
    "stated_next_step": ["Send only the next-step sentence.",
                         "Add an owner or deadline if work is shared."],
    "big_new_task": ["Answer inline if it is small.",
                     "Finish the old thread first, then pivot."],
    "continue": ["Stop and verify the last step first.",
                 "Ask for a summary of what is done so far."],
    "unresolved_error_violation": [
        "Re-state the rule and ask for a revert only.",
        "Ask for a diff of everything it touched."],
}


def build_why(state, turns, constraints, decision, violations) -> list[dict]:
    why: list[dict] = []

    def add(turn_idx, reason, limit=80):
        t = next((x for x in turns if x["index"] == turn_idx), None)
        if t and len(why) < 5:
            why.append({"turn": turn_idx,
                        "quote": re.sub(r"\s+", " ", t["text"])[:limit],
                        "reason": reason})

    if state == "unresolved_error":
        add(_error_turn(turns) or 1, _WHY_WHY[state][0])
    elif state == "unverified_work":
        la = _last_assistant(turns)
        if la:
            add(la["index"], _WHY_WHY[state][0])
    elif state == "open_question" and decision.get("posing_turn"):
        add(decision["posing_turn"], _WHY_WHY[state][0])
    elif state == "stated_next_step":
        if decision.get("resolved_by_turn"):
            add(decision["resolved_by_turn"],
                "your later turn resolved the choice (recency wins)")
        else:
            lu = _last_user(turns)
            la = _last_assistant(turns)
            if (la and (not lu or la["index"] > lu["index"])
                    and STEP_RE.search(la["text"])):
                add(la["index"], "the assistant stated the next step")
            elif lu:
                add(lu["index"], _WHY_WHY[state][0])
    elif state == "big_new_task":
        lu = _last_user(turns)
        if lu:
            add(lu["index"], _WHY_WHY[state][0])
    else:
        la = _last_assistant(turns)
        if la:
            add(la["index"], _WHY_WHY[state][0])
    for v in violations[:2]:
        add(v["rule_turn"], f"violation {v['kind']}: {v['detail'][:50]}")
    for c in constraints[:2]:
        if not any(w["turn"] == c["turn"] and
                   c["text"][:20] in w["quote"] for w in why):
            add(c["turn"], f"standing rule: {c['text'][:50]}")
    if decision.get("available") and decision.get("resolved_by_turn"):
        add(decision["resolved_by_turn"], "decision resolved here")
    return why


# ------------------------------------------------------- output scrubbing

_SECRET_PATS = [
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[REDACTED:private key]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:aws key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
     "[REDACTED:github token]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
     "[REDACTED:slack token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED:api key]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED:google key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
                r"\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED:jwt]"),
    (re.compile(r"(?i)\b(?:bearer|token)\s+[A-Za-z0-9._-]{16,}\b"),
     "[REDACTED:token]"),
    (re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|"
        r"access[_-]?key|auth[_-]?token|private[_-]?key)"
        r"(\s*[:=]\s*)([\"']?)[^\s\"',;]+"),
        r"\1\2\3[REDACTED]"),
]


def _scrub(text: str) -> str:
    """mask credential-shaped substrings. Transcript text may contain
    real secrets; the card must not echo them (CLI, /api/next, score)."""
    for pat, rep in _SECRET_PATS:
        text = pat.sub(rep, text)
    return text


def _scrub_card(obj):
    if isinstance(obj, str):
        return _scrub(obj)
    if isinstance(obj, list):
        return [_scrub_card(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub_card(v) for k, v in obj.items()}
    return obj


def analyze(transcript: str) -> dict:
    """transcript -> full card. One entrypoint."""
    turns = split_turns(transcript)
    constraints = extract_constraints(turns)
    decision = extract_decision(turns)
    state = classify(turns, decision)
    violations = check_violations(turns, constraints, state)
    your_call = bool(decision["available"]
                     and decision["resolved_by_turn"] is None)
    message = build_message(state, turns, constraints, decision,
                            violations, your_call)
    alts = list(_ALTS["unresolved_error_violation"] if violations
                else _ALTS[state])
    alts = [a for a in alts if a != message][:2] + ["Continue."]
    return _scrub_card({
        "state": state,
        "message": message,
        "your_call": your_call,
        "violations": violations,
        "decisions": [decision],
        "constraints_carried": constraints,
        "why": build_why(state, turns, constraints, decision, violations),
        "alternatives": list(dict.fromkeys(alts))[:3],
        "turn_count": len(turns),
        "schema": 1,
    })


# ------------------------------------------------------------------ scorer

def _lev_words(a: str, b: str) -> int:
    """word-level Levenshtein on normalized tokens — how many words chef
    would change before pressing enter."""
    ta = re.findall(r"[a-z0-9']+", a.lower())
    tb = re.findall(r"[a-z0-9']+", b.lower())
    if not ta or not tb:
        return abs(len(ta) - len(tb))
    prev = list(range(len(tb) + 1))
    for i, wa in enumerate(ta, 1):
        cur = [i]
        for j, wb in enumerate(tb, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (wa != wb)))
        prev = cur
    return prev[-1]


def score_fixtures(fixtures_dir: str | Path) -> dict:
    """run the 10 fixtures against PASSBAR conditions. Ground truth is
    never modified — a failed run is a result."""
    d = Path(fixtures_dir)
    files = sorted(d.glob("fx*.json"))
    if not files:
        raise SystemExit(f"[!] no fx*.json fixtures in {d}")
    rows = []
    intent_ok = constr_ok = invented = continue_hits = 0
    viol_hits = viol_expected = 0
    for f in files:
        fx = json.loads(f.read_text(encoding="utf-8"))
        a = analyze(fx["transcript"])
        exp = fx.get("decision") or {}
        i_ok = a["state"] == fx["expected_state"]
        carried = [_norm(c["text"]) for c in a["constraints_carried"]]
        c_ok = all(any(_norm(core).rstrip(".") in cc for cc in carried)
                   for core in fx.get("expected_constraints", []))
        eng_choice = (a["decisions"][0].get("choice")
                      if a["decisions"] else None)
        inv = False
        if not exp.get("available"):
            inv = eng_choice is not None
        elif exp.get("resolved_by_turn") is None:
            inv = eng_choice is not None
        elif eng_choice is not None and _norm(eng_choice) != _norm(
                exp.get("choice") or ""):
            inv = True
        exp_kinds = {v["kind"] for v in fx.get("violations_expected", [])}
        got_kinds = {v["kind"] for v in a["violations"]}
        v_hit = exp_kinds <= got_kinds if exp_kinds else got_kinds == set()
        exp_msg = fx.get("expected_message")
        edits = _lev_words(a["message"], exp_msg) if exp_msg else None
        c_edits = _lev_words("Continue.", exp_msg) if exp_msg else None
        viol_hits += int(v_hit)
        viol_expected += len(exp_kinds)
        intent_ok += int(i_ok)
        constr_ok += int(c_ok)
        invented += int(inv)
        continue_hits += int(fx["expected_state"] == "continue")
        rows.append({"id": fx["id"], "expected": fx["expected_state"],
                     "engine": a["state"], "intent": i_ok,
                     "constraints": c_ok, "invented": inv,
                     "violations_ok": v_hit,
                     "your_call": a["your_call"],
                     "message": a["message"],
                     "edits": edits, "continue_edits": c_edits})
    n = len(files)
    measured = [r for r in rows if r["edits"] is not None]
    within = EDIT_WORDS_MAX
    mq = {
        "measured": len(measured),
        "edit_words_max": within,
        "edit_miss": sum(1 for r in measured if r["edits"] > within),
        "engine_sendable": sum(1 for r in measured if r["edits"] <= within),
        "continue_sendable": sum(1 for r in measured
                                 if r["continue_edits"] <= within),
        "tool_added_value": sum(1 for r in measured
                                if r["edits"] <= within
                                and r["continue_edits"] > within),
        "tied_with_continue": sum(1 for r in measured
                                  if r["edits"] <= within
                                  and r["continue_edits"] <= within),
        "worse_than_continue": sum(1 for r in measured
                                   if r["edits"] > within
                                   and r["continue_edits"] <= within),
    }
    bar = {
        "intent": {"got": intent_ok, "need": PASS_BAR["intent_min"],
                   "ok": intent_ok >= PASS_BAR["intent_min"]},
        "constraints": {"got": constr_ok, "need": n, "ok": constr_ok == n},
        "invented": {"got": invented, "need": 0, "ok": invented == 0},
        "beat_continue": {"engine": intent_ok, "continue": continue_hits,
                          "ok": intent_ok > continue_hits},
    }
    passed = all(v["ok"] for v in bar.values())
    return {"n": n, "rows": rows, "bar": bar, "passed": passed,
            "message_quality": mq,
            "violation_detection": {"fixtures_ok": viol_hits,
                                    "total": n}}


def _print_report(rep: dict) -> None:
    print(f"{'id':<6}{'expected':<20}{'engine':<20}"
          f"{'intent':<8}{'constr':<8}{'inv':<6}{'viol':<6}{'edits':<7}")
    for r in rep["rows"]:
        edits = "-" if r.get("edits") is None else str(r["edits"])
        print(f"{r['id']:<6}{r['expected']:<20}{r['engine']:<20}"
              f"{'ok' if r['intent'] else 'MISS':<8}"
              f"{'ok' if r['constraints'] else 'MISS':<8}"
              f"{'INV' if r['invented'] else '-':<6}"
              f"{'ok' if r['violations_ok'] else 'MISS':<6}"
              f"{edits:<7}")
    print("-" * 81)
    for k, v in rep["bar"].items():
        print(f"{k:<16} {v}")
    print("violation detection (report-only):",
          f"{rep['violation_detection']['fixtures_ok']}"
          f"/{rep['violation_detection']['total']} fixtures exact")
    mq = rep["message_quality"]
    print(f"message quality (report-only, <= {mq['edit_words_max']} word "
          f"edits = sendable): engine {mq['engine_sendable']}"
          f"/{mq['measured']} sendable, {mq['edit_miss']} edit-misses; "
          f"beats continue {mq['tool_added_value']}, ties "
          f"{mq['tied_with_continue']}, worse {mq['worse_than_continue']}")
    print("RESULT:", "PASS" if rep["passed"] else "FAIL")


# ---------------------------------------------------------------------- cli

def render_card(card: dict) -> str:
    lines = [f"STATE: {card['state']}",
             f"YOUR CALL: {'yes' if card['your_call'] else 'no'}"]
    if card["violations"]:
        lines.append("VIOLATIONS:")
        for v in card["violations"]:
            lines.append(f"  - [{v['kind']}] {v['detail']}")
    lines += ["", "MESSAGE:", card["message"], "", "WHY:"]
    for w in card["why"]:
        lines.append(f"  - turn {w['turn']}: \"{w['quote']}\" — {w['reason']}")
    lines += ["", "CONSTRAINTS CARRIED:"]
    for c in card["constraints_carried"]:
        lines.append(f"  - turn {c['turn']}: {c['text']}")
    lines += ["", "ALTERNATIVES:"]
    lines += [f"  - {a}" for a in card["alternatives"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(
        prog="nextmsg",
        description="PromptForge next-message engine — transcript in, "
                    "next message out, with the reason.")
    p.add_argument("source", nargs="?",
                   help="transcript file, or '-' for stdin")
    p.add_argument("--json", action="store_true",
                   help="emit the card as JSON")
    p.add_argument("--score", metavar="DIR",
                   help="score fixtures dir against the pass bar")
    args = p.parse_args(argv)
    if args.score:
        rep = score_fixtures(args.score)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False))
        else:
            _print_report(rep)
        return 0 if rep["passed"] else 3
    if not args.source:
        p.error("need a transcript file, '-' for stdin, or --score DIR")
    text = (sys.stdin.read() if args.source == "-"
            else Path(args.source).read_text(encoding="utf-8"))
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("transcript"),
                                                     str):
                text = obj["transcript"]
        except ValueError:
            pass
    card = analyze(text)
    if args.json:
        print(json.dumps(card, indent=2, ensure_ascii=False))
    else:
        print(render_card(card))
    return 0


if __name__ == "__main__":
    sys.exit(main())
