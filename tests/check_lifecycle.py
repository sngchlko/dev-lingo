#!/usr/bin/env python3
"""Exercise real worker/process cleanup with a local, non-inference Codex fixture."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/dev-lingo/scripts"))
import prewarm


def process_table():
    raw = subprocess.check_output(["ps", "-axo", "pid=,ppid=,stat=,rss="], text=True)
    return {int(parts[0]): {"pid": int(parts[0]), "ppid": int(parts[1]),
                           "status": parts[2], "rss_kib": int(parts[3])}
            for parts in (line.split() for line in raw.splitlines()) if len(parts) == 4}


def descendants(table, parent):
    found = {parent}
    while True:
        expanded = found | {pid for pid, row in table.items() if row["ppid"] in found}
        if expanded == found:
            return {pid: table[pid] for pid in found if pid in table}
        found = expanded


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Lifecycle check timed out")
        time.sleep(0.01)


def main():
    samples = []
    launched = []
    real_popen = subprocess.Popen
    original_reapers = {thread.ident for thread in threading.enumerate() if thread.name == "dev-lingo-reaper"}
    with tempfile.TemporaryDirectory(prefix="dev-lingo-lifecycle-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp") as directory:
        root = Path(directory)
        runtime = root / "worker"
        runtime.mkdir(mode=0o700)
        record = root / "fixture-pids.jsonl"
        blocked = root / "blocked"
        fake = root / "fake-codex"
        fake.write_text("#!" + sys.executable + "\n" + '''import json,os,sys,time
from pathlib import Path
with Path(%r).open('a') as f:
    f.write(json.dumps({'pid':os.getpid(),'cwd':os.getcwd()})+'\\n')
for line in sys.stdin:
    message=json.loads(line)
    if 'id' not in message: continue
    method=message['method']
    if method=='config/read': result={'config':{'mcp_servers':{}}}
    elif method=='thread/start': result={'thread':{'id':'fixture','ephemeral':True}}
    else: result={}
    print(json.dumps({'id':message['id'],'result':result}),flush=True)
    if method=='turn/start':
        text=json.loads(message['params']['input'][0]['text'])['text_to_rewrite']
        if text=='BLOCK':
            Path(%r).touch()
            time.sleep(20)
            continue
        answer={'explanation_label':'Explanation','sentences':[{'english':'Could you take another look?','explanation':''}]}
        item={'type':'agentMessage','text':json.dumps(answer)}
        print(json.dumps({'method':'item/completed','params':{'threadId':'fixture','item':item}}),flush=True)
        print(json.dumps({'method':'turn/completed','params':{'threadId':'fixture','turn':{'status':'failed' if text=='FAIL' else 'completed','items':[item]}}}),flush=True)
''' % (str(record), str(blocked)))
        fake.chmod(0o700)
        def launch(command, **kwargs):
            process = real_popen(command, **kwargs)
            if "--serve" in command:
                launched.append(process)
            return process
        def ready():
            return prewarm.request({"action": "status"}) == {"prepared": True}
        def own_records():
            return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
        try:
            with patch.object(prewarm, "runtime_directory", return_value=runtime), \
                    patch.object(prewarm.subprocess, "Popen", side_effect=launch), \
                    patch.dict(os.environ, {"DEV_LINGO_CODEX": str(fake), "DEV_LINGO_PREWARM": "1"}):
                prewarm.warm()
                wait_for(ready)
                worker = launched[0]
                for index in range(100):
                    prewarm.warm()
                    fail = index % 10 == 9
                    try:
                        result = prewarm.request({"action": "translate", "prompt": "FAIL" if fail else "x" * 16000})
                        assert not fail and result is not None
                    except RuntimeError:
                        assert fail
                    wait_for(ready)
                    if index % 10 == 9:
                        table = process_table()
                        tree = descendants(table, worker.pid)
                        assert len(tree) == 2, tree
                        assert all(not row["status"].startswith("Z") for row in tree.values())
                        sample = {"completed": index + 1, "worker_rss_kib": table[worker.pid]["rss_kib"],
                                  "process_count": len(tree), "zombie_count": 0}
                        lsof = shutil.which("lsof")
                        if lsof:
                            opened = subprocess.check_output([lsof, "-nP", "-a", "-p", str(worker.pid), "-Ff"], text=True)
                            sample["file_descriptors"] = sum(line[1:].isdigit() for line in opened.splitlines() if line.startswith("f"))
                        samples.append(sample)
                assert len(launched) == 1, "Repeated warm() spawned duplicate workers"
                assert max(s["worker_rss_kib"] for s in samples) - min(s["worker_rss_kib"] for s in samples) < 8192
                if "file_descriptors" in samples[0]:
                    assert len({s["file_descriptors"] for s in samples}) == 1, samples
                # Cancel an active request through an actual SIGTERM.
                errors = []
                def active():
                    try:
                        prewarm.request({"action": "translate", "prompt": "BLOCK"})
                    except RuntimeError:
                        errors.append("cancelled")
                client = threading.Thread(target=active)
                client.start()
                wait_for(blocked.exists)
                worker.terminate()
                worker.wait(timeout=5)
                client.join(5)
                assert not client.is_alive() and errors == ["cancelled"]
                wait_for(lambda: not any(r["pid"] in process_table() for r in own_records()))
                assert not (runtime / "worker.sock").exists()
                # SIGKILL cannot run Python finally blocks. The stdio child's
                # EOF must still close it, and the next warm repairs the socket.
                prewarm.warm()
                wait_for(ready)
                killed = launched[-1]
                os.kill(killed.pid, signal.SIGKILL)
                killed.wait(timeout=5)
                wait_for(lambda: not any(r["pid"] in process_table() for r in own_records()))
                prewarm.warm()
                wait_for(ready)
                assert prewarm.stop()
                launched[-1].wait(timeout=5)
                wait_for(lambda: not any(r["pid"] in process_table() for r in own_records()))
                wait_for(lambda: not any(t.name == "dev-lingo-reaper" and t.ident not in original_reapers for t in threading.enumerate()))
                assert not (runtime / "worker.sock").exists()
        finally:
            for process in launched:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
            # Only directories recorded by our fixture are eligible for cleanup.
            for item in own_records():
                path = Path(item["cwd"])
                if path.name.startswith("dev-lingo-prepared-"):
                    shutil.rmtree(path, ignore_errors=True)
    report = {"cycles": 100, "successful_fixture_turns": 90, "failed_fixture_turns": 10,
              "normal_operation_worker_starts": 1, "samples": samples,
              "sigterm_during_request_cleaned": True, "sigkill_child_eof_cleanup": True,
              "stale_socket_recovered": True, "reaper_threads_finished": True,
              "scope": "Actual worker and OS processes with a local fake Codex; no paid inference. Finite test, not proof against every possible leak."}
    path = ROOT / "reports/lifecycle-check.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
