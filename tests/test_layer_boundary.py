"""The claim that `emalia.mail` carries no agent dependencies, enforced.

README and `docs/design.md` both state that importing `emalia.mail` does not
load railtracks or a provider SDK. That is the property which makes the toolkit
worth installing on its own, and it is the kind of claim that decays quietly: a
convenience import added inside the mail layer would break it without failing
anything else.

A subprocess is used because the offline suite has already imported most of the
package by the time any test runs, so `sys.modules` in-process proves nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

#: Importing any of these from `emalia.mail` would defeat the point of the
#: layer. `requests` is included because it is a hard dependency of the package
#: but deliberately not of the mail layer, which uses `urllib`.
FORBIDDEN = (
    "railtracks",
    "anthropic",
    "openai",
    "google.genai",
    "requests",
    "typer",
    "cryptography",
)

_PROBE = """
import json, sys
import emalia.mail  # noqa: F401
json.dump(sorted(sys.modules), sys.stdout)
"""


@pytest.fixture(scope="module")
def imported_modules() -> list[str]:
    """Every module loaded by a bare `import emalia.mail`, in a fresh process."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stderr}"
    modules: list[str] = json.loads(result.stdout)
    return modules


@pytest.mark.parametrize("forbidden", FORBIDDEN)
def test_mail_layer_does_not_import(forbidden: str, imported_modules: list[str]) -> None:
    root = forbidden.split(".")[0]
    offenders = [name for name in imported_modules if name == root or name.startswith(f"{root}.")]
    assert not offenders, (
        f"`import emalia.mail` pulled in {forbidden}. The mail layer is meant to be "
        f"usable without the agent stack; loaded: {offenders[:5]}"
    )


def test_the_mail_layer_actually_imported(imported_modules: list[str]) -> None:
    """Guards the test above from passing because nothing was imported at all."""
    assert "emalia.mail" in imported_modules
    assert "emalia.mail.folders" in imported_modules
    assert "emalia.mail.oauth" in imported_modules


def test_cryptography_is_only_imported_when_something_signs() -> None:
    """The `gcp` extra is optional, so the import must stay inside the signer."""
    probe = """
import sys
import emalia.mail.oauth  # noqa: F401
print("cryptography" in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
