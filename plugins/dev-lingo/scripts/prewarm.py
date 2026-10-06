"""User-private, idle-limited holder of one unused translation process."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

from lingo_core import ROOT, MAX_PROMPT, coach_path, translation_environment, validate_result

IDLE_SECONDS = 120
MAX_WIRE = 2 * 1024 * 1024


def runtime_directory():
    from codex_provider import codex_path
    binary = Path(codex_path())
    digest = hashlib.sha256()
    for path in sorted((ROOT / "scripts").glob("*.py")) + [coach_path(), ROOT / "prompts/output.schema.json"]:
        digest.update(path.read_bytes())
    config = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    digest.update(str((str(ROOT), str(binary), binary.stat().st_mtime_ns,
                       os.environ.get("DEV_LINGO_MODEL", ""), str(config),
                       config.stat().st_mtime_ns if config.exists() else None)).encode())
    directory = Path("/tmp") / ("dev-lingo-%s-%s" % (os.getuid(), digest.hexdigest()[:20]))
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("번역 준비 폴더의 접근 권한이 올바르지 않습니다.")
    return directory


def warm():
    """Keep one worker; reap any child we launch even in long-lived callers."""
    if os.environ.get("DEV_LINGO_PREWARM") == "0":
        return
    directory = runtime_directory()
    try:
        lock = os.open(str(directory / "worker.lock"), os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        lock = None
    if lock is not None:
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        finally:
            os.close(lock)
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", str(directory)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=translation_environment(), start_new_session=True, close_fds=True)
    threading.Thread(target=process.wait, name="dev-lingo-reaper", daemon=True).start()


def send(connection, value):
    connection.sendall(json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n")


def receive(connection, limit=MAX_WIRE):
    raw = bytearray()
    deadline = time.monotonic() + connection.gettimeout()
    while not raw.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("Translation connection timed out")
        connection.settimeout(remaining)
        chunk = connection.recv(min(65536, limit + 1 - len(raw)))
        if not chunk:
            raise ConnectionError("번역 준비 연결이 종료되었습니다.")
        raw.extend(chunk)
        if len(raw) > limit:
            raise ValueError("번역 준비 요청이 너무 깁니다.")
    return json.loads(raw)


def request(value):
    """A busy/unready worker gets no prompt; the caller can safely use exec."""
    directory = runtime_directory()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.15)
        try:
            connection.connect(str(directory / "worker.sock"))
            if receive(connection, 1024) != {"ready": True}:
                return None
        except (OSError, ValueError):
            return None
        # Once input may have been submitted, never retry with a second model call.
        connection.settimeout(43)
        try:
            send(connection, value)
            response = receive(connection)
        except (OSError, ValueError) as error:
            raise RuntimeError("준비된 번역 실행을 완료하지 못했습니다.") from error
        if "error" in response:
            raise RuntimeError("준비된 번역 실행에 실패했습니다.")
        return response


def translate_if_ready(prompt):
    try:
        warm()
    except (OSError, RuntimeError):
        return None
    response = request({"action": "translate", "prompt": prompt})
    return validate_result(response["result"]) if response is not None else None


def stop():
    return request({"action": "stop"}) is not None


def serve(directory):
    from codex_prepared import PreparedTranslation
    os.umask(0o077)
    lock = os.open(str(directory / "worker.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        return
    prepared = None
    endpoint = directory / "worker.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    def interrupted(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        endpoint.unlink(missing_ok=True)
        server.bind(str(endpoint))
        server.listen(8)
        server.settimeout(0.5)
        prepared = PreparedTranslation()
        last_used = time.monotonic()
        while time.monotonic() - last_used < IDLE_SECONDS:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(2)
                try:
                    if prepared.process is None or prepared.process.poll() is not None:
                        prepared.close()
                        prepared = PreparedTranslation()
                    send(connection, {"ready": True})
                    value = receive(connection, 128 * 1024)
                except (OSError, ValueError):
                    continue
                if value == {"action": "stop"}:
                    send(connection, {"stopped": True})
                    break
                if value == {"action": "status"}:
                    send(connection, {"prepared": True})
                    continue
                prompt = value.get("prompt") if isinstance(value, dict) else None
                if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT or value.get("action") != "translate":
                    send(connection, {"error": True})
                    continue
                result = None
                try:
                    result = validate_result(prepared.translate(prompt))
                    prepared.close()
                    prepared = None
                    send(connection, {"result": result})
                except Exception:
                    if prepared is not None:
                        prepared.close()
                        prepared = None
                    try:
                        send(connection, {"error": True})
                    except OSError:
                        pass
                finally:
                    # The worker keeps only an unused process between requests.
                    prompt = value = result = None
            prepared = PreparedTranslation()
            last_used = time.monotonic()
    finally:
        if prepared is not None:
            prepared.close()
        server.close()
        endpoint.unlink(missing_ok=True)
        os.close(lock)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]))
