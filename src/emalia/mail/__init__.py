"""The Emalia email toolkit.

A typed IMAP/SMTP layer with no dependency on agents, LLMs, or `railtracks`.
Import it on its own if all you want is convenient mail access:

```python
from emalia.mail import MailClient, SearchCriteria

with MailClient.from_env() as mail:
    uids = mail.search(SearchCriteria().unseen().from_("alice@example.com"))
    for message in mail.imap.fetch_many(uids):
        mail.reply(message, "Got it.")
```
"""

from emalia.mail.accounts import PROVIDER_PRESETS, MailAccount, ProviderPreset
from emalia.mail.client import MailClient
from emalia.mail.compose import (
    build_forward,
    build_message,
    build_reply,
    load_attachment,
    save_attachments,
)
from emalia.mail.folders import decode_folder, encode_folder
from emalia.mail.imap import ImapSession, SearchCriteria
from emalia.mail.models import (
    Attachment,
    EmailAddress,
    EmailMessage,
    EmailSummary,
    parse_address_list,
    summaries_to_text,
)
from emalia.mail.oauth import (
    GMAIL_SCOPE,
    OUTLOOK_SCOPE,
    AuthMethod,
    OAuthCredentials,
    ServiceAccountCredentials,
    TokenCredentials,
    default_token_path,
    xoauth2_string,
)
from emalia.mail.parse import (
    decode_header_value,
    html_to_text,
    parse_message,
    strip_quoted_reply,
)
from emalia.mail.smtp import SmtpSender

__all__ = [
    "GMAIL_SCOPE",
    "OUTLOOK_SCOPE",
    "PROVIDER_PRESETS",
    "Attachment",
    "AuthMethod",
    "EmailAddress",
    "EmailMessage",
    "EmailSummary",
    "ImapSession",
    "MailAccount",
    "MailClient",
    "OAuthCredentials",
    "ProviderPreset",
    "SearchCriteria",
    "ServiceAccountCredentials",
    "SmtpSender",
    "TokenCredentials",
    "build_forward",
    "build_message",
    "build_reply",
    "decode_folder",
    "decode_header_value",
    "default_token_path",
    "encode_folder",
    "html_to_text",
    "load_attachment",
    "parse_address_list",
    "parse_message",
    "save_attachments",
    "strip_quoted_reply",
    "summaries_to_text",
    "xoauth2_string",
]
