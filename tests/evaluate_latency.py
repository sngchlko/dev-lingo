#!/usr/bin/env python3
"""Compare isolated translator latency; saves only explicit test fixtures."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import signal
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/dev-lingo/scripts"))
from codex_provider import translation_command, translate
from lingo_core import translation_environment, validate_result

CASES = [
    ("reported_korean", "아니야 이건 코덱스에서 사람말을 영어로 번역해주는 시스템이야. 훅을 활용해서"),
    ("english", "Thanks! I think this way have problem. Can you check it one more time?"),
    ("constraints", "UserService.getUser()에서 timeout이 나. Redis 때문일 수도 있는데 아직 확실하지 않아. API는 바꾸지 말고 서버 재시작하지 말고 로그만 봐줄래?"),
    ("japanese", "この方法は何かおかしい気がします。もう一度見てもらえますか？"),
]
def run(case, variant, repeat):
    row = {"case": case[0], "variant": variant, "repeat": repeat}
    if variant == "stream":
        started = time.monotonic()
        try:
            row["result"] = validate_result(translate(case[1]))
        except (RuntimeError, ValueError) as error:
            row["error"] = str(error)
        row["seconds"] = round(time.monotonic() - started, 3)
        return row
    with tempfile.TemporaryDirectory(prefix="dev-lingo-latency-") as directory:
        workdir = Path(directory)
        output = workdir / "answer.json"
        command = translation_command(workdir, output)
        started = time.monotonic()
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True,
                                   env=translation_environment(), start_new_session=True)
        def stop():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        timer = threading.Timer(45, stop)
        timer.start()
        try:
            process.stdin.write(json.dumps({"text_to_rewrite": case[1]}, ensure_ascii=False))
            process.stdin.close()
            unexpected_tool = False
            for line in process.stdout:
                event = json.loads(line)
                elapsed = round(time.monotonic() - started, 3)
                kind = event.get("item", {}).get("type")
                if event.get("type") == "turn.started":
                    row["started_seconds"] = elapsed
                if kind == "agent_message":
                    row["answer_seconds"] = elapsed
                if kind in {"command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call"}:
                    unexpected_tool = True
                if event.get("type") == "turn.completed":
                    row["completed_seconds"] = elapsed
                    row["usage"] = event.get("usage")
            process.wait()
            row["seconds"] = round(time.monotonic() - started, 3)
            if process.returncode or unexpected_tool:
                row["error"] = "Failed or unexpected tool use"
            else:
                row["result"] = validate_result(json.loads(output.read_text()))
        finally:
            timer.cancel()
            if process.poll() is None:
                stop()
            process.wait()
            process.stdout.close()
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    tasks = [(case, variant, repeat) for case in CASES
             for variant in ["baseline", "stream"] for repeat in range(args.repeats)]
    random.Random(20261007).shuffle(tasks)
    rows = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run, *task) for task in tasks]):
            row = future.result()
            rows.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != "result"}), flush=True)
    summary = {}
    for variant in ["baseline", "stream"]:
        good = [r for r in rows if r["variant"] == variant and "result" in r]
        summary[variant] = {"successes": len(good), "runs": len(rows) // 2}
        for field in ["seconds", "completed_seconds"]:
            values = [r[field] for r in good if field in r]
            summary[variant]["mean_" + field] = round(statistics.mean(values), 3) if values else None
            summary[variant]["median_" + field] = round(statistics.median(values), 3) if values else None
    report = {"evaluated_at": datetime.now(timezone.utc).isoformat(), "cases": CASES,
              "method": "Shuffled runs, two workers; CLI defaults, same coach and schema. End-to-end wall times include startup and shutdown.",
              "candidate": "Return on confirmed turn.completed; stop and reap the isolated process. No model, effort, coach, schema, or feature changes.",
              "summary": summary,
              "results": sorted(rows, key=lambda r: (r["case"], r["variant"], r["repeat"])),
              "limitations": "Small sample; backend load and output length affect timings."}
    path = ROOT / "reports/latency-evaluation.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(path), "summary": summary}), flush=True)
    return 0 if all("result" in r for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
