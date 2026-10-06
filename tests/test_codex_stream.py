"""Exercise completion, timeout and cleanup against actual local processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/dev-lingo/scripts"))
import codex_provider

RESULT = {"explanation_label": "해설", "sentences": [{"english": "Thanks!", "explanation": ""}]}


def events(value=RESULT):
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(value, ensure_ascii=False)}}, ensure_ascii=False) + "\n"


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"DEV_LINGO_PREWARM": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def translate_with(self, body):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-codex"
            fake.write_text("#!" + sys.executable + "\nimport json,os,sys,time\nfrom pathlib import Path\nsys.stdin.read()\n" + body)
            fake.chmod(0o700)
            with patch.object(codex_provider, "codex_path", return_value=str(fake)):
                return codex_provider.translate("고마워!")

    def test_completed_turn_skips_shutdown_delay_and_cleans_process_and_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "process.json"
            body = "Path(%r).write_text(json.dumps({'pid':os.getpid(),'cwd':sys.argv[sys.argv.index('--cd')+1],'file_output':'--output-last-message' in sys.argv}))\n" % str(marker)
            body += "print(%r, end='', flush=True)\nprint('{\"type\":\"turn.completed\"}', flush=True)\ntime.sleep(4)\n" % events()
            start = time.monotonic()
            self.assertEqual(self.translate_with(body), RESULT)
            self.assertLess(time.monotonic() - start, 2)
            metadata = json.loads(marker.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(metadata["pid"], 0)
            self.assertFalse(Path(metadata["cwd"]).exists())
            self.assertFalse(metadata["file_output"])

    def test_message_without_successful_completion_is_never_accepted(self):
        for tail in ["", "print('{\"type\":\"turn.failed\"}', flush=True)\n"]:
            with self.subTest(tail=tail):
                with self.assertRaises(RuntimeError):
                    self.translate_with("print(%r, end='', flush=True)\ntime.sleep(0.03)\n" % events() + tail)

    def test_tool_after_message_is_rejected_before_completion(self):
        body = "print(%r, end='', flush=True)\nprint('{\"type\":\"item.started\",\"item\":{\"type\":\"mcp_tool_call\"}}', flush=True)\nprint('{\"type\":\"turn.completed\"}', flush=True)\n" % events()
        with self.assertRaisesRegex(RuntimeError, "도구"):
            self.translate_with(body)

    def test_partial_utf8_events_and_last_message(self):
        wire = events({"draft": True}) + events() + '{"type":"turn.completed"}\n'
        body = "wire=%r\nfor byte in wire:\n os.write(1,bytes([byte]))\n" % wire.encode("utf-8")
        self.assertEqual(self.translate_with(body), RESULT)

    def test_completion_without_message_and_invalid_json_are_rejected(self):
        with self.assertRaises(RuntimeError):
            self.translate_with("print('{\"type\":\"turn.completed\"}', flush=True)\n")
        malformed = {"type": "item.completed", "item": {"type": "agent_message", "text": "not JSON"}}
        with self.assertRaises(ValueError):
            self.translate_with("print(%r, flush=True)\nprint('{\"type\":\"turn.completed\"}', flush=True)\n" % json.dumps(malformed))

    def test_timeout_covers_blocked_stdin_and_kills_unresponsive_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(4)"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, "초과"):
                codex_provider.read_result(process, timeout=0.15, input_data=b"x" * (2 * 1024 * 1024))
        finally:
            codex_provider.stop_process(process)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(process.poll())

    def test_cleanup_kills_helper_even_after_group_leader_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "helper.pid"
            program = """import os,signal,time
from pathlib import Path
marker=Path(%r)
if os.fork()==0:
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    marker.write_text(str(os.getpid()))
    time.sleep(20)
else:
    while not marker.exists(): time.sleep(.01)
""" % str(marker)
            process = subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
            try:
                process.wait(timeout=3)
                helper = int(marker.read_text())
                self.assertEqual(os.getpgid(helper), process.pid)
                codex_provider.stop_process(process)
                deadline = time.monotonic() + 3
                while True:
                    try:
                        os.kill(helper, 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() >= deadline:
                        self.fail("Helper survived cleanup after its leader exited")
                    time.sleep(.01)
            finally:
                try:
                    os.killpg(process.pid, 9)
                except (ProcessLookupError, PermissionError):
                    pass
                process.wait()
                process.stdin.close()
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
