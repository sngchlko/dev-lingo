#!/usr/bin/env python3
"""Capture the actual Codex model request locally, without an inference call.

Checks the installed CLI runtime, not a mock of our hook function. The local
provider intentionally rejects inference after capturing each request.
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


SENTINEL = "DEV_LINGO_UI_ONLY_82d1a730"
EXPLANATION_SENTINEL = "take another lookは、もう一度確認してほしいときに使う表現です。"
PROMPT = "DEV_LINGO_ORIGINAL_PROMPT_91e042"


def check(app_server=False):
    captured = []
    class Capture(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            captured.append(json.loads(raw))
            time.sleep(1)
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
            command = "python3 " + shlex.quote(str(fixture))
            handler = "{type=\"command\",command=" + json.dumps(command) + "}"
            provider = '{name="Local capture",base_url="http://127.0.0.1:%s/v1",wire_api="responses",supports_websockets=false,requires_openai_auth=false}' % server.server_port
            cli = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
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
                fake_codex.write_text("#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\nsys.stdin.read()\nPath(%r).write_text('executed')\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(%r)}}),flush=True)\nprint(json.dumps({'type':'turn.completed'}),flush=True)\n" % (str(marker), fake_result))
                fake_codex.chmod(0o700)
                env = dict(os.environ, DEV_LINGO_CODEX=str(fake_codex), DEV_LINGO_PREWARM="0")
                client = Client(["codex", "app-server"] + options, env=env)
                try:
                    listed = client.call("hooks/list", {"cwds": [str(root)]})
                    fixture_hooks = [hook for entry in listed["data"] for hook in entry["hooks"] if hook.get("pluginId") == "dev-lingo@dev-lingo-local"]
                    assert len(fixture_hooks) == 2 and all(h["trustStatus"] == "trusted" for h in fixture_hooks), "Install and trust Dev Lingo before the app-server integration check"
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
                    ui_seen = SENTINEL in json.dumps(client.notifications)
                    assert EXPLANATION_SENTINEL in json.dumps(client.notifications, ensure_ascii=False), "Localized explanation missing from UI notification"
                    assert marker.exists(), "App-server hook did not execute: " + json.dumps([n for n in client.notifications if n.get("method") == "warning"])
                    assert ui_seen, "Translation UI notification missing: " + json.dumps([n for n in client.notifications if "hook" in str(n.get("method", "")) or "warning" in str(n.get("method", ""))])
                    ui_entries = [entry for event in client.notifications
                                  for entry in event.get("params", {}).get("run", {}).get("entries", [])
                                  if SENTINEL in entry.get("text", "")]
                    assert ui_entries and all(entry["kind"] == "warning" for entry in ui_entries), "Translation is not a UI-only warning entry"
                    expected_message = "통역: " + SENTINEL + "\n解説: " + EXPLANATION_SENTINEL + "\n통역: Thanks!"
                    assert any(entry["text"] == expected_message for entry in ui_entries), "Sentence explanations are not paired in translation order"
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
                              "native_ui_notification_present": ui_seen if app_server else None}))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    check(False)
    check(True)
