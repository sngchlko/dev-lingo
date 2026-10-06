#!/usr/bin/env python3
"""Compare translation models without changing source or installed settings."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/dev-lingo"
INSTALLED = Path.home() / ".codex/plugins/cache/dev-lingo-local/dev-lingo/0.1.0"
sys.path.insert(0, str(PLUGIN / "scripts"))
spec = importlib.util.spec_from_file_location("lingo", PLUGIN / "scripts/dev_lingo.py")
lingo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lingo)

CASES = [
    {"id": "reported_korean", "input": "아니야 이건 코덱스에서 사람말을 영어로 번역해주는 시스템이야. 훅을 활용해서",
     "criteria": "Conversational correction; keep Codex, hooks, and translation into English."},
    {"id": "awkward_english_and_thanks", "input": "Thanks! I think this way have problem. Can you check it one more time?",
     "criteria": "Correct awkward English, preserve uncertainty and recheck request, omit unnecessary explanation for thanks."},
    {"id": "technical_uncertainty", "input": "UserService.getUser()에서 timeout이 나. Redis 때문일 수도 있는데 아직 확실하지 않아. API는 바꾸지 말고 서버 재시작하지 말고 로그만 봐줄래?",
     "criteria": "Keep identifier, timeout, uncertainty about Redis, logs only, no API changes, no server restart."},
    {"id": "long_constraints", "input": "이번 배포 전에 캐시를 붙이는 게 좋을지 검토해주고, 아직 병목이 어디인지 확인하지 못했으니 먼저 로그만 살펴본 다음에 판단하되 서버를 재시작하거나 API 응답 형식을 바꾸지는 말아줘.",
     "criteria": "Keep deployment timing, uncertain caching decision, unknown bottleneck, logs first, no restart or API response changes."},
]


def fingerprints():
    paths = [base / name for base in [PLUGIN, INSTALLED]
             for name in ["scripts/dev_lingo.py", "prompts/coach.txt", "prompts/output.schema.json"]]
    paths.append(ROOT / "dist/dev-lingo-0.1.0.zip")
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.exists()}


def run(case, model, effort, repeat):
    row = {"case": case["id"], "model": model, "effort": effort, "repeat": repeat}
    with tempfile.TemporaryDirectory(prefix="dev-lingo-model-test-") as directory:
        workdir = Path(directory)
        output = workdir / "answer.json"
        command = lingo.translation_command(workdir, output)
        if "--model" in command:
            command[command.index("--model") + 1] = model
        else:
            command[-1:-1] = ["--model", model]
        command[-1:-1] = ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        started = time.monotonic()
        try:
            process = subprocess.run(command, input=json.dumps({"text_to_rewrite": case["input"]}, ensure_ascii=False),
                                     text=True, capture_output=True, env=lingo.translation_environment(), timeout=45)
            row["seconds"] = round(time.monotonic() - started, 3)
            for line in process.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("item", {}).get("type") in {"command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call"}:
                    raise RuntimeError("Unexpected tool use in translation")
                if event.get("type") == "turn.completed":
                    row["usage"] = event.get("usage")
                if event.get("type") in {"error", "turn.failed"}:
                    row.setdefault("errors", []).append(event)
            if process.returncode != 0:
                row["error"] = "Translation process exited with code " + str(process.returncode)
            else:
                row["result"] = lingo.validate_result(json.loads(output.read_text()))
        except (subprocess.TimeoutExpired, ValueError, RuntimeError) as error:
            row["seconds"] = round(time.monotonic() - started, 3)
            row["error"] = type(error).__name__ + ": " + str(error)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["gpt-6.1-sol", "gpt-6-luna"])
    parser.add_argument("--effort", default="low")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    before = fingerprints()
    tasks = [(case, model, args.effort, repeat) for repeat in range(1, args.repeats + 1)
             for case in CASES for model in args.models]
    random.Random(20261006).shuffle(tasks)
    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run, *task) for task in tasks]):
            row = future.result()
            results.append(row)
            print(json.dumps({key: row[key] for key in ["case", "model", "repeat", "seconds"]} |
                             {"success": "result" in row}, ensure_ascii=False), flush=True)
    assert fingerprints() == before, "Source, installed plugin, or ZIP changed during evaluation"
    summary = {}
    for model in args.models:
        rows = [row for row in results if row["model"] == model]
        times = [row["seconds"] for row in rows if "result" in row]
        summary[model] = {"successes": len(times), "runs": len(rows),
                          "mean_seconds": round(statistics.mean(times), 3) if times else None,
                          "median_seconds": round(statistics.median(times), 3) if times else None}
    report = {"evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
              "codex_version": subprocess.run(["codex", "--version"], capture_output=True, text=True, check=True).stdout.strip(),
              "method": "Same coach, output schema, low effort, and isolated CLI flags; shuffled runs, two concurrent workers; includes CLI startup and request time.",
              "coach": (PLUGIN / "prompts/coach.txt").read_text(), "cases": CASES,
              "results": sorted(results, key=lambda row: (row["case"], row["model"], row["repeat"])),
              "summary": summary, "source_installed_and_zip_unchanged": True,
              "limitations": "Small qualitative sample; network load, backend load, caching, and output length can affect timings."}
    path = ROOT / "reports/translation-model-comparison.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(path), "summary": summary}, ensure_ascii=False), flush=True)
    return 0 if all("result" in row for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
