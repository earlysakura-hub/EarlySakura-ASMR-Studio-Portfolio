"""Own one gated Windows process tree; never select processes by name or PID scan."""
import ctypes
from ctypes import wintypes as w
import os
import subprocess
import threading
import time


class Cancelled(Exception):
    pass


class _BasicLimits(ctypes.Structure):
    _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
                ("Flags", w.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                ("MaxWorkingSet", ctypes.c_size_t), ("ActiveLimit", w.DWORD),
                ("Affinity", ctypes.c_size_t), ("Priority", w.DWORD), ("Scheduling", w.DWORD)]


class _IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("Basic", _BasicLimits), ("IO", _IO), ("ProcessMemory", ctypes.c_size_t),
                ("JobMemory", ctypes.c_size_t), ("PeakProcessMemory", ctypes.c_size_t),
                ("PeakJobMemory", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ("User", "Kernel", "PeriodUser", "PeriodKernel")] + [
        (name, w.DWORD) for name in ("PageFaults", "Total", "Active", "Terminated")]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [("Size", w.DWORD), ("Usage", w.DWORD), ("ThreadID", w.DWORD),
                ("ProcessID", w.DWORD), ("BasePriority", w.LONG),
                ("DeltaPriority", w.LONG), ("Flags", w.DWORD)]


class ProcessTree:
    def __init__(self):
        if os.name != "nt":
            raise OSError("安全进程树取消仅支持 Windows。")
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, ctypes.POINTER(_ThreadEntry)], w.BOOL),
            "Thread32Next": ([w.HANDLE, ctypes.POINTER(_ThreadEntry)], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self._api, name)
            function.argtypes, function.restype = args, result
        self._lock = threading.RLock()
        self.cancel_requested = threading.Event()
        self._job = self._api.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.Basic.Flags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway permitted.
        try:
            self._check(self._api.SetInformationJobObject(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except Exception:
            self._api.CloseHandle(self._job)
            self._job = None
            raise

    @staticmethod
    def _check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def active_count(self):
        with self._lock:
            if self._job is None:
                return 0
            data = _Accounting()
            self._check(self._api.QueryInformationJobObject(self._job, 1, ctypes.byref(data), ctypes.sizeof(data), None))
            return data.Active

    @property
    def closed(self):
        with self._lock:
            return self._job is None

    def start(self, command, keep_stdin=False, **kwargs):
        """Child MUST wait on stdin for START before spawning or touching outputs."""
        with self._lock:
            if self.cancel_requested.is_set():
                raise Cancelled("已在启动前取消。")
            # Suspend from creation: the venv Python redirector may itself spawn
            # base Python before user code reaches the stdin gate.
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW | 0x4, **kwargs)  # CREATE_SUSPENDED
            try:
                # Popen retains the actual process HANDLE; no PID reuse race.
                self._check(self._api.AssignProcessToJobObject(self._job, w.HANDLE(int(process._handle))))
                self._resume_owned_process(process)
                process.stdin.write("START\n")
                process.stdin.flush()
                if not keep_stdin:
                    process.stdin.close()
                return process
            except Exception:
                # The gate was never released on assignment failure: no descendants.
                process.kill()
                process.wait()
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                if process.stdout:
                    process.stdout.close()
                raise

    def _resume_owned_process(self, process):
        snapshot = self._api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry()
            entry.Size = ctypes.sizeof(entry)
            found = False
            more = self._api.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                # The retained process handle and suspended live process prevent
                # PID reuse. Never act on threads of any other process.
                if entry.ProcessID == process.pid:
                    thread = self._api.OpenThread(0x2, False, entry.ThreadID)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self._api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        found = True
                    finally:
                        self._api.CloseHandle(thread)
                more = self._api.Thread32Next(snapshot, ctypes.byref(entry))
            if not found:
                raise OSError("无法恢复本次创建的工作进程，未允许业务执行。")
        finally:
            self._api.CloseHandle(snapshot)

    def cancel(self):
        with self._lock:
            if self._job is None:
                return
            self.cancel_requested.set()
            self._check(self._api.TerminateJobObject(self._job, 1223))

    def finish(self):
        """Confirm ALL owned descendants exited before callers report cancellation."""
        with self._lock:
            if self._job is None:
                return
            if self.active_count():
                self._check(self._api.TerminateJobObject(self._job, 1223))
        # Usually milliseconds. No timeout is reported as a successful cancellation.
        while self.active_count():
            time.sleep(0.05)
        with self._lock:
            if self._job is not None:
                self._check(self._api.CloseHandle(self._job))
                self._job = None
