"""Built-in read-only tools a caller opts into (M10: FR-35..FR-39, NFR-13, NFR-14).

Nothing here is imported by `import agentsdk`, and nothing registers these tools
for you: a Runner holds exactly the tools its caller passed, and an agent's
tool_profile still decides which of them may run.

Every refusal is an ordinary tool error the model reads -- never a raise out of
the run, and never a message that names what lies outside the tool's confinement.

File tools (read_file_tool, list_directory_tool, glob_tool, grep_tool) are bound
to a root folder. A path is checked twice: by its spelling, and then on the
handle actually opened, whose final path -- every symlink, junction and reparse
point followed -- must lie inside the root. An open handle stops the folder
and its parents from being renamed, so nothing can be swapped in after that
check. They are implemented with Windows handle APIs and refuse to construct on
other platforms for now.

fetch_tool reads allowlisted http(s) URLs and nothing else; web_search_tool
wraps a SearchBackend you supply. Both label their results as untrusted
external content.
"""

from __future__ import annotations

import asyncio
import codecs
import concurrent.futures
import contextvars
import fnmatch
import functools
import ipaddress
import itertools
import math
import os
import re
import socket
import sys
import threading
import time
import zlib
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .primitives import unstorable_reason
from .tools import ResultProvenance, Tool, ToolOutput, ToolSpec
from .version import __version__


class _Refused(Exception):
    """A refusal the model reads. Its message never names an outside path."""


def _positive(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


# =============================================================================
# The file-tool thread pool (FR-45)
# =============================================================================

# P2-D6: one pool for every file tool in the process.
_file_tool_threads = 4
_file_pool: concurrent.futures.ThreadPoolExecutor | None = None
_file_pool_lock = threading.Lock()


def set_file_tool_threads(count: int) -> None:
    """How many threads every built-in file tool in this process shares (4 by default).

    Call it before the first file tool call. After that the pool exists, and
    this raises RuntimeError rather than resize a pool that may be running work.
    """
    global _file_tool_threads
    threads = _positive("count", count)
    with _file_pool_lock:
        if _file_pool is not None:
            raise RuntimeError(
                "set_file_tool_threads must be called before the first file tool call; the pool already exists"
            )
        _file_tool_threads = threads


def _file_tool_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _file_pool
    with _file_pool_lock:
        if _file_pool is None:
            numbers = itertools.count(1)

            def name_thread() -> None:
                threading.current_thread().name = f"agentsdk-file-tool-{next(numbers)}"

            _file_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=_file_tool_threads, thread_name_prefix="agentsdk-file-tool", initializer=name_thread
            )
        return _file_pool


async def _on_file_pool(function: Callable[..., str], *args: Any) -> str:
    """Blocking file work, on the SDK's own pool rather than the loop's default executor.

    Every store call goes through the default executor (FR-20), and in M10 review
    round 1 twenty concurrent greps sharing it held a store call for 139 s
    (KNOWLEDGE-0d5f3a4e). Awaiting the pool's future lets the executor's timeout
    cancel a call still waiting for a thread, which then never starts; a call
    already running cannot be interrupted and ends at its own walk budget.
    """
    context = contextvars.copy_context()
    return await asyncio.get_running_loop().run_in_executor(
        _file_tool_pool(), functools.partial(context.run, function, *args)
    )


# =============================================================================
# The file layer
# =============================================================================


def _plain(path: str) -> str:
    """A final path without the \\\\?\\ prefix Windows puts on it."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _extended(path: str) -> str:
    """The \\\\?\\ form, so no Win32 path rewriting happens between check and open."""
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


class _FileSystem:
    """Every file-system call the file tools make, and nothing else.

    Kept in one place so a test can record the thread of each call (AC-29): a
    call made anywhere else would be invisible to it, and a test reads this
    module's source to refuse one.
    """

    _ACCESS = 0x0001 | 0x0080 | 0x00100000  # read data / list directory, read attributes, synchronize
    _SHARE = 0x1 | 0x2  # read and write, NOT delete: no rename under an open handle
    _OPEN_EXISTING = 3
    _BACKUP_SEMANTICS = 0x02000000  # lets CreateFileW open a directory

    def __init__(self) -> None:
        self._k32: Any = None

    def kernel32(self) -> Any:
        if self._k32 is None:
            import ctypes
            from ctypes import wintypes as w

            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateFileW.restype = w.HANDLE
            k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
            k.GetFinalPathNameByHandleW.restype = w.DWORD
            k.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
            k.GetFileInformationByHandleEx.restype = w.BOOL
            k.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
            k.ReadFile.restype = w.BOOL
            k.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]
            k.CloseHandle.restype = w.BOOL
            k.CloseHandle.argtypes = [w.HANDLE]
            self._k32 = k
        return self._k32

    def open_handle(self, path: str) -> int:
        """Open a file or a directory, following every link, or raise OSError."""
        import ctypes

        handle = self.kernel32().CreateFileW(
            _extended(path), self._ACCESS, self._SHARE, None, self._OPEN_EXISTING, self._BACKUP_SEMANTICS, None
        )
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "cannot open")
        return handle

    def close(self, handle: int) -> None:
        self.kernel32().CloseHandle(handle)

    def final_path(self, handle: int) -> str:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = self.kernel32().GetFinalPathNameByHandleW(handle, buffer, 32768, 0)
        if not length or length >= 32768:
            raise OSError(ctypes.get_last_error(), "no final path")
        return _plain(buffer.value)

    def standard_info(self, handle: int) -> tuple[int, int, bool]:
        """(size in bytes, number of hard links, is a directory)."""
        import ctypes

        buffer = ctypes.create_string_buffer(24)
        if not self.kernel32().GetFileInformationByHandleEx(handle, 1, buffer, 24):  # FileStandardInfo
            raise OSError(ctypes.get_last_error(), "no file information")
        raw = buffer.raw
        return int.from_bytes(raw[8:16], "little"), int.from_bytes(raw[16:20], "little"), raw[21] != 0

    def list_handle(self, handle: int, limit: int) -> list[tuple[str, int]]:
        """(name, attributes) of the entries of the directory the handle holds.

        Enumerated through the handle, not a path: what is listed is the
        directory that was checked.
        """
        import ctypes

        entries: list[tuple[str, int]] = []
        buffer = ctypes.create_string_buffer(65536)
        information_class = 15  # FileFullDirectoryRestartInfo, then FileFullDirectoryInfo
        while len(entries) < limit:
            if not self.kernel32().GetFileInformationByHandleEx(handle, information_class, buffer, len(buffer)):
                error = ctypes.get_last_error()
                if error == 18:  # ERROR_NO_MORE_FILES
                    break
                raise OSError(error, "cannot list")
            information_class = 14
            raw, offset = buffer.raw, 0
            while True:
                following = int.from_bytes(raw[offset:offset + 4], "little")
                attributes = int.from_bytes(raw[offset + 56:offset + 60], "little")
                length = int.from_bytes(raw[offset + 60:offset + 64], "little")
                name = raw[offset + 68:offset + 68 + length].decode("utf-16-le", errors="surrogatepass")
                if name not in (".", ".."):
                    entries.append((name, attributes))
                if not following:
                    break
                offset += following
        return entries

    def read(self, handle: int, limit: int) -> bytes:
        import ctypes
        from ctypes import wintypes as w

        chunks, remaining = [], limit
        buffer = ctypes.create_string_buffer(max(1, min(limit, 1 << 20)))
        count = w.DWORD()
        while remaining > 0:
            if not self.kernel32().ReadFile(handle, buffer, min(remaining, len(buffer)), ctypes.byref(count), None):
                raise OSError(ctypes.get_last_error(), "cannot read")
            if count.value == 0:
                break
            chunks.append(buffer.raw[:count.value])
            remaining -= count.value
        return b"".join(chunks)

    def realpath(self, path: str) -> str:
        return _plain(os.path.realpath(path))


_fs = _FileSystem()


# =============================================================================
# File confinement
# =============================================================================

_NOT_ALLOWED = (
    "path refused: give a relative path inside the root folder, without '..', drive letters, "
    "device or stream names, 8.3 short names, or trailing dots or spaces"
)
_OUTSIDE = "path refused: it resolves outside the root folder"
_NOT_FOUND = "no such file or directory inside the root folder"
_HARD_LINK = "file refused: it has more than one hard link, so its location cannot be told from its path"
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"} | {
    f"{kind}{n}" for kind in ("COM", "LPT") for n in "0123456789¹²³"
}
_FORBIDDEN = set('<>:"|?*') | {chr(code) for code in range(32)}
_SHORT_NAME = re.compile(r"~\d")
_REPARSE_POINT, _DIRECTORY = 0x400, 0x10
_WALK_ENTRIES = 100_000
_WALK_SECONDS = 20.0
_LINE_CHARS = 300


def _components(value: Any, *, pattern: bool = False) -> list[str]:
    """The segments of a model-supplied relative path, or a refusal.

    The spelling check. It is not what confines a path -- the handle check is --
    but it refuses every form FR-36 names before anything touches the disk, so
    no refusal depends on what exists outside.
    """
    if not isinstance(value, str) or value.startswith(("/", "\\")):
        raise _Refused(_NOT_ALLOWED)
    forbidden = _FORBIDDEN - {"*", "?"} if pattern else _FORBIDDEN
    parts = []
    for part in re.split(r"[\\/]", value):
        if part in ("", "."):
            continue
        if (
            part == ".."
            or any(character in forbidden for character in part)
            or part[-1] in ". "
            or part.split(".")[0].rstrip(" ").upper() in _RESERVED
            or (pattern and "**" in part and part != "**")
        ):
            raise _Refused(_NOT_ALLOWED)
        parts.append(part)
    return parts


class _Root:
    """A root folder, resolved once, at construction (FR-36)."""

    def __init__(self, root: Any) -> None:
        if sys.platform != "win32":
            raise NotImplementedError(
                "the file tools confine paths with Windows handle APIs and are not available on this platform yet"
            )
        path = os.fspath(root)
        if not isinstance(path, str) or not path:
            raise ValueError("root must be a non-empty path")
        handle = _fs.open_handle(os.path.abspath(path))
        try:
            if not _fs.standard_info(handle)[2]:
                raise ValueError("root must be a directory")
            self.path = _fs.final_path(handle)
        finally:
            _fs.close(handle)
        self._prefix = self.path.rstrip("\\") + "\\"

    def inside(self, final: str) -> bool:
        # Exact case: final paths carry the on-disk case for both sides, and a
        # case-sensitive directory can hold siblings that differ only in case.
        return final == self.path or final.startswith(self._prefix)

    def join(self, directory: str, name: str) -> str:
        return directory.rstrip("\\") + "\\" + name


Seam = Callable[[str], None] | None


def _open_inside(root: _Root, parts: list[str], seam: Seam) -> tuple[int, str]:
    """A handle on the path, proven inside the root by its own final path."""
    candidate = root.path
    for part in parts:
        if _SHORT_NAME.search(part) and part.casefold() not in _names_in(root, candidate):
            raise _Refused(_NOT_ALLOWED)  # an 8.3 alias, not a name the directory holds
        candidate = root.join(candidate, part)
    if not root.inside(_fs.realpath(candidate)):
        raise _Refused(_OUTSIDE)
    if seam is not None:
        seam(candidate)  # tests swap a link here, after the check above
    try:
        handle = _fs.open_handle(candidate)
    except OSError:
        raise _Refused(_NOT_FOUND) from None
    try:
        final = _fs.final_path(handle)
    except BaseException:
        _fs.close(handle)
        raise
    if not root.inside(final):  # the check that decides
        _fs.close(handle)
        raise _Refused(_OUTSIDE)
    return handle, final


def _names_in(root: _Root, directory: str) -> set[str]:
    try:
        handle = _fs.open_handle(directory)
    except OSError:
        raise _Refused(_NOT_FOUND) from None
    try:
        if not root.inside(_fs.final_path(handle)):
            raise _Refused(_OUTSIDE)
        return {name.casefold() for name, _ in _fs.list_handle(handle, _WALK_ENTRIES)}
    finally:
        _fs.close(handle)


def _entry(root: _Root, directory: str, name: str, attributes: int, seam: Seam) -> tuple[str, str] | None:
    """('dir' | 'file', final path) for a directory entry, or None to withhold it.

    Withheld: anything resolving outside the root, a file with more than one
    hard link, or anything that cannot be opened (a dangling link).
    """
    path = root.join(directory, name)
    if attributes & _DIRECTORY and not attributes & _REPARSE_POINT:
        return "dir", path
    if seam is not None:
        seam(path)
    try:
        handle = _fs.open_handle(path)
    except OSError:
        return None
    try:
        final = _fs.final_path(handle)
        if not root.inside(final):
            return None
        _, links, is_dir = _fs.standard_info(handle)
        if is_dir:
            return "dir", final
        return None if links > 1 else ("file", final)
    finally:
        _fs.close(handle)


def _guarded(function: Callable[..., str]) -> Callable[..., str]:
    """Any failure but a refusal becomes a message that names no path."""

    def run(*args: Any) -> str:
        try:
            return function(*args)
        except _Refused:
            raise
        except Exception as exc:  # noqa: BLE001 - an OS error message can carry a path
            raise _Refused(f"the file tool failed: {type(exc).__name__}") from None

    return run


def _decode(data: bytes, complete: bool) -> str | None:
    """UTF-8 text, or None for a binary file. A cut read keeps whole characters."""
    if b"\x00" in data:
        return None
    try:
        return codecs.getincrementaldecoder("utf-8")().decode(data, final=complete)
    except UnicodeDecodeError:
        return None


@_guarded
def _read(root: _Root, max_bytes: int, seam: Seam, path: str) -> str:
    handle, _ = _open_inside(root, _components(path), seam)
    try:
        size, links, is_dir = _fs.standard_info(handle)
        if is_dir:
            raise _Refused("not a file: use the directory listing tool")
        if links > 1:
            raise _Refused(_HARD_LINK)
        data = _fs.read(handle, max_bytes + 1)
    finally:
        _fs.close(handle)
    cut = len(data) > max_bytes
    text = _decode(data[:max_bytes], complete=not cut)
    if text is None:
        return f"[binary file of {size} bytes: not returned as text]"
    if cut:
        text += f"\n[file truncated: {len(text.encode('utf-8'))} of {size} bytes shown]"
    return text


def _notes(withheld: int, **counts: int) -> list[str]:
    notes = []
    if withheld:
        notes.append(
            f"[{withheld} entries withheld: they resolve outside the root folder, have more than "
            "one hard link, cannot be opened, or have a name that cannot be stored]"
        )
    for label, count in counts.items():
        if count:
            notes.append(f"[{count} {label.replace('_', ' ')}]")
    return notes


def _unstorable_name(name: str) -> bool:
    """A name no store can hold, such as one with a lone surrogate (FR-47).

    Windows allows one in a file name, and one such name used to turn a whole
    listing, glob or grep into a tool error at the executor's storability check.
    The entry is withheld instead, and a folder's whole subtree with it.
    """
    return unstorable_reason(name) is not None


@_guarded
def _list(root: _Root, max_entries: int, seam: Seam, path: str) -> str:
    handle, final = _open_inside(root, _components(path), seam)
    try:
        if not _fs.standard_info(handle)[2]:
            raise _Refused("not a directory")
        entries = _fs.list_handle(handle, _WALK_ENTRIES)
    finally:
        _fs.close(handle)
    lines, withheld, cut = [], 0, False
    for name, attributes in sorted(entries, key=lambda entry: entry[0].casefold()):
        if len(lines) == max_entries:
            cut = True
            break
        if _unstorable_name(name):
            withheld += 1
            continue
        found = _entry(root, final, name, attributes, None)
        if found is None:
            withheld += 1
        else:
            lines.append(name + "/" if found[0] == "dir" else name)
    notes = _notes(withheld)
    if cut:
        notes.append(f"[listing cut at {max_entries} entries]")
    return "\n".join(lines + notes) if lines or notes else "[empty directory]"


class _Walk:
    """Budget and bookkeeping shared by glob and grep."""

    def __init__(self, root: _Root, seam: Seam) -> None:
        self.root, self.seam = root, seam
        self.deadline = time.monotonic() + _WALK_SECONDS
        self.visited = 0
        self.withheld = 0
        self.stopped = False

    def spent(self) -> bool:
        """Whether the budget is used up. Read before every entry, not only before
        every directory: one flat folder of 24000 files ran a grep 20.8 s against a
        20 s budget when the clock was read per directory (C5, M10 review round 1)."""
        if not self.stopped and (time.monotonic() > self.deadline or self.visited > _WALK_ENTRIES):
            self.stopped = True
        return self.stopped

    def entries(self, directory: str) -> list[tuple[str, int]]:
        """A directory's entries, read through a handle proven inside the root."""
        if self.spent():
            return []
        if self.seam is not None:
            self.seam(directory)
        try:
            handle = _fs.open_handle(directory)
        except OSError:
            self.withheld += 1
            return []
        try:
            if not self.root.inside(_fs.final_path(handle)):
                self.withheld += 1
                return []
            listed = _fs.list_handle(handle, _WALK_ENTRIES)
        finally:
            _fs.close(handle)
        self.visited += len(listed)
        return sorted(listed, key=lambda entry: entry[0].casefold())

    def notes(self) -> list[str]:
        notes = _notes(self.withheld)
        if self.stopped:
            notes.append("[search stopped early: it reached its time or size budget]")
        return notes


@_guarded
def _glob(root: _Root, max_results: int, seam: Seam, pattern: str) -> str:
    parts = _components(pattern, pattern=True)
    if not parts:
        raise _Refused("pattern refused: it matches nothing")
    if parts[-1] == "**":
        parts.append("*")
    walk = _Walk(root, seam)
    results: list[str] = []
    seen: set[str] = set()
    # Entries withheld for an unstorable name, by (directory, name): a pattern
    # such as '**' meets a folder once to descend into it and again to show it,
    # and counted it twice (R3, M11 review round 1).
    unstorable: set[tuple[str, str]] = set()
    # (directory's final path, requested names so far, pattern index, ancestors)
    stack = [(root.path, (), 0, frozenset({root.path.casefold()}))]
    while stack and len(results) <= max_results and not walk.stopped:
        directory, names, index, ancestors = stack.pop()
        part = parts[index]
        if part == "**":
            stack.append((directory, names, index + 1, ancestors))
        literal = not any(character in part for character in "*?[")
        entries = walk.entries(directory)
        if literal and _SHORT_NAME.search(part) and part.casefold() not in {n.casefold() for n, _ in entries}:
            raise _Refused(_NOT_ALLOWED)
        for name, attributes in reversed(entries):
            if walk.spent():
                break
            matched = part == "**" or fnmatch.fnmatchcase(name.casefold(), part.casefold())
            if not matched:
                continue
            if _unstorable_name(name):
                # Counted where the entry would have been used: shown here, or a
                # folder descended into. A file met by '**' is used, if at all,
                # by the part after it, which counts it there.
                last = index == len(parts) - 1
                used = (part != "**" and last) or (attributes & (_DIRECTORY | _REPARSE_POINT) and (part == "**" or not last))
                if used and (directory, name) not in unstorable:
                    unstorable.add((directory, name))
                    walk.withheld += 1
                continue
            found = _entry(root, directory, name, attributes, None)
            if found is None:
                walk.withheld += 1
                continue
            kind, final = found
            relative = names + (name,)
            if part != "**" and index == len(parts) - 1:
                shown = "/".join(relative) + ("/" if kind == "dir" else "")
                if shown not in seen:
                    seen.add(shown)
                    results.append(shown)
            if kind == "dir" and final.casefold() not in ancestors:
                if part == "**":
                    stack.append((final, relative, index, ancestors | {final.casefold()}))
                elif index < len(parts) - 1:
                    stack.append((final, relative, index + 1, ancestors | {final.casefold()}))
    notes = walk.notes()
    if len(results) > max_results:
        results = results[:max_results]
        notes.append(f"[results cut at {max_results}]")
    lines = sorted(results) + notes
    return "\n".join(lines) if results else "\n".join(["[no matches]"] + notes)


@_guarded
def _grep(root: _Root, max_matches: int, max_file_bytes: int, seam: Seam, text: str, path: str) -> str:
    parts = _components(path)
    handle, final = _open_inside(root, parts, seam)
    try:
        _, links, is_dir = _fs.standard_info(handle)
    finally:
        _fs.close(handle)
    if not is_dir and links > 1:
        # Named directly, it is refused as read refuses it; met while walking, withheld.
        raise _Refused(_HARD_LINK)
    walk = _Walk(root, seam)
    matches: list[str] = []
    skipped = {"files_skipped_as_larger_than_the_cap": 0, "binary_files_skipped": 0}

    def search(file_path: str, relative: str) -> None:
        if walk.seam is not None:
            walk.seam(file_path)
        try:
            opened = _fs.open_handle(file_path)
        except OSError:
            walk.withheld += 1
            return
        try:
            if not root.inside(_fs.final_path(opened)):
                walk.withheld += 1
                return
            size, links, is_directory = _fs.standard_info(opened)
            if is_directory:
                return
            if links > 1:
                walk.withheld += 1
                return
            if size > max_file_bytes:
                skipped["files_skipped_as_larger_than_the_cap"] += 1
                return
            data = _fs.read(opened, max_file_bytes + 1)
        finally:
            _fs.close(opened)
        content = _decode(data[:max_file_bytes], complete=True) if len(data) <= max_file_bytes else None
        if content is None:
            skipped["binary_files_skipped" if len(data) <= max_file_bytes else "files_skipped_as_larger_than_the_cap"] += 1
            return
        for number, line in enumerate(content.split("\n"), 1):
            if text in line:  # literal: no pattern a model sends can backtrack (D6)
                matches.append(f"{relative}:{number}: {line.rstrip(chr(13))[:_LINE_CHARS]}")
                if len(matches) > max_matches:
                    return

    if not is_dir:
        search(final, "/".join(parts))
    else:
        stack = [(final, tuple(parts), frozenset({final.casefold()}))]
        while stack and len(matches) <= max_matches and not walk.stopped:
            directory, names, ancestors = stack.pop()
            for name, attributes in reversed(walk.entries(directory)):
                if len(matches) > max_matches or walk.spent():
                    break
                if _unstorable_name(name):  # a file or a whole folder, withheld (FR-47)
                    walk.withheld += 1
                    continue
                entry_path = root.join(directory, name)
                if attributes & _DIRECTORY or attributes & _REPARSE_POINT:
                    found = _entry(root, directory, name, attributes, None)
                    if found is None:
                        walk.withheld += 1
                    elif found[0] == "dir":
                        if found[1].casefold() not in ancestors:
                            stack.append((found[1], names + (name,), ancestors | {found[1].casefold()}))
                    else:
                        search(entry_path, "/".join(names + (name,)))
                else:
                    search(entry_path, "/".join(names + (name,)))
    notes = walk.notes() + _notes(0, **skipped)
    if len(matches) > max_matches:
        matches = matches[:max_matches]
        notes.append(f"[matches cut at {max_matches}]")
    return "\n".join(matches + notes) if matches else "\n".join(["[no matches]"] + notes)


def _file_tool(name: str, description: str, properties: dict[str, Any], required: list[str],
               root: _Root, configuration: dict[str, Any], fn: Callable[..., Awaitable[str]]) -> Tool:
    return Tool(
        spec=ToolSpec(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            configuration={"root": root.path, **configuration},
            # Read-only and confined, so its calls may run beside each other (FR-44).
            concurrency_safe=True,
        ),
        fn=fn,
    )


_PATH = {"type": "string", "minLength": 1, "maxLength": 1024}


def read_file_tool(root: Any, *, max_bytes: int = 1_000_000, name: str = "read_file",
                   _between_check_and_open: Seam = None) -> Tool:
    """Read a UTF-8 text file under `root`, capped at `max_bytes`."""
    base, limit, seam = _Root(root), _positive("max_bytes", max_bytes), _between_check_and_open

    async def read_file(path: str) -> str:
        return await _on_file_pool(_read, base, limit, seam, path)

    return _file_tool(name, "Read a text file inside the root folder. Paths are relative to that folder.",
                      {"path": _PATH}, ["path"], base, {"max_bytes": limit}, read_file)


def list_directory_tool(root: Any, *, max_entries: int = 1000, name: str = "list_directory",
                        _between_check_and_open: Seam = None) -> Tool:
    """List a directory under `root`, capped at `max_entries`; directories end in '/'."""
    base, limit, seam = _Root(root), _positive("max_entries", max_entries), _between_check_and_open

    async def list_directory(path: str = ".") -> str:
        return await _on_file_pool(_list, base, limit, seam, path)

    return _file_tool(name, "List a directory inside the root folder. Directories end in '/'.",
                      {"path": {**_PATH, "default": "."}}, [], base, {"max_entries": limit}, list_directory)


def glob_tool(root: Any, *, max_results: int = 1000, name: str = "glob_files",
              _between_check_and_open: Seam = None) -> Tool:
    """Find paths under `root` matching a pattern ('*', '?', '[...]', '**'), capped at `max_results`."""
    base, limit, seam = _Root(root), _positive("max_results", max_results), _between_check_and_open

    async def glob_files(pattern: str) -> str:
        return await _on_file_pool(_glob, base, limit, seam, pattern)

    return _file_tool(name, "Find files inside the root folder by pattern, for example '**/*.py'.",
                      {"pattern": _PATH}, ["pattern"], base, {"max_results": limit}, glob_files)


def grep_tool(root: Any, *, max_matches: int = 200, max_file_bytes: int = 1_000_000, name: str = "grep_files",
              _between_check_and_open: Seam = None) -> Tool:
    """Search text files under `root` for literal text (never a regular expression, D6)."""
    base, seam = _Root(root), _between_check_and_open
    matches, file_bytes = _positive("max_matches", max_matches), _positive("max_file_bytes", max_file_bytes)

    async def grep_files(text: str, path: str = ".") -> str:
        return await _on_file_pool(_grep, base, matches, file_bytes, seam, text, path)

    return _file_tool(
        name,
        "Search text files inside the root folder for literal text. Reports path:line: text.",
        {"text": {"type": "string", "minLength": 1, "maxLength": 1000}, "path": {**_PATH, "default": "."}},
        ["text"], base, {"max_matches": matches, "max_file_bytes": file_bytes}, grep_files,
    )


# =============================================================================
# Fetch (FR-38)
# =============================================================================

# Whitespace, control characters and backslashes: a backslash is a separator to
# some URL parsers and not to others, so a URL holding one means two things.
_URL_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\\]")
_REDIRECTS = {301, 302, 303, 307, 308}
_TEXT_TYPES = {"application/json", "application/xml", "application/xhtml+xml", "application/javascript"}
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_NOT_PUBLIC = "URL refused: its host resolves to an address that is not on the public internet"


def _allow_entry(entry: Any) -> str:
    """One allowlist entry, normalised the way a URL's host is: lowercase, no
    trailing dot, IDNA-encoded. '*.' admits subdomains, not the name itself."""
    if not isinstance(entry, str):
        raise TypeError(f"allowlist entries must be strings, got {type(entry).__name__}")
    spelled = entry.strip()
    wildcard = spelled.startswith("*.")
    host = spelled[2:] if wildcard else spelled
    # A port is refused by its spelling, not by parsing: httpx drops a default
    # port (':80') and an empty one (':'), so the parsed URL shows none.
    bracketed = host.startswith("[") and host.endswith("]")
    if (
        not host
        or _URL_FORBIDDEN.search(host)
        or any(character in host for character in "/@?#*%")
        or (":" in host and not bracketed)
        or (host.startswith("[") and not bracketed)
    ):
        raise ValueError(f"allowlist entry {entry!r} is not a host name")
    try:
        url = httpx.URL(f"http://{host}/")
    except Exception:  # noqa: BLE001
        raise ValueError(f"allowlist entry {entry!r} is not a host name") from None
    normal = url.raw_host.decode("ascii").lower().rstrip(".")
    if url.port is not None or url.userinfo or url.raw_path != b"/" or not normal:
        raise ValueError(f"allowlist entry {entry!r} is not a host name")
    return ("*." if wildcard else "") + normal


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Globally routable, and not a special-purpose address in disguise."""
    if address.version == 6:
        if address.teredo is not None:
            return False
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is None and any(address in network for network in _NAT64):
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded is not None and not _is_public(embedded):
            return False
        if address.is_site_local:
            return False
    return address.is_global and not (
        address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
    )


def _literal_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a numeric host spells, or None for a name.

    A host whose last label is a number is an address, not a name to look up
    (the WHATWG rule): '2130706433', '0x7f.1' and '127.1' are all loopback to
    some resolver. Only the plain forms are accepted; the rest are refused
    rather than sent to DNS, whose answer could stand in for them.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    last = host.rsplit(".", 1)[-1]
    if last.isdigit() or re.fullmatch(r"0x[0-9a-f]*", last):
        raise _Refused("URL refused: a numeric host must be a plain IPv4 or IPv6 address")
    return None


@dataclass(frozen=True)
class _FetchOptions:
    allowlist: frozenset[str]
    max_bytes: int
    timeout: float
    max_redirects: int
    resolve: Callable[[str, int], Awaitable[Sequence[str]]]
    connect: Callable[[str, int], tuple[str, int]]
    ssl_context: Any


def _target(text: Any, allowlist: frozenset[str]) -> tuple[httpx.URL, str]:
    if not isinstance(text, str) or _URL_FORBIDDEN.search(text):
        raise _Refused("URL refused: it holds whitespace, a control character or a backslash")
    try:
        url = httpx.URL(text)
        raw = url.raw_host.decode("ascii")
        port = url.port
    except Exception:  # noqa: BLE001
        raise _Refused("URL refused: it cannot be parsed") from None
    if url.scheme not in ("http", "https"):
        raise _Refused("URL refused: only http and https are allowed")
    if url.userinfo:
        raise _Refused("URL refused: it carries a user name or password")
    if not raw or "%" in raw or (port is not None and not 0 < port < 65536):
        raise _Refused("URL refused: its host or port is not valid")
    host = raw.lower().rstrip(".")
    allowed = host in allowlist or any(
        entry.startswith("*.") and host.endswith(entry[1:]) for entry in allowlist
    )
    if not allowed:
        raise _Refused("URL refused: its host is not on the allowlist")
    return url, host


async def _public_addresses(host: str, port: int, options: _FetchOptions) -> list[str]:
    literal = _literal_address(host)
    if literal is not None:
        answers: Sequence[Any] = [str(literal)]
    else:
        try:
            answers = list(await options.resolve(host, port))
        except Exception:  # noqa: BLE001
            raise _Refused("URL refused: its host could not be resolved") from None
    if not answers:
        raise _Refused("URL refused: its host could not be resolved")
    addresses = []
    for answer in answers:
        try:
            address = ipaddress.ip_address(str(answer).split("%")[0])
        except ValueError:
            raise _Refused(_NOT_PUBLIC) from None
        if not _is_public(address):  # every answer, not only the one used
            raise _Refused(_NOT_PUBLIC)
        addresses.append(str(address))
    return addresses


async def _system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _pinned(url: httpx.URL, host: str, address: str, port: int, options: _FetchOptions) -> httpx.Request:
    """A request that connects to `address` -- the one checked, never a second
    DNS answer -- while Host and TLS name the host, so the certificate is still
    verified against it."""
    dial_address, dial_port = options.connect(address, port)
    netloc = f"[{dial_address}]" if ":" in dial_address else dial_address
    shown = f"[{host}]" if ":" in host else host
    return httpx.Request(
        "GET",
        f"{url.scheme}://{netloc}:{dial_port}{url.raw_path.decode('ascii')}",
        # Built by hand: no client default headers, no cookie jar, no auth.
        headers={
            "Host": shown if url.port is None else f"{shown}:{url.port}",
            "Accept": "text/html, text/plain, application/json, application/xml;q=0.9, */*;q=0.1",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": f"agentsdk-fetch/{__version__}",
        },
        extensions={"sni_hostname": host} if url.scheme == "https" else {},
    )


async def _bounded_body(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    """At most `limit` DECODED bytes, decompressing no further than that.

    Raw chunks are decoded here with a cap on each step's output, because a
    library decoder expands a whole network chunk at once: 64 KB of a
    compression bomb is tens of megabytes before anything can count it.
    """
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding in ("", "identity"):
        decoder = None
    elif encoding in ("gzip", "x-gzip", "deflate"):
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if "gzip" in encoding else zlib.MAX_WBITS)
    else:
        raise _Refused("fetch refused: the response uses a content encoding this tool does not decode")
    body = bytearray()
    async for chunk in response.aiter_raw():
        data = chunk
        while data:
            room = limit + 1 - len(body)
            if decoder is None:
                body += data[:room]
                data = b""
            else:
                try:
                    piece = decoder.decompress(data, room)
                except zlib.error:
                    raise _Refused("fetch failed: the response body could not be decoded") from None
                body += piece
                remaining = decoder.unconsumed_tail
                if not piece and remaining == data:
                    break
                data = remaining
            if len(body) > limit:
                return bytes(body[:limit]), True
    return bytes(body), False


def _media(content_type: str) -> tuple[str, str]:
    media, _, parameters = content_type.partition(";")
    charset = "utf-8"
    for parameter in parameters.split(";"):
        key, _, value = parameter.partition("=")
        if key.strip().lower() != "charset":
            continue
        try:
            codec = codecs.lookup(value.strip().strip('"'))
        except LookupError:
            continue  # a name no codec knows is read as UTF-8, as before
        # codecs.lookup also resolves bytes-to-bytes codecs -- zlib, bz2, base64 --
        # and the server chooses the name, so charset=zlib_codec was a second
        # decompression stage past the decoded-byte cap (D1, M10 review round 1).
        # It failed closed only through an assert inside the standard library,
        # which python -O strips. A text encoding turns bytes into str, so that
        # is checked as well as the codec's own flag.
        try:
            text = getattr(codec, "_is_text_encoding", True) and isinstance(
                codec.incrementaldecoder("strict").decode(b"", False), str
            )
        except Exception:  # noqa: BLE001 - a codec that cannot say is not trusted
            text = False
        if not text:
            raise _Refused("fetch refused: the response names a charset that is not a text encoding")
        charset = codec.name
    return media.strip().lower(), charset


@functools.lru_cache(maxsize=1)
def _default_tls() -> Any:
    """The certificate store, loaded once: loading it costs about 0.4 s. Not from
    the environment (SSL_CERT_FILE), for the same reason proxies are not."""
    return httpx.create_ssl_context(trust_env=False)


async def _fetch_hops(options: _FetchOptions, text: str) -> ToolOutput:
    client: httpx.AsyncClient | None = None
    try:
        for _ in range(options.max_redirects + 1):  # the first request, then each redirect
            url, host = _target(text, options.allowlist)
            port = url.port or (443 if url.scheme == "https" else 80)
            addresses = await _public_addresses(host, port, options)
            if client is None:
                # Built only once a hop has passed every check, so a refused URL
                # costs no connection setup at all.
                client = httpx.AsyncClient(
                    trust_env=False,  # environment proxies would bypass the address check
                    verify=options.ssl_context if options.ssl_context is not None else _default_tls(),
                    follow_redirects=False,  # each hop is checked here instead
                    timeout=httpx.Timeout(options.timeout),
                )
            try:
                response = await client.send(_pinned(url, host, addresses[0], port, options), stream=True)
            except Exception as exc:  # noqa: BLE001 - its message can name the pinned address
                raise _Refused(f"fetch failed: {type(exc).__name__}") from None
            try:
                location = response.headers.get("location")
                if response.status_code in _REDIRECTS and location is not None:
                    if _URL_FORBIDDEN.search(location):
                        raise _Refused("redirect refused: its location holds whitespace or a backslash")
                    try:
                        text = str(url.join(location))
                    except Exception:  # noqa: BLE001
                        raise _Refused("redirect refused: its location cannot be parsed") from None
                    continue
                media, charset = _media(response.headers.get("content-type", ""))
                if not (media.startswith("text/") or media in _TEXT_TYPES or media.endswith(("+json", "+xml"))):
                    raise _Refused("fetch refused: the response is not text")
                body, cut = await _bounded_body(response, options.max_bytes)
                status = response.status_code
            finally:
                await response.aclose()
            content = codecs.getincrementaldecoder(charset)(errors="replace").decode(body, final=not cut)
            if cut:
                content += f"\n[body truncated at {options.max_bytes} bytes]"
            return ToolOutput(f"[HTTP {status}]\n{content}", source_uri=str(url))
    finally:
        if client is not None:
            await client.aclose()
    # The redirect cap is the loop's bound and nothing else: a second check
    # inside the loop duplicated this one, and mutation M21 showed either could
    # be deleted with every test still green.
    raise _Refused(f"fetch refused: more than {options.max_redirects} redirects")


async def _fetch(options: _FetchOptions, text: str) -> ToolOutput:
    try:
        async with asyncio.timeout(options.timeout):  # one deadline: DNS, every hop, the body
            return await _fetch_hops(options, text)
    except TimeoutError:
        raise _Refused(f"fetch stopped: it passed its total deadline of {options.timeout:g} seconds") from None
    except _Refused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _Refused(f"fetch failed: {type(exc).__name__}") from None


def fetch_tool(
    allowlist: Iterable[str],
    *,
    max_bytes: int = 1_000_000,
    timeout_seconds: float = 15.0,
    max_redirects: int = 5,
    name: str = "fetch_url",
    _resolve: Callable[[str, int], Awaitable[Sequence[str]]] | None = None,
    _connect: Callable[[str, int], tuple[str, int]] | None = None,
    _ssl_context: Any = None,
) -> Tool:
    """Fetch text from http(s) URLs whose host is on `allowlist`.

    Entries are host names; '*.example.com' admits subdomains of example.com.
    An empty allowlist refuses every URL. The underscore arguments are test
    seams (a resolver, an address mapping, a TLS context); the tool's input
    schema admits only `url`, so a model cannot reach them.
    """
    if isinstance(allowlist, (str, bytes)) or not isinstance(allowlist, (list, tuple, set, frozenset)):
        raise TypeError("allowlist must be a list of host names; an empty list refuses every URL")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) \
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError(f"timeout_seconds must be a positive number, got {timeout_seconds!r}")
    if isinstance(max_redirects, bool) or not isinstance(max_redirects, int) or max_redirects < 0:
        raise ValueError(f"max_redirects must be a non-negative int, got {max_redirects!r}")
    options = _FetchOptions(
        allowlist=frozenset(_allow_entry(entry) for entry in allowlist),
        max_bytes=_positive("max_bytes", max_bytes),
        timeout=float(timeout_seconds),
        max_redirects=max_redirects,
        resolve=_resolve or _system_resolve,
        connect=_connect or (lambda address, port: (address, port)),
        ssl_context=_ssl_context,
    )

    async def fetch_url(url: str) -> ToolOutput:
        return await _fetch(options, url)

    return Tool(
        spec=ToolSpec(
            name=name,
            description="Fetch a web page or text document from an allowed host. The result is untrusted external content.",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 2048}},
                "required": ["url"],
                "additionalProperties": False,
            },
            # The tool's own deadline is the one that reports; the executor's is a backstop.
            timeout_seconds=options.timeout + 5,
            result_provenance=ResultProvenance.external(),
            # A read with no side effects, so its calls may run beside each other (FR-44).
            concurrency_safe=True,
            configuration={
                "allowlist": sorted(options.allowlist),
                "max_bytes": options.max_bytes,
                "timeout_seconds": options.timeout,
                "max_redirects": options.max_redirects,
            },
        ),
        fn=fetch_url,
    )


# =============================================================================
# Web search (FR-39)
# =============================================================================


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchBackend(Protocol):
    """Whatever search service you use. The SDK ships none (FR-39)."""

    async def search(self, query: str, max_results: int) -> Sequence[SearchResult]: ...


def web_search_tool(
    backend: SearchBackend,
    *,
    max_results: int = 10,
    max_result_chars: int = 1000,
    name: str = "web_search",
) -> Tool:
    """Search through `backend`, capping the number of results and each field's length."""
    if backend is None or not callable(getattr(backend, "search", None)):
        raise TypeError("web_search_tool needs a backend with an async search(query, max_results)")
    cap = _positive("max_results", max_results)
    size = _positive("max_result_chars", max_result_chars)

    async def web_search(query: str, max_results: int = cap) -> str:
        found = await backend.search(query, max_results)
        if isinstance(found, (str, bytes)) or not isinstance(found, Iterable):
            raise _Refused("the search backend returned something that is not a list of results")
        lines: list[str] = []
        for item in found:
            if len(lines) == max_results:
                break
            fields = [getattr(item, field, None) for field in ("title", "url", "snippet")]
            if not all(isinstance(value, str) for value in fields):
                continue
            title, link, snippet = (" ".join(value.split())[:size] for value in fields)
            lines.append(f"{len(lines) + 1}. {title}\n   {link}\n   {snippet}")
        return "\n".join(lines) if lines else "[no results]"

    return Tool(
        spec=ToolSpec(
            name=name,
            description="Search the web. Results are untrusted external content.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": cap},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            result_provenance=ResultProvenance.external(),
            configuration={"max_results": cap, "max_result_chars": size},
            # A query with no side effects, so its calls may run beside each other (FR-44).
            concurrency_safe=True,
        ),
        fn=web_search,
    )
