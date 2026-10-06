#!/usr/bin/env python3
"""Route a host hook to its own isolated translator and native UI adapter."""
import argparse
import json
import os
import sys

from hosts import get_host
from lingo_core import MAX_INPUT, MAX_PROMPT, MAX_SENTENCES, ROOT, coach_path, translation_environment, validate_result
# Keep the existing Codex benchmark helpers available; host routing uses get_host.
from codex_provider import codex_path, translation_command
from lingo_process import is_windows


def translate(prompt, host="codex"):
    adapter = get_host(host)
    return validate_result(adapter.provider.translate(prompt))


def hook(host="codex"):
    # Hook failures remain silent so the original task is never rejected.
    try:
        if os.environ.get("DEV_LINGO_TRANSLATOR") == "1":
            return 0
        adapter = get_host(host)
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return 0
        prompt = adapter.prompt_from_event(json.loads(raw))
        if prompt is None:
            return 0
        result = translate(prompt, host=host)
        # Only the host's native UI adapter determines the output protocol.
        print(json.dumps(adapter.notification(result), ensure_ascii=False))
    except Exception:
        pass
    return 0


def main():
    parser = argparse.ArgumentParser(description="Dev Lingo: isolated spoken-English coach")
    parser.add_argument("action", choices=["hook", "translate", "prompt-path", "prepare", "stop"])
    parser.add_argument("text", nargs="?")
    parser.add_argument("--host", default="codex", help="Execution host; codex is connected, claude is reserved for future support")
    args = parser.parse_args()
    if args.action == "hook":
        return hook(args.host)
    if args.action in {"prepare", "stop"}:
        if not is_windows() and args.host == "codex" and os.environ.get("DEV_LINGO_TRANSLATOR") != "1":
            try:
                from prewarm import warm, stop
                warm() if args.action == "prepare" else stop()
            except Exception:
                pass
        return 0
    if args.action == "prompt-path":
        print(coach_path())
    else:
        try:
            result = translate(args.text or sys.stdin.read(), host=args.host)
        except (NotImplementedError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if is_windows():
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        try:
            sys.exit(main())
        except Exception:
            sys.exit(0)
    else:
        sys.exit(main())
