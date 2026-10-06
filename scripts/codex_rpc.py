"""Small stdio client for supported Codex app-server management methods."""
import json
import queue
import subprocess
import threading


class Client:
    def __init__(self, command=None, env=None):
        self.process = subprocess.Popen(command or ["codex", "app-server"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        self.messages = queue.Queue()
        self.notifications = []
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": "dev_lingo_setup", "version": "0.1.0"},
                                 "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized"})

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
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
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
