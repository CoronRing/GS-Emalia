# Quick start

From `pip install` to a mailbox that answers itself, in about ten minutes.

If you only want the Python email library and not the agent, skip to
[Just the toolkit](#just-the-toolkit).

## Contents

- [1. Install](#1-install)
- [2. A mailbox](#2-a-mailbox)
- [3. A model](#3-a-model)
- [4. Configure](#4-configure)
- [5. Check](#5-check)
- [6. Run](#6-run)
- [Where to go next](#where-to-go-next)
- [Just the toolkit](#just-the-toolkit)

## 1. Install

```bash
pip install emalia
```

Python 3.11 or newer. With [uv](https://github.com/astral-sh/uv), `uv add emalia`.

## 2. A mailbox

Use a **dedicated address**, not your personal one. Emalia reads unread mail in
the inbox and marks what it answers as read; pointing it at the mailbox you
live in is a bad first experience.

### Gmail

1. Turn on 2-Step Verification.
2. Create an App Password at <https://myaccount.google.com/apppasswords>.
3. Use that as the password. Google has rejected account passwords over IMAP
   since 2022.

### Outlook, Microsoft 365

Enable IMAP under Mail → Sync email, and create an app password if your account
has MFA. Some tenants disable basic authentication entirely, in which case
Emalia cannot connect and no configuration will change that.

### Anything else

`gmail`, `outlook`, `yahoo`, `icloud`, `fastmail`, `zoho` and `proton` (via
Bridge) have presets. For anything else, set the hosts explicitly — see
[configuration.md](configuration.md).

## 3. A model

An API key for one provider. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or
`GEMINI_API_KEY`. A local [Ollama](https://ollama.com) server needs no key —
set `provider = "ollama"` and a model name.

## 4. Configure

```bash
emalia init
```

That writes two files.

**`.env`** — secrets. Add it to `.gitignore` immediately.

```bash
EMALIA_ADDRESS=assistant@gmail.com
EMALIA_PASSWORD=abcd efgh ijkl mnop      # the app password
EMALIA_PROVIDER=gmail
ANTHROPIC_API_KEY=sk-ant-...
```

**`emalia.toml`** — everything else. Safe to commit; the loader refuses to read
a password from it.

```toml
instance_name = "Emalia"
poll_interval = 30.0

[llm]
provider = "anthropic"
model = "claude-sonnet-4-6"

[policy]
allowed_senders = ["you@example.com"]
sandbox_roots = []
enabled_toolsets = ["email", "file_read"]
```

**Put your own address in `allowed_senders`.** It starts empty, and an empty
allowlist means nobody — Emalia will refuse to start rather than answer the
world. This is the one edit you cannot skip.

If you want it to read files, add a directory to `sandbox_roots`. Nothing
outside those roots is reachable, so make it a folder you are comfortable
having summarised into an email.

## 5. Check

```bash
emalia check
```

```
Instance: Emalia
  address: assistant@gmail.com
  password: ***

Mail servers
  imap: ok
  smtp: ok

Model
  provider: anthropic
  model: claude-sonnet-4-6
  credentials: ok

Policy
  valid
  allowed_senders: ['you@example.com']
  enabled_toolsets: ['email', 'file_read']

Everything checks out.
```

Anything red is a setup problem, and the message says which. The usual ones:

| Message | Fix |
|---|---|
| `[AUTHENTICATIONFAILED] Invalid credentials` | Use an app password, not the account password. If you cannot create one, see [authentication.md](authentication.md) |
| `535 5.7.8 Username and Password not accepted` | Same, on the SMTP side |
| `invalid_grant` on an OAuth setup | The refresh token expired or was revoked; [authentication.md](authentication.md) explains which |
| `credentials: ANTHROPIC_API_KEY is not set` | Add it to `.env` |
| `invalid: allowed_senders is empty` | Add your address, as above |

Two commands prove each half independently without involving the model:

```bash
emalia inbox           # reads
emalia send you@example.com -s "hello" -b "from emalia"    # writes
```

## 6. Run

Try it without sending anything first:

```bash
emalia once --dry-run --verbose
```

That handles one batch and logs the replies it would have sent. When you are
happy:

```bash
emalia run
```

Now email the address in plain English. A first message that exercises a tool:

> **Subject:** what's in my shared folder
>
> Can you list what's in the shared folder and tell me which file changed most
> recently?

Answers arrive in thread, so the conversation reads normally in your mail
client.

To run it on a schedule instead of as a daemon, `emalia once` handles one batch
and exits non-zero if anything failed — which is what cron and systemd timers
want.

## Where to go next

- Widen carefully. Read [SECURITY.md](../SECURITY.md) before adding
  `file_write`, `http`, `shell` or `python`, or before touching
  `allow_any_sender`.
- Give it tools of your own — a function with type hints and a docstring is all
  it takes. See [examples/03_custom_tools.py](../examples/03_custom_tools.py).
- Tune the prompt with `extra_instructions` in `emalia.toml`, without forking
  anything.
- `emalia audit` shows what it has been doing.

## Just the toolkit

No model, no API key, no agent:

```python
from emalia.mail import MailClient, SearchCriteria

with MailClient.from_env() as mail:
    for message in mail.unread(limit=5):
        print(message.sender.address, message.subject)
        print(message.body)              # quoted history already stripped
        mail.reply(message, "Got it.")   # threads correctly

    uids = mail.search(SearchCriteria().from_("billing@").since("01-Jun-2026"))
    mail.save_attachments(mail.fetch(uids[0]), "~/invoices")
```

`MailClient.from_env()` reads the same `EMALIA_ADDRESS` / `EMALIA_PASSWORD` /
`EMALIA_PROVIDER` variables, or construct a `MailAccount` yourself. Full
reference in [toolkit.md](toolkit.md).
