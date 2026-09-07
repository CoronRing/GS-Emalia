"""Assemble the tool groups a policy permits.

The registry is the choke point: a group the policy does not enable is never
constructed, so its tools never appear in the schema the model sees. Capability
is decided here, not argued about in the prompt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, TypeAlias, cast

from emalia.mail.client import MailClient
from emalia.security.policy import Policy
from emalia.tools.base import ToolGroup
from emalia.tools.email_tools import EmailTools
from emalia.tools.exec_tools import PythonTools, ShellTools
from emalia.tools.file_tools import FileReadTools, FileWriteTools
from emalia.tools.http_tools import HttpTools

if TYPE_CHECKING:
    from railtracks.built_nodes.function.base import RTFunction
    from railtracks.nodes.nodes import Node

    #: What `rt.agent_node(tool_nodes=...)` accepts. Imported only for typing
    #: so that `emalia.tools` can be imported without paying railtracks' import
    #: cost until a node is actually built.
    ToolNode: TypeAlias = "type[Node] | RTFunction"
else:
    ToolNode = object

__all__ = ["build_groups", "build_tools", "build_tool_nodes"]

logger = logging.getLogger(__name__)


def build_groups(
    policy: Policy,
    *,
    client: MailClient | None = None,
    dry_run: bool = False,
) -> list[ToolGroup]:
    """Construct the tool groups the policy enables.

    Args:
        policy: The policy deciding which groups exist.
        client: A mail client. Required for the ``email`` group; without one,
            that group is skipped even if the policy enables it.
        dry_run: Log outgoing mail rather than sending it.

    Returns:
        The constructed groups, in a stable order.
    """
    groups: list[ToolGroup] = []

    if policy.toolset_enabled("email"):
        if client is None:
            logger.warning("The 'email' toolset is enabled but no MailClient was given; skipping.")
        else:
            groups.append(EmailTools(client, policy, dry_run=dry_run))

    if policy.toolset_enabled("file_read"):
        groups.append(FileReadTools(policy))
    if policy.toolset_enabled("file_write"):
        groups.append(FileWriteTools(policy))
    if policy.toolset_enabled("http"):
        groups.append(HttpTools(policy))
    if policy.toolset_enabled("shell"):
        logger.warning("The shell toolset is live: an email can run commands on this machine.")
        groups.append(ShellTools(policy))
    if policy.toolset_enabled("python"):
        logger.warning("The python toolset is live: an email can run code on this machine.")
        groups.append(PythonTools(policy))

    return groups


def build_tools(
    policy: Policy,
    *,
    client: MailClient | None = None,
    dry_run: bool = False,
    extra: Sequence[Callable[..., object]] = (),
) -> list[Callable[..., object]]:
    """Collect every permitted tool as a plain callable.

    Args:
        policy: The policy deciding which groups exist.
        client: A mail client, needed for the email group.
        dry_run: Log outgoing mail rather than sending it.
        extra: Additional callables to append. These are not policy-gated;
            whoever passes them owns their safety.

    Returns:
        The callables, in group order, with `extra` last.

    Raises:
        ValueError: If two tools would share a name, which railtracks refuses
            because the model could not address them apart.
    """
    tools: list[Callable[..., object]] = []
    for group in build_groups(policy, client=client, dry_run=dry_run):
        tools.extend(group.tools())
    tools.extend(extra)

    seen: dict[str, int] = {}
    for tool in tools:
        name = getattr(tool, "__name__", repr(tool))
        seen[name] = seen.get(name, 0) + 1
    duplicates = sorted(name for name, count in seen.items() if count > 1)
    if duplicates:
        raise ValueError(
            f"Duplicate tool name(s): {duplicates}. Rename one, or pass it through "
            "railtracks.function_node(..., name=...)."
        )
    return tools


def build_tool_nodes(
    policy: Policy,
    *,
    client: MailClient | None = None,
    dry_run: bool = False,
    extra: Sequence[object] = (),
) -> list[ToolNode]:
    """Collect every permitted tool as a railtracks function node.

    Args:
        policy: The policy deciding which groups exist.
        client: A mail client, needed for the email group.
        dry_run: Log outgoing mail rather than sending it.
        extra: Additional nodes or callables to append. Plain callables are
            wrapped; anything already a node is passed through.

    Returns:
        Nodes suitable for `rt.agent_node(tool_nodes=...)`.
    """
    import railtracks as rt

    tools = build_tools(policy, client=client, dry_run=dry_run)
    nodes: list[ToolNode] = [rt.function_node(tool) for tool in tools]

    for item in extra:
        if callable(item) and not isinstance(item, type):
            nodes.append(rt.function_node(item))
        else:
            # Already a node type, or something railtracks will reject with a
            # better message than we could produce here.
            nodes.append(cast("ToolNode", item))
    return nodes
