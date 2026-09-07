"""Use the mail toolkit on its own: no agent, no model, no API key.

Run with the mailbox credentials in your environment or a .env file:

    python examples/01_toolkit_only.py
"""

from __future__ import annotations

from emalia.mail import MailClient, SearchCriteria


def main() -> None:
    """Read the inbox, search it, and reply to one message."""
    with MailClient.from_env() as mail:
        print("Folders:", ", ".join(mail.list_folders()))

        print("\nFive most recent messages:")
        for summary in mail.summaries(limit=5):
            print(" ", summary.to_line())

        unread = mail.unread(limit=3)
        print(f"\n{len(unread)} unread")
        for message in unread:
            print(f"\n--- {message.subject} from {message.sender}")
            # `body` is what the sender typed this time: the quoted thread
            # history has already been stripped.
            print(message.body[:300])
            for attachment in message.attachments:
                print(f"    attachment: {attachment.filename} ({attachment.size} bytes)")

        # Anything matching an IMAP SEARCH expression, without writing one.
        recent_from_alice = mail.search(
            SearchCriteria().from_("alice@").since("01-Jan-2026"),
            limit=5,
        )
        print(f"\nMatching UIDs from alice: {recent_from_alice}")

        if unread:
            # Threads correctly in the recipient's client: In-Reply-To and
            # References are set for you.
            mail.reply(unread[0], "Thanks, I have this. Replying from a script.")
            print(f"\nReplied to {unread[0].sender}")


if __name__ == "__main__":
    main()
