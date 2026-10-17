"""The optional AI model: the user's choice, validation, persistence, Ollama."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import unittest.mock

from companion import ai, model


class FakeOllama:
    """/api/tags and /api/chat; `answers` maps a sentence to the JSON content."""

    def __init__(self, models=("qwen3:4b",), answers=None, reject_think=False):
        outer = self
        self.models, self.answers, self.reject_think, self.chats = list(models), answers or {}, reject_think, []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply(200, {"models": [{"name": n, "size": 2_500_000_000,
                                             "details": {"parameter_size": "4.0B", "quantization_level": "Q4_K_M"}}
                                            for n in outer.models]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.chats.append(body)
                if body["model"] not in outer.models:
                    return self.reply(404, {"error": "model not found"})
                if outer.reject_think and "think" in body:
                    return self.reply(400, {"error": "unknown field think"})
                text = body["messages"][-1]["content"]
                content = outer.answers.get(text, '{"command": "unknown", "place": "", "field": 0}')
                self.reply(200, {"message": {"role": "assistant", "content": content}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def answer(command, place="", field=0):
    return json.dumps({"command": command, "place": place, "field": field})


GOOD = {text: answer(expected) for text, expected in ai.VALIDATION}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        self.settings = ai.Settings(self.folder / "settings.json")
        self.notices, self.menus = [], []

    def ollama(self, **kwargs):
        fake = FakeOllama(**kwargs)
        self.addCleanup(fake.close)
        return fake, ai.Ollama(fake.url, timeout=2)

    def make_ai(self, client, **kwargs):
        return ai.AI(self.settings, self.notices.append, show_menu=lambda: self.menus.append(1), ollama=client,
                     folder=self.folder, **kwargs)

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), deadline, "condition not reached")
            time.sleep(0.01)


class SettingsTests(Fixture):
    def test_no_choice_until_the_user_makes_one_and_it_is_remembered(self):
        self.assertIsNone(self.settings.ai())
        self.settings.set_ai({"provider": "ollama", "model": "qwen3:4b"})
        self.assertEqual(ai.Settings(self.settings.path).ai(), {"provider": "ollama", "model": "qwen3:4b"})
        self.settings.path.write_text("{not json")
        self.assertIsNone(self.settings.ai())                     # broken file: ask again, never guess
        self.settings.path.write_text(json.dumps({"ai": {"provider": "cloud"}}))
        self.assertIsNone(self.settings.ai())


class DiscoveryTests(Fixture):
    def test_the_list_shows_what_is_installed_then_downloads_then_no_ai(self):
        _, client = self.ollama(models=["qwen3:4b", "llama3.2:latest"])
        (self.folder / "llama").mkdir()
        (self.folder / "llama" / "llama-server.exe").write_bytes(b"")
        (self.folder / model.MANAGED["lightweight"]["file"]).write_bytes(b"")
        choices = ai.discover(client, self.folder)
        self.assertEqual([(c.provider, c.model) for c in choices],
                         [("ollama", "qwen3:4b"), ("ollama", "llama3.2:latest"), ("farmhand", "lightweight"),
                          ("install", "recommended"), ("none", "")])
        self.assertTrue(all(len(c.label) <= 60 for c in choices))
        self.assertIn("Q4_K_M", choices[0].label)
        self.assertIn("2.5 GB", choices[3].label)

    def test_without_ollama_or_models_the_choice_is_download_or_no_ai(self):
        choices = ai.discover(ai.Ollama("http://127.0.0.1:9", timeout=0.3), self.folder)
        self.assertEqual([(c.provider, c.model) for c in choices],
                         [("install", "lightweight"), ("install", "recommended"), ("none", "")])
        self.assertEqual(choices[-1].label, "Continue without AI")


class OllamaTests(Fixture):
    def test_requests_are_constrained_local_and_use_ollamas_own_runtime(self):
        fake, client = self.ollama(answers={"bring that back to the shed": answer("move_pair", "shed")})
        proposal = client.interpreter("qwen3:4b").interpret("bring that back to the shed")
        self.assertEqual(proposal, {"version": 1, "command": "move_pair", "target": {"type": "place", "name": "shed"}})
        body = fake.chats[0]
        self.assertEqual(body["format"], model.MODEL_SCHEMA)
        self.assertIs(body["think"], False)
        self.assertEqual(body["options"], {"temperature": 0})          # nothing about GPU layers: Ollama decides
        self.assertFalse(body["stream"])

    def test_an_older_ollama_without_think_still_works(self):
        fake, client = self.ollama(reject_think=True, answers={"how far along are you": answer("status")})
        self.assertEqual(client.interpreter("qwen3:4b").interpret("how far along are you")["command"], "status")
        self.assertEqual(["think" in c for c in fake.chats], [True, False])

    def test_a_vanished_model_is_reported_and_malformed_output_is_not_understood(self):
        fake, client = self.ollama(models=["other"], answers={"x": "nonsense"})
        interpreter = client.interpreter("qwen3:4b")
        self.assertIsNone(interpreter.interpret("bring it here"))
        self.assertTrue(interpreter.missing)
        fake.models.append("qwen3:4b")
        self.assertIsNone(client.interpreter("qwen3:4b").interpret("x"))


class ValidationTests(Fixture):
    def test_validation_checks_only_what_farmhand_needs(self):
        _, client = self.ollama(answers=GOOD)
        self.assertEqual(ai.validate(client.interpreter("qwen3:4b")), (True, "6_OF_6"))
        wrong = dict(GOOD, **{ai.VALIDATION[0][0]: answer("status"), ai.VALIDATION[1][0]: answer("status")})
        _, client = self.ollama(answers=wrong)
        self.assertEqual(ai.validate(client.interpreter("qwen3:4b")), (False, "ONLY_4_OF_6"))
        unsafe = dict(GOOD, **{"plow field 3": answer("move_pair", "", 3)})
        _, client = self.ollama(answers=unsafe)
        self.assertEqual(ai.validate(client.interpreter("qwen3:4b")), (False, "UNSAFE_ANSWER"))
        ticks = iter(range(0, 1000, 30))
        _, client = self.ollama(answers=GOOD)
        self.assertEqual(ai.validate(client.interpreter("qwen3:4b"), budget=60, clock=lambda: next(ticks)),
                         (False, "TOO_SLOW"))
        _, client = self.ollama(models=["other"])
        self.assertEqual(ai.validate(client.interpreter("qwen3:4b"))[0], False)


class ChoiceTests(Fixture):
    def test_continue_without_ai_is_saved_and_switches_ai_off(self):
        _, client = self.ollama()
        brain = self.make_ai(client)
        brain.open_menu()
        brain.choose(len(brain.menu))                              # the last line
        self.assertEqual(self.settings.ai(), {"provider": "none"})
        self.assertEqual(self.notices, ["AI_OFF"])
        self.assertFalse(brain.enabled)

    def test_a_validated_pick_is_saved_and_used(self):
        _, client = self.ollama(answers=GOOD)
        brain = self.make_ai(client)
        brain.open_menu()
        brain.choose(1)
        self.wait(lambda: self.notices[-1:] == ["AI_READY"])
        self.assertEqual(self.notices, ["AI_VALIDATING", "AI_READY"])
        self.assertEqual(self.settings.ai(), {"provider": "ollama", "model": "qwen3:4b"})
        self.assertTrue(brain.enabled)
        self.assertEqual(brain.interpret(ai.VALIDATION[2][0]), {"version": 1, "command": "status"})

    def test_a_failing_pick_is_never_saved_and_the_list_comes_back(self):
        _, client = self.ollama(models=["good:1b", "bad:1b"], answers=GOOD)
        self.settings.set_ai({"provider": "ollama", "model": "good:1b"})
        brain = self.make_ai(client)
        brain.start()
        brain.open_menu()
        client.interpreter = lambda name: (ai.OllamaInterpreter(client, name) if name == "good:1b"
                                           else type("Bad", (), {"interpret": lambda s, t: None, "missing": False})())
        brain.choose(2)                                            # bad:1b
        self.wait(lambda: self.notices[-1:] == ["AI_INCOMPATIBLE"])
        self.assertEqual(self.menus, [1])
        self.assertEqual(self.settings.ai(), {"provider": "ollama", "model": "good:1b"})   # unchanged
        self.assertTrue(brain.enabled)                                                   # the working one stays

    def test_installing_a_farmhand_model_then_validating_it(self):
        _, client = self.ollama(models=[])
        installs = []

        class Server:
            def __init__(self, folder, choice):
                self.choice, self.process = choice, type("P", (), {"poll": lambda s: None})()

            def start(self):
                return True

            def ready(self):
                return True

            def stop(self):
                pass

        brain = self.make_ai(client, server_factory=Server,
                             installer=lambda folder, choice: installs.append(choice))
        brain.open_menu()
        lightweight = next(n for n, c in enumerate(brain.menu, 1) if c.provider == "install" and c.model == "lightweight")
        with unittest.mock.patch.object(ai, "validate", lambda interpreter: (True, "6_OF_6")):
            brain.choose(lightweight)
            self.wait(lambda: self.notices[-1:] == ["AI_READY"])
        self.assertEqual(installs, ["lightweight"])
        self.assertEqual(self.notices, ["AI_INSTALLING", "AI_VALIDATING", "AI_READY"])
        self.assertEqual(self.settings.ai(), {"provider": "farmhand", "model": "lightweight"})

    def test_a_failed_download_changes_nothing(self):
        _, client = self.ollama(models=[])

        def broken(folder, choice):
            raise OSError("offline")

        brain = self.make_ai(client, installer=broken)
        brain.open_menu()
        brain.choose(1)
        self.wait(lambda: self.notices[-1:] == ["AI_INSTALL_FAILED"])
        self.assertIsNone(self.settings.ai())
        self.assertFalse(brain.enabled)

    def test_a_saved_model_that_disappears_is_reported_not_replaced(self):
        fake, client = self.ollama(models=["qwen3:4b", "other:1b"], answers=GOOD)
        self.settings.set_ai({"provider": "ollama", "model": "qwen3:4b"})
        brain = self.make_ai(client)
        brain.start()
        fake.models.remove("qwen3:4b")
        self.assertIsNone(brain.interpret("bring that back to the shed"))
        self.assertEqual(self.notices, ["AI_MODEL_MISSING"])
        self.assertEqual(self.settings.ai(), {"provider": "ollama", "model": "qwen3:4b"})
        self.assertEqual([c["model"] for c in fake.chats], ["qwen3:4b"])     # never another model

    def test_stale_or_out_of_range_picks_do_nothing(self):
        _, client = self.ollama()
        brain = self.make_ai(client)
        brain.open_menu()
        for index in (0, -1, 99):
            brain.choose(index)
        self.assertEqual(self.notices, [])
        self.assertIsNone(self.settings.ai())


if __name__ == "__main__":
    unittest.main()
