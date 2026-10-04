"""API tests for website.py — same-engine web surface."""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import brain
import forge
import website


class TestWebsiteApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pfweb_"))
        cls.brain_db = cls.tmp / "brain.db"
        brain.set_db(cls.brain_db)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         website.make_handler(cls.tmp))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read()
            e.close()
            try:
                return e.code, json.loads(raw.decode("utf-8"))
            except ValueError:
                return e.code, {"ok": False, "error": raw.decode("utf-8")}

    def test_01_index_page(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            self.assertIn("PromptForge", body)

    def test_02_fields(self):
        st, j = self.req("/api/fields")
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        ids = [f["id"] for f in j["fields"]]
        self.assertIn("cyber.web", ids)
        self.assertGreaterEqual(len(ids), 9)
        self.assertIn("role", j["struct_labels"])

    def test_03_spec(self):
        st, j = self.req("/api/spec?id=cyber.web")
        self.assertEqual(st, 200)
        self.assertTrue(any(s.get("required")
                            for s in j["spec"]["slots"]))
        self.assertNotIn("_file", j["spec"])  # private keys stripped
        st, j = self.req("/api/spec?id=nope.nope")
        self.assertEqual(st, 400)
        self.assertIn("unknown field", j["error"])

    def test_04_gen(self):
        st, j = self.req("/api/gen", {"field": "cyber.web",
                                      "slots": {"target": "example.com"},
                                      "mode": "full", "yes": True})
        self.assertEqual(st, 200)
        self.assertIn("# ", j["prompt"])
        self.assertIn("## Methodology", j["prompt"])
        self.assertEqual(j["payload"]["field"], "cyber.web")
        self.assertEqual(j["values"]["target"], "example.com")

    def test_05_gen_strict_fails_on_placeholder(self):
        st, j = self.req("/api/gen", {"field": "cyber.web", "slots": {},
                                      "yes": True, "strict": True})
        self.assertEqual(st, 400)
        self.assertIn("strict", j["error"])

    def test_06_gen_missing_slot_without_placeholders(self):
        st, j = self.req("/api/gen", {"field": "cyber.web", "slots": {},
                                      "yes": False})
        self.assertEqual(st, 400)
        self.assertIn("missing required slot", j["error"])

    def test_07_check(self):
        st, j = self.req("/api/check", {"field": "cyber.web", "text":
                                        "# x\n\n## Role\nr\n"})
        self.assertEqual(st, 200)
        self.assertIn("score", j["result"])
        self.assertTrue(j["result"]["items"])
        st, j = self.req("/api/check", {"field": "cyber.web", "text": "  "})
        self.assertEqual(st, 400)

    def test_08_fix(self):
        text = "# weak\n\nno structure here\n"
        st, j = self.req("/api/fix", {"field": "cyber.web", "text": text,
                                      "slots": {}})
        self.assertEqual(st, 200)
        self.assertLessEqual(j["before"], j["after"])
        self.assertIn("prompt", j)
        self.assertIn("report", j)

    def test_09_compare(self):
        st, j = self.req("/api/compare", {"field": "cyber.web",
                                          "a": "# a\n## Role\nx\n",
                                          "b": "b"})
        self.assertEqual(st, 200)
        self.assertIn(j["winner"], ("A", "B", "tie"))
        st, j = self.req("/api/compare", {"field": "cyber.web",
                                          "a": "", "b": "x"})
        self.assertEqual(st, 400)

    def test_10_merge(self):
        st, j = self.req("/api/merge", {"field": "cyber.web",
                                        "a": "# a\n## Role\nx\n",
                                        "b": "## Task\ndo\n", "slots": {}})
        self.assertEqual(st, 200)
        self.assertIn("merged", j)
        self.assertIn("took_from", j["meta"])

    def test_11_ensemble(self):
        st, j = self.req("/api/ensemble", {"field": "cyber.web",
                                           "text": "# base\n\n## Role\nx\n",
                                           "attempts": 4, "top": 2,
                                           "seed": 7, "slots": {}})
        self.assertEqual(st, 200)
        self.assertLessEqual(len(j["top"]), 2)
        self.assertTrue(j["log"])
        st, j = self.req("/api/ensemble", {"field": "cyber.web",
                                           "text": "x", "top": 0})
        self.assertEqual(st, 400)

    def test_12_lint(self):
        st, j = self.req("/api/lint", {})
        self.assertEqual(st, 200)
        self.assertEqual(j["errors"], [])
        self.assertGreaterEqual(j["count"], 9)

    def test_15_save(self):
        st, j = self.req("/api/save", {"path": "out/new.txt",
                                       "text": "hello"})
        self.assertEqual(st, 200)
        self.assertTrue((self.tmp / "out" / "new.txt").is_file())
        st, j = self.req("/api/save", {"path": "../evil.txt", "text": "x"})
        self.assertEqual(st, 404)
        st, j = self.req("/api/save", {"path": "", "text": "x"})
        self.assertEqual(st, 400)

    def test_16_unknown_route_and_field(self):
        st, j = self.req("/api/nope")
        self.assertEqual(st, 404)
        self.assertFalse(j["ok"])
        st, j = self.req("/api/gen", {"field": "bad.field", "slots": {}})
        self.assertEqual(st, 400)
        self.assertIn("unknown field", j["error"])

    CHAT = ("audit https://staging.example.com/app/login for sqli. "
            "scope: only test login, do not touch /billing")

    def test_17_parse(self):
        st, j = self.req("/api/parse", {"text": self.CHAT})
        self.assertEqual(st, 200)
        ctx = j["ctx"]
        self.assertEqual(ctx["spec_id"], "cyber.web")
        self.assertIn("target", ctx["slots"])
        st, j = self.req("/api/parse", {"text": "   "})
        self.assertEqual(st, 400)

    def test_18_compile(self):
        st, j = self.req("/api/compile", {"text": self.CHAT})
        self.assertEqual(st, 200)
        self.assertIn("staging.example.com", j["prompt"])
        self.assertGreaterEqual(j["score"], 85)
        self.assertEqual(j["report"]["spec_id"], "cyber.web")
        self.assertEqual(j["report"]["field"], "Web Application Pentest")
        # no template match -> universal build (any field, never 400)
        st, j = self.req("/api/compile",
                         {"text": "just chatting about lunch"})
        self.assertEqual(st, 200)
        self.assertEqual(j["report"]["spec_id"], "general")
        self.assertEqual(j["report"]["field"], "General")
        self.assertIn("lunch", j["prompt"])
        st, j = self.req("/api/compile", {"text": self.CHAT, "spec": "cyber.api"})
        self.assertEqual(st, 200)
        self.assertEqual(j["report"]["spec_id"], "cyber.api")

    def test_18b_check_and_compare_without_field(self):
        st, j = self.req("/api/check", {"text": "just chatting about lunch"})
        self.assertEqual(st, 200)
        self.assertIn("score", j["result"])
        st, j = self.req("/api/compare",
                         {"a": "one prompt", "b": "another prompt"})
        self.assertEqual(st, 200)
        self.assertIn(j["winner"], ("A", "B", "tie"))

    def test_18c_check_design_text_without_field(self):
        st, j = self.req("/api/check",
                         {"text": "make a marvel themed pdf invite for my event"})
        self.assertEqual(st, 200)
        self.assertIn("design", j["title"])

    def test_19_learn_and_brain(self):
        st, j = self.req("/api/compile", {"text": self.CHAT})
        gen = j["prompt"]
        cor = "Always show tool versions.\n" + gen
        st, j = self.req("/api/learn", {"field": "cyber.web",
                                        "generated": gen, "corrected": cor})
        self.assertEqual(st, 200)
        self.assertTrue(j["summary"]["learned"])
        st, j = self.req("/api/learn", {"field": "cyber.web",
                                        "generated": gen, "corrected": gen})
        self.assertEqual(st, 400)
        st, j = self.req("/api/brain?action=show")
        self.assertEqual(st, 200)
        self.assertTrue(j["patterns"])
        pid = j["patterns"][0]["id"]
        st, j = self.req("/api/brain/action", {"action": "forget", "id": pid})
        self.assertEqual(st, 200)
        self.assertEqual(j["removed"], 1)
        st, j = self.req("/api/brain/action", {"action": "reset"})
        self.assertEqual(st, 400)  # refuses without yes:true
        st, j = self.req("/api/brain/action", {"action": "reset", "yes": True})
        self.assertEqual(st, 200)
        self.assertEqual(j["status"]["patterns"], 0)
        st, j = self.req("/api/brain?action=export")
        self.assertEqual(st, 200)
        self.assertIn("promptforge-brain-v1", j["export"])
        st, j = self.req("/api/brain?action=nope")
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
