#!/usr/bin/env python3
"""PromptForge website — local web app mirroring the CLI, same engine.

language: python 3.10+ | runtime: stdlib only | target: localhost browser
run:      python website.py [--port 8765] [--no-open]
entry:    double-click "Open Website.bat"

serves a single-page UI from ./web/ and a JSON API under /api/ that calls
forge.py functions directly (no logic duplication).
"""

import argparse
import difflib
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import brain
import context_parser
import forge

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
MAX_BODY = 8 * 1024 * 1024

MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon"}


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def ok(payload):
    return 200, {"ok": True, **payload}


def err_from(exc):
    if isinstance(exc, ApiError):
        return exc.status, {"ok": False, "error": str(exc)}
    if isinstance(exc, forge.MissingSlot):
        return 400, {"ok": False, "error":
                     f"[!] missing required slot '{exc.name}' — fill it, "
                     f"or enable 'placeholders'"}
    if isinstance(exc, KeyError):
        return 400, {"ok": False, "error": f"[!] {exc.args[0]}"}
    if isinstance(exc, (FileNotFoundError, FileExistsError, ValueError)):
        return 400, {"ok": False, "error": f"[!] {exc}"}
    return 500, {"ok": False, "error": f"[!] {exc}"}


def _as_of_or_400(value):
    if value in (None, ""):
        return None
    try:
        brain.as_of_day(value)
    except (ValueError, TypeError) as exc:
        raise ApiError(400, f"[!] bad as_of — expected YYYY-MM-DD: "
                            f"{value!r}") from exc
    return value


def spec_or_400(field):
    specs = forge.load_specs()
    return forge.get_spec(specs, field)


def spec_auto(field, *texts):
    """explicit field wins; otherwise infer from the text, else universal"""
    if field:
        return spec_or_400(field)
    specs = forge.load_specs()
    probe = " ".join(t for t in texts if t)
    ctx = context_parser.parse(probe, specs=specs)
    if ctx["spec_id"]:
        return forge.get_spec(specs, ctx["spec_id"])
    slug, label = context_parser.guess_field(probe)
    return forge.universal_spec(label, slug)


def clean_spec(spec):
    return {k: v for k, v in spec.items() if not k.startswith("_")}


def within_root(rel):
    if rel is None:
        rel = ""
    rel = str(rel).replace("\\", "/").lstrip("/")
    target = (ROOT / rel).resolve() if rel else ROOT
    if target != ROOT and ROOT not in target.parents:
        raise ApiError(404, "[!] not found")
    return target, rel


# ------------------------------------------------------------------ handlers

def api_fields(query, body):
    specs = forge.load_specs()
    fields = [{"id": s.get("id", fid), "domain": s.get("domain", "?"),
               "name": s.get("name", ""), "summary": s.get("summary", "")}
              for fid, s in specs.items()]
    fields.sort(key=lambda f: f["id"])
    return ok({"fields": fields, "struct_labels": forge.STRUCT_LABELS})


def api_spec(query, body):
    fid = (query.get("id") or [""])[0]
    if not fid:
        raise ApiError(400, "[!] missing id")
    spec = spec_or_400(fid)
    return ok({"spec": clean_spec(spec)})


def api_gen(query, body):
    spec = spec_or_400(body.get("field", ""))
    provided = body.get("slots") or {}
    yes = bool(body.get("yes", True))
    use_brain = bool(body.get("use_brain", body.get("learned", True)))
    as_of = _as_of_or_400(body.get("as_of"))
    slots = dict(provided)
    if use_brain:
        slots = {**brain.slot_prefs(spec["id"], as_of=as_of), **slots}
    values, warnings = forge.slot_values(spec, slots,
                                         interactive=False, assume_yes=yes)
    if body.get("strict"):
        bad = [s["name"] for s in spec.get("slots", [])
               if s.get("required")
               and (not str(values.get(s["name"], "")).strip()
                    or forge.PLACEHOLDER_RE.fullmatch(str(values.get(s["name"], ""))))]
        if bad:
            raise ApiError(400, "[!] strict: required slots unresolved: "
                                + ", ".join(bad))
    prompt = forge.build(spec, values, body.get("mode", "full"))
    payload = forge.gen_payload(spec, values, body.get("mode", "full"), prompt)
    payload.update(forge.brain_meta(use_brain, as_of))
    return ok({"prompt": prompt, "values": values, "warnings": warnings,
               "payload": payload})


def api_check(query, body):
    spec = spec_auto(body.get("field"), body.get("text") or "")
    text = body.get("text") or ""
    if not text.strip():
        raise ApiError(400, "[!] empty prompt — paste text or load a file")
    result = forge.judge(spec, text)
    title = f"{spec['id']} / input"
    return ok({"result": result, "title": title})


def api_fix(query, body):
    spec = spec_auto(body.get("field"), body.get("text") or "")
    text = body.get("text") or ""
    if not text.strip():
        raise ApiError(400, "[!] empty prompt — paste text or load a file")
    values, warnings = forge.slot_values(spec, body.get("slots") or {},
                                         missing_ok=True)
    before = forge.judge(spec, text)
    fixed, rep = forge.fix_prompt(spec, text, values)
    after = forge.judge(spec, fixed)
    diff = "\n".join(difflib.unified_diff(
        text.splitlines(), fixed.splitlines(),
        fromfile="input", tofile="fixed", lineterm=""))
    return ok({"before": before["score"], "after": after["score"],
               "report": rep, "diff": diff, "prompt": fixed,
               "warnings": warnings})


def api_compare(query, body):
    spec = spec_auto(body.get("field"),
                     body.get("a") or "", body.get("b") or "")
    ta, tb = body.get("a") or "", body.get("b") or ""
    if not ta.strip() or not tb.strip():
        raise ApiError(400, "[!] both prompts are required")
    ra, rb = forge.judge(spec, ta), forge.judge(spec, tb)
    winner = ("A" if ra["score"] > rb["score"]
              else "B" if rb["score"] > ra["score"] else "tie")
    return ok({"a": ra, "b": rb, "winner": winner})


def api_merge(query, body):
    spec = spec_auto(body.get("field"),
                     body.get("a") or "", body.get("b") or "")
    ta, tb = body.get("a") or "", body.get("b") or ""
    if not ta.strip() or not tb.strip():
        raise ApiError(400, "[!] both prompts are required")
    values, warnings = forge.slot_values(spec, body.get("slots") or {},
                                         missing_ok=True)
    merged, meta = forge.merge_prompts(spec, ta, tb, values)
    ra, rb, rm = forge.judge(spec, ta), forge.judge(spec, tb), forge.judge(spec, merged)
    return ok({"a": ra["score"], "b": rb["score"], "merged": rm["score"],
               "meta": meta, "prompt": merged, "warnings": warnings})


def api_ensemble(query, body):
    spec = spec_auto(body.get("field"), body.get("text") or "")
    text = body.get("text") or ""
    if not text.strip():
        raise ApiError(400, "[!] empty prompt — paste text or load a file")
    attempts = body.get("attempts")
    attempts = 12 if attempts in (None, "") else int(attempts)
    top = body.get("top")
    top = 3 if top in (None, "") else int(top)
    if attempts < 1:
        raise ApiError(400, "[!] attempts must be >= 1")
    if top < 1:
        raise ApiError(400, "[!] --top must be >= 1")
    seed = body.get("seed")
    seed = int(seed) if seed not in (None, "") else None
    values, warnings = forge.slot_values(spec, body.get("slots") or {},
                                         missing_ok=True)
    ranked, log = forge.ensemble(spec, text, values, attempts=attempts,
                                 seed=seed, topk=top)
    return ok({"log": log, "top": [{"score": s, "prompt": t} for s, t in ranked],
               "warnings": warnings})


def api_lint(query, body):
    specs = forge.load_specs(fail_fast=False)
    errors, warnings = forge.lint_specs(specs)
    return ok({"errors": errors, "warnings": warnings, "count": len(specs)})


def api_save(query, body):
    rel = body.get("path") or ""
    if not str(rel).strip():
        raise ApiError(400, "[!] path required (e.g. prompts/my.txt)")
    text = body.get("text")
    if text is None:
        raise ApiError(400, "[!] text required")
    target, rel = within_root(rel)
    if target == ROOT or target.is_dir():
        raise ApiError(400, "[!] path must be a file inside the project")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ApiError(400, f"[!] {exc}") from exc
    return ok({"path": rel, "bytes": len(text.encode("utf-8"))})


def api_parse(query, body):
    text = body.get("text") or ""
    if not text.strip():
        raise ApiError(400, "[!] empty text — paste your chat first")
    ctx = context_parser.parse(text, specs=forge.load_specs(),
                               spec_id=body.get("spec"))
    return ok({"ctx": ctx})


def api_compile(query, body):
    text = body.get("text") or ""
    if not text.strip():
        raise ApiError(400, "[!] empty text — paste your chat first")
    res = forge.compile_prompt(
        text, spec_id=body.get("spec"),
        explicit=body.get("slots") or None,
        use_brain=bool(body.get("use_brain", body.get("learned", True))),
        mode=body.get("mode", "full"),
        as_of=_as_of_or_400(body.get("as_of")))
    return ok(res)


def api_learn(query, body):
    generated = body.get("generated") or ""
    corrected = body.get("corrected") or ""
    spec_id = body.get("field") or ""
    if not spec_id:
        raise ApiError(400, "[!] field required")
    if not generated.strip() or not corrected.strip():
        raise ApiError(400, "[!] both generated and corrected prompts required")
    if generated == corrected:
        raise ApiError(400, "[!] texts are identical — nothing to learn")
    summary = brain.learn_from_diff(spec_id, generated, corrected)
    return ok({"summary": summary, "status": brain.status()})


def api_brain(query, body):
    action = (query.get("action") or ["show"])[0]
    if action == "show":
        pats = brain.suggest()
        return ok({"patterns": pats, "status": brain.status(),
                   "auto_apply": brain.AUTO_APPLY, "half_life_days": 30})
    if action == "export":
        return ok({"export": brain.export_json()})
    raise ApiError(400, f"[!] unknown brain action '{action}'")


def api_brain_post(query, body):
    action = body.get("action") or ""
    if action == "forget":
        pid = int(body.get("id") or 0)
        return ok({"removed": brain.forget(pid), "status": brain.status()})
    if action == "reset":
        if body.get("yes") is not True:
            raise ApiError(400, "[!] reset needs {\"yes\": true} — "
                                "this deletes every learned pattern")
        return ok({"removed": brain.reset(), "status": brain.status()})
    raise ApiError(400, f"[!] unknown brain action '{action}'")


ROUTES = {
    "/api/fields": api_fields,
    "/api/spec": api_spec,
    "/api/gen": api_gen,
    "/api/check": api_check,
    "/api/fix": api_fix,
    "/api/compare": api_compare,
    "/api/merge": api_merge,
    "/api/ensemble": api_ensemble,
    "/api/lint": api_lint,
    "/api/save": api_save,
    "/api/parse": api_parse,
    "/api/compile": api_compile,
    "/api/learn": api_learn,
    "/api/brain": api_brain,
    "/api/brain/action": api_brain_post,
}


def make_handler(root=None):
    if root is not None:  # test hook: save against a scratch tree
        globals()["ROOT"] = Path(root).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "PromptForge"

        def _send(self, status, payload, ctype="application/json; charset=utf-8"):
            if isinstance(payload, (dict, list)):
                data = json.dumps(payload, ensure_ascii=False,
                                  default=str).encode("utf-8")
            else:
                data = payload if isinstance(payload, bytes) \
                    else str(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ROUTES:
                try:
                    if method == "POST":
                        length = int(self.headers.get("Content-Length") or 0)
                        if length > MAX_BODY:
                            raise ApiError(413, "[!] body too large")
                        raw = self.rfile.read(length) if length else b"{}"
                        body = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(body, dict):
                            raise ApiError(400, "[!] JSON object expected")
                    else:
                        body = {}
                    query = parse_qs(parsed.query)
                    status, payload = ROUTES[path](query, body)
                except json.JSONDecodeError as exc:
                    status, payload = 400, {"ok": False,
                                            "error": f"[!] bad JSON: {exc}"}
                except Exception as exc:  # noqa: BLE001 — API boundary
                    status, payload = err_from(exc)
                self._send(status, payload)
                return True
            return False

        def do_GET(self):
            if self._dispatch("GET"):
                return
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                index = WEB_DIR / "index.html"
                if not index.is_file():
                    self._send(500, "[!] web/index.html missing".encode(),
                               "text/plain; charset=utf-8")
                    return
                self._send(200, index.read_bytes(), "text/html; charset=utf-8")
                return
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                target = (WEB_DIR / rel).resolve()
                if WEB_DIR not in target.parents or not target.is_file():
                    self._send(404, b"[!] not found",
                               "text/plain; charset=utf-8")
                    return
                ctype = MIME.get(target.suffix.lower(),
                                 "application/octet-stream")
                self._send(200, target.read_bytes(), ctype)
                return
            self._send(404, b"[!] not found", "text/plain; charset=utf-8")

        def do_POST(self):
            if not self._dispatch("POST"):
                self._send(404, b"[!] not found", "text/plain; charset=utf-8")

        def log_message(self, fmt, *args):
            pass

    return Handler


def run(port: int, open_browser: bool = True) -> int:
    if not (WEB_DIR / "index.html").is_file():
        print(f"[!] missing {WEB_DIR / 'index.html'}", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler())
    url = f"http://127.0.0.1:{port}/"
    print(f"[+] PromptForge website  {url}   (Ctrl+C to stop)", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[+] stopped")
    finally:
        server.server_close()
    return 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(
        prog="website",
        description="PromptForge local website — same engine as the CLI.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true",
                   help="do not auto-open the browser")
    args = p.parse_args(argv)
    return run(args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    sys.exit(main())
