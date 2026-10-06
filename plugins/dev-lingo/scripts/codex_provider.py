"""Codex-only translator: each request starts a separate, isolated Codex run."""
import json
import os
from pathlib import Path
import signal
import selectors
import subprocess
import tempfile
import time
import queue
import threading

from lingo_core import ROOT, coach_path, translation_environment
from lingo_process import find_codex, is_windows, spawn_isolated, stop_windows

DEFAULT_MODEL = "gpt-6.1-sol"


def codex_path():
    return find_codex()


def translation_command(workdir, output=None):
    command = [codex_path(), "exec", "--ignore-user-config", "--ignore-rules",
               "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
               "--color", "never", "--json", "--cd", str(workdir),
               "--output-schema", str(ROOT / "prompts/output.schema.json"),
               "-c", "model_instructions_file=" + json.dumps(str(coach_path())),
               "-c", "project_doc_max_bytes=0", "-c", "approval_policy=\"never\"",
               "-c", "web_search=\"disabled\"", "-c", "model_reasoning_effort=\"low\""]
    # Avoid inherited OMX hooks, plugin workflows, memories, skills search, and shell execution.
    for feature in ["hooks", "plugins", "apps", "memories", "multi_agent", "shell_tool", "skill_search"]:
        command.extend(["--disable", feature])
    command.extend(["--model", os.environ.get("DEV_LINGO_MODEL") or DEFAULT_MODEL])
    # File output is only needed by the offline evaluation scripts.
    if output is not None:
        command.extend(["--output-last-message", str(output)])
    command.append("-")
    return command


def read_result(process, timeout=45, input_data=None):
    """Accept a final message only after Codex confirms the turn completed."""
    if is_windows():
        return read_windows_result(process, timeout, input_data)
    deadline = time.monotonic() + timeout
    pending = b""
    received = 0
    answer = None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if input_data is not None:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
        while True:
            remaining = deadline - time.monotonic()
            ready = selector.select(max(0, remaining))
            if remaining <= 0 or not ready:
                raise RuntimeError("영어 변환 시간이 초과되었습니다. 원래 Codex 작업에는 영향이 없습니다.")
            if any(key.fileobj is process.stdin for key, _ in ready):
                written = os.write(process.stdin.fileno(), input_data[:65536])
                input_data = input_data[written:]
                if not input_data:
                    selector.unregister(process.stdin)
                    process.stdin.close()
            if not any(key.fileobj is process.stdout for key, _ in ready):
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("별도 Codex 실행이 번역 완료 전에 종료되었습니다.")
            received += len(chunk)
            if received > 2 * 1024 * 1024:
                raise RuntimeError("번역 실행의 출력이 너무 깁니다.")
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                item = event.get("item") or {}
                item_type = item.get("type") if isinstance(item, dict) else None
                if item_type in {"command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call"}:
                    raise RuntimeError("번역 전용 실행에서 도구 사용이 감지되어 결과를 제외했습니다.")
                if event.get("type") in {"error", "turn.failed"}:
                    raise RuntimeError("별도 Codex 실행에 실패했습니다. CLI 로그인과 사용량 한도를 확인해 주세요.")
                if event.get("type") == "item.completed" and item_type == "agent_message":
                    answer = item.get("text")
                if event.get("type") == "turn.completed":
                    if not isinstance(answer, str):
                        raise RuntimeError("완료된 번역 결과를 받지 못했습니다.")
                    return json.loads(answer)


def read_windows_result(process, timeout=45, input_data=None):
    """Windows pipes need blocking reader/writer threads, not selectors."""
    deadline = time.monotonic() + timeout
    chunks = queue.Queue(maxsize=32)
    stopped = threading.Event()

    def enqueue(value):
        while not stopped.is_set():
            try:
                chunks.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def reader():
        try:
            while not stopped.is_set():
                chunk = os.read(process.stdout.fileno(), 65536)
                enqueue(chunk)
                if not chunk:
                    return
        except (OSError, ValueError) as error:
            enqueue(error)

    def writer():
        try:
            process.stdin.write(input_data)
            process.stdin.flush()
        except (OSError, ValueError) as error:
            enqueue(error)
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=reader, daemon=True)]
    if input_data is not None:
        threads.append(threading.Thread(target=writer, daemon=True))
    process._dev_lingo_pipe_threads = (stopped, threads)
    for thread in threads:
        thread.start()
    pending = b""
    received = 0
    answer = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("영어 변환 시간이 초과되었습니다.")
            try:
                chunk = chunks.get(timeout=remaining)
            except queue.Empty:
                raise RuntimeError("영어 변환 시간이 초과되었습니다.")
            if isinstance(chunk, Exception) or not chunk:
                raise RuntimeError("별도 Codex 실행이 번역 완료 전에 종료되었습니다.")
            received += len(chunk)
            if received > 2 * 1024 * 1024:
                raise RuntimeError("번역 실행의 출력이 너무 깁니다.")
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                item = event.get("item") or {}
                item_type = item.get("type") if isinstance(item, dict) else None
                if item_type in {"command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call"}:
                    raise RuntimeError("번역 전용 실행에서 도구 사용이 감지되어 결과를 제외했습니다.")
                if event.get("type") in {"error", "turn.failed"}:
                    raise RuntimeError("별도 Codex 실행에 실패했습니다.")
                if event.get("type") == "item.completed" and item_type == "agent_message":
                    answer = item.get("text")
                if event.get("type") == "turn.completed":
                    if not isinstance(answer, str):
                        raise RuntimeError("완료된 번역 결과를 받지 못했습니다.")
                    return json.loads(answer)
    finally:
        stopped.set()


def stop_process(process):
    """Reap our isolated process without waiting for CLI shutdown bookkeeping."""
    if is_windows():
        return stop_windows(process)
    def signal_group(value):
        try:
            os.killpg(process.pid, value)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Some sandboxes reject a group that has already disappeared.
            # A live child remains ours to signal directly; never signal a
            # possibly reused group after its leader has been reaped.
            if process.poll() is None:
                process.send_signal(value)
    try:
        # A leader can exit before its helpers. Signal its private process
        # group even when poll() has already reaped the leader.
        signal_group(signal.SIGTERM)
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            signal_group(signal.SIGKILL)
            process.wait()
    finally:
        # Reap stubborn helpers that outlived the group leader's graceful exit.
        try:
            signal_group(signal.SIGKILL)
        finally:
            process.stdin.close()
            process.stdout.close()


def translate_once(prompt):
    with tempfile.TemporaryDirectory(prefix="dev-lingo-") as directory:
        workdir = Path(directory)
        process = spawn_isolated(translation_command(workdir),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env=translation_environment())
        try:
            payload = json.dumps({"text_to_rewrite": prompt}, ensure_ascii=False).encode("utf-8")
            return read_result(process, input_data=payload)
        finally:
            stop_process(process)


def translate(prompt):
    if not is_windows() and os.environ.get("DEV_LINGO_PREWARM") != "0":
        from prewarm import translate_if_ready
        result = translate_if_ready(prompt)
        if result is not None:
            return result
    return translate_once(prompt)
