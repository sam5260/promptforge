# Step 1 — determinism: use_brain chokepoint, brain_hash, as_of pinning
import ast
import contextlib
import datetime
import inspect
import io
import json
import pathlib
import tempfile
import unittest

import brain
import forge

CHAT = ("audit https://staging.example.com/login for sqli. "
        "built on next.js. scope: test sqli only. "
        "always give me a findings table.")
TODAY = datetime.date.today()


class DetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        brain.set_db(pathlib.Path(self.tmp.name) / "brain.db")

    def tearDown(self):
        self.tmp.cleanup()

    def auto_rule(self, times=None):
        times = int(brain.AUTO_APPLY) if times is None else times
        for _ in range(times):
            brain.record("constraint_added", "cyber.web",
                         brain._norm("always give a findings table"),
                         "always give a findings table")
            brain.record("slot_override", "cyber.web", "target",
                         "https://prod.example.com")


class TestChokepoint(DetCase):
    def test_use_brain_keyword_only_no_default(self):
        sig = inspect.signature(forge.compile_prompt)
        p = sig.parameters["use_brain"]
        self.assertEqual(p.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(p.default, inspect.Parameter.empty)

    def test_missing_use_brain_is_typeerror(self):
        with self.assertRaises(TypeError):
            forge.compile_prompt(CHAT)

    def test_every_caller_states_use_brain(self):
        root = pathlib.Path(forge.__file__).parent
        files = [root / "forge.py", root / "website.py",
                 *sorted((root / "tests").glob("test_*.py"))]
        for f in files:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            skipped = set()
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and fn.name == "test_missing_use_brain_is_typeerror":
                    skipped.update(range(fn.lineno, (fn.end_lineno or fn.lineno) + 1))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if node.lineno in skipped:
                    continue
                fn = node.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else getattr(fn, "id", None))
                if name == "compile_prompt":
                    kws = {kw.arg for kw in node.keywords}
                    self.assertIn("use_brain", kws,
                                  f"{f.name}:{node.lineno} omits use_brain")


class TestByteIdentical(DetCase):
    def test_same_input_same_as_of_byte_identical(self):
        self.auto_rule()
        a = forge.compile_prompt(CHAT, use_brain=True, as_of=TODAY)
        b = forge.compile_prompt(CHAT, use_brain=True, as_of=TODAY)
        self.assertEqual(a["prompt"], b["prompt"])
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))

    def test_report_carries_provenance_fields(self):
        res = forge.compile_prompt(CHAT, use_brain=True, as_of=TODAY)
        rep = res["report"]
        self.assertTrue(rep["use_brain"])
        self.assertEqual(rep["as_of"], TODAY.isoformat())
        self.assertEqual(len(rep["brain_hash"]), 64)
        self.assertEqual(rep["brain_hash"], brain.payload_hash(as_of=TODAY))


class TestAsOfPinning(DetCase):
    def test_as_of_plus_30d_changes_hash(self):
        self.auto_rule()
        h0 = brain.payload_hash(as_of=TODAY)
        h1 = brain.payload_hash(as_of=TODAY + datetime.timedelta(days=1))
        h30 = brain.payload_hash(as_of=TODAY + datetime.timedelta(days=30))
        self.assertNotEqual(h0, h1)
        self.assertNotEqual(h0, h30)

    def test_subthreshold_rules_do_not_churn_hash(self):
        h_empty = brain.payload_hash(as_of=TODAY)
        for _ in range(int(brain.AUTO_APPLY) - 1):
            brain.record("constraint_added", "cyber.web",
                         brain._norm("always x"), "always x")
        self.assertEqual(brain.payload_hash(as_of=TODAY), h_empty)
        brain.record("constraint_added", "cyber.web",
                     brain._norm("always x"), "always x")
        self.assertNotEqual(brain.payload_hash(as_of=TODAY), h_empty)

    def test_bad_as_of_rejected(self):
        with self.assertRaises(ValueError):
            brain.as_of_day("not-a-date")


class TestNoBrainDeterminism(DetCase):
    def test_no_brain_output_ignores_brain_state(self):
        off_clean = forge.compile_prompt(CHAT, use_brain=False, as_of=TODAY)
        self.auto_rule()
        off_dirty = forge.compile_prompt(CHAT, use_brain=False, as_of=TODAY)
        self.assertEqual(off_clean["prompt"], off_dirty["prompt"])
        self.assertEqual(json.dumps(off_clean, sort_keys=True),
                         json.dumps(off_dirty, sort_keys=True))
        self.assertFalse(off_dirty["report"]["use_brain"])
        self.assertNotIn("## Your preferences", off_dirty["prompt"])

    def test_brain_on_applies_and_matches_never_taught_state(self):
        on_clean = forge.compile_prompt(CHAT, use_brain=True, as_of=TODAY)
        self.auto_rule()
        on_dirty = forge.compile_prompt(CHAT, use_brain=True, as_of=TODAY)
        off_dirty = forge.compile_prompt(CHAT, use_brain=False, as_of=TODAY)
        self.assertNotEqual(on_clean["prompt"], on_dirty["prompt"])
        self.assertIn("## Your preferences", on_dirty["prompt"])
        self.assertEqual(on_clean["prompt"], off_dirty["prompt"])
        self.assertNotEqual(on_dirty["report"]["brain_hash"],
                            off_dirty["report"]["brain_hash"])


class TestCliNoBrain(DetCase):
    def _compile(self, *extra):
        path = pathlib.Path(self.tmp.name) / "chat.txt"
        path.write_text(CHAT, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = forge.main(["compile", str(path), "--format", "json",
                             "--as-of", TODAY.isoformat(), *extra])
        self.assertEqual(rc, 0, err.getvalue())
        return json.loads(out.getvalue()), err.getvalue()

    def test_cli_no_brain_flag_and_alias(self):
        self.auto_rule()
        off, err = self._compile("--no-brain")
        self.assertFalse(off["report"]["use_brain"])
        self.assertIn("brain: hash=", err)
        self.assertNotIn("## Your preferences", off["prompt"])
        off_alias, _ = self._compile("--no-learned")
        self.assertEqual(off["prompt"], off_alias["prompt"])
        on, _ = self._compile()
        self.assertTrue(on["report"]["use_brain"])
        self.assertNotEqual(off["prompt"], on["prompt"])

    def test_cli_bad_as_of_rejected(self):
        path = pathlib.Path(self.tmp.name) / "chat.txt"
        path.write_text(CHAT, encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = forge.main(["compile", str(path), "--format", "json",
                             "--as-of", "not-a-date"])
        self.assertEqual(rc, forge.EXIT_VALIDATION)

    def test_cli_gen_no_brain(self):
        self.auto_rule()

        def run_gen(*extra):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = forge.main(["gen", "cyber.web", "-s", "scope=web app",
                                 "--yes", "--format", "json",
                                 "--as-of", TODAY.isoformat(), *extra])
            self.assertEqual(rc, 0)
            return json.loads(out.getvalue())

        off = run_gen("--no-brain")
        on = run_gen()
        self.assertFalse(off["use_brain"])
        self.assertTrue(on["use_brain"])
        self.assertIn("brain_hash", off)
        self.assertEqual(on["slots"]["target"], "https://prod.example.com")
        self.assertNotEqual(on["slots"]["target"], off["slots"]["target"])


if __name__ == "__main__":
    unittest.main()
