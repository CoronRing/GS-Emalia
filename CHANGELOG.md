# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-09-07

Token authentication for the mailbox, alongside the app passwords that already
worked. Providers are withdrawing password authentication — Microsoft has turned
basic auth off for most tenants, and a Google Workspace admin can disable app
passwords for a whole domain — so an app password is no longer something Emalia
can assume is available.

There are now four ways to authenticate. The one that matters for a deployed
system is the service account: no expiry, no browser at any point, and rotation
by `gcloud` rather than by a person.

Alongside that, Microsoft mailboxes get the same consent flow Google has, and
mailbox names outside ASCII now work at all.

### Added

- **Modified UTF-7 mailbox names** (RFC 3501 §5.1.3), in `emalia.mail.folders`.
  A mailbox named `Wysłane`, `已发送` or `Gelöscht` could not previously be
  selected, and appeared mangled in `list_folders()`. `select()` and `move()`
  now encode the name and `list_folders()` decodes it, so non-English mailboxes
  work on every provider. Python's built-in `utf-7` codec is the unmodified
  RFC 2152 one and does not produce this encoding.
- **`emalia auth microsoft login`**, the same PKCE consent flow for Outlook.com
  and Microsoft 365, with `status` and `logout` beside it. `--tenant` accepts a
  directory id for a single-tenant app registration. Worth having because
  Microsoft is withdrawing basic auth in December 2026, which makes an app
  password a dead end on those accounts specifically.
- `OAuthProvider` gathers the endpoints, scope and revocation URL that differ
  between identity providers, so the consent flow itself stays plain RFC 6749.
  `default_token_path()` now takes a provider, and each writes its own file
  rather than overwriting the other.

- **Service account authentication** with domain-wide delegation, in
  `ServiceAccountCredentials`. Signs an RS256 assertion to mint a token for a
  mailbox in a Workspace domain. The credential never expires and is rotated by
  `gcloud iam service-accounts keys create`, so an unattended deployment never
  needs a human at a browser. Configured with `EMALIA_SERVICE_ACCOUNT_FILE`, or
  `EMALIA_SERVICE_ACCOUNT_KEY` for a secret store with no filesystem.
- **`emalia auth google service-account`**, which prints the numeric client ID
  and scope the Admin console asks for — the value buried in the key file that
  most failed setups get wrong — and with `--check` mints a token to prove the
  delegation is live.
- **Provider-standard environment names.** `GOOGLE_APP_PASSWORD`,
  `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
  `GOOGLE_OAUTH_REFRESH_TOKEN` and `GOOGLE_SERVICE_ACCOUNT_FILE` are accepted
  as aliases for the `EMALIA_` names, for the default prefix only.
  `GOOGLE_APPLICATION_CREDENTIALS` is honoured when `EMALIA_AUTH` asks for it.
- **`EMALIA_AUTH`** names the method outright: `password`, `oauth` or
  `service_account`. Otherwise the method is inferred.
- An optional `gcp` extra (`pip install "emalia[gcp]"`) carrying
  `cryptography`, which is the one thing in the mail layer the standard library
  cannot do. The other credentials never sign anything and pull in nothing.
- **XOAUTH2 authentication** for IMAP and SMTP. `emalia.mail.oauth` holds
  `OAuthCredentials`: refresh-token exchange, in-memory access-token caching
  with an early-refresh margin, the SASL string, and a token file in the
  `authorized_user` shape `gcloud` and `google-auth` also use. Standard library
  only, so `emalia.mail` still needs nothing beyond it.
- **`emalia auth google login`**, an OAuth consent flow with PKCE and a
  loopback redirect. `--print-env` prints the credential as environment
  variables for CI; `--no-browser` prints the URL for a headless host. Client
  files from the Cloud Console or `gws auth setup` are found automatically.
- **`emalia auth google status`**, which shows the stored grant and, with
  `--refresh`, proves it still exchanges. **`emalia auth google logout`**
  deletes the local file.
- **Several ways to supply each credential**: an environment triple for CI, a
  token or key file path, inline JSON, or `EMALIA_AUTH=oauth` for the default
  location. The e2e suite accepts all of them under its own `EMALIA_E2E_`
  prefix.
- `emalia check` now verifies the token can be obtained before touching the
  mail servers, so an expired grant, an unauthorised delegation and a disabled
  mailbox no longer all look alike.
- [docs/authentication.md](docs/authentication.md), covering all four methods
  with a table of which expire and which need a browser, how to create an OAuth
  client and a service account, how to rotate a key, and the seven-day
  refresh-token expiry that applies to any unverified app with an External
  audience.

### Changed

- `MailAccount.password` is now optional, and `MailAccount.auth` reports
  `"password"`, `"oauth"` or `"service_account"`. Supplying two credentials is
  refused rather than resolved by precedence. `MailAccount.oauth` accepts any
  `TokenCredentials`, so the mail sessions never learn which kind they hold.
- Authentication failures name the mechanism that failed and what fixes it. A
  rejected refresh token explains the four causes of `invalid_grant`; a service
  account rejected with `unauthorized_client` names the Admin console page that
  grants delegation, rather than passing the bare error through.
- The offline suite now clears every credential variable before each test. It
  previously depended on the developer's environment being empty, which stopped
  being true once the shared aliases were accepted.
- `MailAccount.redacted()` reports the auth method, and for OAuth shows a
  fragment of the client ID — enough to tell two clients apart — with
  everything else masked.
- `list_folders()` reads mailbox names sent as IMAP literals, which is the form
  servers use for exactly the non-ASCII names it previously dropped, and keeps
  a name that will not decode rather than losing it from the listing.

### Compatibility

Nothing is removed. `MailAccount(address=..., password=..., imap_host=...)`
and every existing `EMALIA_*` variable behave exactly as before. The one
behaviour change is the error raised when no credential is set, which now names
both options.

`default_token_path()` gained a leading `provider` argument that defaults to
`"google"`, so existing calls and the existing `google_oauth.json` file are
unaffected. `list_folders()` now returns decoded names: code comparing against
a raw `&AUI-` form would need updating, though such code was already broken.

## [0.1.0] — 2026-09-06

A complete rewrite. The 2023 codebase was a keyword-driven email controller
(`READ/1 <path>`, `SHELL/4 <command>`) that never reached a working state. This
release rebuilds it on [railtracks](https://github.com/RailtownAI/railtracks)
around natural language, and ships the email layer as a toolkit in its own
right.

Nothing from the previous code is importable. There was no release on PyPI, so
there is nothing to migrate.

### Added

**The mail toolkit — `emalia.mail`**

- `MailClient`, a facade over IMAP and SMTP with lazily opened, reused
  connections.
- `MailAccount` with presets for gmail, outlook, yahoo, icloud, fastmail, zoho
  and proton, plus explicit-host configuration for anything else.
- `EmailMessage`, `EmailAddress`, `Attachment`, `EmailSummary` — plain
  dataclasses, so `email.message.Message` never reaches a caller.
- `SearchCriteria`, a builder for IMAP `SEARCH` expressions with quoted values.
- `strip_quoted_reply`, `html_to_text`, `decode_header_value`, `parse_message`.
- Threaded replies with `In-Reply-To` and `References`.
- Directory attachments, zipped with correct relative paths.
- `save_attachments` with filename sanitisation.
- `delete` and `expunge`, deliberately absent from the `email` toolset: the
  agent can read, send, flag and move mail, but destroying it stays with the
  person running the process.

**Policy and sandboxing — `emalia.security`**

- `Policy`: sender allow/blocklists, recipient gating, toolset gating,
  filesystem roots, attachment ceilings, send and tool-call rate limits, and a
  `validate()` that refuses configurations nobody should reach.
- `resolve_in_sandbox`, resolved-path containment with a credential-file
  denylist.

**Tools — `emalia.tools`**

- Six toolsets: `email`, `file_read`, `file_write`, `http`, `shell`, `python`.
- `build_tool_nodes`, which constructs only what the policy permits.
- Every tool is a plain callable with type hints and a Google-style docstring,
  usable from any railtracks agent or called directly in a test.

**The agent and runtime**

- `build_agent` / `build_flow`, thin wrappers over `rt.agent_node`.
- `EmaliaListener`: polling, sender gating, `Message-ID` deduplication,
  in-thread replies, a JSONL audit log, and graceful shutdown.
- `extra_tools`, replacing the original's "custom tasks" with ordinary Python
  functions.

**CLI**

- `emalia init`, `check`, `run`, `once`, `send`, `inbox`, `audit`, `version`.

**Docs and tests**

- `docs/quickstart.md`, `docs/design.md`, `docs/toolkit.md`,
  `docs/configuration.md`, `docs/e2e-testing.md`, `SECURITY.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`.
- Three runnable examples.
- 190 offline tests, none of which touch a network.
- 38 end-to-end tests against a real mailbox, opt-in behind `EMALIA_E2E=1` and
  a separate set of credentials. Four of them are prompt-injection attempts
  against a live model, asserting not that it refuses but that the capability
  was never registered.
- Assets under `docs/assets/`: a logo, a README banner, and an animated
  terminal walkthrough generated by `scripts/render_demo.py`.

### Security

The original had no meaningful security model. This release treats the inbox as
a hostile, unauthenticated endpoint:

- Sender allowlist is mandatory; an empty one refuses to start rather than
  answering everyone.
- Mail failing the gate is never answered, so the mailbox cannot be used to
  send bounces to spoofed addresses.
- Capability is gated by tool registration rather than by the prompt.
- `send_email` and `forward_email` default to the original sender only.
- File access is confined to `sandbox_roots`, with credential-shaped names
  refused even inside a root.
- `run_python` runs in a subprocess. The original ran `exec()` inside the
  daemon, where the code could read the mail password out of the config object.
- `run_shell` and `run_python` need a second explicit switch and cannot be
  combined with an open sender policy.
- Errors reply with a fixed notice; tracebacks stay in the local log.
- Loop guards: self-address drops, autoresponder and list-header detection,
  `Auto-Submitted` on outgoing replies, persisted deduplication, hourly send
  caps.

### Fixed

Defects in the 2023 code, listed because several are the reason a control
exists now:

- `mark_emails` mapped `"remove"` onto `+FLAGS`, so unflagging flagged instead.
- `fetch_email` used sequence numbers, which shift when mail arrives mid-batch,
  so the wrong message could be fetched.
- `check_path_in_range` measured path distance rather than containment, so it
  could not stop `../..` traversal, and an off-by-one made `layer=1` reject the
  directory it was pointed at.
- Directory attachments zipped with `os.path.relpath(file_path, dirs)`, where
  `dirs` was the walk's subdirectory list, producing unusable archive paths.
- `split_by_reply` was a `pass` stub, so every reply re-sent the whole thread.
- Bodies were decoded as `utf-8-sig` unconditionally; any other charset raised
  and stalled the loop.
- RFC 2047 headers were passed through raw.
- `_action_execute_python` referenced `powershell_path` before assignment, so
  the tool could not run at all.
- `main_loop` referenced `history.csv` as a bare name rather than a string.
- `_action_read_file` attached `path` in the help branch where `path` was
  falsy.
- `Emalia.__init__` passed `self._setting_location` as `load_settings`'s
  `prefix` argument.
- `emalia_main.py` called `Emalia(...)` on the module rather than the class,
  and read `PID` before `main_loop` set it.
- `EmailManager.__init__` fell back to `eval(os.environ.get("HANDLER_SMTP"))`
  for the IMAP configuration.
- `assert_valid_email_received` checked `return_path`, a key `parse_email`
  never produced.
- A fresh IMAP login per operation, costing roughly a second each time by the
  original's own measurements.

[0.2.0]: https://github.com/CoronRing/GS-Emalia/releases/tag/v0.2.0
[0.1.0]: https://github.com/CoronRing/GS-Emalia/releases/tag/v0.1.0
