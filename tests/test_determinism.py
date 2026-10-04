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
        self.assertEqual(rep["schema"], 1)
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


class TestSpecHash(DetCase):
    def test_spec_hash_in_report_stable_and_valid(self):
        a = forge.compile_prompt(CHAT, use_brain=True, as_of=TODAY)
        b = forge.compile_prompt(CHAT, use_brain=True, as_of=TODAY)
        h = a["report"]["spec_hash"]
        self.assertEqual(len(h), 64)
        int(h, 16)  # hex only
        self.assertEqual(h, b["report"]["spec_hash"])
        self.assertEqual(h, forge.specs_hash())

    def test_spec_hash_tracks_spec_set_content(self):
        base = forge.load_specs()
        h0 = forge.specs_hash(base)
        mutated = json.loads(json.dumps(base))
        mutated["cyber.web"]["name"] = "renamed"
        self.assertNotEqual(forge.specs_hash(mutated), h0)
        self.assertEqual(forge.specs_hash(base), h0)

    def test_canonical_json_ignores_key_order(self):
        base = forge.load_specs()
        reordered = json.loads(json.dumps(base, sort_keys=True),
                               object_pairs_hook=lambda kv: dict(reversed(kv)))
        self.assertEqual(forge.specs_hash(reordered), forge.specs_hash(base))

    def test_gen_payload_carries_spec_hash(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = forge.main(["gen", "cyber.web", "--yes", "--format", "json",
                             "--as-of", TODAY.isoformat()])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["spec_hash"], forge.specs_hash())


class TestMergeSeam(DetCase):
    def test_single_seam_called_by_all_three_surfaces(self):
        import unittest.mock as mock
        import website
        with mock.patch.object(forge, "merge_prefs",
                               wraps=forge.merge_prefs) as spy:
            forge.compile_prompt(CHAT, use_brain=True, as_of=TODAY)
            self.assertTrue(spy.called, "compile_prompt skipped the seam")
            spy.reset_mock()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                forge.main(["gen", "cyber.web", "--yes", "--format", "json",
                            "--as-of", TODAY.isoformat()])
            self.assertTrue(spy.called, "CLI gen skipped the seam")
            spy.reset_mock()
            website.api_gen({}, {"field": "cyber.web", "yes": True})
            self.assertTrue(spy.called, "api_gen skipped the seam")

    def test_slot_prefs_only_reachable_through_seam(self):
        root = pathlib.Path(forge.__file__).parent
        for fname in ("forge.py", "website.py"):
            tree = ast.parse((root / fname).read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "slot_prefs"):
                        self.assertEqual(
                            fn.name, "merge_prefs",
                            f"{fname}:{node.lineno} reaches brain.slot_prefs "
                            f"outside the seam")

    def test_explicit_dominates_learned(self):
        self.auto_rule()
        merged = forge.merge_prefs("cyber.web",
                                   {"target": "explicit.example.com"},
                                   True, TODAY)
        self.assertEqual(merged["target"], "explicit.example.com")
        learned = forge.merge_prefs("cyber.web", {}, True, TODAY)
        self.assertEqual(learned["target"], "https://prod.example.com")
        off = forge.merge_prefs("cyber.web", {}, False, TODAY)
        self.assertEqual(off, {})


if __name__ == "__main__":
    unittest.main()
