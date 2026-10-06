"""Prepared translation boundaries, using only local fake processes and sockets."""
import contextlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

if os.name == "nt":
    raise unittest.SkipTest("Unix preparation; Windows uses isolated exec")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/dev-lingo/scripts"))
import codex_prepared
import codex_provider
import prewarm

RESULT = {"explanation_label": "Explanation", "sentences": [
    {"english": "Can you take another look?", "explanation": ""}]}


class PreparedProcessTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.record = self.directory / "requests.jsonl"
        self.fixture = self.directory / "fake_app_server.py"
        self.fixture.write_text("""import json, sys
from pathlib import Path
record = Path(sys.argv[1])
for line in sys.stdin:
    message = json.loads(line)
    with record.open('a') as output:
        output.write(json.dumps(message) + '\\n')
    method = message.get('method')
    if 'id' not in message:
        continue
    if method == 'config/read':
        result = {'config': {'mcp_servers': {'quoted.name': {'command': 'never-run'}}}}
    elif method == 'thread/start':
        result = {'thread': {'id': 'fresh-empty-thread', 'ephemeral': True}}
    elif method == 'turn/start':
        result = {'turn': {'id': 'only-turn'}}
    else:
        result = {}
    print(json.dumps({'id': message['id'], 'result': result}), flush=True)
    if method == 'turn/start':
        answer = {'explanation_label': 'Explanation', 'sentences': [
            {'english': 'Can you take another look?', 'explanation': ''}]}
        item = {'type': 'agentMessage', 'text': json.dumps(answer)}
        print(json.dumps({'method': 'item/completed', 'params': {
            'threadId': 'fresh-empty-thread', 'item': item}}), flush=True)
        print(json.dumps({'method': 'turn/completed', 'params': {
            'threadId': 'fresh-empty-thread', 'turn': {
                'status': 'completed', 'items': [item]}}}), flush=True)
""")
        native_popen = subprocess.Popen
        self.commands = []

        def fake_popen(command, **kwargs):
            self.commands.append(command)
            return native_popen([sys.executable, str(self.fixture), str(self.record)], **kwargs)

        self.stack.enter_context(patch.object(codex_prepared, "codex_path", return_value=sys.executable))
        self.stack.enter_context(patch.object(codex_prepared.subprocess, "Popen", side_effect=fake_popen))
        self.stack.enter_context(patch.dict(os.environ, {"DEV_LINGO_MODEL": ""}))

    def requests(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def prepare(self):
        prepared = codex_prepared.PreparedTranslation()
        self.addCleanup(prepared.close)
        return prepared

    def test_preparation_submits_no_turn_and_one_process_accepts_only_one_prompt(self):
        prepared = self.prepare()
        requests = self.requests()
        self.assertEqual([request["method"] for request in requests],
                         ["initialize", "initialized", "config/read", "thread/start"])
        params = requests[-1]["params"]
        self.assertTrue(params["ephemeral"])
        self.assertEqual(params["model"], "gpt-6.1-sol")
        self.assertEqual(params["developerInstructions"], "")
        self.assertEqual(params["config"]["mcp_servers"]["quoted.name"]["enabled"], False)
        self.assertEqual(prepared.translate("FIRST_PRIVATE_PROMPT"), RESULT)
        with self.assertRaises(RuntimeError):
            prepared.translate("SECOND_PRIVATE_PROMPT")
        turns = [request for request in self.requests() if request["method"] == "turn/start"]
        self.assertEqual(len(turns), 1)
        self.assertIn("FIRST_PRIVATE_PROMPT", json.dumps(turns))
        self.assertNotIn("SECOND_PRIVATE_PROMPT", json.dumps(turns))
        self.assertEqual(turns[0]["params"]["effort"], "low")
        self.assertEqual(turns[0]["params"]["serviceTierForTurn"], "default")

    def test_failed_submission_cannot_reuse_the_process(self):
        prepared = self.prepare()
        with patch.object(prepared, "call", side_effect=RuntimeError("acknowledgement lost")) as call:
            with self.assertRaises(RuntimeError):
                prepared.translate("FIRST_PRIVATE_PROMPT")
            with self.assertRaises(RuntimeError):
                prepared.translate("SECOND_PRIVATE_PROMPT")
        self.assertEqual(call.call_count, 1)

    def test_close_reaps_process_removes_temporary_directory_and_clears_events(self):
        prepared = self.prepare()
        process = prepared.process
        directory = Path(prepared.directory.name)
        prepared.events.append({"private": "previous output"})
        prepared.pending = b"previous output"
        prepared.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(directory.exists())
        self.assertEqual(prepared.pending, b"")
        self.assertFalse(prepared.events)
        prepared.close()


class ClientBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.patch = patch.object(prewarm, "runtime_directory", return_value=self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_unavailable_worker_returns_none_without_attempting_to_send_input(self):
        with patch.object(prewarm, "send") as send:
            self.assertIsNone(prewarm.request({"action": "translate", "prompt": "private"}))
        send.assert_not_called()

    def test_busy_worker_gets_no_prompt_before_readiness(self):
        observed = []
        accepted = threading.Event()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.root / "worker.sock"))
            server.listen(1)

            def busy_worker():
                connection, _ = server.accept()
                with connection:
                    accepted.set()
                    connection.settimeout(2)
                    observed.append(connection.recv(1024))

            worker = threading.Thread(target=busy_worker)
            worker.start()
            self.assertIsNone(prewarm.request({"action": "translate", "prompt": "private"}))
            worker.join(2)
        self.assertTrue(accepted.is_set())
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [b""])

    def test_disconnect_after_submission_does_not_retry_the_model(self):
        observed = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.root / "worker.sock"))
            server.listen(1)

            def dropping_worker():
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(2)
                    prewarm.send(connection, {"ready": True})
                    observed.append(prewarm.receive(connection))

            worker = threading.Thread(target=dropping_worker)
            worker.start()
            with patch.object(prewarm, "warm"), patch.dict(os.environ, {"DEV_LINGO_PREWARM": "1"}), \
                    patch.object(codex_provider, "translate_once") as fallback:
                with self.assertRaises(RuntimeError):
                    codex_provider.translate("private")
                fallback.assert_not_called()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [{"action": "translate", "prompt": "private"}])

    def test_cold_worker_falls_back_only_before_submission(self):
        with patch.object(prewarm, "warm"), patch.dict(os.environ, {"DEV_LINGO_PREWARM": "1"}), \
                patch.object(codex_provider, "translate_once", return_value=RESULT) as fallback:
            self.assertEqual(codex_provider.translate("private"), RESULT)
        fallback.assert_called_once_with("private")

    def test_trickling_input_cannot_extend_the_absolute_receive_deadline(self):
        connection = Mock(spec=socket.socket)
        connection.gettimeout.return_value = 0.1
        connection.recv.side_effect = [b" ", b"{}\n"]
        with patch.object(prewarm.time, "monotonic", side_effect=[0.0, 0.05, 0.11]):
            with self.assertRaises(socket.timeout):
                prewarm.receive(connection)
        self.assertEqual(connection.recv.call_count, 1)
        connection.settimeout.assert_called_once_with(0.05)


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(prewarm, "runtime_directory", return_value=self.directory))
        # signal handlers and umask belong to the process, not this test thread.
        self.stack.enter_context(patch.object(prewarm.signal, "signal"))
        self.stack.enter_context(patch.object(prewarm.os, "umask"))
        self.instances = []
        self.errors = []
        self.threads = []
        instances = self.instances

        class FakePrepared:
            def __init__(self):
                self.inputs = []
                self.closed = False
                self.process = SimpleNamespace(poll=lambda: 0 if self.closed else None)
                instances.append(self)

            def translate(self, prompt):
                if self.closed or self.inputs:
                    raise AssertionError("A prepared process was reused")
                self.inputs.append(prompt)
                if prompt == "FAIL_AFTER_SUBMISSION":
                    raise RuntimeError("The submitted turn failed")
                return RESULT

            def close(self):
                self.closed = True

        self.stack.enter_context(patch.object(codex_prepared, "PreparedTranslation", FakePrepared))
        self.addCleanup(self.stop_threads)

    def start_worker(self):
        def run():
            try:
                prewarm.serve(self.directory)
            except BaseException as error:
                self.errors.append(error)

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        deadline = time.monotonic() + 2
        while thread.is_alive():
            if (self.directory / "worker.sock").exists() and prewarm.request({"action": "status"}) == {"prepared": True}:
                break
            if time.monotonic() > deadline:
                self.fail("Worker did not become ready on its local socket")
            time.sleep(0.005)
        return thread

    def stop_threads(self):
        if any(thread.is_alive() for thread in self.threads):
            prewarm.stop()
        for thread in self.threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in self.threads))
        self.assertEqual(self.errors, [])

    def test_each_request_gets_fresh_process_and_only_empty_process_stays_alive(self):
        worker = self.start_worker()
        for prompt in ["FIRST_PRIVATE_PROMPT", "SECOND_PRIVATE_PROMPT"]:
            self.assertEqual(prewarm.request({"action": "translate", "prompt": prompt}), {"result": RESULT})
        self.assertEqual(prewarm.request({"action": "status"}), {"prepared": True})
        self.assertEqual([instance.inputs for instance in self.instances],
                         [["FIRST_PRIVATE_PROMPT"], ["SECOND_PRIVATE_PROMPT"], []])
        self.assertEqual([instance.closed for instance in self.instances], [True, True, False])
        self.assertTrue(prewarm.stop())
        worker.join(3)
        self.assertTrue(all(instance.closed for instance in self.instances))
        self.assertFalse((self.directory / "worker.sock").exists())

    def test_idle_worker_reaps_unused_process_and_removes_socket(self):
        with patch.object(prewarm, "IDLE_SECONDS", 0.02):
            worker = self.start_worker()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.instances), 1)
        self.assertTrue(self.instances[0].closed)
        self.assertFalse((self.directory / "worker.sock").exists())

    def test_failed_turn_is_closed_before_a_new_empty_process_is_prepared(self):
        self.start_worker()
        with self.assertRaises(RuntimeError):
            prewarm.request({"action": "translate", "prompt": "FAIL_AFTER_SUBMISSION"})
        self.assertEqual(prewarm.request({"action": "status"}), {"prepared": True})
        self.assertEqual([instance.inputs for instance in self.instances], [["FAIL_AFTER_SUBMISSION"], []])
        self.assertEqual([instance.closed for instance in self.instances], [True, False])
        self.assertEqual(prewarm.request({"action": "translate", "prompt": "RECOVERY_INPUT"}), {"result": RESULT})
        self.assertEqual(self.instances[1].inputs, ["RECOVERY_INPUT"])

    def test_dead_prepared_process_is_replaced_before_accepting_the_next_prompt(self):
        self.start_worker()
        self.assertEqual(prewarm.request({"action": "status"}), {"prepared": True})
        dead = self.instances[0]
        dead.process.poll = lambda: 1
        self.assertEqual(prewarm.request({"action": "translate", "prompt": "NEXT_PRIVATE_PROMPT"}),
                         {"result": RESULT})
        self.assertEqual(prewarm.request({"action": "status"}), {"prepared": True})
        self.assertEqual([instance.inputs for instance in self.instances], [[], ["NEXT_PRIVATE_PROMPT"], []])
        self.assertEqual([instance.closed for instance in self.instances], [True, True, False])

    def test_simultaneous_request_falls_back_without_reaching_busy_prepared_process(self):
        self.start_worker()
        self.assertEqual(prewarm.request({"action": "status"}), {"prepared": True})
        translating = threading.Event()
        release = threading.Event()
        original = self.instances[0].translate
        responses = []

        def slow_translate(prompt):
            translating.set()
            if not release.wait(3):
                raise RuntimeError("Test did not release the first request")
            return original(prompt)

        self.instances[0].translate = slow_translate
        first = threading.Thread(target=lambda: responses.append(
            prewarm.request({"action": "translate", "prompt": "FIRST_PRIVATE_PROMPT"})))
        first.start()
        try:
            self.assertTrue(translating.wait(2))
            with patch.object(prewarm, "warm"), patch.dict(os.environ, {"DEV_LINGO_PREWARM": "1"}), \
                    patch.object(codex_provider, "translate_once", return_value=RESULT) as fallback:
                self.assertEqual(codex_provider.translate("SECOND_PRIVATE_PROMPT"), RESULT)
                fallback.assert_called_once_with("SECOND_PRIVATE_PROMPT")
        finally:
            release.set()
            first.join(3)
        self.assertFalse(first.is_alive())
        self.assertEqual(responses, [{"result": RESULT}])
        self.assertEqual(prewarm.request({"action": "status"}), {"prepared": True})
        self.assertEqual([instance.inputs for instance in self.instances], [["FIRST_PRIVATE_PROMPT"], []])

    def test_duplicate_worker_does_not_replace_socket_or_prepared_process(self):
        self.start_worker()
        self.assertEqual(prewarm.request({"action": "status"}), {"prepared": True})
        socket_inode = (self.directory / "worker.sock").stat().st_ino
        duplicate = self.start_worker()
        duplicate.join(2)
        self.assertFalse(duplicate.is_alive())
        self.assertEqual(len(self.instances), 1)
        self.assertEqual((self.directory / "worker.sock").stat().st_ino, socket_inode)
        self.assertEqual(prewarm.request({"action": "status"}), {"prepared": True})


class PrivateRuntimeTests(unittest.TestCase):
    def test_runtime_directory_rejects_shared_permissions_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "runtime"
            original_path = Path

            def routed_path(value):
                return root if value == "/tmp" else original_path(value)

            with patch.object(prewarm, "Path", side_effect=routed_path), \
                    patch.object(codex_provider, "codex_path", return_value=sys.executable):
                created = prewarm.runtime_directory()
                self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o700)
                self.assertEqual(created.stat().st_uid, os.getuid())
                created.chmod(0o755)
                with self.assertRaises(RuntimeError):
                    prewarm.runtime_directory()
                created.rmdir()
                target.mkdir(mode=0o700)
                created.symlink_to(target, target_is_directory=True)
                with self.assertRaises(RuntimeError):
                    prewarm.runtime_directory()


if __name__ == "__main__":
    unittest.main()
