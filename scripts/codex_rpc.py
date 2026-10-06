"""Small stdio client for supported Codex app-server management methods."""
import json
import queue
import subprocess
import threading
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/dev-lingo/scripts"))
from lingo_process import find_codex, spawn_isolated
from codex_provider import stop_process


class Client:
    def __init__(self, command=None, env=None, capabilities=None):
        self.process = spawn_isolated(command or [find_codex(), "app-server"], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env)
        self.messages = queue.Queue()
        self.notifications = []
        self.counter = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.call("initialize", {"clientInfo": {"name": "dev_lingo_setup", "version": "0.1.0"},
                                 "capabilities": dict({"experimentalApi": True}, **(capabilities or {}))})
        self.send({"method": "initialized"})

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        except (OSError, ValueError):
            pass
        self.messages.put({"error": {"message": "Codex app-server closed"}})

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method, params, timeout=30):
        self.counter += 1
        identity = self.counter
        self.send({"id": identity, "method": method, "params": params})
        while True:
            message = self.messages.get(timeout=timeout)
            if message.get("id") == identity:
                if "error" in message:
                    raise RuntimeError(message["error"]["message"])
                return message["result"]
            if "error" in message and "method" not in message and "id" not in message:
                raise RuntimeError(message["error"]["message"])
            self.notifications.append(message)

    def close(self):
        stop_process(self.process)
        self.reader.join(timeout=2)
