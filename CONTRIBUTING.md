# Contributing to Emalia

Thanks for looking. Issues and pull requests are both welcome.

## Getting set up

```bash
git clone https://github.com/CoronRing/GS-Emalia
cd GS-Emalia
uv venv && uv pip install -e ".[dev]"     # or: pip install -e ".[dev]"
pytest
```

The suite is offline and takes about six seconds. If it does not pass on a
fresh clone, that is a bug worth reporting on its own.

## Before opening a pull request

Four commands, the same four CI runs:

```bash
ruff check src tests examples
ruff format src tests examples
mypy
pytest
```

CI additionally runs the tests on Linux, macOS and Windows against Python 3.11,
3.12 and 3.13, and builds the wheel. Windows is not decoration — path handling
in `emalia.security.paths` and filename sanitisation in `emalia.mail.compose`
both have platform-specific behaviour.

## House style

The code aims to read as though one person wrote it in an afternoon and knew
what they were doing. In practice:

- **Type everything.** `disallow_untyped_defs` is on for `src/emalia`. Google-style
  docstrings with `Args:`, `Returns:` and `Raises:` on anything public.
- **Comment the "why", never the "what".** A comment earns its place by
  explaining a decision that the code cannot: a protocol quirk, a security
  reason, an ordering that looks arbitrary and is not. If it restates the line
  below it, delete it.
- **No archaeology in comments.** Nothing about what a function used to be
  called or which version removed a thing. That is what the changelog and git
  are for.
- **Errors are for the person reading them.** Say what is wrong and what to do
  about it, and name the setting or variable involved.
- **Tests read as sentences.** `test_replying_sets_answered_on_the_original`,
  not `test_reply_2`.

## Where things belong

```
src/emalia/mail/       stdlib only — must never import railtracks
src/emalia/security/   Policy and path containment; the single audit point
src/emalia/tools/      one module per toolset
src/emalia/agent.py    thin on purpose; behaviour belongs in the policy
src/emalia/runtime/    the listener and its persisted state
tests/                 offline, fakes, fast
tests/e2e/             real mail server, opt-in
```

The layer rule is not stylistic. `emalia.mail` importing anything from
`railtracks` would add roughly seventeen seconds to the import time of a
library whose main appeal is that it does not do that. There is a test for it.

## Changes that need a conversation first

Open an issue before writing code if you are planning to:

- **Widen a default.** Every default in `Policy` is narrow deliberately.
  Loosening one is a security change, not a convenience change.
- **Add a tool the agent can call.** New capability reachable from an untrusted
  email needs a threat model to go with it. Note that `MailClient.delete()`
  exists and is deliberately *not* exposed as a tool.
- **Add a runtime dependency.** The current four are the floor, not a budget.
- **Change the wire format** of the audit log or the seen-set.

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md) for how to report
one privately.

## Testing against a real mailbox

Most contributions need only the offline suite. If you are touching IMAP, SMTP
or MIME handling, run the end-to-end suite too — a fake will agree with any
assumption you hold about a protocol, and a real server will not. Setup is in
[docs/e2e-testing.md](docs/e2e-testing.md).

You do not need to run it to open a pull request. Say in the description that
you did not, and a maintainer will.

## Regenerating the README assets

`docs/assets/demo.svg` is generated. Edit `scripts/render_demo.py` and re-run
it rather than hand-editing the SVG:

```bash
python scripts/render_demo.py
```

## Commit messages

A short imperative summary, and a body explaining why if the reason is not
obvious from the diff. No tooling attribution, no emoji prefixes.

## License

Contributions are accepted under the [MIT License](LICENSE).
