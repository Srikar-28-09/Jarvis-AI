"""Private file and directory operations for persisted credentials.

POSIX operations are anchored to directory descriptors and reject symlinks in
every path component. Windows creates credential directories and files with
protected ACLs for the current user and SYSTEM, validates existing ACLs, and
holds non-delete-sharing directory handles while rejecting reparse points.
"""

from __future__ import annotations

import errno
import ctypes
import json
import os
import secrets
import stat
import uuid
from pathlib import Path
from ctypes import wintypes
from typing import Any, Callable, Optional

_POSIX = os.name == "posix"
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | _NOFOLLOW
)


def _validate_private_path_directory(info: os.stat_result, path: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(errno.ENOTDIR, "credential path component is not a directory", path)
    if info.st_uid not in (0, os.geteuid()):
        raise PermissionError(errno.EACCES, "credential path component has an unexpected owner", path)


def _check_regular_file(fd: int, path: str) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, "credential path is not a regular file", path)
    if not _POSIX:
        attributes = getattr(info, "st_file_attributes", 0)
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise OSError(errno.ELOOP, "credential path is a reparse point", path)
    return info


def _open_directory_posix(path: str, *, create: bool, mode: int = 0o700) -> int:
    absolute = os.path.abspath(path)
    current = os.open(os.path.sep, _DIR_FLAGS)
    info = os.fstat(current)
    try:
        for component in Path(absolute).parts[1:]:
            try:
                child = os.open(component, _DIR_FLAGS | _NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=mode, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, _DIR_FLAGS | _NOFOLLOW, dir_fd=current)
            info = os.fstat(child)
            try:
                _validate_private_path_directory(info, component)
            except OSError:
                os.close(child)
                raise
            os.close(current)
            current = child
        if info.st_uid != os.geteuid():
            raise PermissionError(errno.EACCES, "credential directory is not owned by current user", absolute)
        os.fchmod(current, mode)
        return current
    except BaseException:
        os.close(current)
        raise


def _open_directory_windows(
    path: str, *, create: bool
) -> "_WindowsDirectory":
    absolute = os.path.abspath(path)
    drive, tail = os.path.splitdrive(absolute)
    current = drive + os.path.sep if drive else os.path.sep
    handles = []
    try:
        components = (None, *Path(tail).parts)
        for index, component in enumerate(components):
            if component not in (None, "", os.path.sep):
                current = os.path.join(current, component)
                if create:
                    _create_windows_directory(current)
            handle = _open_windows_directory_handle(current)
            handles.append(handle)
            if index == len(components) - 1:
                _validate_windows_security(handle, is_directory=True)
        return _WindowsDirectory(absolute, handles)
    except BaseException:
        for handle in reversed(handles):
            _close_windows_handle(handle)
        raise


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOL)]


class _WindowsDirectory(os.PathLike):
    def __init__(self, path: str, handles: list):
        self.path = path
        self.handles = handles

    def __fspath__(self) -> str:
        return self.path

    def close(self) -> None:
        if self.handles:
            for handle in reversed(self.handles):
                _close_windows_handle(handle)
            self.handles.clear()


def _windows_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return kernel32


def _windows_security_descriptor(is_directory: bool):
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    user_sid = _windows_current_user_sid()
    inherit = "OICI" if is_directory else ""
    sddl = f"O:{user_sid}D:P(A;{inherit};FA;;;SY)(A;{inherit};FA;;;{user_sid})"
    descriptor = wintypes.LPVOID()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return descriptor


def _windows_current_user_sid() -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = _windows_kernel32()
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, size.value, ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID)).contents
        sid_string = wintypes.LPWSTR()
        convert = advapi32.ConvertSidToStringSidW
        convert.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
        convert.restype = wintypes.BOOL
        if not convert(sid, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_string.value
        finally:
            kernel32.LocalFree(ctypes.cast(sid_string, wintypes.HLOCAL))
    finally:
        kernel32.CloseHandle(token)


def _windows_security_attributes(is_directory: bool):
    descriptor = _windows_security_descriptor(is_directory)
    attributes = _SECURITY_ATTRIBUTES(
        ctypes.sizeof(_SECURITY_ATTRIBUTES), descriptor, False
    )
    return descriptor, attributes


def _create_windows_directory(path: str) -> None:
    kernel32 = _windows_kernel32()
    descriptor, attributes = _windows_security_attributes(is_directory=True)
    try:
        create_directory = kernel32.CreateDirectoryW
        create_directory.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(_SECURITY_ATTRIBUTES))
        create_directory.restype = wintypes.BOOL
        if not create_directory(path, ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error != 183:  # ERROR_ALREADY_EXISTS
                raise ctypes.WinError(error)
    finally:
        if descriptor:
            if descriptor:
                kernel32.LocalFree(descriptor)


def _sid_to_string(sid) -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = _windows_kernel32()
    sid_string = wintypes.LPWSTR()
    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
    convert.restype = wintypes.BOOL
    if not convert(sid, ctypes.byref(sid_string)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return sid_string.value
    finally:
        kernel32.LocalFree(ctypes.cast(sid_string, wintypes.HLOCAL))


def _windows_acl_facts(handle):
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    get_security = advapi32.GetSecurityInfo
    get_security.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    )
    get_security.restype = wintypes.DWORD
    status = get_security(
        handle, 1, 0x00000001 | 0x00000004,
        ctypes.byref(owner), None, ctypes.byref(dacl), None,
        ctypes.byref(descriptor),
    )
    if status:
        raise ctypes.WinError(status)
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        advapi32.GetSecurityDescriptorControl.argtypes = (
            wintypes.LPVOID, ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not dacl:
            return _sid_to_string(owner), bool(control.value & 0x1000), None

        acl_info = _ACL_SIZE_INFORMATION()
        advapi32.GetAclInformation.argtypes = (
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD
        )
        advapi32.GetAclInformation.restype = wintypes.BOOL
        if not advapi32.GetAclInformation(
            dacl, ctypes.byref(acl_info), ctypes.sizeof(acl_info), 2
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        advapi32.GetAce.argtypes = (
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)
        )
        advapi32.GetAce.restype = wintypes.BOOL
        aces = []
        for index in range(acl_info.AceCount):
            ace = wintypes.LPVOID()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte * 2)).contents
            if header[0] != 0:  # ACCESS_ALLOWED_ACE_TYPE only
                aces.append((header[0], None, None))
                continue
            mask = ctypes.cast(
                ctypes.c_void_p(ace.value + 4), ctypes.POINTER(wintypes.DWORD)
            ).contents.value
            sid = ctypes.c_void_p(ace.value + 8)
            ace_flags = header[1]
            aces.append((ace_flags, mask, _sid_to_string(sid)))
        return _sid_to_string(owner), bool(control.value & 0x1000), aces
    finally:
        _windows_kernel32().LocalFree(descriptor)


def _validate_windows_acl_facts(owner_sid, dacl_protected, aces, *, is_directory):
    user_sid = _windows_current_user_sid()
    if owner_sid != user_sid:
        raise PermissionError(errno.EACCES, "credential path has an unexpected owner")
    expected_flags = 0x03 if is_directory else 0
    if not dacl_protected or aces is None or len(aces) != 2:
        raise PermissionError(errno.EACCES, "credential path has an unsafe Windows ACL")
    expected = {(user_sid, expected_flags), ("S-1-5-18", expected_flags)}
    actual = set()
    for flags, mask, sid in aces:
        if mask != 0x001F01FF or sid not in (user_sid, "S-1-5-18"):
            raise PermissionError(errno.EACCES, "credential path has an unsafe Windows ACL")
        actual.add((sid, flags))
    if actual != expected:
        raise PermissionError(errno.EACCES, "credential path has an unsafe Windows ACL")


def _validate_windows_security(handle, *, is_directory):
    _validate_windows_acl_facts(*_windows_acl_facts(handle), is_directory=is_directory)


def _close_windows_handle(handle) -> None:
    _windows_kernel32().CloseHandle(handle)


def _open_windows_directory_handle(path: str):
    kernel32 = _windows_kernel32()
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        path,
        0x00020080,  # READ_CONTROL | FILE_READ_ATTRIBUTES
        0x00000001 | 0x00000002,  # Share read/write, but deny delete/rename.
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _FILE_ATTRIBUTE_TAG_INFO()
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = (
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_info.restype = wintypes.BOOL
    if not get_info(
        handle, 9, ctypes.byref(info), ctypes.sizeof(info)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)
    if not info.FileAttributes & 0x10 or info.FileAttributes & 0x400:
        kernel32.CloseHandle(handle)
        raise OSError(errno.ELOOP, "credential directory is not a plain directory", path)
    return handle


def _open_windows_file(path: str, *, create: bool) -> int:
    kernel32 = _windows_kernel32()
    descriptor, attributes = (
        _windows_security_attributes(is_directory=False) if create else (None, None)
    )
    try:
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            path,
            (0x40000000 | 0x00020080 | 0x00010000) if create else 0x80000000,
            0x00000001,
            ctypes.byref(attributes) if create else None,
            1 if create else 3,  # CREATE_NEW / OPEN_EXISTING
            0x00200080,  # OPEN_REPARSE_POINT / FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(descriptor)

    try:
        _validate_windows_file_handle(handle)
        import msvcrt

        return msvcrt.open_osfhandle(
            handle.value if hasattr(handle, "value") else handle,
            (os.O_WRONLY if create else os.O_RDONLY)
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOINHERIT", 0),
        )
    except BaseException:
        try:
            if create:
                _mark_windows_file_for_deletion(handle)
        finally:
            kernel32.CloseHandle(handle)
        raise


def _mark_windows_file_for_deletion(handle) -> None:
    kernel32 = _windows_kernel32()
    mark_delete = kernel32.SetFileInformationByHandle
    mark_delete.argtypes = (
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    mark_delete.restype = wintypes.BOOL
    disposition = _FILE_DISPOSITION_INFO(True)
    if not mark_delete(
        handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _validate_windows_file_handle(handle):
    kernel32 = _windows_kernel32()
    info = _FILE_ATTRIBUTE_TAG_INFO()
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = (
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_info.restype = wintypes.BOOL
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.FileAttributes & 0x400 or info.FileAttributes & 0x10:
        raise OSError(errno.ELOOP, "credential path is not a plain file")
    _validate_windows_security(handle, is_directory=False)


def _open_directory(path: str, *, create: bool, mode: int = 0o700):
    if _POSIX:
        return _open_directory_posix(path, create=create, mode=mode)
    return _open_directory_windows(path, create=create)


def _safe_existing_fd(directory, name: str, full_path: str) -> int:
    if _POSIX:
        return os.open(name, _FILE_FLAGS, dir_fd=directory)
    return _open_windows_file(full_path, create=False)


def _read_private_file(directory, name: str, full_path: str) -> bytes:
    fd = _safe_existing_fd(directory, name, full_path)
    try:
        info = _check_regular_file(fd, full_path)
        if _POSIX:
            if info.st_uid != os.geteuid():
                raise PermissionError(errno.EACCES, "credential file is not owned by current user", full_path)
            os.fchmod(fd, 0o600)
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 65536:
                raise OSError(errno.EFBIG, "credential file exceeds size limit", full_path)
        return b"".join(chunks)
    finally:
        os.close(fd)


def secure_load_or_create_key(path: str, generate_key: Callable[[], bytes]) -> bytes:
    """Load a private regular key or atomically install one generated key.

    A completed temporary file is hard-linked into place, so concurrent
    creators either install one complete key or read the winner. Existing
    invalid keys are returned for the caller's cryptographic validator to
    reject; they are never silently replaced.
    """
    parent, name = os.path.split(os.path.abspath(path))
    directory = _open_directory(parent, create=True)
    try:
        try:
            return _read_private_file(directory, name, path)
        except FileNotFoundError:
            pass

        key = generate_key()
        temporary = f".{name}.tmp.{uuid.uuid4().hex}"
        fd = (
            os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=directory)
            if _POSIX
            else _open_windows_file(os.path.join(directory, temporary), create=True)
        )
        try:
            view = memoryview(key)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short write while creating credential key")
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            _unlink(directory, temporary)
            raise
        else:
            os.close(fd)

        try:
            if _POSIX:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            else:
                os.link(os.path.join(directory, temporary), path)
        except FileExistsError:
            pass
        finally:
            _unlink(directory, temporary)
        if _POSIX:
            os.fsync(directory)
        return _read_private_file(directory, name, path)
    finally:
        _close_directory(directory)


def _unlink(directory, name: str) -> None:
    try:
        if _POSIX:
            os.unlink(name, dir_fd=directory)
        else:
            os.unlink(os.path.join(directory, name))
    except FileNotFoundError:
        pass


def _close_directory(directory) -> None:
    if _POSIX:
        os.close(directory)
    else:
        directory.close()


def _open_child_directory(directory, component: str, _base_path: str, *, create: bool):
    if _POSIX:
        try:
            child = os.open(component, _DIR_FLAGS | _NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(component, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(component, _DIR_FLAGS | _NOFOLLOW, dir_fd=directory)
        info = os.fstat(child)
        if not stat.S_ISDIR(info.st_mode):
            os.close(child)
            raise OSError(errno.ENOTDIR, "credential path component is not a directory", component)
        try:
            _validate_private_path_directory(info, component)
        except OSError:
            os.close(child)
            raise
        os.fchmod(child, 0o700)
        return child
    child_path = os.path.join(directory, component)
    return _open_directory_windows(child_path, create=create)


def secure_read_json_at(base_dir: str, relative_path: str) -> Any:
    """Read JSON beneath a trusted directory without following POSIX symlinks."""
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError("relative path must not contain traversal components")
    if not relative.parts:
        raise ValueError("relative path must name a file")
    directory = _open_directory(base_dir, create=False)
    try:
        for component in relative.parts[:-1]:
            child = _open_child_directory(
                directory, component, os.path.join(base_dir, *relative.parts[:-1]), create=False
            )
            _close_directory(directory)
            directory = child
        raw = _read_private_file(directory, relative.parts[-1], os.path.join(base_dir, *relative.parts))
        return json.loads(raw.decode("utf-8"))
    finally:
        _close_directory(directory)


def secure_file_exists_at(base_dir: str, relative_path: str) -> bool:
    """Check for a regular file beneath a directory without following symlinks."""
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError("relative path must not contain traversal components")
    if not relative.parts:
        raise ValueError("relative path must name a file")
    try:
        directory = _open_directory(base_dir, create=False)
    except FileNotFoundError:
        return False
    try:
        for component in relative.parts[:-1]:
            try:
                child = _open_child_directory(
                    directory, component, os.path.join(base_dir, *relative.parts[:-1]), create=False
                )
            except FileNotFoundError:
                return False
            _close_directory(directory)
            directory = child
        full_path = os.path.join(base_dir, *relative.parts)
        try:
            fd = _safe_existing_fd(directory, relative.parts[-1], full_path)
        except FileNotFoundError:
            return False
        try:
            info = _check_regular_file(fd, full_path)
            if _POSIX:
                if info.st_uid != os.geteuid():
                    raise PermissionError(errno.EACCES, "credential file is not owned by current user", full_path)
                os.fchmod(fd, 0o600)
            return True
        finally:
            os.close(fd)
    finally:
        _close_directory(directory)


def secure_write_json_at(
    base_dir: str,
    relative_path: str,
    data: Any,
    *,
    indent: Optional[int] = None,
) -> None:
    """Atomically write private JSON beneath a validated directory."""
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError("relative path must not contain traversal components")
    if not relative.parts:
        raise ValueError("relative path must name a file")
    directory = _open_directory(base_dir, create=True)
    try:
        for component in relative.parts[:-1]:
            child = _open_child_directory(
                directory, component, os.path.join(base_dir, *relative.parts[:-1]), create=True
            )
            _close_directory(directory)
            directory = child
        name = relative.parts[-1]
        full_path = os.path.join(base_dir, *relative.parts)
        try:
            existing = _safe_existing_fd(directory, name, full_path)
        except FileNotFoundError:
            pass
        else:
            try:
                info = _check_regular_file(existing, full_path)
                if _POSIX:
                    if info.st_uid != os.geteuid():
                        raise PermissionError(errno.EACCES, "credential file is not owned by current user", full_path)
                    os.fchmod(existing, 0o600)
            finally:
                os.close(existing)

        temporary = f".{name}.tmp.{secrets.token_hex(16)}"
        fd = (
            os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=directory)
            if _POSIX
            else _open_windows_file(os.path.join(directory, temporary), create=True)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=indent)
                stream.flush()
                os.fsync(stream.fileno())
            if _POSIX:
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            else:
                os.replace(os.path.join(directory, temporary), os.path.join(directory, name))
        finally:
            _unlink(directory, temporary)
    finally:
        _close_directory(directory)
