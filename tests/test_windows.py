"""Windows streaming on every host, and native process jobs on Windows."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/dev-lingo/scripts"))
import codex_provider
import lingo_process

RESULT = {"explanation_label": "해설", "sentences": [{"english": "Could you take another look?", "explanation": "다시 확인해 달라는 표현이에요."}]}


class WindowsStreamTests(unittest.TestCase):
    def test_windows_session_start_needs_no_python_or_translation(self):
        self.assertEqual(lingo_process.windows_hook_command("prepare"), "exit 0")
        self.assertIn("dev_lingo.py", lingo_process.windows_hook_command("hook"))

    def run_fixture(self, body, timeout=2, payload=None):
        with tempfile.TemporaryDirectory(prefix="dev lingo 한글 ") as directory:
            fixture = Path(directory) / "fixture.py"
            fixture.write_text("import sys,os,json,time\n" + body, encoding="utf-8")
            process = lingo_process.spawn_isolated([sys.executable, "-X", "utf8", str(fixture)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                return codex_provider.read_windows_result(process, timeout, payload)
            finally:
                if os.name == "nt":
                    lingo_process.stop_windows(process)
                else:
                    codex_provider.stop_process(process)
                    stopped, threads = process._dev_lingo_pipe_threads
                    stopped.set()
                    for thread in threads:
                        thread.join(2)
                self.assertFalse(any(t.is_alive() for t in threads) if os.name != "nt" else process._dev_lingo_pipe_threads is not None)

    def wire(self):
        return (json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(RESULT, ensure_ascii=False)}}, ensure_ascii=False) + '\n{"type":"turn.completed"}\n').encode("utf-8")

    def test_fragmented_unicode_and_stdin_are_preserved(self):
        payload = json.dumps({"text_to_rewrite": "다시 확인해줘 $(echo test)"}, ensure_ascii=False).encode("utf-8")
        body = "value=json.loads(sys.stdin.buffer.read())\nassert value['text_to_rewrite']==%r\nfor byte in %r:\n os.write(1,bytes([byte]))\n" % ("다시 확인해줘 $(echo test)", self.wire())
        self.assertEqual(self.run_fixture(body, payload=payload), RESULT)

    def test_only_confirmed_completion_is_accepted_without_shutdown_wait(self):
        start = time.monotonic()
        self.assertEqual(self.run_fixture("os.write(1,%r)\ntime.sleep(10)\n" % self.wire()), RESULT)
        self.assertLess(time.monotonic() - start, 3)
        for tail in [b"", b'{"type":"turn.failed"}\n']:
            wire = self.wire().split(b'{"type":"turn.completed"}')[0] + tail
            with self.assertRaises(RuntimeError):
                self.run_fixture("os.write(1,%r)\n" % wire)

    def test_timeout_includes_a_blocked_stdin_and_cleans_reader_threads(self):
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "초과"):
            self.run_fixture("time.sleep(10)\n", timeout=.15, payload=b"x" * (2 * 1024 * 1024))
        self.assertLess(time.monotonic() - start, 3)

    def test_tool_events_and_oversized_streams_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "도구"):
            self.run_fixture("print('{\"type\":\"item.started\",\"item\":{\"type\":\"mcp_tool_call\"}}',flush=True)\n")
        with self.assertRaisesRegex(RuntimeError, "너무"):
            self.run_fixture("os.write(1,b'x'*(2*1024*1024+1))\n")

    def test_windows_never_imports_unix_prewarm_even_when_enabled(self):
        with patch.object(codex_provider, "is_windows", return_value=True), patch.dict(os.environ, {"DEV_LINGO_PREWARM": "1"}), patch.object(codex_provider, "translate_once", return_value=RESULT) as translate:
            self.assertEqual(codex_provider.translate("다시 확인해줘"), RESULT)
        translate.assert_called_once_with("다시 확인해줘")

    def test_npm_shim_resolves_to_native_binary_with_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory(prefix="npm 한글 ") as directory:
            root = Path(directory)
            shim = root / "codex.cmd"; shim.touch()
            binary = root / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            binary.parent.mkdir(parents=True); binary.touch()
            with patch.object(lingo_process, "is_windows", return_value=True), patch.dict(os.environ, {"DEV_LINGO_CODEX": str(shim)}):
                self.assertEqual(lingo_process.find_codex(), str(binary.resolve()))


@unittest.skipUnless(os.name == "nt", "Native Windows Job Objects")
class WindowsJobTests(unittest.TestCase):
    def test_job_closure_kills_helper_after_its_parent_has_exited(self):
        import ctypes
        from ctypes import wintypes
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "helper.pid"
            body = "import subprocess,sys;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);Path(%r).write_text(str(p.pid))" % str(marker)
            process = lingo_process.spawn_isolated([sys.executable, "-c", body], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            handle = None
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]; api.OpenProcess.restype = wintypes.HANDLE
            api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            try:
                process.wait(timeout=5)
                helper = int(marker.read_text())
                handle = api.OpenProcess(0x1000 | 0x100000, False, helper)
                self.assertTrue(handle)
                code = wintypes.DWORD()
                self.assertTrue(api.GetExitCodeProcess(handle, ctypes.byref(code)))
                self.assertEqual(code.value, 259)
                lingo_process.stop_windows(process)
                self.assertEqual(api.WaitForSingleObject(handle, 3000), 0)
                api.GetExitCodeProcess(handle, ctypes.byref(code))
                self.assertNotEqual(code.value, 259)
            finally:
                lingo_process.stop_windows(process)
                if handle: api.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
