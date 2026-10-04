# v2 tests — brain: learning store, thresholds, apply layer
import pathlib
import tempfile
import unittest

import brain
import forge

GEN_PREFERENCE = "- Always include an executive summary for management."
VICTIM_PREFIX = "4. "


def _mktext(title="Web Application Pentest Prompt"):
    return (f"# {title} — generated prompt\n\n"
            "## Role\nYou are a pentester.\n\n"
            "## Task\nAudit the app.\n\n"
            "## Methodology\n1. scope in\n2. recon\n3. probe\n4. hunt injection "
            "classes appropriate to the stack\n5. report\n\n"
            "## Output contract\n- findings table\n\n"
            "## Quality checklist (self-verify before delivering)\n"
            "- [ ] [target] result names the target explicitly\n\n"
            "## Do not\n- Do not invent facts\n")


class BrainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        brain.set_db(pathlib.Path(self.tmp.name) / "brain.db")

    def tearDown(self):
        self.tmp.cleanup()


class TestRecordAndStatus(BrainCase):
    def test_record_upserts(self):
        i1 = brain.record("boilerplate_removed", "cyber.web", "long intro line", "long intro line")
        i2 = brain.record("boilerplate_removed", "cyber.web", "long intro line", "long intro line")
        self.assertEqual(i1, i2)
        st = brain.status()
        self.assertEqual(st["patterns"], 1)
        self.assertEqual(st["by_kind"]["boilerplate_removed"]["hits"], 2)

    def test_record_rejects_empty(self):
        self.assertIsNone(brain.record("boilerplate_removed", "x", "   ", ""))
        self.assertEqual(brain.status()["patterns"], 0)

    def test_decay_halves_over_half_life(self):
        import time
        now = time.time()
        self.assertAlmostEqual(brain._decayed(4, now - 30 * 86400, now), 2.0, places=2)
        self.assertAlmostEqual(brain._decayed(4, now, now), 4.0, places=2)


class TestLearnDiff(BrainCase):
    def test_three_kinds_and_slot_line_not_boilerplate(self):
        gen = _mktext()
        line4 = next(l for l in gen.splitlines() if l.startswith(VICTIM_PREFIX))
        cor = gen.replace(line4 + "\n", "")
        cor = cor.replace("## Do not", f"## Do not\n{GEN_PREFERENCE}")
        cor = cor.replace("- **target**: https://a.test", "- **target**: https://b.test")
        cor = "- **target**: https://b.test\n" + cor
        gen = "- **target**: https://a.test\n" + gen
        out = brain.learn_from_diff("cyber.web", gen, cor)
        kinds = sorted(i["kind"] for i in out["learned"])
        self.assertEqual(kinds, ["boilerplate_removed", "constraint_added",
                                 "slot_override"])
        # only ONE boilerplate event: the slot line must not double-count
        recs = brain.suggest()
        boiler = [r for r in recs if r["kind"] == "boilerplate_removed"]
        self.assertEqual(len(boiler), 1)

    def test_headings_and_checklist_lines_ignored(self):
        gen = _mktext()
        cor = gen.replace("## Task\n", "")
        cor = cor.replace("- [ ] [target] result names the target explicitly\n", "")
        out = brain.learn_from_diff("cyber.web", gen, cor)
        self.assertEqual(out["learned"], [])
        self.assertGreater(out["ignored"], 0)

    def test_identical_texts_learn_nothing(self):
        gen = _mktext()
        out = brain.learn_from_diff("cyber.web", gen, gen)
        self.assertEqual(out["learned"], [])


class TestThresholdsAndApply(BrainCase):
    def _promote_boilerplate(self, text, times):
        for _ in range(int(times)):
            brain.record("boilerplate_removed", "cyber.web", brain._norm(text), text)

    def test_below_threshold_suggest_only(self):
        victim = "4. hunt injection classes appropriate to the stack"
        self._promote_boilerplate(victim, brain.AUTO_APPLY - 1)
        pats = brain.suggest()
        self.assertEqual(pats[0]["status"], "suggest")
        prompt, rep = brain.apply("cyber.web", _mktext())
        self.assertEqual(rep["suppressed"], [])

    def test_at_threshold_suppresses_and_appends(self):
        victim = "4. hunt injection classes appropriate to the stack"
        self._promote_boilerplate(victim, int(brain.AUTO_APPLY))
        for _ in range(int(brain.AUTO_APPLY)):
            brain.record("constraint_added", "cyber.web",
                         brain._norm(GEN_PREFERENCE), GEN_PREFERENCE)
        prompt, rep = brain.apply("cyber.web", _mktext())
        self.assertTrue(rep["suppressed"])
        self.assertTrue(rep["added"])
        self.assertNotIn("hunt injection classes", prompt)
        self.assertIn("## Your preferences", prompt)
        self.assertIn("- [learned]", prompt)

    def test_no_learned_flag_is_noop(self):
        prompt, rep = brain.apply("cyber.web", _mktext(), use_learned=False)
        self.assertEqual(rep, {"suppressed": [], "added": [], "considered": 0})
        self.assertIn("hunt injection classes", prompt)

    def test_headings_never_suppressed(self):
        for _ in range(int(brain.AUTO_APPLY)):
            brain.record("boilerplate_removed", "cyber.web",
                         brain._norm("## Task"), "## Task")
        prompt, rep = brain.apply("cyber.web", _mktext())
        self.assertEqual(rep["suppressed"], [])
        self.assertIn("## Task", prompt)

    def test_apply_idempotent_on_prefs_section(self):
        for _ in range(int(brain.AUTO_APPLY)):
            brain.record("constraint_added", "cyber.web",
                         brain._norm(GEN_PREFERENCE), GEN_PREFERENCE)
        once, _ = brain.apply("cyber.web", _mktext())
        twice, _ = brain.apply("cyber.web", once)
        self.assertEqual(once.count("## Your preferences"), 1)
        self.assertEqual(twice.count("## Your preferences"), 1)
        self.assertEqual(once, twice)


class TestForgetResetExport(BrainCase):
    def test_lifecycle(self):
        brain.record("constraint_added", "cyber.web", "always x", "always x")
        brain.record("constraint_added", "cyber.web", "always y", "always y")
        pid = brain.suggest()[-1]["id"]
        self.assertEqual(brain.forget(pid), 1)
        self.assertEqual(brain.forget(pid), 0)
        self.assertEqual(len(brain.suggest()), 1)
        self.assertEqual(brain.reset(), 1)
        self.assertEqual(brain.status()["patterns"], 0)
        self.assertIn("promptforge-brain-v1", brain.export_json())


class TestCompileWithBrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        brain.set_db(pathlib.Path(self.tmp.name) / "brain.db")
        self.chat = ("audit https://staging.example.com/login for sqli. "
                     "do not touch /billing")

    def tearDown(self):
        self.tmp.cleanup()

    def test_learned_slot_fills_absent_slot(self):
        sid = "cyber.web"
        for _ in range(int(brain.AUTO_APPLY)):
            brain.record("slot_override", sid, "target", "https://prod.example.com")
        res = forge.compile_prompt("audit the login flow for sqli")
        self.assertIn("prod.example.com", res["prompt"])

    def test_explicit_text_beats_learned_pref(self):
        sid = "cyber.web"
        for _ in range(int(brain.AUTO_APPLY)):
            brain.record("slot_override", sid, "target", "https://prod.example.com")
        res = forge.compile_prompt(self.chat)
        self.assertIn("staging.example.com", res["prompt"])

    def test_compile_no_template_universal_fallback(self):
        res = forge.compile_prompt("just chatting about lunch")
        self.assertEqual(res["report"]["spec_id"], "general")
        self.assertEqual(res["report"]["field"], "General")
        self.assertIn("## Role", res["prompt"])
        self.assertIn("lunch", res["prompt"])
        self.assertGreater(res["score"], 0)

    def test_compile_report_shape(self):
        res = forge.compile_prompt(self.chat)
        rep = res["report"]
        self.assertEqual(rep["spec_id"], "cyber.web")
        self.assertIn("inferred", rep)
        self.assertIn("learned", rep)
        self.assertGreaterEqual(res["score"], 85)
        self.assertNotIn("## Your preferences", res["prompt"])


if __name__ == "__main__":
    unittest.main()
