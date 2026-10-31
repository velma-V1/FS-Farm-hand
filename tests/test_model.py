"""The optional local model layer, against a fake llama-server (no model, no network)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from companion import model
from companion.coordinator import COMMAND_VALIDATOR
from companion.language import ChainInterpreter, RuleInterpreter


class FakeServer:
    """Answers /v1/chat/completions with whatever `reply` returns for the request."""

    def __init__(self, reply):
        outer = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                status, payload = reply(body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def chat(content):
    return 200, {"choices": [{"message": {"role": "assistant", "content": content}}]}


class ModelInterpreterTests(unittest.TestCase):
    def serve(self, reply):
        server = FakeServer(reply)
        self.addCleanup(server.close)
        return server, model.ModelInterpreter(server.url, timeout=2)

    def test_request_is_constrained_deterministic_and_local(self):
        server, interpreter = self.serve(lambda body: chat('{"command": "status", "say": ""}'))
        self.assertEqual(interpreter.interpret("how far along are you"), {"version": 1, "command": "status"})
        path, body = server.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_schema", "schema": model.MODEL_SCHEMA})
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "how far along are you"})
        self.assertIn("fetch_pair", body["messages"][0]["content"])      # the schema is described

    def test_every_capability_is_reached_through_the_rules_wording(self):
        # The completion plan, stage 2: different phrasings reach the same supported
        # workflow. The model only rewrites; the rules read the result.
        rules = RuleInterpreter()
        cases = [("fetch_pair", "bring a tractor and the forage wagon here"),
                 ("fetch_pair", "bring the cultivator to the cow barn"),
                 ("fetch_pair", "bring the plow to field 3"),
                 ("fetch_pair", "bring the 1124 trailer here with the gmc"),
                 ("move_pair", "take this to the farm silo"),
                 ("move_pair", "come here"),
                 ("hitch_pair", "hitch the 980 plow"),
                 ("hitch_pair", "hitch the gooseneck with the gmc"),
                 ("detach_pair", "unhitch the seeder"),
                 ("field_work", "plow and seed a small area of field 1 anyway"),
                 ("field_work", "cultivate the smallest field"),
                 ("buy", "buy the gmc truck and a gooseneck"),
                 ("save_game", "save the game")]
        for command, say in cases:
            with self.subTest(say=say):
                _, interpreter = self.serve(lambda body, c=command, w=say: chat(json.dumps({"command": c, "say": w})))
                proposal = interpreter.interpret(say)         # grounded: the player said these words
                self.assertEqual(proposal, rules.interpret(say))
                self.assertEqual(proposal["command"], command)
                self.assertTrue(COMMAND_VALIDATOR.is_valid(proposal))
        _, interpreter = self.serve(lambda body: chat(json.dumps({"command": "status", "say": "bring it here"})))
        self.assertEqual(interpreter.interpret("x"), {"version": 1, "command": "status"})
        # STOP is the rules' alone (scripts/model_eval.py, Qwen3 1.7B: "don't sell the grain yet"
        # came back as stop): a model's stop is dropped, never cancelling the player's work.
        _, interpreter = self.serve(lambda body: chat(json.dumps({"command": "stop", "say": ""})))
        self.assertIsNone(interpreter.interpret("don't sell the grain yet"))
        # "hitch it and bring it to the barn": the rules make it a fetch to that place.
        _, interpreter = self.serve(lambda body: chat(json.dumps({"command": "hitch_pair",
                                                                  "say": "bring the cultivator to the barn"})))
        self.assertEqual(interpreter.interpret("x")["command"], "fetch_pair")

    def test_a_rewrite_the_rules_cannot_read_or_read_otherwise_is_not_understood(self):
        answers = [{"command": "buy", "say": "plow field 3"},                 # the rules read field work
                   {"command": "field_work", "say": "plough the north bit"},   # not a form the rules know
                   {"command": "fetch_pair", "say": "don't bring the tractor"},
                   {"command": "move_pair", "say": "what are you doing"},
                   {"command": "unknown", "say": "come here"},
                   {"command": "fetch_pair", "say": ""}]
        for answer in answers:
            with self.subTest(answer=answer):
                _, interpreter = self.serve(lambda body, a=answer: chat(json.dumps(a)))
                self.assertIsNone(interpreter.interpret("something"))

    def test_anything_off_schema_or_broken_is_not_understood(self):
        replies = [chat("not json"), chat('{"command": "plow", "say": "plow field 3"}'),
                   chat('{"command": "fetch_pair"}'),
                   chat('{"command": "fetch_pair", "say": "come here", "vehicle_id": 11}'),
                   chat('{"command": "move_pair", "say": "' + "x" * 161 + '"}'),
                   chat('{"command": "move_pair", "place": "barn", "field": 0}'),
                   (200, {"choices": []}), (200, b"<html>"), (500, {"error": "boom"})]
        for reply in replies:
            with self.subTest(reply=reply):
                _, interpreter = self.serve(lambda body, r=reply: r)
                self.assertIsNone(interpreter.interpret("bring it"))

    def test_slow_down_or_unready_model_is_not_understood_quickly(self):
        def slow(body):
            time.sleep(1.5)
            return chat('{"command": "status", "say": ""}')

        server = FakeServer(slow)
        self.addCleanup(server.close)
        started = time.monotonic()
        self.assertIsNone(model.ModelInterpreter(server.url, timeout=0.3).interpret("hm"))
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertIsNone(model.ModelInterpreter("http://127.0.0.1:9", timeout=0.3).interpret("hm"))
        server2, interpreter = self.serve(lambda body: chat('{"command": "status", "say": ""}'))
        interpreter.ready = lambda: False
        self.assertIsNone(interpreter.interpret("hm"))
        self.assertEqual(server2.requests, [])

    def test_rules_answer_before_the_model_and_stop_never_reaches_it(self):
        server, interpreter = self.serve(lambda body: chat('{"command": "status", "say": ""}'))
        chain = ChainInterpreter(RuleInterpreter(), interpreter)
        self.assertEqual(chain.interpret("STOP"), {"version": 1, "command": "stop"})
        self.assertEqual(chain.interpret("bring me a tractor and forage wagon")["command"], "fetch_pair")
        self.assertEqual(server.requests, [])
        self.assertEqual(chain.interpret("how far along are you"), {"version": 1, "command": "status"})
        self.assertEqual(len(server.requests), 1)


class RuntimeTests(unittest.TestCase):
    def test_server_runs_cpu_only_local_and_small(self):
        command = model.server_command(Path("C:/tools/llama-server.exe"), Path("D:/models/x.gguf"), 39231)
        self.assertEqual(command[:3], [str(Path("C:/tools/llama-server.exe")), "-m", str(Path("D:/models/x.gguf"))])
        pairs = dict(zip(command[1::2], command[2::2]))
        self.assertEqual(pairs["-ngl"], "0")                 # default is auto: GPU unless told otherwise
        self.assertEqual(pairs["--device"], "none")
        self.assertEqual(pairs["--host"], "127.0.0.1")
        self.assertEqual(pairs["-np"], "1")
        self.assertLessEqual(int(pairs["-t"]), 4)
        self.assertIn("--no-webui", command)

    @unittest.skipUnless(sys.platform == "win32", "Windows Job Object")
    def test_the_server_dies_with_the_companion(self):
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        job = model.bind_to_this_process(child)
        self.assertIsNone(child.poll())
        model.close_job(job)            # what Windows does when the companion ends, however it ends
        child.wait(timeout=10)
        self.assertIsNotNone(child.returncode)

    def test_an_occupied_port_keeps_the_model_off(self):
        started = []
        with tempfile.TemporaryDirectory() as folder:
            runtime, gguf = Path(folder) / "llama-server.exe", Path(folder) / "m.gguf"
            runtime.write_bytes(b"")
            gguf.write_bytes(b"")
            ok = type("R", (), {"status": 200, "__enter__": lambda s: s, "__exit__": lambda s, *a: None})()
            server = model.ModelServer(runtime, gguf, opener=lambda *a, **k: ok,
                                       popen=lambda *a, **k: started.append(a))
            self.assertFalse(server.start())
            self.assertEqual(started, [])

    def test_no_llama_cpp_or_no_model_means_rules_only(self):
        with tempfile.TemporaryDirectory() as folder:
            started = []
            for runtime in (None, Path(folder) / "llama-server.exe"):
                server = model.ModelServer(runtime, Path(folder) / "m.gguf", popen=lambda *a, **k: started.append(a))
                self.assertFalse(server.start())
                self.assertFalse(server.ready())
            self.assertEqual(started, [])


class LlamaCppDiscoveryTests(unittest.TestCase):
    """Farmhand downloads no model and no runtime (the user, 2026-10-31): it finds the user's own
    llama-server and GGUF models, in the folders llama.cpp and LM Studio use and in an earlier
    Farmhand's download folder, and only reads them."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def put(self, *parts):
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"gguf")
        return path

    def test_models_are_found_in_every_folder_and_nothing_is_changed(self):
        lm = self.put("lmstudio", "lmstudio-community", "Qwen3-4B-GGUF", "Qwen3-4B-Q4_K_M.gguf")
        self.put("lmstudio", "lmstudio-community", "Qwen3-4B-GGUF", "mmproj-Qwen3-4B-f16.gguf")   # no language model
        cache = self.put("cache", "bartowski_Phi-4-mini-Q4_K_M.gguf")
        legacy = self.put("legacy", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")
        self.put("cache", "notes.txt")
        before = sorted((p, p.stat().st_mtime_ns) for p in self.root.rglob("*"))
        llama = model.LlamaCpp(which=lambda name: None, legacy=self.root / "legacy",
                               dirs=[self.root / "missing", self.root / "cache", self.root / "lmstudio",
                                     self.root / "legacy", self.root / "legacy"])
        self.assertEqual(llama.models(), [cache, legacy, lm])        # by name, each once
        self.assertEqual(sorted((p, p.stat().st_mtime_ns) for p in self.root.rglob("*")), before)

    def test_the_runtime_is_the_users_on_path_else_an_earlier_farmhand_download(self):
        legacy_server = self.put("legacy", "llama", "llama-server.exe")
        on_path = model.LlamaCpp(which=lambda name: "C:/llama/llama-server.exe" if name == "llama-server.exe" else None,
                                 legacy=self.root / "legacy")
        self.assertEqual(on_path.runtime(), Path("C:/llama/llama-server.exe"))
        self.assertEqual(model.LlamaCpp(which=lambda name: None, legacy=self.root / "legacy").runtime(), legacy_server)
        self.assertIsNone(model.LlamaCpp(which=lambda name: None, legacy=self.root / "none").runtime())

    def test_farmhand_carries_no_model_url_or_downloader(self):
        source = (Path(__file__).resolve().parents[1] / "companion" / "model.py").read_text()
        for gone in ("https://", "sha256", "def download", "def install(", "MANAGED", "recommended (", "zipfile"):
            self.assertNotIn(gone, source)


if __name__ == "__main__":
    unittest.main()


class GroundingTests(unittest.TestCase):
    """A model's rewording counts only when it is in the player's own words (scripts/model_eval.py,
    Qwen3 4B: "collect the straw on field 5 with the forage wagon" came back "cultivate field 5")."""

    def test_invented_operations_fields_and_names_are_dropped(self):
        from companion.model import to_proposal
        cases = ((("field_work", "cultivate field 5"), "collect the straw on field 5 with the forage wagon", False),
                 (("field_work", "cultivate and seed field 4"), "get field 4 cultivated and drilled with barley", True),
                 (("field_work", "plow field 7"), "plow field 3", False),
                 (("haul_tractor", "use the gmc to take the deutz to field 3 and unload it"),
                  "put the deutz on the gmc's trailer and drop it off at field 3", True),
                 (("haul_tractor", "use the 379 to take the deutz to field 3 and unload it"),
                  "put the deutz on the gmc's trailer and drop it off at field 3", False),
                 # Qwen3 1.7B: the straw sentence came back "unload the combine".
                 (("unload_combine", "unload the combine"), "get the loading wagon out and bring the straw in off field 5",
                  False),
                 (("unload_combine", "unload the combine"), "the harvester's full of wheat, empty it", True),
                 (("field_work", "collect the straw on field 5"),
                  "get the loading wagon out and bring the straw in off field 5", True))
        for (command, say), text, kept in cases:
            with self.subTest(text=text, say=say):
                got = to_proposal({"command": command, "say": say}, text)
                self.assertEqual(got is not None, kept, got)
