"""Filesystem tools.

Split into a read group and a write group so an operator can hand out reading
without handing out writing. Every path goes through
`emalia.security.resolve_in_sandbox`, which is the only place containment is
decided.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from collections.abc import Callable
from datetime import UTC
from pathlib import Path

from emalia.errors import ToolExecutionError
from emalia.security.paths import describe_roots, resolve_in_sandbox
from emalia.tools.base import ToolGroup, tool_result

__all__ = ["FileReadTools", "FileWriteTools"]

logger = logging.getLogger(__name__)

_BINARY_HINT = (
    "This looks like a binary file, so its contents are not shown. "
    "Attach it to an email instead if the user needs it."
)


def _looks_binary(path: Path, sample: int = 4096) -> bool:
    """Guess whether a file is binary by looking for NUL bytes in its head."""
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(sample)
    except OSError:
        return True


class FileReadTools(ToolGroup):
    """Read-only filesystem access inside the policy's sandbox roots."""

    toolset = "file_read"

    def tools(self) -> list[Callable[..., str]]:
        """The read tools, as plain callables."""
        return [
            self._read_file(),
            self._list_directory(),
            self._search_files(),
            self._file_info(),
        ]

    def _read_file(self) -> Callable[..., str]:
        policy = self.policy

        @tool_result(policy)
        def read_file(path: str, max_lines: int = 0) -> str:
            """Read a text file from this machine.

            Only files inside the allowed directories can be read, and
            credential-shaped files are refused even there.

            Args:
                path: The file to read. Relative paths resolve against the
                    working directory.
                max_lines: Stop after this many lines. 0 reads the whole file,
                    subject to the output limit.

            Returns:
                The file contents, or an explanation of why it could not be
                read.
            """
            resolved = resolve_in_sandbox(path, policy.sandbox_roots, must_exist=True)
            if resolved.is_dir():
                raise ToolExecutionError(
                    f"{resolved} is a directory. Use list_directory, or attach it to an "
                    "email to have it zipped."
                )
            if _looks_binary(resolved):
                return f"{resolved} ({resolved.stat().st_size} bytes). {_BINARY_HINT}"

            text = resolved.read_text(encoding="utf-8", errors="replace")
            if max_lines > 0:
                lines = text.splitlines()
                text = "\n".join(lines[:max_lines])
                if len(lines) > max_lines:
                    text += f"\n\n[stopped after {max_lines} of {len(lines)} lines]"
            return text

        return read_file

    def _list_directory(self) -> Callable[..., str]:
        policy = self.policy

        @tool_result(policy)
        def list_directory(path: str = ".", show_hidden: bool = False) -> str:
            """List the entries in a directory.

            Args:
                path: The directory to list.
                show_hidden: Include entries whose name starts with a dot.

            Returns:
                One entry per line, directories marked and file sizes shown.
            """
            resolved = resolve_in_sandbox(path, policy.sandbox_roots, must_exist=True)
            if not resolved.is_dir():
                raise ToolExecutionError(f"{resolved} is not a directory.")

            lines: list[str] = []
            for entry in sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                if entry.name.startswith(".") and not show_hidden:
                    continue
                if entry.is_dir():
                    lines.append(f"{entry.name}/")
                else:
                    try:
                        lines.append(f"{entry.name}  ({entry.stat().st_size} bytes)")
                    except OSError:
                        lines.append(f"{entry.name}  (unreadable)")
            body = "\n".join(lines) if lines else "(empty)"
            return f"{resolved}:\n{body}"

        return list_directory

    def _search_files(self) -> Callable[..., str]:
        policy = self.policy

        @tool_result(policy)
        def search_files(
            name_contains: str,
            path: str = "",
            limit: int = 50,
            files_only: bool = False,
        ) -> str:
            """Find files or directories by name.

            Args:
                name_contains: Text that must appear in the entry's name. The
                    match is case-insensitive.
                path: Where to start searching. Empty searches every allowed
                    directory.
                limit: Stop after this many matches. Capped at 200.
                files_only: Skip directories in the results.

            Returns:
                One matching path per line, or a line saying nothing matched.
            """
            if not name_contains.strip():
                raise ToolExecutionError("search_files needs something to search for.")

            roots = (
                [resolve_in_sandbox(path, policy.sandbox_roots, must_exist=True)]
                if path
                else list(policy.sandbox_roots)
            )
            if not roots:
                raise ToolExecutionError(
                    f"No searchable directories are configured (roots: {describe_roots(roots)})."
                )

            needle = name_contains.lower()
            capped = max(1, min(limit, 200))
            matches: list[str] = []
            for root in roots:
                for current, dirs, files in os.walk(root):
                    # Skipping these keeps a search over a home directory from
                    # spending minutes inside build output.
                    dirs[:] = [
                        d
                        for d in dirs
                        if d not in {".git", "node_modules", "__pycache__", ".venv", "venv"}
                    ]
                    candidates = files if files_only else [*dirs, *files]
                    for entry in candidates:
                        if needle in entry.lower():
                            matches.append(str(Path(current) / entry))
                            if len(matches) >= capped:
                                break
                    if len(matches) >= capped:
                        break
                if len(matches) >= capped:
                    break

            if not matches:
                return f"No entry matching {name_contains!r} under {describe_roots(roots)}."
            suffix = f"\n[stopped at {capped} matches]" if len(matches) >= capped else ""
            return "\n".join(matches) + suffix

        return search_files

    def _file_info(self) -> Callable[..., str]:
        policy = self.policy

        @tool_result(policy)
        def file_info(path: str) -> str:
            """Report a file or directory's size, type and modification time.

            Args:
                path: The entry to describe.

            Returns:
                A short description, one field per line.
            """
            resolved = resolve_in_sandbox(path, policy.sandbox_roots, must_exist=True)
            stat = resolved.stat()
            guessed, encoding = mimetypes.guess_type(resolved.name)
            from datetime import datetime

            fields = [
                f"path: {resolved}",
                f"kind: {'directory' if resolved.is_dir() else 'file'}",
                f"size: {stat.st_size} bytes",
                f"modified: {datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()}",
                f"type: {guessed or 'unknown'}",
            ]
            if encoding:
                fields.append(f"encoding: {encoding}")
            if resolved.is_dir():
                try:
                    fields.append(f"entries: {sum(1 for _ in resolved.iterdir())}")
                except OSError:
                    fields.append("entries: unreadable")
            return "\n".join(fields)

        return file_info


class FileWriteTools(ToolGroup):
    """Write access inside the policy's sandbox roots.

    Off by default. Enabling it lets anyone who can get past the sender gate
    put bytes on the machine.
    """

    toolset = "file_write"

    def tools(self) -> list[Callable[..., str]]:
        """The write tools, as plain callables."""
        return [self._write_file(), self._delete_file()]

    def _write_file(self) -> Callable[..., str]:
        policy = self.policy

        @tool_result(policy)
        def write_file(path: str, content: str, append: bool = False) -> str:
            """Write text to a file on this machine.

            Parent directories are created as needed. Only paths inside the
            allowed directories can be written.

            Args:
                path: The file to write.
                content: The text to write.
                append: Add to the end of an existing file instead of
                    replacing it.

            Returns:
                Confirmation naming the path and how many bytes were written.
            """
            resolved = resolve_in_sandbox(path, policy.sandbox_roots)
            if resolved.is_dir():
                raise ToolExecutionError(f"{resolved} is a directory, not a file.")
            resolved.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if append else "w"
            with resolved.open(mode, encoding="utf-8", newline="") as handle:
                handle.write(content)
            verb = "Appended" if append else "Wrote"
            return f"{verb} {len(content.encode('utf-8'))} bytes to {resolved}."

        return write_file

    def _delete_file(self) -> Callable[..., str]:
        policy = self.policy

        @tool_result(policy)
        def delete_file(path: str) -> str:
            """Delete a single file.

            Directories are refused. Deleting a tree from an email instruction
            is not something this tool will do, whatever the request says.

            Args:
                path: The file to delete.

            Returns:
                Confirmation, or the reason the deletion was refused.
            """
            resolved = resolve_in_sandbox(path, policy.sandbox_roots, must_exist=True)
            if resolved.is_dir():
                raise ToolExecutionError(
                    f"{resolved} is a directory. Only single files can be deleted."
                )
            size = resolved.stat().st_size
            resolved.unlink()
            logger.warning("Deleted %s (%d bytes) on a tool call", resolved, size)
            return f"Deleted {resolved} ({size} bytes)."

        return delete_file
