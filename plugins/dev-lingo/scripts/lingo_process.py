"""Native executable discovery and Windows process-tree ownership."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def is_windows():
    return sys.platform == "win32"


def windows_hook_command(action):
    bootstrap = ("import os,runpy,sys;"
                 "sys.path.insert(0,os.path.join(os.environ['PLUGIN_ROOT'],'scripts'));"
                 "runpy.run_path(os.path.join(os.environ['PLUGIN_ROOT'],'scripts','dev_lingo.py'),run_name='__main__')")
    return 'python -X utf8 -c "' + bootstrap + '" ' + action + ' --host codex'


def find_codex():
    binary = os.environ.get("DEV_LINGO_CODEX") or shutil.which("codex.exe" if is_windows() else "codex")
    if not binary and is_windows():
        binary = shutil.which("codex")
    if not binary:
        candidates = [Path.home() / ".local/bin/codex", Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")]
        if is_windows():
            candidates = [Path.home() / ".local/bin/codex.exe"]
        binary = next((str(path) for path in candidates if path.is_file()), None)
    if not binary:
        raise RuntimeError("Codex CLI를 찾을 수 없습니다. Codex CLI 설치와 로그인을 확인해 주세요.")
    path = Path(binary).resolve()
    if is_windows() and path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        # npm's shell shim is not a native executable. Invoke its packaged exe
        # directly, keeping all arguments out of cmd.exe/PowerShell parsing.
        package = (path.parent / "node_modules/@openai/codex").resolve()
        vendors = [package / "vendor"]
        for modules in [package / "node_modules", package.parent.parent, path.parent / "node_modules"]:
            for arch in ["x64", "arm64"]:
                vendors.append(modules / ("@openai/codex-win32-" + arch) / "vendor")
        for vendor in vendors:
            for target in ["x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc"]:
                for folder in ["bin", "codex"]:
                    candidate = vendor / target / folder / "codex.exe"
                    if candidate.is_file():
                        return str(candidate.resolve())
        raise RuntimeError("Codex npm 런처의 codex.exe를 찾지 못했습니다. Codex CLI를 다시 설치하거나 DEV_LINGO_CODEX에 codex.exe의 절대 경로를 지정해 주세요.")
    return str(path)


class WindowsJob:
    """A private job kills descendants when this owner exits or closes it."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ["ReadOperationCount", "WriteOperationCount",
                        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount"]]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, process):
        import ctypes
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def spawn_isolated(command, **kwargs):
    if not is_windows():
        return subprocess.Popen(command, start_new_session=True, **kwargs)
    job = WindowsJob()
    process = None
    try:
        process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
        job.attach(process)
        process._dev_lingo_job = job
        return process
    except BaseException:
        job.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for pipe in [process.stdin, process.stdout]:
                if pipe is not None:
                    pipe.close()
        raise


def stop_windows(process):
    job = getattr(process, "_dev_lingo_job", None)
    if job is not None:
        job.close()
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    readers = getattr(process, "_dev_lingo_pipe_threads", None)
    if readers:
        stopped, threads = readers
        stopped.set()
        for thread in threads:
            thread.join(timeout=2)
        process._dev_lingo_pipe_threads = None
    for pipe in [process.stdin, process.stdout]:
        if pipe is not None:
            pipe.close()
