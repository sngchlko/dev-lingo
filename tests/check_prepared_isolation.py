#!/usr/bin/env python3
"""Capture prepared translator requests locally, without paid inference."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/dev-lingo/scripts"))
from codex_prepared import PreparedTranslation
from lingo_core import coach_path


def main():
    captured = []
    class Capture(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"intentional local isolation check"}}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_popen = subprocess.Popen
    try:
        with tempfile.TemporaryDirectory(prefix="dev-lingo-prepared-check-") as directory:
            root = Path(directory)
            marker = root / "mcp-or-notify-started"
            helper = root / "mcp_probe.py"
            helper.write_text("from pathlib import Path\nPath(%r).touch()\n" % str(marker))
            instructions = root / "personal-instructions.txt"
            instructions.write_text("DEV_LINGO_PERSONAL_BASE_MUST_NOT_LEAK")
            provider = '{name="Local capture",base_url="http://127.0.0.1:%s/v1",wire_api="responses",supports_websockets=false,requires_openai_auth=false}' % server.server_port
            def launch(command, **kwargs):
                command = list(command) + [
                    "-c", "model_providers.dev_lingo_capture=" + provider,
                    "-c", "features.enable_request_compression=false",
                    "-c", 'developer_instructions="DEV_LINGO_PERSONAL_DEVELOPER_MUST_NOT_LEAK"',
                    "-c", "model_instructions_file=" + json.dumps(str(instructions)),
                    "-c", "notify=" + json.dumps([sys.executable, str(helper)]),
                    "-c", 'mcp_servers."dev-lingo-probe".command=' + json.dumps(sys.executable),
                    "-c", 'mcp_servers."dev-lingo-probe".args=' + json.dumps([str(helper)]),
                ]
                return real_popen(command, **kwargs)
            class LocalPrepared(PreparedTranslation):
                def call(self, method, params):
                    if method == "thread/start":
                        params["modelProvider"] = "dev_lingo_capture"
                        params["config"]["model_provider"] = "dev_lingo_capture"
                    return super().call(method, params)
            prompts = ["DEV_LINGO_FIRST_INPUT_39412", "DEV_LINGO_SECOND_INPUT_52891"]
            ids = []
            with patch("codex_prepared.subprocess.Popen", side_effect=launch):
                for index, prompt in enumerate(prompts):
                    prepared = LocalPrepared()
                    workdir = Path(prepared.directory.name)
                    ids.append(prepared.thread_id)
                    try:
                        time.sleep(0.2)
                        assert len(captured) == index, "Preparation unexpectedly invoked a model"
                        assert not marker.exists(), "Personal MCP or notification command executed"
                        try:
                            prepared.translate(prompt)
                        except RuntimeError:
                            pass
                        assert len(captured) == index + 1, "Did not capture exactly one request"
                        request = captured[-1]
                        inputs = json.dumps(request.get("input"), ensure_ascii=False)
                        assert prompt in inputs
                        assert "DEV_LINGO_PERSONAL_BASE_MUST_NOT_LEAK" not in inputs
                        assert "DEV_LINGO_PERSONAL_DEVELOPER_MUST_NOT_LEAK" not in inputs
                        assert coach_path().read_text().strip() in inputs.replace('\\n', '\n').replace('\\"', '"'), "Missing coach instructions"
                        if index:
                            assert prompts[0] not in inputs, "Previous input leaked into a fresh translation"
                        assert request["model"] == "gpt-6.1-sol"
                        assert request["reasoning"]["effort"] == "low"
                        assert request.get("service_tier") not in {"fast", "priority"}
                        try:
                            prepared.translate("MUST_NOT_BE_SENT")
                            raise AssertionError("Used translation process was reused")
                        except RuntimeError:
                            pass
                        assert len(captured) == index + 1
                    finally:
                        prepared.close()
                    assert not workdir.exists()
            assert len(set(ids)) == 2
            assert not marker.exists()
            print(json.dumps({"prepared_without_inference": True, "model": "gpt-6.1-sol", "effort": "low",
                              "independent_requests": len(captured), "personal_instructions_absent": True,
                              "personal_mcp_and_notify_disabled": True, "previous_input_absent": True,
                              "process_reuse_rejected": True, "temporary_directories_removed": True}))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
