"""Windows child-process containment for the CLI executor (no shell).

The CLI starts suspended, joins a kill-on-close Job Object, then resumes.
Closing the job kills its descendants even after the original CLI exits.
"""

import subprocess
import sys


def run_windows(command: list[str]) -> int:
    import ctypes
    from ctypes import wintypes as w

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", w.DWORD),
            ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_processes", w.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", w.DWORD),
            ("scheduling", w.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", IO),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    class Startup(ctypes.Structure):
        _fields_ = [
            ("cb", w.DWORD),
            ("reserved", w.LPWSTR),
            ("desktop", w.LPWSTR),
            ("title", w.LPWSTR),
            *[
                (name, w.DWORD)
                for name in ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")
            ],
            ("show", w.WORD),
            ("reserved_size", w.WORD),
            ("reserved_data", ctypes.c_void_p),
            ("stdin", w.HANDLE),
            ("stdout", w.HANDLE),
            ("stderr", w.HANDLE),
        ]

    class Process(ctypes.Structure):
        _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "GetStdHandle": ([w.DWORD], w.HANDLE),
        "CreateProcessW": (
            [
                w.LPCWSTR,
                w.LPWSTR,
                ctypes.c_void_p,
                ctypes.c_void_p,
                w.BOOL,
                w.DWORD,
                ctypes.c_void_p,
                w.LPCWSTR,
                ctypes.POINTER(Startup),
                ctypes.POINTER(Process),
            ],
            w.BOOL,
        ),
        "ResumeThread": ([w.HANDLE], w.DWORD),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
        "CloseHandle": ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return 125
    info = Process()
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            return 125
        startup = Startup()
        startup.cb = ctypes.sizeof(startup)
        startup.flags = 0x100  # STARTF_USESTDHANDLES
        startup.stdin = kernel.GetStdHandle(w.DWORD(-10))
        startup.stdout = kernel.GetStdHandle(w.DWORD(-11))
        startup.stderr = kernel.GetStdHandle(w.DWORD(-12))
        argv = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        if not kernel.CreateProcessW(
            None, argv, None, None, True, 0x4, None, None, ctypes.byref(startup), ctypes.byref(info)
        ):
            return 125
        if not kernel.AssignProcessToJobObject(job, info.process):
            kernel.TerminateProcess(info.process, 125)
            return 125
        if kernel.ResumeThread(info.thread) == 0xFFFFFFFF:
            return 125
        if kernel.WaitForSingleObject(info.process, 0xFFFFFFFF) != 0:
            return 125
        code = w.DWORD()
        return (
            int(code.value) if kernel.GetExitCodeProcess(info.process, ctypes.byref(code)) else 125
        )
    finally:
        kernel.CloseHandle(job)
        if info.thread:
            kernel.CloseHandle(info.thread)
        if info.process:
            kernel.CloseHandle(info.process)


def main() -> int:
    if sys.platform != "win32" or len(sys.argv) < 2:
        return 125
    return run_windows(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
