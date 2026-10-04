# v2 tests — context parser (deterministic, never raises)
import unittest

import context_parser
import forge

SPECS = forge.load_specs()


class TestRanking(unittest.TestCase):
    def test_web_chat_picks_cyber_web(self):
        text = "audit https://app.example.com/login for sqli and broken access control"
        hits = context_parser.rank_specs(text)
        self.assertEqual(hits[0][0], "cyber.web")

    def test_api_chat_picks_cyber_api(self):
        hits = context_parser.rank_specs("review this graphql api endpoint for bola")
        self.assertEqual(hits[0][0], "cyber.api")

    def test_osint_chat_picks_cyber_osint(self):
        hits = context_parser.rank_specs("passive osint recon on ACME Corp, whois and breach")
        self.assertEqual(hits[0][0], "cyber.osint")

    def test_model_chat_picks_aiml_model(self):
        hits = context_parser.rank_specs("fine-tune a 7B model, what epochs and hyperparameters?")
        self.assertEqual(hits[0][0], "aiml.model")

    def test_empty_and_unrelated_return_empty(self):
        self.assertEqual(context_parser.rank_specs(""), [])
        self.assertEqual(context_parser.rank_specs("tell me a joke about cats"), [])


class TestParse(unittest.TestCase):
    def test_parse_happy_path(self):
        text = ("audit https://staging.example.com/app/login — next.js + postgres, "
                "auth jwt. scope: test sqli only, do not touch /billing. "
                "always give a findings table.")
        ctx = context_parser.parse(text, specs=SPECS)
        self.assertEqual(ctx["spec_id"], "cyber.web")
        self.assertGreaterEqual(ctx["confidence"], 0.6)
        self.assertEqual(ctx["slots"]["target"]["value"],
                         "https://staging.example.com/app/login")
        self.assertIn("stack", ctx["slots"])
        self.assertIn("scope", ctx["slots"])
        pol = {c["polarity"] for c in ctx["constraints"]}
        self.assertIn("positive", pol)
        self.assertIn("negative", pol)

    def test_parse_never_raises(self):
        for junk in ["", "   \n\n  ", "\x00\x01", "🙂 emoji only", "a" * 50000]:
            ctx = context_parser.parse(junk, specs=SPECS)
            self.assertIn("spec_id", ctx)
            self.assertIn("slots", ctx)

    def test_forced_spec_id(self):
        ctx = context_parser.parse("whatever text", specs=SPECS,
                                   spec_id="cyber.web")
        self.assertEqual(ctx["spec_id"], "cyber.web")
        self.assertEqual(ctx["confidence"], 1.0)

    def test_bad_forced_spec_id_is_none(self):
        ctx = context_parser.parse("text", specs=SPECS, spec_id="no.such")
        self.assertIsNone(ctx["spec_id"])

    def test_kv_line_fills_slot(self):
        ctx = context_parser.parse("target: https://x.test/api\nobjects: users, orders",
                                   specs=SPECS, spec_id="cyber.api")
        self.assertEqual(ctx["slots"]["target"]["value"], "https://x.test/api")
        self.assertEqual(ctx["slots"]["objects"]["value"], "users, orders")

    def test_cve_lands_in_facts(self):
        ctx = context_parser.parse("analyse CVE-2024-12345 in the wild", specs=SPECS)
        self.assertTrue(any(f["value"].upper() == "CVE-2024-12345"
                            for f in ctx["facts"]))

    def test_ip_detected_when_no_url(self):
        ctx = context_parser.parse("scan 10.0.0.5 for open services", specs=SPECS,
                                   spec_id="cyber.network")
        self.assertIn("target", ctx["slots"])


class TestConstraints(unittest.TestCase):
    def test_polarity_split(self):
        out = context_parser.extract_constraints(
            "never use outdated libraries. always keep diffs minimal. hello there friend")
        pol = [c["polarity"] for c in out]
        self.assertIn("negative", pol)
        self.assertIn("positive", pol)
        self.assertEqual(len(out), 2)

    def test_tiny_and_huge_sentences_skipped(self):
        out = context_parser.extract_constraints("ok. " + "x" * 400)
        self.assertEqual(out, [])


class TestGuessField(unittest.TestCase):
    def test_design_bucket(self):
        slug, label = context_parser.guess_field(
            "make a marvel themed pdf invite for my event, 11 pages")
        self.assertEqual(slug, "design")
        self.assertEqual(label, "Design")

    def test_development_bucket(self):
        slug, _ = context_parser.guess_field(
            "write a python script that parses csv files")
        self.assertEqual(slug, "development")

    def test_general_fallback(self):
        slug, _ = context_parser.guess_field("just chatting about lunch")
        self.assertEqual(slug, "general")

    def test_empty_text_never_raises(self):
        for junk in ["", "   ", "\x00"]:
            slug, _ = context_parser.guess_field(junk)
            self.assertEqual(slug, "general")

    def test_matched_template_label_in_parse(self):
        ctx = context_parser.parse(
            "audit https://app.example.com/login for sqli", specs=SPECS)
        self.assertEqual(ctx["field"], "Web Application Pentest")

    def test_unmatched_text_falls_back_to_bucket_label(self):
        ctx = context_parser.parse("just chatting about lunch", specs=SPECS)
        self.assertIsNone(ctx["spec_id"])
        self.assertEqual(ctx["field"], "General")

    def test_unmatched_design_text_gets_design_label(self):
        ctx = context_parser.parse(
            "make a marvel themed pdf invite for my event", specs=SPECS)
        if ctx["spec_id"] is None:
            self.assertEqual(ctx["field"], "Design")


if __name__ == "__main__":
    unittest.main()
