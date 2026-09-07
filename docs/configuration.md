# Configuration

Settings resolve in this order, each layer overriding the one before:

1. Built-in defaults
2. `emalia.toml` (or `.emalia.toml`) in the working directory, or a path passed
   to `--config`
3. `EMALIA_*` environment variables, including anything in a `.env` file
4. Arguments passed to `EmaliaConfig.load(...)`

**Secrets only ever come from layer 3.** The loader rejects an `[account]`
table that tries to set `password`, so `emalia.toml` is safe to commit.

`emalia init` writes a starter pair of files. `emalia check` prints the
resolved configuration with the password redacted.

---

## Mail account

Environment only.

| Variable | Meaning |
|---|---|
| `EMALIA_ADDRESS` | The mailbox address. Required. |
| `EMALIA_PASSWORD` | The password or app password. Required. |
| `EMALIA_PROVIDER` | A preset: `gmail`, `outlook`, `yahoo`, `icloud`, `fastmail`, `zoho`, `proton` |
| `EMALIA_IMAP_HOST` / `EMALIA_IMAP_PORT` | Explicit IMAP endpoint. Overrides the preset. |
| `EMALIA_SMTP_HOST` / `EMALIA_SMTP_PORT` | Explicit SMTP endpoint. Overrides the preset. |
| `EMALIA_SMTP_SECURITY` | `ssl` (implicit TLS, usually 465) or `starttls` (usually 587) |
| `EMALIA_USERNAME` | Login name, when it differs from the address |
| `EMALIA_DISPLAY_NAME` | The name recipients see in the `From` header |
| `EMALIA_TIMEOUT` | Socket timeout in seconds. Default 30. |

Either `EMALIA_PROVIDER` or `EMALIA_IMAP_HOST` is required. Explicit hosts win
over a preset, so a self-hosted server that otherwise looks like Gmail needs no
new preset.

### Provider notes

| Provider | IMAP | SMTP | Note |
|---|---|---|---|
| gmail | imap.gmail.com:993 | smtp.gmail.com:465 (SSL) | Needs an App Password with 2FA on. The account password will not work. |
| outlook | outlook.office365.com:993 | smtp-mail.outlook.com:587 (STARTTLS) | Personal accounts need an App Password. Many work tenants disable basic auth entirely. |
| yahoo | imap.mail.yahoo.com:993 | smtp.mail.yahoo.com:465 (SSL) | Needs an App Password. |
| icloud | imap.mail.me.com:993 | smtp.mail.me.com:587 (STARTTLS) | Needs an app-specific password. |
| fastmail | imap.fastmail.com:993 | smtp.fastmail.com:465 (SSL) | Create an app password scoped to mail. |
| zoho | imap.zoho.com:993 | smtp.zoho.com:465 (SSL) | |
| proton | 127.0.0.1:1143 | 127.0.0.1:1025 (STARTTLS) | Goes through the local Proton Mail Bridge, which must be running. |

---

## Top level

```toml
instance_name = "Emalia"      # the name the agent answers to
poll_interval = 30.0          # seconds between IMAP polls, minimum 1
batch_size = 5                # most messages handled per poll
max_concurrent = 1            # messages processed in parallel
state_dir = ".emalia"         # seen-set and audit log live here
attachment_dir = ""           # where inbound attachments are saved
footer = ""                   # appended to outgoing bodies; omit for a generated one
extra_instructions = ""       # appended to the system prompt
dry_run = false               # do everything except send
```

Environment equivalents: `EMALIA_INSTANCE_NAME`, `EMALIA_POLL_INTERVAL`,
`EMALIA_BATCH_SIZE`, `EMALIA_MAX_CONCURRENT`, `EMALIA_STATE_DIR`,
`EMALIA_ATTACHMENT_DIR`, `EMALIA_DRY_RUN`.

`extra_instructions` is the place to give the agent house rules — tone, sign-off,
what to escalate — without forking the package.

---

## `[llm]`

```toml
[llm]
provider = "anthropic"        # anthropic | openai | gemini | azure | ollama | huggingface | compatible
model = "claude-sonnet-4-6"
api_base = ""                 # required for `compatible`, optional for `ollama`
api_key_env = ""              # override which variable the key is read from
temperature = 0.7             # omit for the provider default
max_tokens = 4096             # omit for the provider default
```

Environment equivalents: `EMALIA_LLM_PROVIDER`, `EMALIA_LLM_MODEL`,
`EMALIA_LLM_API_BASE`, `EMALIA_LLM_API_KEY_ENV`, `EMALIA_LLM_TEMPERATURE`,
`EMALIA_LLM_MAX_TOKENS`.

The API key itself is read by railtracks from the provider's own variable:

| Provider | Key variable |
|---|---|
| anthropic | `ANTHROPIC_API_KEY` |
| openai | `OPENAI_API_KEY` |
| gemini | `GEMINI_API_KEY` |
| azure | `AZURE_API_KEY` |
| huggingface | `HF_TOKEN` |
| ollama | none |
| compatible | whatever `api_key_env` names |

Any OpenAI-shaped endpoint works through `compatible`:

```toml
[llm]
provider = "compatible"
model = "my-model"
api_base = "https://api.example.com/v1"
api_key_env = "MY_API_KEY"
```

---

## `[policy]`

Every field, with its default. See [SECURITY.md](../SECURITY.md) for what each
one is defending against.

```toml
[policy]
# Who is answered. Globs allowed. Empty means nobody, and an empty allowlist
# makes the listener refuse to start unless allow_any_sender is set.
allowed_senders = []
blocked_senders = []
allow_any_sender = false

# Who the agent may address mail to. Empty means "only whoever wrote in".
allowed_recipients = []

# A shared secret that must appear in the subject line.
require_token = ""

# email | file_read | file_write | http | shell | python
enabled_toolsets = ["email", "file_read"]

# Required in addition to listing shell or python above.
allow_dangerous_tools = false

# Directories the file tools may touch. Empty refuses every path.
sandbox_roots = []

max_attachment_bytes = 10485760        # 10 MiB per attachment
max_total_attachment_bytes = 20971520  # 20 MiB per message

max_replies_per_run = 50               # negative for no limit
max_replies_per_hour = 30              # negative for no limit
max_tool_calls = 25                    # per incoming message
command_timeout = 60.0                 # seconds for shell and python tools
max_output_chars = 20000               # any tool's output is truncated here
```

Environment equivalents (comma-separated where a list is expected):
`EMALIA_ALLOWED_SENDERS`, `EMALIA_BLOCKED_SENDERS`, `EMALIA_ALLOWED_RECIPIENTS`,
`EMALIA_TOOLSETS`, `EMALIA_ALLOW_ANY_SENDER`, `EMALIA_ALLOW_DANGEROUS_TOOLS`,
`EMALIA_TOKEN`, `EMALIA_SANDBOX_ROOTS` (separated by `:` on POSIX, `;` on
Windows), `EMALIA_MAX_REPLIES_PER_RUN`, `EMALIA_MAX_REPLIES_PER_HOUR`,
`EMALIA_MAX_TOOL_CALLS`.

### Combinations the loader refuses

These raise `ConfigurationError` rather than starting in a state you would
regret:

- `allowed_senders` empty and `allow_any_sender` off — nobody could ever be
  answered, so this is a misconfiguration, not a lockdown.
- `shell` or `python` in `enabled_toolsets` without `allow_dangerous_tools`.
- `shell` or `python` together with `allow_any_sender` — that is an
  unauthenticated remote shell.

### Warnings

`emalia check` prints these but does not stop:

- `allow_any_sender` is on.
- A dangerous toolset is live.
- File toolsets are enabled but `sandbox_roots` is empty, so every file
  operation will be refused.
- A sandbox root does not exist.
- `max_replies_per_hour` is unlimited.

---

## Example: a locked-down personal assistant

```toml
instance_name = "Jeeves"
poll_interval = 60.0
state_dir = "/var/lib/emalia"

[llm]
provider = "anthropic"
model = "claude-sonnet-4-6"

[policy]
allowed_senders = ["you@example.com"]
require_token = "JV-2026"
enabled_toolsets = ["email", "file_read"]
sandbox_roots = ["/srv/emalia-shared"]
max_replies_per_hour = 20
max_tool_calls = 15
```

## Example: a team helper with write access

```toml
instance_name = "Deskbot"

[llm]
provider = "openai"
model = "gpt-5"

[policy]
allowed_senders = ["*@mycompany.com"]
blocked_senders = ["noreply@*", "*@contractors.mycompany.com"]
allowed_recipients = ["*@mycompany.com"]
enabled_toolsets = ["email", "file_read", "file_write"]
sandbox_roots = ["/srv/shared/docs"]
max_replies_per_hour = 100
```
