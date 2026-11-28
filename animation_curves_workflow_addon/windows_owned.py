"""Windows-owned work directories, exact Blender builds and child job lifetime."""

import ctypes as ct
from ctypes import wintypes as wt
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import time


from .host_compat import is_supported_host

CREATE_SUSPENDED = 0x4
EXTENDED_STARTUPINFO_PRESENT = 0x80000


def safe(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts[1:]):
        raise ValueError("Expected an absolute, non-redirected Windows path")
    for ancestor in (path, *path.parents):
        try:
            stat = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.st_file_attributes & 0x400:
            raise ValueError(f"Windows reparse point is out of scope: {ancestor}")
    return path


def sha(path):
    with safe(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_identity(bpy):
    version = tuple(bpy.app.version)
    machine = platform.machine()
    if platform.system() != "Windows" or machine.lower() not in {"amd64", "x86_64"} or not is_supported_host(version):
        raise ValueError("Unqualified Windows Blender platform or version")
    build = bpy.app.build_hash.decode()
    binary = safe(bpy.app.binary_path)
    expected = sha(binary)
    return {"platform": "Windows", "machine": machine, "version": list(version), "build": build,
            "binary": str(binary), "sha256": expected}


class _SecurityAttributes(ct.Structure):
    _fields_ = [("length", wt.DWORD), ("descriptor", ct.c_void_p), ("inherit", wt.BOOL)]


class _BasicLimits(ct.Structure):
    _fields_ = [("per_process_user_time", ct.c_int64), ("per_job_user_time", ct.c_int64),
                ("flags", wt.DWORD), ("minimum_working_set", ct.c_size_t), ("maximum_working_set", ct.c_size_t),
                ("active_process_limit", wt.DWORD), ("affinity", ct.c_size_t), ("priority", wt.DWORD), ("schedule", wt.DWORD)]


class _ExtendedLimits(ct.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", ct.c_uint64 * 6), ("memory", ct.c_size_t * 4)]


class _Accounting(ct.Structure):
    _fields_ = [("times", ct.c_int64 * 4), ("faults", wt.DWORD), ("total", wt.DWORD),
                ("active", wt.DWORD), ("terminated", wt.DWORD)]


def _api():
    k = ct.WinDLL("kernel32", use_last_error=True)
    adv = ct.WinDLL("advapi32", use_last_error=True)
    ptr = ct.c_void_p
    signatures = (
        (k, "CreateDirectoryW", (wt.LPCWSTR, ct.POINTER(_SecurityAttributes)), wt.BOOL),
        (k, "CreateJobObjectW", (ptr, wt.LPCWSTR), wt.HANDLE),
        (k, "SetInformationJobObject", (wt.HANDLE, ct.c_int, ptr, wt.DWORD), wt.BOOL),
        (k, "QueryInformationJobObject", (wt.HANDLE, ct.c_int, ptr, wt.DWORD, ptr), wt.BOOL),
        (k, "AssignProcessToJobObject", (wt.HANDLE, wt.HANDLE), wt.BOOL),
        (k, "TerminateJobObject", (wt.HANDLE, wt.UINT), wt.BOOL),
        (k, "ResumeThread", (wt.HANDLE,), wt.DWORD),
        (k, "CloseHandle", (wt.HANDLE,), wt.BOOL),
        (k, "LocalFree", (ptr,), ptr),
        (k, "GetCurrentProcess", (), wt.HANDLE),
        (k, "GetWindowsDirectoryW", (wt.LPWSTR, wt.UINT), wt.UINT),
        (adv, "OpenProcessToken", (wt.HANDLE, wt.DWORD, ct.POINTER(wt.HANDLE)), wt.BOOL),
        (adv, "GetTokenInformation", (wt.HANDLE, ct.c_int, ptr, wt.DWORD, ct.POINTER(wt.DWORD)), wt.BOOL),
        (adv, "ConvertSidToStringSidW", (ptr, ct.POINTER(wt.LPWSTR)), wt.BOOL),
        (adv, "ConvertStringSecurityDescriptorToSecurityDescriptorW", (wt.LPCWSTR, wt.DWORD, ct.POINTER(ptr), ptr), wt.BOOL),
    )
    for dll, name, args, result in signatures:
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    return k, adv


def _check(result):
    if not result:
        raise ct.WinError(ct.get_last_error())


def _user_sid(k, adv):
    token = wt.HANDLE()
    _check(adv.OpenProcessToken(k.GetCurrentProcess(), 8, ct.byref(token)))
    try:
        size = wt.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ct.byref(size))
        _check(size.value)
        buffer = ct.create_string_buffer(size.value)
        _check(adv.GetTokenInformation(token, 1, buffer, size.value, ct.byref(size)))
        sid = wt.LPWSTR()
        _check(adv.ConvertSidToStringSidW(ct.cast(buffer, ct.POINTER(ct.c_void_p))[0], ct.byref(sid)))
        try:
            return sid.value
        finally:
            k.LocalFree(sid)
    finally:
        k.CloseHandle(token)


def private_directory(path):
    path = safe(path)
    k, adv = _api()
    sid = _user_sid(k, adv)
    descriptor = ct.c_void_p()
    sddl = f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)"
    _check(adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ct.byref(descriptor), None))
    try:
        attributes = _SecurityAttributes(ct.sizeof(_SecurityAttributes), descriptor, False)
        _check(k.CreateDirectoryW(str(path), ct.byref(attributes)))
    finally:
        k.LocalFree(descriptor)
    return safe(path)


def private_env(root, executable=None):
    root = safe(root)
    k, _ = _api()
    windows_path = ct.create_unicode_buffer(32768)
    length = k.GetWindowsDirectoryW(windows_path, len(windows_path))
    _check(0 < length < len(windows_path))
    windows = safe(windows_path.value)
    paths = [windows / "System32", windows]
    if executable is not None:
        paths.insert(0, safe(executable).parent)
    env = {"SystemRoot": str(windows), "WINDIR": str(windows), "PATH": os.pathsep.join(map(str, paths)),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1"}
    for child, variables in (
        ("profile", ("USERPROFILE", "HOME")), ("appdata", ("APPDATA",)), ("localappdata", ("LOCALAPPDATA",)),
        ("config", ("BLENDER_USER_CONFIG",)), ("scripts", ("BLENDER_USER_SCRIPTS",)),
        ("datafiles", ("BLENDER_USER_DATAFILES",)), ("tmp", ("TMP", "TEMP")),
    ):
        path = private_directory(root / child)
        env.update({key: str(path) for key in variables})
    return env


class OwnedProcess:
    """Assign a suspended process before any child instructions can execute."""

    def __init__(self, command, *, env, cwd, log):
        import _winapi as win
        import msvcrt

        binary = safe(command[0])
        cwd = safe(cwd)
        self._kernel, _ = _api()
        self._win = win
        self.returncode = None
        self._process = None
        self._job = self._kernel.CreateJobObjectW(None, None)
        _check(self._job)
        thread = None
        assigned = False
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # Kill on close. Breakaway is not enabled.
        try:
            _check(self._kernel.SetInformationJobObject(self._job, 9, ct.byref(limits), ct.sizeof(limits)))
            with open(os.devnull, "rb") as null:
                handles = []
                owner = win.GetCurrentProcess()
                try:
                    for stream in (null, log):
                        handle = win.DuplicateHandle(owner, msvcrt.get_osfhandle(stream.fileno()), owner, 0, True,
                                                     win.DUPLICATE_SAME_ACCESS)
                        handles.append(handle)
                    startup = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESTDHANDLES, hStdInput=handles[0],
                                                     hStdOutput=handles[1], hStdError=handles[1],
                                                     lpAttributeList={"handle_list": handles})
                    self._process, thread, self.pid, _ = win.CreateProcess(
                        str(binary), subprocess.list2cmdline([str(value) for value in command]), None, None, True,
                        win.CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT,
                        env, str(cwd), startup,
                    )
                finally:
                    for handle in handles:
                        win.CloseHandle(handle)
            _check(self._kernel.AssignProcessToJobObject(self._job, self._process))
            assigned = True
            if self._kernel.ResumeThread(thread) != 1:
                raise RuntimeError("Windows owned process did not resume from its initial suspension")
        except BaseException:
            try:
                if self._process is not None:
                    if assigned:
                        self.kill()
                    else:
                        win.TerminateProcess(self._process, 7)
                    win.WaitForSingleObject(self._process, 10000)
            finally:
                self._close_handles()
            raise
        finally:
            if thread is not None:
                win.CloseHandle(thread)

    def _active(self):
        if not self._job:
            return 0
        accounting = _Accounting()
        _check(self._kernel.QueryInformationJobObject(self._job, 1, ct.byref(accounting), ct.sizeof(accounting), None))
        return accounting.active

    def _close_handles(self):
        if self._job:
            _check(self._kernel.CloseHandle(self._job))
            self._job = None
        if self._process is not None:
            self._win.CloseHandle(self._process)
            self._process = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self._win.WaitForSingleObject(self._process, 0) == self._win.WAIT_TIMEOUT or self._active():
            return None
        code = self._win.GetExitCodeProcess(self._process)
        self._close_handles()
        self.returncode = code
        return code

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("Windows owned job", timeout)
            time.sleep(.025)
        return self.returncode

    def kill(self):
        if self._active():
            _check(self._kernel.TerminateJobObject(self._job, 7))
