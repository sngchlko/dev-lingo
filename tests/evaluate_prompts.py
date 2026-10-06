#!/usr/bin/env python3
"""Compare two spoken-English prompt drafts using isolated real Codex runs.

Does not edit the source coach prompt, installed plugin, or distribution ZIP.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/dev-lingo/scripts/dev_lingo.py"
INSTALLED = Path.home() / ".codex/plugins/cache/dev-lingo-local/dev-lingo/0.1.0/prompts/coach.txt"
TRACKED = [ROOT / "plugins/dev-lingo/prompts/coach.txt", INSTALLED, ROOT / "dist/dev-lingo-0.1.0.zip"]

OUTPUT_INSTRUCTION = 'Return JSON with "explanation_label" and a "sentences" array. Set "explanation_label" to the word for "Explanation" in the input\'s dominant natural language, without a colon; use "해설" for Korean. Put each final English sentence in its own item, in order; never group multiple sentences in one item. Each item has "english" and "explanation" (one short sentence in the same detected language explaining a useful expression, nuance, or correction in that sentence; use "" if none). Detect the language from the input\'s prose, ignoring code and technical identifiers. Skip generic remarks and mere paraphrases.\n'
PROMPTS = {
    "short": 'Rewrite input in any language as natural, everyday spoken English a senior software architect in Silicon Valley would use. Keep the original meaning and technical details.\n' + OUTPUT_INSTRUCTION,
    "spoken_full": 'You are an English conversation coach for software engineers.\nRewrite input in any language as something a senior software architect in Silicon Valley would naturally say in an everyday conversation.\nUse everyday words, natural contractions, and a conversational tone. Keep it clear and concise.\nPreserve the original meaning and technical details.\n' + OUTPUT_INSTRUCTION,
}
CASES = [
    {"id": "korean_request", "input": "이 코드 너무 복잡해. 기존 동작 유지하면서 단순하게 바꿔줘.", "criteria": "Natural spoken request; simplify code; preserve existing behavior."},
    {"id": "uncertain_question", "input": "캐시를 넣으면 좀 빨라질 것 같은데, 먼저 병목부터 확인해줄래?", "criteria": "Caching benefit remains uncertain; ask to check bottleneck first."},
    {"id": "awkward_english", "input": "I think this way have problem. Can you check it one more time?", "criteria": "Correct awkward English into a natural request without inventing details."},
    {"id": "mixed_identifiers", "input": "UserService의 getUser()에서 timeout이 나는데, API는 바꾸지 말고 원인만 찾아줘.", "criteria": "Preserve UserService/getUser(), timeout, no API changes, diagnosis only."},
    {"id": "everyday_conversation", "input": "오늘은 머리가 안 돌아가네. 이 얘기는 내일 다시 하자.", "criteria": "Everyday idiom, postpone discussion until tomorrow, no forced technical language."},
    {"id": "already_natural", "input": "Sounds good. Let's go with that.", "criteria": "Keep a brief natural acknowledgement; do not inflate or invent corrections."},
    {"id": "uncertainty_and_negation", "input": "아마 Redis 문제일 수도 있는데, 아직 확실하지 않아. 재시작하지 말고 로그만 확인해줄래?", "criteria": "Preserve uncertainty, Redis, logs only, and no restart."},
]


def fingerprints():
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in TRACKED if path.exists()}


def run_case(case, variant, prompt_file):
    env = dict(os.environ, DEV_LINGO_COACH_FILE=str(prompt_file))
    started = time.monotonic()
    process = subprocess.run([sys.executable, str(SCRIPT), "translate", case["input"]],
                             env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=55)
    row = {"case": case["id"], "variant": variant, "seconds": round(time.monotonic()-started, 2)}
    if process.returncode == 0:
        row["result"] = json.loads(process.stdout)
    else:
        row["error"] = "Translation process exited with code " + str(process.returncode)
    return row


def main():
    before = fingerprints()
    version = subprocess.run(["codex", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    results = []
    with tempfile.TemporaryDirectory(prefix="dev-lingo-spoken-test-") as directory:
        paths = {}
        for variant, prompt in PROMPTS.items():
            path = Path(directory) / (variant + ".txt")
            path.write_text(prompt)
            paths[variant] = path
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_case, case, variant, paths[variant]) for case in CASES for variant in PROMPTS]
            for future in as_completed(futures):
                row = future.result()
                results.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    after = fingerprints()
    assert before == after, "Source prompt, installed prompt, or ZIP was modified"
    report = {"codex_version": version, "prompts": PROMPTS, "cases": CASES,
              "results": sorted(results, key=lambda row: (row["case"], row["variant"])),
              "files_unchanged": before == after,
              "successes": sum("result" in row for row in results), "runs": len(results)}
    path = ROOT / "reports/prompt-comparison.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(path), "successes": report["successes"], "runs": report["runs"], "files_unchanged": True}), flush=True)
    return 0 if report["successes"] == report["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
