"""The optional local model layer, against a fake llama-server (no model, no network)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile

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
        server, interpreter = self.serve(lambda body: chat('{"command": "status", "place": "", "field": 0}'))
        self.assertEqual(interpreter.interpret("how far along are you"), {"version": 1, "command": "status"})
        path, body = server.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_schema", "schema": model.MODEL_SCHEMA})
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "how far along are you"})
        self.assertIn("fetch_pair", body["messages"][0]["content"])      # the schema is described

    def test_answers_map_to_validated_proposals(self):
        cases = [({"command": "fetch_pair", "place": "", "field": 0}, {"version": 1, "command": "fetch_pair"}),
                 ({"command": "fetch_pair", "place": " cow barn ", "field": 0},
                  {"version": 1, "command": "fetch_pair", "target": {"type": "place", "name": "cow barn"}}),
                 ({"command": "move_pair", "place": "", "field": 7},
                  {"version": 1, "command": "move_pair", "target": {"type": "field_entrance", "field": 7}}),
                 ({"command": "stop", "place": "barn", "field": 3}, {"version": 1, "command": "stop"}),
                 ({"command": "unknown", "place": "", "field": 0}, None)]
        for answer, expected in cases:
            with self.subTest(answer=answer):
                _, interpreter = self.serve(lambda body, a=answer: chat(json.dumps(a)))
                proposal = interpreter.interpret("something")
                self.assertEqual(proposal, expected)
                if proposal is not None:
                    self.assertTrue(COMMAND_VALIDATOR.is_valid(proposal))

    def test_anything_off_schema_or_broken_is_not_understood(self):
        replies = [chat("not json"), chat('{"command": "plow", "place": "", "field": 0}'),
                   chat('{"command": "fetch_pair"}'),
                   chat('{"command": "fetch_pair", "place": "", "field": 0, "vehicle_id": 11}'),
                   chat('{"command": "move_pair", "place": "' + "x" * 65 + '", "field": 0}'),
                   chat('{"command": "move_pair", "place": "bad\\u0007name", "field": 0}'),
                   (200, {"choices": []}), (200, b"<html>"), (500, {"error": "boom"})]
        for reply in replies:
            with self.subTest(reply=reply):
                _, interpreter = self.serve(lambda body, r=reply: r)
                self.assertIsNone(interpreter.interpret("bring it"))

    def test_slow_down_or_unready_model_is_not_understood_quickly(self):
        def slow(body):
            time.sleep(1.5)
            return chat('{"command": "status", "place": "", "field": 0}')

        server = FakeServer(slow)
        self.addCleanup(server.close)
        started = time.monotonic()
        self.assertIsNone(model.ModelInterpreter(server.url, timeout=0.3).interpret("hm"))
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertIsNone(model.ModelInterpreter("http://127.0.0.1:9", timeout=0.3).interpret("hm"))
        server2, interpreter = self.serve(lambda body: chat('{"command": "status", "place": "", "field": 0}'))
        interpreter.ready = lambda: False
        self.assertIsNone(interpreter.interpret("hm"))
        self.assertEqual(server2.requests, [])

    def test_rules_answer_before_the_model_and_stop_never_reaches_it(self):
        server, interpreter = self.serve(lambda body: chat('{"command": "status", "place": "", "field": 0}'))
        chain = ChainInterpreter(RuleInterpreter(), interpreter)
        self.assertEqual(chain.interpret("STOP"), {"version": 1, "command": "stop"})
        self.assertEqual(chain.interpret("bring me a tractor and forage wagon")["command"], "fetch_pair")
        self.assertEqual(server.requests, [])
        self.assertEqual(chain.interpret("how far along are you"), {"version": 1, "command": "status"})
        self.assertEqual(len(server.requests), 1)


class RuntimeTests(unittest.TestCase):
    def test_server_runs_cpu_only_local_and_small(self):
        command = model.server_command(Path("C:/fh/model"), 39231)
        self.assertTrue(command[0].endswith("llama-server.exe"))
        pairs = dict(zip(command[1::2], command[2::2]))
        self.assertEqual(pairs["-ngl"], "0")                 # default is auto: GPU unless told otherwise
        self.assertEqual(pairs["--device"], "none")
        self.assertEqual(pairs["--host"], "127.0.0.1")
        self.assertEqual(pairs["-np"], "1")
        self.assertLessEqual(int(pairs["-t"]), 4)
        self.assertIn("--no-webui", command)

    def test_not_installed_means_rules_only(self):
        with tempfile.TemporaryDirectory() as folder:
            started = []
            server = model.ModelServer(Path(folder), popen=lambda *a, **k: started.append(a))
            self.assertFalse(server.start())
            self.assertEqual(started, [])
            self.assertFalse(server.ready())

    def test_download_is_verified_before_use(self):
        data = b"model bytes" * 100
        artifact = {"url": "x", "file": "m.gguf", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        opener = lambda url, timeout: io.BytesIO(data)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "m.gguf"
            model.download(artifact, target, opener)
            self.assertEqual(target.read_bytes(), data)
            model.download(artifact, target, lambda *a, **k: self.fail("downloaded twice"))
            bad = {**artifact, "sha256": "0" * 64}
            other = Path(folder) / "n.gguf"
            with self.assertRaises(ValueError):
                model.download(bad, other, opener)
            self.assertFalse(other.exists() or other.with_name("n.gguf.part").exists())

    def test_archive_entries_cannot_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ("../evil.exe", "/abs.exe", "C:/x.exe", "a/../../b.exe"):
                with self.subTest(name=name):
                    archive = Path(folder) / "a.zip"
                    with zipfile.ZipFile(archive, "w") as zf:
                        zf.writestr(name, b"x")
                    with self.assertRaises(ValueError):
                        model.extract(archive, Path(folder) / "out")
            archive = Path(folder) / "ok.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("llama-server.exe", b"x")
            model.extract(archive, Path(folder) / "out")
            self.assertTrue((Path(folder) / "out" / "llama-server.exe").is_file())

    def test_pinned_artifacts_are_the_verified_ones(self):
        self.assertEqual(model.RUNTIME["sha256"], "f78eded047061a3f86372db426797400a2f1bb0ec37868d5927dd820eb3386c0")
        self.assertEqual(model.MODEL["sha256"], "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597")
        self.assertIn("/resolve/a06e946bb6b655725eafa393f4a9745d460374c9/", model.MODEL["url"])
        self.assertTrue(model.RUNTIME["url"].startswith("https://github.com/ggml-org/llama.cpp/releases/download/b11714/"))


if __name__ == "__main__":
    unittest.main()
