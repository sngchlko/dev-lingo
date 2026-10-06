#!/usr/bin/env python3
"""Evaluate explanation language on real isolated translation runs."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/dev-lingo/scripts/dev_lingo.py"
CASES = [
    {"id": "korean", "language": "Korean", "input": "이 방식은 뭔가 문제가 있는 것 같아. 다시 한번 봐줄래?"},
    {"id": "english", "language": "English", "input": "I think this way have problem. Can you check it one more time?"},
    {"id": "japanese", "language": "Japanese", "input": "この方法は何かおかしい気がします。もう一度見てもらえますか？"},
    {"id": "spanish", "language": "Spanish", "input": "Creo que hay algo raro en este enfoque. ¿Puedes revisarlo otra vez?"},
    {"id": "french", "language": "French", "input": "Je pense qu'il y a un problème avec cette approche. Tu peux y jeter un autre coup d'œil ?"},
    {"id": "chinese", "language": "Chinese", "input": "这个方法好像有点问题。你能再检查一下吗？"},
    {"id": "korean_with_identifiers", "language": "Korean", "input": "UserService.getUser()에서 timeout이 나. API 바꾸지 말고 logs만 확인해줘."},
]


def run(case):
    started = time.monotonic()
    process = subprocess.run([sys.executable, str(SCRIPT), "translate", case["input"], "--host", "codex"],
                             cwd=ROOT, capture_output=True, text=True, timeout=55)
    row = {"case": case["id"], "expected_explanation_language": case["language"],
           "seconds": round(time.monotonic() - started, 2)}
    if process.returncode == 0:
        row["result"] = json.loads(process.stdout)
    else:
        row["error"] = "Translation process exited with code " + str(process.returncode)
    return row


def main():
    prompt = (ROOT / "plugins/dev-lingo/prompts/coach.txt").read_text()
    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run, case) for case in CASES]):
            row = future.result()
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    report = {"evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
              "codex_version": subprocess.run(["codex", "--version"], capture_output=True, text=True, check=True).stdout.strip(),
              "model_selection": "CLI defaults; no model or reasoning effort override",
              "prompt": prompt, "cases": CASES, "results": sorted(results, key=lambda row: row["case"]),
              "successes": sum("result" in row for row in results), "runs": len(results)}
    path = ROOT / "reports/explanation-language-evaluation.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(path), "successes": report["successes"], "runs": report["runs"]}), flush=True)
    return 0 if report["successes"] == report["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
