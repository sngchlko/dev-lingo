#!/usr/bin/env python3
"""Capture the actual Codex model request locally, without an inference call.

Checks native sync UI output with the desktop's warning opt-out setting.
The local provider rejects inference so no external model is called.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from codex_rpc import Client
from lingo_process import find_codex


SENTINEL = "DEV_LINGO_UI_ONLY_82d1a730"
EXPLANATION_SENTINEL = "take another lookは、もう一度確認してほしいときに使う表現です。"
PROMPT = "DEV_LINGO_ORIGINAL_PROMPT_91e042"


def check(app_server=False):
    captured = []
    request_times = []
    class Capture(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            captured.append(json.loads(raw))
            request_times.append(time.time())
            time.sleep(0.2)
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"intentional local isolation test","type":"invalid_request_error"}}')
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="dev-lingo-isolation-") as directory:
            root = Path(directory)
            marker = root / "hook-executed"
            fixture = root / "ui_only_hook.py"
            fixture.write_text("import json\nfrom pathlib import Path\nPath(%r).write_text('executed')\nprint(json.dumps({'systemMessage': %r}))\n" % (str(marker), SENTINEL))
            command = subprocess.list2cmdline([sys.executable, str(fixture)]) if os.name == "nt" else "python3 " + shlex.quote(str(fixture))
            handler = "{type=\"command\",command=" + json.dumps(command) + "}"
            provider = '{name="Local capture",base_url="http://127.0.0.1:%s/v1",wire_api="responses",supports_websockets=false,requires_openai_auth=false}' % server.server_port
            cli = [find_codex(), "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                   "--sandbox", "read-only", "--json", "--cd", str(root), "--dangerously-bypass-hook-trust",
                   "-c", "model_provider=\"dev_lingo_capture\"", "-c", "model=\"capture-model\"",
                   "-c", "model_providers.dev_lingo_capture=" + provider,
                   "-c", "project_doc_max_bytes=0", "-c", "features.enable_request_compression=false",
                   "-c", "hooks.UserPromptSubmit=[{hooks=[" + handler + "]}]",
                   "--enable", "hooks", "--disable", "plugins", "--disable", "apps", PROMPT]
            ui_seen = False
            if app_server:
                options = cli[cli.index("-c"):cli.index(PROMPT)]
                hook_option = "hooks.UserPromptSubmit=[{hooks=[" + handler + "]}]"
                index = options.index(hook_option)
                del options[index-1:index+1]
                index = options.index("plugins")
                options[index-1] = "--enable"
                fake_codex = root / "fake-codex"
                fake_result = {"explanation_label": "解説", "sentences": [
                    {"english": SENTINEL, "explanation": EXPLANATION_SENTINEL},
                    {"english": "Thanks!", "explanation": ""}]}
                fake_codex.write_text("#!" + sys.executable + "\nimport json,sys,time\nfrom pathlib import Path\nsys.stdin.read()\ntime.sleep(2)\nPath(%r).write_text('executed')\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(%r)}}),flush=True)\nprint(json.dumps({'type':'turn.completed'}),flush=True)\n" % (str(marker), fake_result))
                fake_codex.chmod(0o700)
                if os.name == "nt":
                    fake_codex = root / "fake-codex.exe"
                    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
                    source = root / "fixture.cs"
                    message = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(fake_result, ensure_ascii=False)}}, ensure_ascii=False)
                    source.write_text('using System;using System.IO;using System.Text;using System.Threading;class Fixture{static void Main(){Console.InputEncoding=new UTF8Encoding(false);Console.OutputEncoding=new UTF8Encoding(false);Console.In.ReadToEnd();Thread.Sleep(2000);File.WriteAllText(%s,"executed");Console.WriteLine(%s);Console.WriteLine("{\\\"type\\\":\\\"turn.completed\\\"}");}}' % (json.dumps(str(marker)), json.dumps(message)), encoding="utf-8")
                    subprocess.run([str(compiler), "/nologo", "/out:" + str(fake_codex), str(source)], check=True, capture_output=True)
                env = dict(os.environ, DEV_LINGO_CODEX=str(fake_codex), DEV_LINGO_PREWARM="0")
                client = Client([find_codex(), "app-server"] + options, env=env,
                                capabilities={"optOutNotificationMethods": ["warning"]})
                try:
                    listed = client.call("hooks/list", {"cwds": [str(root)]})
                    fixture_hooks = [hook for entry in listed["data"] for hook in entry["hooks"] if hook.get("pluginId") == "dev-lingo@dev-lingo-local"]
                    assert len(fixture_hooks) == 2 and all(h["trustStatus"] == "trusted" for h in fixture_hooks), "Install and trust Dev Lingo before the app-server integration check"
                    definition = json.loads(Path(fixture_hooks[0]["sourcePath"]).read_text(encoding="utf-8"))
                    assert not definition["hooks"]["UserPromptSubmit"][0]["hooks"][0].get("async", False), "Desktop delivery requires a synchronous hook result"
                    thread_config = {"features.hooks": True, "features.plugins": True, "project_doc_max_bytes": 0}
                    thread = client.call("thread/start", {"cwd": str(root), "ephemeral": True, "config": thread_config,
                        "baseInstructions": "Test model context isolation only.", "developerInstructions": "",
                        "approvalPolicy": "never", "sandbox": "read-only"})["thread"]
                    client.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": PROMPT}]})
                    while True:
                        notification = client.messages.get(timeout=20)
                        client.notifications.append(notification)
                        if notification.get("method") == "turn/completed":
                            break
                    assert marker.exists(), "Installed translation did not finish"
                    assert request_times[0] >= marker.stat().st_mtime, "Synchronous hook did not finish before the working request"
                    # Verify a follow-up also excludes the UI translation.
                    client.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "DEV_LINGO_FOLLOWUP_PROBE"}]})
                    while True:
                        notification = client.messages.get(timeout=20)
                        client.notifications.append(notification)
                        if notification.get("method") == "turn/completed":
                            break
                    ui_seen = SENTINEL in json.dumps(client.notifications)
                    assert EXPLANATION_SENTINEL in json.dumps(client.notifications, ensure_ascii=False), "Localized explanation missing from UI notification"
                    assert marker.exists(), "App-server hook did not execute: " + json.dumps([n for n in client.notifications if n.get("method") == "warning"])
                    assert ui_seen, "Translation UI notification missing: " + json.dumps([n for n in client.notifications if "hook" in str(n.get("method", "")) or "warning" in str(n.get("method", ""))])
                    expected_message = "통역: " + SENTINEL + "\n解説: " + EXPLANATION_SENTINEL + "\n통역: Thanks!"
                    warnings = [event for event in client.notifications if event.get("method") == "warning"]
                    assert not warnings, "Desktop warning opt-out was not reproduced"
                    entries = [entry for event in client.notifications if event.get("method") == "hook/completed"
                               for entry in event.get("params", {}).get("run", {}).get("entries", [])]
                    assert any(entry.get("kind") == "warning" and entry.get("text") == expected_message for entry in entries), "Paired translation did not arrive through the desktop-supported hook/completed event"
                    result = subprocess.CompletedProcess([], 0, "", "")
                finally:
                    client.close()
            else:
                result = subprocess.run(cli, capture_output=True, text=True, timeout=30)
            assert marker.exists(), "Hook did not execute; absence from input would not prove isolation: " + result.stderr[-2000:]
            assert captured, "No model request was captured: " + result.stderr[-1200:]
            inputs = json.dumps([request.get("input") for request in captured], ensure_ascii=False)
            assert PROMPT in inputs, "Original user prompt was lost"
            assert SENTINEL not in inputs, "UI-only hook output contaminated model input"
            assert "解説" not in inputs, "Localized heading contaminated model input"
            assert EXPLANATION_SENTINEL not in inputs, "Localized explanation contaminated model input"
            coach_intro = (Path(__file__).resolve().parents[1] / "plugins/dev-lingo/prompts/coach.txt").read_text().splitlines()[0]
            assert coach_intro not in inputs, "Coach instructions contaminated model input"
            print(json.dumps({"surface": "app-server" if app_server else "exec", "hook_executed": True,
                              "model_requests_captured": len(captured), "original_prompt_present": True,
                              "ui_translation_absent_from_model_input": True,
                              "desktop_warning_opt_out": True if app_server else None,
                              "desktop_hook_result_notification_present": ui_seen if app_server else None,
                              "native_ui_notification_present": ui_seen if app_server else None}))
    finally:
        server.shutdown()
        server.server_close()


def check_async_warning_opt_out():
    """A completed async hook remains invisible with the app's capability."""
    captured = []
    class Capture(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"local desktop transport check","type":"invalid_request_error"}}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="dev-lingo-warning-optout-") as directory:
            root = Path(directory)
            marker = root / "async-finished"
            fixture = root / "async.py"
            fixture.write_text("import sys,time,json\nfrom pathlib import Path\nsys.stdin.read()\ntime.sleep(.3)\nprint(json.dumps({'systemMessage':%r}),flush=True)\nPath(%r).touch()\n" % (SENTINEL, str(marker)), encoding="utf-8")
            command = subprocess.list2cmdline([sys.executable, str(fixture)]) if os.name == "nt" else shlex.join([sys.executable, str(fixture)])
            handler = '{type="command",async=true,command=' + json.dumps(command) + '}'
            provider = '{name="Local capture",base_url="http://127.0.0.1:%s/v1",wire_api="responses",supports_websockets=false,requires_openai_auth=false}' % server.server_port
            options = [find_codex(), "app-server",
                       "-c", 'model_provider="dev_lingo_transport_check"', "-c", 'model="capture-model"',
                       "-c", "model_providers.dev_lingo_transport_check=" + provider,
                       "-c", "features.enable_request_compression=false", "-c", "project_doc_max_bytes=0",
                       "-c", "hooks.UserPromptSubmit=[{hooks=[" + handler + "]}]",
                       "--enable", "hooks", "--disable", "plugins", "--disable", "apps"]
            home = root / "codex-home"
            home.mkdir()
            client = Client(options, env=dict(os.environ, CODEX_HOME=str(home)),
                            capabilities={"optOutNotificationMethods": ["warning"]})
            events = []
            try:
                hooks = [hook for entry in client.call("hooks/list", {"cwds": [directory]})["data"]
                         for hook in entry["hooks"] if hook["enabled"]]
                assert len(hooks) == 1, "Expected only the isolated async fixture"
                hook = hooks[0]
                client.call("config/batchWrite", {"edits": [{
                    "keyPath": "hooks.state." + json.dumps(hook["key"]) + ".trusted_hash",
                    "mergeStrategy": "upsert", "value": hook["currentHash"]}], "reloadUserConfig": True})
                thread = client.call("thread/start", {"cwd": directory, "ephemeral": True,
                    "baseInstructions": "Test notification transport only.", "developerInstructions": "",
                    "approvalPolicy": "never", "sandbox": "read-only"})["thread"]
                def turn(text):
                    client.call("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": text}]})
                    while True:
                        event = client.messages.get(timeout=10)
                        events.append(event)
                        if event.get("method") == "turn/completed":
                            break
                turn("ASYNC_TRANSPORT_ORIGINAL")
                deadline = time.monotonic() + 5
                while not marker.exists():
                    assert time.monotonic() < deadline, "Async fixture did not execute"
                    time.sleep(.02)
                time.sleep(.3)
                turn("ASYNC_TRANSPORT_FOLLOWUP")
                assert len(captured) == 2
                assert not any(event.get("method") == "warning" for event in events)
                assert not any(event.get("method") == "hook/completed" for event in events)
                assert SENTINEL not in json.dumps([request.get("input") for request in captured])
                print(json.dumps({"surface": "desktop-capability-reproduction", "async_hook_finished": True,
                                  "warning_opted_out": True, "async_notification_visible": False,
                                  "translation_absent_from_model_input": True}))
            finally:
                client.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    check(False)
    check(True)
    check_async_warning_opt_out()
