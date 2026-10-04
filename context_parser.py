#!/usr/bin/env python3
"""context parser — turn raw chat text into structured compile input.
language: python 3.10+ | runtime: stdlib only | target: promptforge v2 pipeline

deterministic rules only: domain lexicon -> template pick, regex facts,
sentence-level constraint detection. never raises on weird input.
"""

import re

import forge

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
KV_LINE_RE = re.compile(r"^\s*(?:[-*+]\s*)?([A-Za-z][A-Za-z0-9 _./-]{0,24})\s*[:=]\s*(\S.*)$")
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

# spec id -> [(keyword, weight)] ; multiword keywords weigh double
LEXICON = {
    "cyber.web": [
        ("web app", 2), ("website", 2), ("web application", 3), ("http", 1),
        ("sqli", 2), ("sql injection", 3), ("xss", 2), ("csrf", 2), ("ssrf", 2),
        ("idor", 2), ("burp", 2), ("pentest", 2), ("penetration test", 3),
        ("login", 1), ("session", 1), ("cookie", 1), ("waf", 2), ("lfi", 2),
        ("rce", 1), ("redirect", 1), ("form", 1), ("url", 1),
    ],
    "cyber.api": [
        ("api", 2), ("rest", 1), ("graphql", 3), ("swagger", 3), ("openapi", 3),
        ("endpoint", 2), ("grpc", 3), ("rpc", 1), ("webhook", 2),
        ("rate limit", 2), ("mass assignment", 3), ("object-level", 3),
        ("bola", 3), ("api key", 2),
    ],
    "cyber.binary": [
        ("binary", 2), ("reverse engineer", 3), ("reversing", 2), ("reverse engineering", 3),
        ("decompile", 3), ("disassembl", 2), ("ghidra", 3), ("x64dbg", 3),
        ("radare", 3), ("unpack", 2), ("pe32", 3), ("elf", 2), (".net", 2),
        ("crackme", 3), ("binary ninja", 3), ("ida", 1), ("patch", 1),
    ],
    "cyber.cloud": [
        ("aws", 3), ("azure", 3), ("gcp", 3), ("google cloud", 3), ("s3", 3),
        ("iam", 2), ("lambda", 2), ("cloud", 2), ("bucket", 2), ("subscription", 2),
        ("terraform", 2), ("eks", 3), ("account", 1), ("kubernetes", 2),
    ],
    "cyber.malware": [
        ("malware", 3), ("ransomware", 3), ("trojan", 3), ("botnet", 3),
        ("c2", 2), ("c&c", 3), ("sandbox", 2), ("ioc", 3), ("family", 2),
        ("attribution", 2), ("dynamic analysis", 3), ("behavior report", 3),
        ("static analysis", 2), ("sample", 1),
    ],
    "cyber.network": [
        ("network", 2), ("active directory", 3), ("domain controller", 3),
        ("lateral movement", 3), ("nmap", 3), ("subnet", 2), ("firewall", 2),
        ("vpn", 2), ("red team", 2), ("pivot", 2), ("ntlm", 3), ("kerberos", 3),
        ("internal", 1), ("wifi", 2), ("phishing", 2),
    ],
    "cyber.osint": [
        ("osint", 3), ("recon", 2), ("reconnaissance", 3), ("passive", 2),
        ("footprint", 3), ("whois", 3), ("social media", 2), ("breach", 2),
        ("open source intelligence", 3), ("leak", 1), ("investigate", 1),
        ("people search", 3),
    ],
    "aiml.llm": [
        ("llm", 3), ("chatbot", 3), ("prompt", 2), ("rag", 3), ("guardrail", 3),
        ("ai feature", 3), ("ai assistant", 3), ("agent", 2), ("system prompt", 3),
        ("few-shot", 3), ("ai app", 3),
    ],
    "aiml.model": [
        ("fine-tune", 3), ("finetune", 3), ("training", 2), ("train", 1),
        ("hyperparameter", 3), ("dataset", 2), ("epochs", 3), ("overfitting", 3),
        ("benchmark", 2), ("checkpoint", 2), ("loss", 1),
    ],
}

NEG_MARKERS = ["don't", "do not", "dont", "never", "avoid", "refrain",
               "must not", "mustn't", "no brute", "no dos", "no-ddos",
               "stop at", "exclude", "out of scope", "don’t"]
POS_MARKERS = ["always", "must ", "ensure ", "keep ", "only use", "prefer "]
SCOPE_MARKERS = ["in scope", "out of scope", "only test", "do not test",
                 "don't test", "allowed", "excluded", "scope is", "off limits"]

KEY_SLOTS = {
    "url": "target", "host": "target", "hostname": "target", "endpoint": "target",
    "site": "target", "baseurl": "target", "application": "target", "app": "target",
    "targeturl": "target", "what": "target",
    "sample": "sample", "binary": "sample", "file": "sample", "path": "sample",
    "hash": "sample", "malware": "sample",
    "org": "subject", "company": "subject", "organization": "subject",
    "person": "subject", "name": "subject",
    "goal": "objective", "aim": "objective", "mission": "objective",
    "provider": "provider", "cloud": "provider",
    "style": "api_style", "apitype": "api_style", "type": "api_style",
    "auth": "auth_scheme", "authentication": "auth_scheme",
    "scope": "scope", "scoperules": "scope", "allowed": "scope",
    "rules": "scope", "constraints": "constraints", "constraint": "constraints",
    "limits": "constraints", "ruleseng": "constraints",
    "tech": "stack", "techstack": "stack", "technology": "stack", "stack": "stack",
    "detect": "detection", "edr": "detection",
    "access": "initial_access", "entry": "entry", "start": "entry",
    "audience": "output_audience", "reader": "output_audience",
    "tools": "tools", "tooling": "tools",
    "objective": "objective", "task": "objective",
    "platform": "platform", "os": "platform", "arch": "arch",
    "architecture": "arch", "protections": "protections",
    "knownfamily": "known_family", "family": "known_family",
    "analysisenv": "analysis_env", "env": "analysis_env",
    "objects": "objects", "scopes": "scopes", "authscheme": "auth_scheme",
    "apisc": "api_style", "data": "data", "success": "success",
    "budget": "budget", "model": "model", "feature": "feature req",
    "inputs": "inputs", "failure": "failure_cost", "failurecost": "failure_cost",
    "initialaccess": "initial_access", "rolemodel": "role_model",
    "outputaudience": "output_audience", "knownfamily": "known_family",
}

TECH_MAP = [
    ("stack", ["react", "next.js", "nextjs", "node", "express", "django", "flask",
               "laravel", "rails", "spring", "postgres", "postgresql", "mysql",
               "mongodb", "redis", "sqlite", "fastapi", "php", "asp.net"]),
    ("api_style", ["graphql", "rest", "grpc"]),
    ("auth_scheme", ["jwt", "oauth2", "oauth", "oidc", "sso", "session cookie",
                     "api key", "basic auth", "saml"]),
    ("provider", ["aws", "azure", "gcp", "google cloud"]),
    ("platform", ["windows", "linux", "android", "macos", "macos", "ios"]),
    ("arch", ["x86-64", "x64", "amd64", "arm64", "aarch64", ".net", "pe32", "elf"]),
    ("tools", ["ghidra", "ida pro", "ida", "binary ninja", "x64dbg", "windbg",
               "radare2", "burp", "nmap", "wireshark", "volatility"]),
    ("detection", ["crowdstrike", "sentinelone", "defender", "carbon black",
                   "sophos", "edr"]),
]


def _kw_re(kw):
    return re.compile(r"(?<!\w)" + re.escape(kw).replace(r"\ ", r"\s+") + r"(?!\w)", re.I)


def rank_specs(text):
    """-> [(spec_id, score, matched_keywords)] sorted by score desc"""
    if not text or not text.strip():
        return []
    hits = []
    for spec_id, words in LEXICON.items():
        matched, score = [], 0
        for kw, w in words:
            if _kw_re(kw).search(text):
                matched.append(kw)
                score += w
        if score:
            hits.append((spec_id, score, matched))
    hits.sort(key=lambda t: (-t[1], t[0]))
    return hits


def _key(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def slot_for_key(raw):
    k = _key(raw)
    if k in KEY_SLOTS:
        return KEY_SLOTS[k]
    for slot_key, slot in KEY_SLOTS.items():
        if k and (slot_key.startswith(k) or k.startswith(slot_key)):
            return slot
    return None


def extract_constraints(text):
    """sentence-level polarity detection -> [{text, polarity}]"""
    out = []
    for raw in SENT_SPLIT_RE.split(text or ""):
        s = raw.strip().strip("-*•> ").strip()
        if len(s) < 8 or len(s) > 300:
            continue
        low = " " + s.lower() + " "
        if any(m in low for m in NEG_MARKERS):
            out.append({"text": s, "polarity": "negative"})
        elif any(m in low for m in POS_MARKERS) and any(
                m in low for m in ("always", "must", "ensure", "keep", "prefer")):
            out.append({"text": s, "polarity": "positive"})
    return out


def _facts(text):
    facts = []
    for rx, kind in ((CVE_RE, "cve"), (EMAIL_RE, "email")):
        for m in rx.finditer(text or ""):
            facts.append({"kind": kind, "value": m.group(0)})
    return facts


def detect_slots(text, spec):
    """regex/keyword extraction mapped onto one spec's slot names"""
    slots = {}
    if not text or not spec:
        return slots

    def put(name, value, conf, how):
        value = value.strip().rstrip(".,;:)")
        if not value or name in slots:
            return
        if any(s.get("name") == name for s in spec.get("slots", [])):
            slots[name] = {"value": value, "conf": conf, "how": how}

    m = URL_RE.search(text)
    if m:
        put("target", m.group(0), 0.9, "url found")
    for m in IP_RE.finditer(text):
        put("target", m.group(0), 0.7, "ip found")
        break

    units = list(text.splitlines())
    units += [s for s in SENT_SPLIT_RE.split(text) if s and s not in units]
    for unit in units:
        kv = KV_LINE_RE.match(unit.strip())
        if kv:
            slot = slot_for_key(kv.group(1))
            if slot:
                put(slot, kv.group(2), 0.85, "key: value in your text")

    for slot, words in TECH_MAP:
        for w in words:
            if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text, re.I):
                put(slot, w, 0.8, "keyword in your text")
                break

    scope_slot = next((s["name"] for s in spec.get("slots", [])
                       if s["name"] in ("scope", "scope_rules", "constraints")),
                      None)
    if scope_slot:
        for sent in SENT_SPLIT_RE.split(text):
            s = sent.strip().strip("-*•> ")
            if 8 <= len(s) <= 300 and any(m in s.lower() for m in SCOPE_MARKERS):
                put(scope_slot, s, 0.7, "scope sentence detected")
                break
    return slots


FIELD_BUCKETS = [
    ("design", "Design", ["design", "theme", "poster", "layout", "mockup",
     "wireframe", "figma", "branding", "logo", "palette", "typography",
     "pdf", "slides", "powerpoint", "invitation", "flyer", "banner",
     "illustration", "color", "colour", "minecraft", "marvel", "aesthetic"]),
    ("writing", "Writing", ["essay", "article", "blog", "story", "poem",
     "novel", "rewrite", "proofread", "grammar", "caption", "screenplay",
     "letter", "copywriting", "headline", "ghostwrite"]),
    ("development", "Software Development", ["code", "coding", "function",
     "script", "app", "website", "webapp", "debug", "refactor", "python",
     "javascript", "typescript", "golang", "rust", "compiler", "regex",
     "backend", "frontend", "deploy", "repository", "framework", "stacktrace"]),
    ("data", "Data & Analysis", ["dataset", "excel", "csv", "pandas",
     "dataframe", "chart", "spreadsheet", "statistics", "regression",
     "dashboard", "tableau", "powerbi", "visualization", "numpy",
     "correlation"]),
    ("security", "Security", ["pentest", "penetration", "vulnerability",
     "xss", "cve", "exploit", "malware", "phishing", "osint", "ransomware",
     "bug bounty", "sql injection", "forensics"]),
    ("business", "Business & Marketing", ["business", "startup", "revenue",
     "marketing", "campaign", "seo", "sales", "pitch", "investor", "budget",
     "profit", "strategy", "pricing", "swot", "funnel", "conversion"]),
    ("education", "Education", ["teach", "lesson", "quiz", "exam", "homework",
     "study", "syllabus", "tutor", "course", "student", "curriculum",
     "explain"]),
    ("research", "Research", ["research", "thesis", "literature", "citation",
     "survey", "summarize", "summarise", "arxiv", "journal"]),
    ("media", "Media", ["video", "audio", "song", "podcast", "thumbnail",
     "animation", "ffmpeg", "photoshop", "camera", "footage", "subtitle"]),
]


def guess_field(text):
    """deterministic any-field bucket -> (slug, label); never raises"""
    low = (text or "").lower()
    best, best_hits = None, 0
    for slug, label, kws in FIELD_BUCKETS:
        hits = 0
        for kw in kws:
            pat = re.escape(kw).replace(r"\ ", r"\s+")
            if re.search(r"(?<!\w)" + pat + r"(?!\w)", low):
                hits += 1
        if hits > best_hits:
            best, best_hits = (slug, label), hits
    return best or ("general", "General")


def parse(text, specs=None, spec_id=None):
    """main entry — never raises on input shape; missing template -> None"""
    text = text or ""
    if specs is None:
        specs = forge.load_specs()
    ranked = rank_specs(text)
    candidates = [{"id": sid, "score": sc, "keywords": kws[:8]}
                  for sid, sc, kws in ranked[:3]]
    chosen = None
    confidence = 0.0
    if spec_id:
        chosen = spec_id if spec_id in specs else None
        confidence = 1.0 if chosen else 0.0
    elif ranked:
        chosen = ranked[0][0]
        top = ranked[0][1]
        second = ranked[1][1] if len(ranked) > 1 else 0
        confidence = round(top / (top + second), 2) if second else 0.9
    slots = detect_slots(text, specs.get(chosen)) if chosen else {}
    label = None
    if chosen:
        nm = (specs.get(chosen) or {}).get("name") or ""
        label = nm[:-7] if nm.endswith(" Prompt") else nm
    if not label:
        label = guess_field(text)[1]
    return {
        "spec_id": chosen,
        "field": label,
        "confidence": confidence,
        "candidates": candidates,
        "slots": slots,
        "constraints": extract_constraints(text),
        "facts": _facts(text),
        "chars": len(text),
    }
