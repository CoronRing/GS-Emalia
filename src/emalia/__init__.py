"""Emalia: an agent that lives in your email, and the toolkit behind it.

Two things ship here, and either is usable without the other.

**The toolkit.** `emalia.mail` is a typed IMAP/SMTP layer with no dependency on
agents or LLMs:

```python
from emalia.mail import MailClient

with MailClient.from_env() as mail:
    for message in mail.unread(limit=5):
        mail.reply(message, "Got it, thanks.")
```

**The agent.** `emalia.runtime.EmaliaListener` watches a mailbox and answers
what arrives, using whichever tools the policy permits:

```python
from emalia import EmaliaConfig, EmaliaListener

EmaliaListener(EmaliaConfig.load()).run()
```

Between the two, `emalia.tools` exposes every capability as a railtracks
function node, so any agent can pick up mail access:

```python
import railtracks as rt
from emalia.mail import MailClient
from emalia.security import Policy
from emalia.tools import build_tool_nodes

Agent = rt.agent_node(
    "My Agent",
    tool_nodes=build_tool_nodes(Policy(allowed_senders=["me@example.com"]),
                                client=MailClient.from_env()),
    llm=rt.llm.AnthropicLLM("claude-sonnet-4-6"),
)
```

Security note: an inbox is a public endpoint. `Policy` defaults to answering
nobody and reaching nothing; read `SECURITY.md` before widening it.
"""

from dotenv import find_dotenv, load_dotenv

from emalia.config import EmaliaConfig, LLMSettings
from emalia.errors import (
    ComposeError,
    ConfigurationError,
    EmaliaError,
    MailAuthError,
    MailConnectionError,
    MessageNotFoundError,
    PermissionDeniedError,
    PolicyError,
    RateLimitError,
    SandboxViolationError,
    ToolExecutionError,
)
from emalia.mail import (
    Attachment,
    EmailAddress,
    EmailMessage,
    EmailSummary,
    MailAccount,
    MailClient,
    SearchCriteria,
)
from emalia.security import Policy

__version__ = "0.1.0"

# Credentials live in the environment, and a .env file next to the working
# directory is the ordinary way to put them there. Loading it here rather than
# relying on railtracks doing it means the toolkit half of the package behaves
# the same without railtracks ever being imported.
#
# `usecwd=True` matters: the default search starts from this file, which for an
# installed package is somewhere in site-packages, so the user's own .env would
# never be found. Variables already in the environment win over the file.
load_dotenv(find_dotenv(usecwd=True), override=False)

__all__ = [
    "Attachment",
    "ComposeError",
    "ConfigurationError",
    "EmailAddress",
    "EmailMessage",
    "EmailSummary",
    "EmaliaConfig",
    "EmaliaError",
    "LLMSettings",
    "MailAccount",
    "MailAuthError",
    "MailClient",
    "MailConnectionError",
    "MessageNotFoundError",
    "PermissionDeniedError",
    "Policy",
    "PolicyError",
    "RateLimitError",
    "SandboxViolationError",
    "SearchCriteria",
    "ToolExecutionError",
    "__version__",
]


def __getattr__(name: str) -> object:
    """Expose the agent and runtime lazily.

    Importing them pulls in `railtracks` and a provider SDK, which is several
    seconds and a lot of memory. Someone who only wants `MailClient` should
    not pay for it.
    """
    if name in ("build_agent", "build_flow", "render_incoming", "render_system_prompt"):
        from emalia import agent

        return getattr(agent, name)
    if name in ("EmaliaListener", "ListenerStats"):
        from emalia import runtime

        return getattr(runtime, name)
    if name == "build_tool_nodes":
        from emalia.tools import build_tool_nodes

        return build_tool_nodes
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
