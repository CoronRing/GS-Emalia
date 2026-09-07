"""Policy and sandboxing.

`Policy` is the one object that says what Emalia may do; `resolve_in_sandbox`
is the one function that decides whether a path is reachable.
"""

from emalia.security.paths import (
    DEFAULT_DENY_GLOBS,
    describe_roots,
    is_within,
    resolve_in_sandbox,
)
from emalia.security.policy import (
    DANGEROUS_TOOLSETS,
    DEFAULT_TOOLSETS,
    Policy,
    SenderDecision,
    Toolset,
    normalise_patterns,
    truncate,
)

__all__ = [
    "DANGEROUS_TOOLSETS",
    "DEFAULT_DENY_GLOBS",
    "DEFAULT_TOOLSETS",
    "Policy",
    "SenderDecision",
    "Toolset",
    "describe_roots",
    "is_within",
    "normalise_patterns",
    "resolve_in_sandbox",
    "truncate",
]
