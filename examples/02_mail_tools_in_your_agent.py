"""Give an existing railtracks agent mail access.

Nothing here uses Emalia's listener. The point is that the tools are ordinary
railtracks nodes, gated by the same policy whether Emalia calls them or you do.

    python examples/02_mail_tools_in_your_agent.py
"""

from __future__ import annotations

import railtracks as rt

from emalia.mail import MailClient
from emalia.security import Policy
from emalia.tools import build_tool_nodes


def main() -> None:
    """Build an inbox-triage agent and ask it one question."""
    # The policy is enforced inside each tool, so it applies here exactly as it
    # would inside Emalia's own runtime.
    policy = Policy(
        allowed_senders=["*"],  # not used for reading; see the note below
        allowed_recipients=[],  # nothing may be mailed to a third party
        enabled_toolsets=["email"],
        max_tool_calls=10,
    )

    with MailClient.from_env() as mail:
        Triage = rt.agent_node(
            "Inbox Triage",
            tool_nodes=build_tool_nodes(policy, client=mail),
            llm=rt.llm.AnthropicLLM("claude-sonnet-4-6"),
            system_message=(
                "You triage an inbox. Summarise what is waiting and say which "
                "messages look like they need a human. Do not send anything."
            ),
        )

        flow = rt.Flow(name="Triage Flow", entry_point=Triage)
        result = flow.invoke("What is in my inbox right now, and what needs me?")
        print(result.text)


# Note on `allowed_senders` here: it gates who the *listener* answers, so it is
# irrelevant to an agent you drive yourself. `allowed_recipients` is the one
# that matters in this shape, because it is what stops the agent mailing
# something it read to somewhere you did not intend.

if __name__ == "__main__":
    main()
