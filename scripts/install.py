#!/usr/bin/env python3
"""Register and install this local marketplace with native Codex commands."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from codex_rpc import Client

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "dev-lingo@dev-lingo-local"


def verify_installed_copy(hook):
    source = ROOT / "plugins/dev-lingo"
    installed = Path(hook["sourcePath"]).resolve().parent.parent
    for name in [".codex-plugin/plugin.json", "hooks/hooks.json", "scripts/dev_lingo.py",
                 "scripts/lingo_core.py", "scripts/codex_provider.py", "scripts/codex_prepared.py",
                 "scripts/prewarm.py", "scripts/hosts.py",
                 "prompts/coach.txt", "prompts/output.schema.json"]:
        expected = hashlib.sha256((source / name).read_bytes()).digest()
        actual = hashlib.sha256((installed / name).read_bytes()).digest()
        if expected != actual:
            raise RuntimeError("Installed plugin differs from the reviewed package: " + name)
    definition = json.loads((installed / "hooks/hooks.json").read_text())
    if set(definition["hooks"]) != {"SessionStart", "UserPromptSubmit"}:
        raise RuntimeError("Unexpected hook definition")
    for event, action in [("SessionStart", "prepare"), ("UserPromptSubmit", "hook")]:
        handlers = definition["hooks"][event]
        if len(handlers) != 1 or len(handlers[0]["hooks"]) != 1:
            raise RuntimeError("Unexpected hook definition")
        expected_command = 'python3 "${PLUGIN_ROOT}/scripts/dev_lingo.py" ' + action + ' --host codex'
        if handlers[0]["hooks"][0]["command"] != expected_command:
            raise RuntimeError("Unexpected hook command")


def main():
    parser = argparse.ArgumentParser(description="Install Dev Lingo for local Codex chats")
    parser.add_argument("--trust-hook", action="store_true", help="Trust only this package's matching installed hook after reviewing its source")
    args = parser.parse_args()
    if not shutil.which("codex"):
        parser.error("Codex CLI must be installed and available in PATH")
    for command in [["codex", "plugin", "marketplace", "add", str(ROOT), "--json"],
                    ["codex", "plugin", "add", PLUGIN_ID, "--json"]]:
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            return result.returncode
    client = Client()
    try:
        listed = client.call("hooks/list", {"cwds": [str(ROOT)]})
        hooks = [hook for entry in listed["data"] for hook in entry["hooks"]
                 if hook.get("pluginId") == PLUGIN_ID and hook.get("source") == "plugin"]
        if len(hooks) != 2:
            raise RuntimeError("Expected exactly two installed Dev Lingo hooks, got " + str(len(hooks)))
        edits = []
        for hook in hooks:
            verify_installed_copy(hook)
            if args.trust_hook and hook["trustStatus"] not in {"managed", "trusted"}:
                edits.append({"keyPath": "hooks.state." + json.dumps(hook["key"]) + ".trusted_hash",
                              "mergeStrategy": "upsert", "value": hook["currentHash"]})
        if edits:
            client.call("config/batchWrite", {"edits": edits, "reloadUserConfig": True})
            listed = client.call("hooks/list", {"cwds": [str(ROOT)]})
            hooks = [h for entry in listed["data"] for h in entry["hooks"] if h.get("pluginId") == PLUGIN_ID]
            if not all(h["trustStatus"] in {"trusted", "managed"} for h in hooks):
                raise RuntimeError("Hook trust was not applied")
        print(json.dumps({"plugin": PLUGIN_ID, "installed": True, "enabled": all(h["enabled"] for h in hooks),
                          "hook_trust": "trusted" if all(h["trustStatus"] in {"trusted", "managed"} for h in hooks) else "untrusted",
                          "hook_count": len(hooks), "hook_source": hooks[0]["sourcePath"]}, ensure_ascii=False, indent=2))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
