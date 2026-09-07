"""Filesystem sandboxing for the file tools.

The original's `check_path_in_range` compared path components by depth, which
answered "how far apart are these" rather than "is this inside that". It could
not stop `../../..` and it had an off-by-one that made `layer=1` reject the
directory it was pointed at. Containment here is a resolved-path prefix check,
which is the only form that survives symlinks and traversal.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

from emalia.errors import SandboxViolationError

__all__ = ["resolve_in_sandbox", "is_within", "describe_roots", "DEFAULT_DENY_GLOBS"]

#: Names that are refused inside a sandbox root regardless of the roots given.
#: A root of "my home directory" should still not hand over SSH keys because
#: the model was asked nicely.
DEFAULT_DENY_GLOBS: tuple[str, ...] = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*id_rsa*",
    "*id_ed25519*",
    ".env",
    ".env.*",
    "*.kdbx",
    "credentials",
    "credentials.json",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
)


def is_within(path: Path, root: Path) -> bool:
    """Whether a resolved path sits inside a resolved root.

    Args:
        path: The candidate path, already resolved.
        root: The root directory, already resolved.

    Returns:
        True if `path` is `root` or below it.
    """
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_strict_enough(path: Path) -> Path:
    """Resolve a path, tolerating trailing components that do not exist yet.

    Writing a new file needs its ancestors to resolve, not the file itself.
    Resolving the deepest existing ancestor is also what closes the symlink
    hole: a caller cannot name a not-yet-existing path whose parent is a
    symlink pointing out of the sandbox.

    Args:
        path: The path to resolve.

    Returns:
        An absolute path whose existing prefix is fully resolved and whose
        missing tail is appended verbatim.
    """
    expanded = Path(os.path.expandvars(str(path))).expanduser()
    if expanded.exists():
        return expanded.resolve()

    missing: list[str] = []
    ancestor = expanded
    while not ancestor.exists() and ancestor != ancestor.parent:
        missing.append(ancestor.name)
        ancestor = ancestor.parent

    if not ancestor.exists():
        return expanded.absolute()
    # `missing` was collected leaf-first, so put it back the other way round.
    return ancestor.resolve().joinpath(*reversed(missing))


def _resolve_candidates(path: str | os.PathLike[str], roots: Sequence[Path]) -> list[Path]:
    """Every place a caller-supplied path might reasonably mean.

    An absolute path means itself. A relative path is interpreted against each
    sandbox root, in order, before falling back to the working directory.
    Resolving a bare ``notes.txt`` against the daemon's cwd would be a
    surprise: the cwd is usually outside the sandbox, so the tool would refuse
    a name the operator can see sitting in the root they configured.

    Args:
        path: The path as given.
        roots: The sandbox roots, in preference order.

    Returns:
        Candidate resolved paths, most likely first.
    """
    raw = Path(os.path.expandvars(str(path))).expanduser()
    if raw.is_absolute():
        return [_resolve_strict_enough(raw)]

    candidates = [_resolve_strict_enough(root / raw) for root in roots]
    candidates.append(_resolve_strict_enough(raw))
    return candidates


def resolve_in_sandbox(
    path: str | os.PathLike[str],
    roots: Sequence[Path],
    *,
    must_exist: bool = False,
    deny_globs: Iterable[str] = DEFAULT_DENY_GLOBS,
) -> Path:
    """Resolve a caller-supplied path and assert it is inside the sandbox.

    Args:
        path: The path as given, possibly relative, possibly containing ``..``
            or environment variables. A relative path is interpreted against
            each sandbox root before the working directory.
        roots: The sandbox roots. An empty sequence refuses everything, which
            is the correct behaviour when file access was never configured.
        must_exist: Also require that the resolved path exists.
        deny_globs: Basename patterns refused even inside a root.

    Returns:
        The resolved absolute path.

    Raises:
        SandboxViolationError: If there are no roots, the path resolves
            outside all of them, it matches a deny pattern, or `must_exist` is
            set and it does not exist.
    """
    if not roots:
        raise SandboxViolationError(
            "File access is not configured: the policy has no sandbox_roots, "
            "so every path is refused."
        )

    candidates = _resolve_candidates(path, roots)
    contained = [c for c in candidates if any(is_within(c, root) for root in roots)]
    # An existing candidate beats a merely plausible one, so a relative name
    # that exists under the second root is found rather than reported missing
    # under the first.
    resolved = next((c for c in contained if c.exists()), None) or (
        contained[0] if contained else None
    )

    if resolved is None:
        raise SandboxViolationError(
            f"{path} resolves to {candidates[0]}, which is outside the allowed "
            f"directories ({describe_roots(roots)})."
        )

    name = resolved.name.lower()
    for pattern in deny_globs:
        if fnmatch.fnmatch(name, pattern.lower()):
            raise SandboxViolationError(
                f"{resolved.name} matches the deny pattern {pattern!r} and is refused "
                "even inside the sandbox. Credential files are never readable."
            )

    if must_exist and not resolved.exists():
        raise SandboxViolationError(f"No such file or directory: {resolved}")

    return resolved


def describe_roots(roots: Sequence[Path]) -> str:
    """Render sandbox roots for an error message or a system prompt.

    Args:
        roots: The roots to describe.

    Returns:
        A comma-separated list, or ``none`` when there are no roots.
    """
    return ", ".join(str(r) for r in roots) or "none"
