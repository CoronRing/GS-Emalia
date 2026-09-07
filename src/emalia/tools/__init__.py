"""Tool groups, ready to hand to any railtracks agent.

```python
import railtracks as rt
from emalia.mail import MailClient
from emalia.security import Policy
from emalia.tools import build_tool_nodes

policy = Policy(allowed_senders=["me@example.com"], sandbox_roots=["~/notes"])
client = MailClient.from_env()

Agent = rt.agent_node(
    "Inbox Agent",
    tool_nodes=build_tool_nodes(policy, client=client),
    llm=rt.llm.AnthropicLLM("claude-sonnet-4-6"),
    system_message="You work this inbox.",
)
```

Each group is also usable on its own, and the callables it returns are plain
functions you can call in a test without a model anywhere near them.
"""

from emalia.tools.base import ToolGroup, tool_result
from emalia.tools.email_tools import EmailTools
from emalia.tools.exec_tools import PythonTools, ShellTools, default_shell
from emalia.tools.file_tools import FileReadTools, FileWriteTools
from emalia.tools.http_tools import HttpTools
from emalia.tools.registry import build_groups, build_tool_nodes, build_tools

__all__ = [
    "EmailTools",
    "FileReadTools",
    "FileWriteTools",
    "HttpTools",
    "PythonTools",
    "ShellTools",
    "ToolGroup",
    "build_groups",
    "build_tool_nodes",
    "build_tools",
    "default_shell",
    "tool_result",
]
