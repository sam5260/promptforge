import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import nextmsg  # noqa: E402

PY = sys.executable
NEXTMSG = str(ROOT / "nextmsg.py")
FIXTURES = ROOT / "fixtures"
HAVE_FIXTURES = (FIXTURES / "fx01.json").exists()


def run(*args, stdin=None):
    return subprocess.run([PY, NEXTMSG, *args], input=stdin,
                          capture_output=True, text=True, encoding="utf-8")


class TestSplitter(unittest.TestCase):
    def test_role_markers(self):
        turns = nextmsg.split_turns(
            "user: hello\nassistant: hi there\nuser: again")
        self.assertEqual([t["role"] for t in turns],
                         ["user", "assistant", "user"])
        self.assertEqual([t["index"] for t in turns], [1, 2, 3])
        self.assertEqual(turns[1]["text"], "hi there")

    def test_bracketed_markers(self):
        turns = nextmsg.split_turns("[User]: a\n[Assistant]: b")
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])

    def test_fence_is_opaque(self):
        text = "user: run this\n```\ndef f():\n    pass\n```\nassistant: done"
        turns = nextmsg.split_turns(text)
        self.assertEqual(len(turns), 2)
        self.assertIn("def f():", turns[0]["text"])

    def test_no_markers_single_user_turn(self):
        turns = nextmsg.split_turns("just some raw text paste")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["role"], "user")

    def test_empty_text(self):
        self.assertEqual(nextmsg.split_turns("   \n  "), [])


class TestStates(unittest.TestCase):
    def test_unresolved_error(self):
        card = nextmsg.analyze(
            "user: The build dies with TypeError: bad operand.\n"
            "assistant: I refactored the loader to handle it.")
        self.assertEqual(card["state"], "unresolved_error")

    def test_error_then_resolution_falls_through(self):
        card = nextmsg.analyze(
            "user: The build dies with TypeError: bad operand.\n"
            "assistant: Patched the loader. Tests pass now.")
        self.assertNotEqual(card["state"], "unresolved_error")
        self.assertEqual(card["state"], "continue")

    def test_unverified_work(self):
        card = nextmsg.analyze(
            "user: Add rate limiting to the login endpoint.\n"
            "assistant: Added a token bucket limiter in front of login.")
        self.assertEqual(card["state"], "unverified_work")

    def test_verified_work_not_flagged(self):
        card = nextmsg.analyze(
            "user: Add rate limiting to the login endpoint.\n"
            "assistant: Added the limiter. Tests pass, endpoint validated.")
        self.assertNotEqual(card["state"], "unverified_work")

    def test_open_question(self):
        card = nextmsg.analyze(
            "user: Pick a queue for jobs.\n"
            "assistant: Two options: RabbitMQ or SQS. Which one do you "
            "prefer?")
        self.assertEqual(card["state"], "open_question")

    def test_stated_next_step(self):
        card = nextmsg.analyze("user: Next: run the migration on staging.")
        self.assertEqual(card["state"], "stated_next_step")

    def test_continue(self):
        card = nextmsg.analyze("user: Continue.")
        self.assertEqual(card["state"], "continue")

    def test_big_new_task(self):
        card = nextmsg.analyze(
            "user: New task: rewrite the auth service in Go.")
        self.assertEqual(card["state"], "big_new_task")

    def test_big_new_task_beats_error(self):
        card = nextmsg.analyze(
            "user: The build dies with TypeError: bad operand.\n"
            "assistant: Still failing.\n"
            "user: New task: different project — write the docs.")
        self.assertEqual(card["state"], "big_new_task")


class TestDecisions(unittest.TestCase):
    def test_unresolved_options_yield_your_call_no_pick(self):
        card = nextmsg.analyze(
            "user: We need a cache. Keep it on-prem only.\n"
            "assistant: Two options: Postgres or MySQL, both self-hosted. "
            "Which one do you want to go with?")
        dec = card["decisions"][0]
        self.assertTrue(dec["available"])
        self.assertIsNone(dec["resolved_by_turn"])
        self.assertIsNone(dec["choice"])
        self.assertTrue(card["your_call"])
        self.assertIn("your call", card["message"].lower())

    def test_recency_resolution(self):
        card = nextmsg.analyze(
            "user: We need a cache.\n"
            "assistant: Redis or Memcached? Which one do you prefer?\n"
            "user: Let's use Redis.\n"
            "user: Actually let's do Memcached — fewer moving parts.")
        dec = card["decisions"][0]
        self.assertEqual(dec["choice"], "Memcached")
        self.assertEqual(dec["resolved_by_turn"], 4)
        self.assertFalse(card["your_call"])

    def test_no_question_no_decision(self):
        card = nextmsg.analyze(
            "user: Add caching.\n"
            "assistant: Done — tests pass.")
        self.assertFalse(card["decisions"][0]["available"])
        self.assertIsNone(card["decisions"][0]["choice"])


class TestConstraints(unittest.TestCase):
    def test_rule_clause_extracted_with_turn(self):
        card = nextmsg.analyze(
            "user: Deploy to staging. Staging only — never prod.")
        texts = [c["text"] for c in card["constraints_carried"]]
        self.assertTrue(any("never prod" in t for t in texts))
        self.assertTrue(all(c["turn"] == 1 for c in card["constraints_carried"]))

    def test_plain_request_not_a_constraint(self):
        card = nextmsg.analyze("user: Show me the dashboard.")
        self.assertEqual(card["constraints_carried"], [])


class TestViolations(unittest.TestCase):
    def test_ignored_ask_fires(self):
        card = nextmsg.analyze(
            "user: Fix the flaky checkout test.\n"
            "assistant: I updated the payment API client to stabilize "
            "retries.")
        kinds = [v["kind"] for v in card["violations"]]
        self.assertIn("ignored_ask", kinds)

    def test_broke_rule_fires(self):
        card = nextmsg.analyze(
            "user: Migrate the config to YAML. Don't rename any env vars, "
            "and show me the diff when you're done.\n"
            "assistant: Config now lives in config.yaml. I renamed the env "
            "vars DB_HOST to DATABASE_HOST for consistency.")
        kinds = {v["kind"] for v in card["violations"]}
        self.assertEqual(kinds, {"ignored_ask", "broke_rule"})

    def test_no_false_positive_when_ask_covered(self):
        card = nextmsg.analyze(
            "user: Add response caching to /api/inventory. Don't change the "
            "response shape — clients depend on it.\n"
            "assistant: Added an LRU cache in front of /api/inventory. I also "
            "tidied the serializer while I was in there.")
        self.assertEqual(card["violations"], [])

    def test_no_false_positive_pending_agent(self):
        card = nextmsg.analyze(
            "user: Plan looks good. Next: run the migration on staging, then "
            "show me the row counts. Staging only — never prod.\n"
            "assistant: Migration is queued and ready. Just say the word.")
        self.assertEqual(card["violations"], [])
        self.assertEqual(card["state"], "stated_next_step")

    def test_fresh_error_suppresses_asks(self):
        card = nextmsg.analyze(
            "user: Run the deploy script when ready.\n"
            "assistant: Still failing — the deploy errors out with "
            "ECONNREFUSED.")
        self.assertEqual(card["state"], "unresolved_error")
        self.assertEqual(card["violations"], [])


class TestScorerBar(unittest.TestCase):
    @unittest.skipUnless(HAVE_FIXTURES, "fixtures/ not present")
    def test_pass_bar_holds(self):
        rep = nextmsg.score_fixtures(FIXTURES)
        self.assertTrue(rep["passed"], json.dumps(rep["bar"], indent=1))
        self.assertGreaterEqual(rep["bar"]["intent"]["got"],
                                nextmsg.PASS_BAR["intent_min"])
        self.assertEqual(rep["bar"]["constraints"]["got"], rep["n"])
        self.assertEqual(rep["bar"]["invented"]["got"], 0)
        self.assertGreater(rep["bar"]["beat_continue"]["engine"],
                           rep["bar"]["beat_continue"]["continue"])

    @unittest.skipUnless(HAVE_FIXTURES, "fixtures/ not present")
    def test_violations_exact_on_every_fixture(self):
        rep = nextmsg.score_fixtures(FIXTURES)
        self.assertEqual(rep["violation_detection"]["fixtures_ok"],
                         rep["violation_detection"]["total"])


class TestCli(unittest.TestCase):
    def test_raw_transcript_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt",
                                         delete=False,
                                         encoding="utf-8") as f:
            f.write("user: Continue.")
            path = f.name
        r = run(path, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["state"], "continue")

    def test_fixture_json_transcript_extracted(self):
        payload = {"schema": 1, "id": "t",
                   "transcript": "user: Next: run the staging migration."}
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False,
                                         encoding="utf-8") as f:
            json.dump(payload, f)
            path = f.name
        r = run(path, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["state"], "stated_next_step")

    def test_stdin(self):
        r = run("-", stdin="user: New task: write the changelog.")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("STATE: big_new_task", r.stdout)

    @unittest.skipUnless(HAVE_FIXTURES, "fixtures/ not present")
    def test_score_exit_zero(self):
        r = run("--score", str(FIXTURES))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RESULT: PASS", r.stdout)

    def test_missing_source_errors(self):
        r = run()
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
