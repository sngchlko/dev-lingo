#!/usr/bin/env python3
"""Sequential paired latency comparison, including private worker IPC."""
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/dev-lingo/scripts"))
from codex_provider import translate_once
from lingo_core import coach_path, validate_result
from prewarm import request, stop, translate_if_ready, warm
from evaluate_latency import CASES


def main():
    rows = []
    path = ROOT / "reports/prewarm-evaluation.json"
    try:
        for pair in range(8):
            case, prompt = CASES[pair % len(CASES)]
            variants = ["current", "prepared"] if pair % 2 == 0 else ["prepared", "current"]
            for variant in variants:
                preparation = 0
                if variant == "prepared":
                    before = time.monotonic()
                    warm()
                    deadline = before + 20
                    while request({"action": "status"}) is None:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Prepared worker did not become ready")
                        time.sleep(0.1)
                    # Represents time while the user types; production has no sleep.
                    time.sleep(3)
                    preparation = time.monotonic() - before
                started = time.monotonic()
                row = {"pair": pair, "case": case, "variant": variant,
                       "preparation_before_input_seconds": round(preparation, 3)}
                try:
                    result = translate_if_ready(prompt) if variant == "prepared" else translate_once(prompt)
                    if result is None:
                        raise RuntimeError("Prepared worker unavailable; fallback not included")
                    row["result"] = validate_result(result)
                except Exception as error:
                    row["error"] = str(error)
                row["seconds"] = round(time.monotonic() - started, 3)
                rows.append(row)
                print(json.dumps({k: v for k, v in row.items() if k != "result"}), flush=True)
    finally:
        # A just-finished request can be preparing the next empty worker.
        for _ in range(30):
            if stop():
                break
            time.sleep(0.1)
    summary = {}
    for variant in ["current", "prepared"]:
        subset = [row for row in rows if row["variant"] == variant]
        times = [row["seconds"] for row in subset if "result" in row]
        summary[variant] = {"runs": len(subset), "successes": len(times),
                            "mean_seconds": round(statistics.mean(times), 3) if times else None,
                            "median_seconds": round(statistics.median(times), 3) if times else None}
    report = {"evaluated_at": datetime.now(timezone.utc).isoformat(), "model": "gpt-6.1-sol",
              "reasoning_effort": "low", "service_tier": "default", "coach": coach_path().read_text(),
              "cases": CASES, "summary": summary, "results": rows,
              "method": "Eight sequential pairs, alternating order. Current=exec streaming completion. Prepared=one-use empty ephemeral app-server via local Unix socket, including validation and process cleanup.",
              "limitations": "Prepared timing begins after preparation plus 3 seconds of simulated typing time. Cold requests and requests after idle expiry fall back to current exec. Network/backend load affects results; small sample."}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(path), "summary": summary}), flush=True)
    return 0 if all("result" in row for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
