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

    def __init__(self, models=("qwen3:4b",), answers=None, reject_think=False, load_delay=0.0):
        outer = self
        self.models, self.answers, self.reject_think, self.chats = list(models), answers or {}, reject_think, []
        self.load_delay, self.loaded, self.loads = load_delay, set(), []

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
                if self.path == "/api/chat":
                    outer.chats.append(body)
                if body["model"] not in outer.models:
                    return self.reply(404, {"error": "model not found"})
                if body["model"] not in outer.loaded:          # a cold model loads first
                    time.sleep(outer.load_delay)
                    outer.loaded.add(body["model"])
                if self.path == "/api/generate":                # "Load a model"
                    outer.loads.append(body)
                    return self.reply(200, {"model": body["model"], "done": True})
                if outer.reject_think and "think" in body:
                    return self.reply(400, {"error": "unknown field think"})
                text = body["messages"][-1]["content"]
                content = outer.answers.get(text, '{"command": "unknown", "say": ""}')
                self.reply(200, {"message": {"role": "assistant", "content": content}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# The rules' wording for each command (companion/model.py PROMPT).
SAY = {"fetch_pair": "bring a tractor and the forage wagon here", "move_pair": "take this to the barn",
       "field_work": "plow field 3", "buy": "buy a large plow", "hitch_pair": "hitch the forage wagon",
       "haul_tractor": "use the gmc to take the deutz to field 3 and unload it"}


def answer(command, say=None):
    return json.dumps({"command": command, "say": SAY.get(command, "") if say is None else say})


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

    def llama(self, runtime=False, models=()):
        """The user's llama.cpp as found on this PC: a llama-server or not, and GGUF files."""
        for name in models:
            (self.folder / "gguf").mkdir(exist_ok=True)
            (self.folder / "gguf" / name).write_bytes(b"gguf")
        server = self.folder / "llama-server.exe"
        if runtime:
            server.write_bytes(b"")
        return model.LlamaCpp(which=lambda name: str(server) if runtime and name == "llama-server.exe" else None,
                              dirs=[self.folder / "gguf"], legacy=self.folder / "model")

    def make_ai(self, client, **kwargs):
        kwargs.setdefault("llama", self.llama())
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

    def test_an_earlier_farmhand_download_is_kept_as_the_users_own_llama_cpp_model(self):
        # Migration (the user, 2026-10-31): Farmhand no longer downloads models; a saved choice of
        # one it downloaded before is the same file, now found as the user's llama.cpp model.
        self.settings.set_ai({"provider": "farmhand", "model": "recommended"})
        self.assertEqual(self.settings.ai(), {"provider": "llamacpp", "model": str(
            self.folder / "model" / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")})
        self.settings.set_ai({"provider": "farmhand", "model": "something-else"})
        self.assertIsNone(self.settings.ai())                                 # ask again, never guess


    def test_farmhands_pay_defaults_to_the_hourly_giants_rate_and_keeps_the_players_choice(self):
        # The user, 2026-10-30: pay per hour, per job, per day, or none; the game's own wages too.
        self.assertEqual(self.settings.pay(), (1, 0.0))                       # per hour, GIANTS' rate
        self.settings.set_pay("day", 750)
        self.assertEqual(ai.Settings(self.settings.path).pay(), (3, 750.0))
        for mode, code in (("none", 0), ("hour", 1), ("job", 2), ("day", 3), ("game", 4)):
            self.settings.set_pay(mode, 10)
            self.assertEqual(self.settings.pay()[0], code)
        with self.assertRaises(ValueError):
            self.settings.set_pay("week", 10)
        with self.assertRaises(ValueError):
            self.settings.set_pay("hour", -5)
        self.settings.path.write_text(json.dumps({"pay": {"mode": "hour", "amount": "lots"}}))
        self.assertEqual(self.settings.pay(), (1, 0.0))                       # unreadable: the default


class DiscoveryTests(Fixture):
    def test_the_list_shows_only_what_the_user_installed_then_no_ai(self):
        _, client = self.ollama(models=["qwen3:4b", "llama3.2:latest"])
        llama = self.llama(runtime=True, models=("Qwen3-4B-Q4_K_M.gguf", "mmproj-Qwen3-4B-f16.gguf"))
        choices = ai.discover(client, self.folder, llama)
        self.assertEqual([(c.provider, c.model) for c in choices],
                         [("ollama", "qwen3:4b"), ("ollama", "llama3.2:latest"),
                          ("llamacpp", str(self.folder / "gguf" / "Qwen3-4B-Q4_K_M.gguf")), ("none", ""), ("voice", "")])
        self.assertTrue(all(len(c.label) <= 60 for c in choices))
        self.assertIn("Q4_K_M", choices[0].label)
        self.assertEqual(choices[2].label, "Qwen3-4B-Q4_K_M (llama.cpp, 0.0 GB)")
        self.assertFalse(any(word in c.label.lower() for c in choices[:-1] for word in ("install", "recommended")))

    def test_gguf_models_need_the_users_llama_server(self):
        _, client = self.ollama(models=[])
        choices = ai.discover(client, self.folder, self.llama(runtime=False, models=("a.gguf",)))
        self.assertEqual([c.provider for c in choices][:2], ["missing", "none"])

    def test_with_nothing_installed_the_list_says_what_to_install_and_picking_it_tells_again(self):
        brain = self.make_ai(ai.Ollama("http://127.0.0.1:9", timeout=0.3))
        choices = brain.open_menu()
        self.assertEqual([(c.provider, c.label) for c in choices][:2],
                         [("missing", "No AI found: install Ollama or llama.cpp and a model"),
                          ("none", "Continue without AI")])
        brain.choose(1)
        self.assertEqual(self.notices, ["AI_NONE_INSTALLED"])
        self.assertIsNone(self.settings.ai())
        self.assertFalse(brain.busy)

    def test_speech_recognition_is_offered_only_while_missing_and_installs_from_the_menu(self):
        from companion import voice
        _, client = self.ollama(models=[])
        installs = []
        brain = self.make_ai(client, speech_installer=installs.append)
        brain.open_menu()
        brain.choose(len(brain.menu))                       # the speech line
        self.wait(lambda: self.notices[-1:] == ["VOICE_READY"])
        self.assertEqual(installs, [self.folder / "whisper-base.en"])
        self.assertIsNone(self.settings.ai())               # the AI choice is untouched
        for name in voice.WHISPER_FILES:
            (self.folder / "whisper-base.en").mkdir(exist_ok=True)
            (self.folder / "whisper-base.en" / name).write_bytes(b"")
        self.assertNotIn("voice", [c.provider for c in ai.discover(client, self.folder, self.llama())])


class OllamaTests(Fixture):
    def test_requests_are_constrained_local_and_use_ollamas_own_runtime(self):
        fake, client = self.ollama(answers={"bring that back to the shed": answer("move_pair", "take this to the shed")})
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
    def test_a_slow_cold_load_is_not_counted_as_wrong_answers(self):
        # Live: a 7B Ollama model failed ONLY_4_OF_6 with two timeouts; the load had
        # counted against the 10 s per-sentence limit. Loaded first, the same six
        # sentences are judged on their answers.
        fake, client = self.ollama(models=["big:7b"], answers=GOOD, load_delay=1.2)
        cold = ai.Ollama(fake.url, timeout=0.5)
        self.assertIsNone(cold.interpreter("big:7b").interpret(ai.VALIDATION[0][0]))   # the cold load times out
        fake.loaded.clear()
        brain = self.make_ai(ai.Ollama(fake.url, timeout=0.5))
        self.assertEqual(brain._validate(brain.ollama.interpreter("big:7b")), (True, "7_OF_7"))
        self.assertEqual(fake.loads[-1]["keep_alive"], ai.KEEP_ALIVE)
        self.assertTrue(all(c["keep_alive"] == ai.KEEP_ALIVE for c in fake.chats))

    def test_a_model_that_does_not_load_is_incompatible(self):
        _, client = self.ollama(models=["other"])
        brain = self.make_ai(client)
        self.assertEqual(brain._validate(client.interpreter("missing:7b")), (False, "MODEL_DID_NOT_LOAD"))

    def test_validation_checks_only_what_farmhand_needs(self):
        _, client = self.ollama(answers=GOOD)
        self.assertEqual(ai.validate(client.interpreter("qwen3:4b")), (True, "7_OF_7"))
        wrong = dict(GOOD, **{ai.VALIDATION[0][0]: answer("status"), ai.VALIDATION[1][0]: answer("status")})
        _, client = self.ollama(answers=wrong)
        self.assertEqual(ai.validate(client.interpreter("qwen3:4b")), (False, "ONLY_5_OF_7"))
        unsafe = dict(GOOD, **{"tell me a joke about cows": answer("move_pair", "take this to field 3")})
        _, client = self.ollama(answers=unsafe)
        self.assertEqual(ai.validate(client.interpreter("qwen3:4b")), (False, "UNSAFE_ANSWER"))
        unsafe = dict(GOOD, **{"tell me a joke about cows": answer("hitch_pair")})   # hitching moves equipment too
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
        brain.choose([c.provider for c in brain.menu].index("none") + 1)
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

    def test_a_users_llama_cpp_model_is_validated_on_their_own_server_then_saved(self):
        _, client = self.ollama(models=[])
        started = []

        class Server:
            def __init__(self, runtime, gguf, port):
                started.append((runtime, gguf, port))
                self.port, self.process = port, type("P", (), {"poll": lambda s: None})()

            def start(self):
                return True

            def ready(self):
                return True

            def stop(self):
                pass

        llama = self.llama(runtime=True, models=("Phi-4-mini-Q4_K_M.gguf",))
        brain = self.make_ai(client, server_factory=Server, llama=llama)
        brain.open_menu()
        gguf = self.folder / "gguf" / "Phi-4-mini-Q4_K_M.gguf"
        with unittest.mock.patch.object(ai, "validate", lambda interpreter: (True, "7_OF_7")):
            brain.choose(1)
            self.wait(lambda: self.notices[-1:] == ["AI_READY"])
        self.assertEqual(started, [(self.folder / "llama-server.exe", gguf, model.PORT)])
        self.assertEqual(self.notices, ["AI_VALIDATING", "AI_READY"])
        self.assertEqual(self.settings.ai(), {"provider": "llamacpp", "model": str(gguf)})
        self.assertEqual(gguf.read_bytes(), b"gguf")                          # never changed

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

    def test_ollama_not_running_is_reported_once_in_a_while_and_never_switched(self):
        self.settings.set_ai({"provider": "ollama", "model": "qwen3:4b"})
        brain = self.make_ai(ai.Ollama("http://127.0.0.1:9", timeout=0.3))
        brain.start()
        for _ in range(3):
            self.assertIsNone(brain.interpret("haul that thing over"))
        self.assertEqual(self.notices, ["AI_UNREACHABLE"])
        self.assertEqual(self.settings.ai(), {"provider": "ollama", "model": "qwen3:4b"})

    def test_a_model_is_never_used_before_it_passed_validation(self):
        _, client = self.ollama(models=["good:1b", "new:1b"], answers=GOOD)
        self.settings.set_ai({"provider": "ollama", "model": "good:1b"})
        brain = self.make_ai(client)
        brain.start()
        release, used = threading.Event(), []
        real = client.interpreter

        class Slow:
            missing = unreachable = False

            def interpret(self, text):
                used.append(text)
                release.wait(5)
                return {"version": 1, "command": "fetch_pair"}

        client.interpreter = lambda name: Slow() if name == "new:1b" else real(name)
        brain.open_menu()
        brain.choose(2)                                           # new:1b, validating (slow)
        self.wait(lambda: used)
        self.assertEqual(brain.interpret(ai.VALIDATION[2][0])["command"], "status")   # good:1b still answers
        self.assertEqual(len(used), 1)                            # never the candidate
        release.set()
        self.wait(lambda: not brain.busy)
        self.assertEqual(self.notices[-1], "AI_INCOMPATIBLE")      # it said fetch for the joke
        self.assertEqual(self.settings.ai(), {"provider": "ollama", "model": "good:1b"})

    def test_continue_without_ai_during_a_validation_wins_and_busy_picks_are_reported(self):
        _, client = self.ollama(models=["slow:1b", "other:1b"])
        release = threading.Event()

        class Slow:
            missing = unreachable = False

            def interpret(self, text):
                release.wait(5)
                return None

        client.interpreter = lambda name: Slow()
        client.load = lambda name, timeout=0: True
        brain = self.make_ai(client)
        brain.open_menu()
        brain.choose(1)                                           # slow:1b, validating
        self.wait(lambda: "AI_VALIDATING" in self.notices)
        brain.choose(2)                                           # another pick while busy
        self.assertIn("AI_BUSY", self.notices)
        brain.choose([c.provider for c in brain.menu].index("none") + 1)
        release.set()
        self.wait(lambda: not brain.busy, timeout=60)
        self.assertEqual(self.settings.ai(), {"provider": "none"})
        self.assertNotIn("AI_READY", self.notices)
        self.assertFalse(brain.enabled)

    def test_the_menu_never_has_more_than_nine_lines(self):
        _, client = self.ollama(models=[f"m{n}:1b" for n in range(15)])
        choices = ai.discover(client, self.folder, self.llama(runtime=True, models=[f"g{n}.gguf" for n in range(5)]))
        self.assertLessEqual(len(choices), 9)
        self.assertIn("none", [c.provider for c in choices])
        from protocol.ipc import encode_menu
        encode_menu(1, "Farmhand AI model", [c.label for c in choices])       # never raises

    def test_a_saved_model_missing_at_start_is_kept_for_reporting(self):
        self.settings.set_ai({"provider": "llamacpp", "model": str(self.folder / "gone.gguf")})
        brain = self.make_ai(ai.Ollama("http://127.0.0.1:9", timeout=0.3), llama=self.llama(runtime=True))
        brain.start()
        self.assertEqual(brain.startup_problem, "AI_MODEL_MISSING")
        self.assertEqual(self.notices, [])                        # the game is not linked yet
        self.assertEqual(self.settings.ai(), {"provider": "llamacpp", "model": str(self.folder / "gone.gguf")})

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
