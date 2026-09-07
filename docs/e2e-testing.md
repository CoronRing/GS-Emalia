# End-to-end testing

The 190 tests in `tests/` run offline against fakes. They are fast, they run in
CI on three operating systems, and they will happily agree with any wrong
assumption you hold about IMAP.

The 38 tests in `tests/e2e/` run against a real mail server. They are the ones
that catch a provider quoting `SEARCH` arguments differently, a `STORE` verb
that silently does nothing, or a reply that threads correctly in your test and
starts a new conversation in Gmail.

They are skipped unless you opt in, and they take a few minutes because real
mail delivery is not instant.

## Contents

- [What is covered](#what-is-covered)
- [Setup](#setup)
- [Running](#running)
- [Safety](#safety)
- [Troubleshooting](#troubleshooting)

## What is covered

| File | Needs | What it proves |
|---|---|---|
| `test_mail_e2e.py` | one mailbox | Send/receive round trips, Unicode subjects, threading headers, quote stripping, attachments byte-for-byte, zipped directories, flags, search, delete |
| `test_tools_e2e.py` | one mailbox | The agent's tools against a live server, and that the policy still refuses what it should |
| `test_agent_e2e.py` | one mailbox + API key | A real model driving real tools, including four prompt-injection attempts |
| `test_listener_e2e.py` | two mailboxes + API key | The whole thing: mail arrives, is gated, is worked, and is answered in thread |

The injection tests are the ones worth reading. They do not assert that the
model refuses — they assert that the capability was never there, which is the
only claim that survives a better-worded attack.

## Setup

### 1. A mailbox for the bot

Any IMAP/SMTP account. For Gmail:

1. Turn on **2-Step Verification** on the account.
2. Create an **App Password** at <https://myaccount.google.com/apppasswords>.
   Google has rejected ordinary account passwords over IMAP since 2022.
3. IMAP is on by default for personal Gmail. For a Workspace account an admin
   may need to enable it.

OAuth works here too, on the same terms as everywhere else — see
[authentication.md](authentication.md). Prefer an app password for this suite
if you can: the weekly scheduled run is exactly the cadence at which an
unverified app's seven-day refresh token expiry causes a mystifying red build.

Use a mailbox you do not mind the suite writing to. It sends itself a dozen or
so messages per run and deletes them afterwards.

### 2. A second mailbox for the peer, optional

`test_listener_e2e.py` needs a sender that is not the bot itself, because the
listener drops mail from its own address — a loop guard worth keeping rather
than working around. Any second mailbox works, including one at a different
provider; it is only ever used to send, though the reply assertions get
stronger if it can also read.

Without it, that one file skips and the other three still run.

### 3. Environment

Put these in your shell or in a `.env` file. Note the `EMALIA_E2E_` prefix:
the suite deliberately cannot read your ordinary `EMALIA_*` variables, so a
configured production instance in the same environment is never at risk.

```bash
# Required
EMALIA_E2E=1
EMALIA_E2E_ADDRESS=emalia-test@gmail.com
EMALIA_E2E_PASSWORD=your-app-password
EMALIA_E2E_PROVIDER=gmail
# Or, instead of EMALIA_E2E_PASSWORD:
# EMALIA_E2E_OAUTH_CLIENT_ID=
# EMALIA_E2E_OAUTH_CLIENT_SECRET=
# EMALIA_E2E_OAUTH_REFRESH_TOKEN=

# For the full round trip
EMALIA_E2E_PEER_ADDRESS=you@example.com
EMALIA_E2E_PEER_PASSWORD=another-app-password
EMALIA_E2E_PEER_PROVIDER=gmail

# For anything with the llm marker
ANTHROPIC_API_KEY=sk-ant-...
EMALIA_E2E_LLM_PROVIDER=anthropic
EMALIA_E2E_LLM_MODEL=claude-sonnet-4-6

# Optional: seconds to wait for delivery, default 180
EMALIA_E2E_TIMEOUT=180
```

## Running

```bash
# Everything, including the model calls
pytest tests/e2e

# The mail layer only: no API key needed, no cost
pytest tests/e2e -m "e2e and not llm"

# One file, verbose, with output as it happens
pytest tests/e2e/test_mail_e2e.py -v -s

# Leave the test messages in the mailbox so you can look at them
pytest tests/e2e --e2e-keep
```

The offline suite is unaffected either way:

```bash
pytest -m "not e2e"     # what CI runs
```

Expect roughly two to four minutes for the full run. Most of it is waiting for
delivery, not computation.

## Safety

Running a suite against a mailbox that may hold real mail deserves more care
than a normal test run. Four things constrain it:

1. **Separate credentials.** Only `EMALIA_E2E_*` is read. A production
   `EMALIA_ADDRESS` in the same environment is invisible to the suite.
2. **A per-run tag.** Every subject the suite sends carries
   `[emalia-e2e-<random>]`. Cleanup matches on that tag, re-reads each
   candidate's subject before deleting it, and touches nothing else. A crashed
   run leaves its mail behind rather than risking a broad delete on the next
   one.
3. **A required token.** The listener tests set `Policy.require_token` to the
   run tag, so the agent will not answer a message that the suite did not
   send — including genuine unread mail sitting in the inbox.
4. **A temporary sandbox.** `sandbox_roots` and `state_dir` are pytest temp
   directories. Nothing on the real filesystem is reachable, however the model
   is prompted.

The suite never enables the `shell` or `python` toolsets.

## Troubleshooting

**`[AUTHENTICATIONFAILED] Invalid credentials`** — an app password is needed,
not the account password. Copy it without the spaces Google displays.

**`535 5.7.8 Username and Password not accepted`** — the same thing on the SMTP
side. Confirm 2-Step Verification is on; app passwords cannot be created
without it.

**`No message with '[emalia-e2e-...]' in INBOX after 180s`** — delivery is
slow, or a filter moved the mail. Check that the account has no rule
redirecting self-addressed mail out of the inbox, and raise
`EMALIA_E2E_TIMEOUT`.

**Everything skips** — `EMALIA_E2E` is unset or `0`. The skip reason on any
test says which variable is missing.

**Only `test_listener_e2e.py` skips** — the peer credentials are not set. That
is expected; see step 2.

**Test mail is piling up** — a run crashed before cleanup, or you used
`--e2e-keep`. Search the mailbox for `emalia-e2e` and delete the results.

## Adding a test here

Put it in `tests/e2e/` only if a fake genuinely cannot answer the question. The
bar is "a real server might disagree with us" — protocol behaviour, provider
quirks, MIME on the wire, or the model's behaviour under a live policy.
Everything else belongs in the offline suite, where it runs in milliseconds and
costs nothing.

If it sends mail, take the `subject` fixture and use it for every subject line.
That is what makes cleanup safe.
