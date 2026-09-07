"""Run the full agent with tools of your own.

This replaces the original's "custom tasks" feature: instead of registering a
command string over email, you write a Python function and pass it in.

    python examples/03_custom_tools.py
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from emalia import EmaliaConfig
from emalia.mail import MailAccount
from emalia.runtime import EmaliaListener
from emalia.security import Policy


def whats_on(day: str) -> str:
    """Look up what is scheduled on a given day.

    The docstring and the type hints are what the model sees, so they are the
    real interface. Vague ones produce a tool the model misuses.

    Args:
        day: The day to look up, as YYYY-MM-DD. Use "today" for today.

    Returns:
        A description of what is scheduled, or a note that the day is free.
    """
    if day.strip().lower() == "today":
        day = dt.date.today().isoformat()
    # Stand-in for a real calendar lookup.
    schedule = {"2026-09-07": "Standup at 09:30, review at 14:00."}
    return schedule.get(day, f"Nothing scheduled on {day}.")


def deploy_status(service: str) -> str:
    """Report the current deploy status of a service.

    Args:
        service: The service name, e.g. "api" or "web".

    Returns:
        The status line for that service.
    """
    return f"{service}: healthy, last deployed 4 hours ago."


def main() -> None:
    """Start a listener whose agent also has the two tools above."""
    config = EmaliaConfig(
        account=MailAccount.from_env(),
        policy=Policy(
            allowed_senders=["you@example.com"],
            sandbox_roots=[Path.home() / "shared"],
            enabled_toolsets=["email", "file_read"],
            max_replies_per_hour=20,
        ),
        instance_name="Jeeves",
        extra_instructions=(
            "You work for one person. Keep replies to a few sentences unless they ask for detail."
        ),
    )

    EmaliaListener(config, extra_tools=[whats_on, deploy_status]).run()


if __name__ == "__main__":
    main()
