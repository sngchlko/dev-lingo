#!/usr/bin/env python3
"""Build a portable marketplace ZIP with a reviewed file allowlist."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "README.md", "README.ko.md", ".agents/plugins/marketplace.json",
    "plugins/dev-lingo/.codex-plugin/plugin.json", "plugins/dev-lingo/hooks/hooks.json",
    "plugins/dev-lingo/scripts/dev_lingo.py", "plugins/dev-lingo/scripts/lingo_core.py",
    "plugins/dev-lingo/scripts/codex_provider.py", "plugins/dev-lingo/scripts/hosts.py",
    "plugins/dev-lingo/scripts/codex_prepared.py", "plugins/dev-lingo/scripts/prewarm.py",
    "plugins/dev-lingo/scripts/lingo_process.py",
    "plugins/dev-lingo/prompts/coach.txt",
    "plugins/dev-lingo/prompts/output.schema.json", "scripts/install.py",
    "scripts/codex_rpc.py", "scripts/package.py", "tests/test_dev_lingo.py",
    "tests/check_context_isolation.py", "tests/evaluate_prompts.py", "tests/evaluate_languages.py",
    "tests/test_codex_stream.py", "tests/evaluate_latency.py", "tests/test_prewarm.py",
    "tests/check_prepared_isolation.py", "tests/evaluate_prewarm.py", "tests/check_lifecycle.py", "tests/test_windows.py"
]


if __name__ == "__main__":
    output = ROOT / "dist/dev-lingo-0.1.4.zip"
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(ROOT / name, "dev-lingo/" + name)
    print(json.dumps({"package": str(output), "files": len(FILES),
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}, indent=2))
