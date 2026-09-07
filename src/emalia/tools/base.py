"""Shared machinery for tool groups.

Three rules every Emalia tool follows, enforced here rather than repeated:

* A tool returns a string. Models read strings, and an email body is a string.
* A tool never lets an exception escape as a traceback. A failure comes back as
  a sentence the model can act on and, if it must, quote to the user. A
  traceback in an email body leaks paths, package versions, and sometimes
  credentials.
* A tool counts against the policy's per-request tool budget, so a model stuck
  in a retry loop costs one email's worth of calls rather than unbounded ones.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from emalia.errors import EmaliaError
from emalia.security.policy import Policy, truncate

__all__ = ["ToolGroup", "tool_result"]

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")


def tool_result(policy: Policy) -> Callable[[Callable[_P, str]], Callable[_P, str]]:
    """Wrap a tool so it counts against the budget and never throws.

    Args:
        policy: The policy holding the per-request tool budget and the output
            ceiling.

    Returns:
        A decorator that preserves the wrapped function's name, signature and
        docstring, all three of which railtracks reads to build the tool
        schema the model sees.
    """

    def decorate(func: Callable[_P, str]) -> Callable[_P, str]:
        @functools.wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> str:
            try:
                policy.charge_tool_call()
                return truncate(func(*args, **kwargs), policy.max_output_chars)
            except EmaliaError as exc:
                # Expected refusals and failures: the message is written for a
                # reader, so pass it through as-is.
                logger.info("%s refused or failed: %s", func.__name__, exc)
                return f"{type(exc).__name__}: {exc}"
            except Exception as exc:
                # Anything else is a bug. Log the detail locally, tell the
                # model only the shape of the problem.
                logger.exception("%s raised an unexpected error", func.__name__)
                return (
                    f"{func.__name__} failed with an unexpected {type(exc).__name__}. "
                    "The details were written to the local log."
                )

        return wrapper

    return decorate


class ToolGroup:
    """Base for a set of related tools bound to a policy.

    Subclasses implement `tools()` and are constructed by
    `emalia.tools.registry.build_groups`.

    Attributes:
        policy: The policy every tool in the group consults.
        toolset: The policy toolset name gating this group.
    """

    toolset: str = ""

    def __init__(self, policy: Policy) -> None:
        """
        Args:
            policy: The policy to enforce.
        """
        self.policy = policy

    def tools(self) -> list[Callable[..., str]]:
        """The plain callables this group exposes.

        Returns:
            Functions with full type hints and Google-style docstrings, ready
            for `railtracks.function_node`.
        """
        raise NotImplementedError

    def as_nodes(self) -> list[object]:
        """The group's tools wrapped as railtracks function nodes.

        Returns:
            Nodes suitable for `rt.agent_node(tool_nodes=...)`.

        Raises:
            ImportError: If `railtracks` is not installed. The rest of the
                toolkit works without it.
        """
        import railtracks as rt

        return [rt.function_node(tool) for tool in self.tools()]
