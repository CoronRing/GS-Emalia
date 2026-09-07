## What this changes

<!-- One or two sentences. If it fixes an issue, "Fixes #123". -->

## Why

<!-- The reason the diff does not explain by itself. -->

## Checks

- [ ] `ruff check src tests examples`
- [ ] `ruff format src tests examples`
- [ ] `mypy`
- [ ] `pytest`
- [ ] `pytest tests/e2e` — or say below why it was not run

## Security

- [ ] This adds no new capability reachable from an incoming email
- [ ] It does, and the threat model is described below
- [ ] It changes a `Policy` default (say which, and why the new one is safe)

<!-- Anything the agent can call is reachable by anyone who knows the address. -->

## Notes for the reviewer

<!-- Anything worth reading first, or a decision you would like a second opinion on. -->
