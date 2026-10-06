"""One-use, in-memory Codex app-server thread prepared before user input."""
from collections import deque
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time

from codex_provider import DEFAULT_MODEL, codex_path, stop_process
from lingo_core import ROOT, coach_path, translation_environment

FEATURES = ["hooks", "plugins", "apps", "memories", "multi_agent", "shell_tool", "skill_search"]


class PreparedTranslation:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="dev-lingo-prepared-")
        self.process = None
        self.pending = b""
        self.events = deque()
        self.sequence = 0
        self.used = False
        self.thread_id = None
        self.model = os.environ.get("DEV_LINGO_MODEL") or DEFAULT_MODEL
        config = {
            "model": self.model, "model_reasoning_effort": "low",
            "model_provider": "openai", "service_tier": "default",
            "model_instructions_file": str(coach_path()), "developer_instructions": "",
            "project_doc_max_bytes": 0, "approval_policy": "never", "web_search": "disabled",
            "notify": [],
        }
        command = [codex_path(), "app-server", "--stdio"]
        for key, value in config.items():
            command.extend(["-c", key + "=" + json.dumps(value)])
        for feature in FEATURES:
            command.extend(["--disable", feature])
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, cwd=self.directory.name,
                                            env=translation_environment(), start_new_session=True)
            os.set_blocking(self.process.stdin.fileno(), False)
            self.deadline = time.monotonic() + 15
            self.call("initialize", {"clientInfo": {"name": "dev_lingo", "version": "0.1.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
            # app-server has no --ignore-user-config. Disable every configured
            # MCP server at thread creation, including names needing quoting.
            effective = self.call("config/read", {"includeLayers": False, "cwd": self.directory.name})["config"]
            overrides = {"features." + feature: False for feature in FEATURES}
            overrides["mcp_servers"] = {
                name: dict({key: settings[key] for key in ("command", "url") if key in settings}, enabled=False)
                for name, settings in effective.get("mcp_servers", {}).items()
            }
            overrides.update(config)
            response = self.call("thread/start", {
                "cwd": self.directory.name, "ephemeral": True, "model": self.model,
                "modelProvider": "openai", "serviceTier": "default",
                "approvalPolicy": "never", "sandbox": "read-only", "config": overrides,
                "baseInstructions": coach_path().read_text(), "developerInstructions": "",
            })
            thread = response["thread"]
            if not thread.get("ephemeral"):
                raise RuntimeError("번역 실행의 임시 기록 설정을 확인하지 못했습니다.")
            self.thread_id = thread["id"]
            self.events.clear()
        except BaseException:
            self.close()
            raise

    def send(self, message):
        raw = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdin, selectors.EVENT_WRITE)
            while raw:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise RuntimeError("번역 준비 또는 응답 시간이 초과되었습니다.")
                raw = raw[os.write(self.process.stdin.fileno(), raw[:65536]):]

    def receive(self):
        while b"\n" not in self.pending:
            remaining = self.deadline - time.monotonic()
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if remaining <= 0 or not selector.select(remaining):
                    raise RuntimeError("번역 준비 또는 응답 시간이 초과되었습니다.")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("번역 전용 Codex 연결이 종료되었습니다.")
            self.pending += chunk
            if len(self.pending) > 2 * 1024 * 1024:
                raise RuntimeError("번역 실행의 출력이 너무 깁니다.")
        line, self.pending = self.pending.split(b"\n", 1)
        message = json.loads(line)
        method = message.get("method", "")
        if "id" in message and method:
            raise RuntimeError("번역 실행에서 도구 또는 승인 요청이 감지되었습니다.")
        if method in {"hook/started", "item/started", "item/completed"}:
            item = message.get("params", {}).get("item", {})
            if method == "hook/started" or item.get("type") not in {"agentMessage", "userMessage", "reasoning", None}:
                raise RuntimeError("번역 실행에서 훅 또는 도구 사용이 감지되었습니다.")
        return message

    def call(self, method, params):
        self.sequence += 1
        identity = self.sequence
        self.send({"id": identity, "method": method, "params": params})
        while True:
            message = self.receive()
            if message.get("id") == identity:
                if "error" in message:
                    raise RuntimeError("Codex 번역 요청을 처리하지 못했습니다.")
                return message["result"]
            if len(self.events) >= 1024:
                raise RuntimeError("번역 실행의 알림이 너무 많습니다.")
            self.events.append(message)

    def translate(self, prompt, timeout=40):
        if self.used:
            raise RuntimeError("이미 사용한 번역 실행은 재사용할 수 없습니다.")
        self.used = True
        self.deadline = time.monotonic() + timeout
        self.call("turn/start", {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": json.dumps({"text_to_rewrite": prompt}, ensure_ascii=False)}],
            "model": self.model, "effort": "low", "serviceTierForTurn": "default",
            "outputSchema": json.loads((ROOT / "prompts/output.schema.json").read_text()),
        })
        answer = None
        received = 0
        while True:
            message = self.events.popleft() if self.events else self.receive()
            received += len(json.dumps(message))
            if received > 4 * 1024 * 1024:
                raise RuntimeError("번역 실행의 출력이 너무 깁니다.")
            params = message.get("params", {})
            if params.get("threadId") != self.thread_id:
                continue
            if message.get("method") == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                answer = params["item"].get("text")
            if message.get("method") == "turn/completed":
                turn = params["turn"]
                if turn.get("status") != "completed" or turn.get("error"):
                    raise RuntimeError("Codex 번역 실행에 실패했습니다.")
                finals = [item.get("text") for item in turn.get("items", []) if item.get("type") == "agentMessage"]
                answer = finals[-1] if finals else answer
                if not isinstance(answer, str):
                    raise RuntimeError("완료된 번역 결과를 받지 못했습니다.")
                return json.loads(answer)

    def close(self):
        if self.process is not None:
            stop_process(self.process)
            self.process = None
        self.pending = b""
        self.events.clear()
        self.directory.cleanup()
