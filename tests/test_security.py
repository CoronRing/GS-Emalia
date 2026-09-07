"""Policy and sandbox behaviour.

These are the tests that matter most: every one of them describes something an
attacker would otherwise be able to do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emalia.errors import (
    ConfigurationError,
    PermissionDeniedError,
    RateLimitError,
    SandboxViolationError,
)
from emalia.security.paths import resolve_in_sandbox
from emalia.security.policy import Policy


class TestSenderGate:
    def test_allowlisted_sender_is_accepted(self) -> None:
        policy = Policy(allowed_senders=["alice@example.com"])
        assert policy.sender_decision("alice@example.com")[0] == "accept"

    def test_sender_matching_is_case_insensitive(self) -> None:
        policy = Policy(allowed_senders=["alice@example.com"])
        assert policy.sender_decision("ALICE@Example.COM")[0] == "accept"

    def test_unlisted_sender_is_ignored(self) -> None:
        policy = Policy(allowed_senders=["alice@example.com"])
        decision, reason = policy.sender_decision("mallory@evil.example")
        assert decision == "ignore"
        assert "allowlist" in reason

    def test_glob_patterns_match_a_domain(self) -> None:
        policy = Policy(allowed_senders=["*@mycompany.com"])
        assert policy.sender_decision("anyone@mycompany.com")[0] == "accept"
        assert policy.sender_decision("anyone@other.com")[0] == "ignore"

    def test_blocklist_beats_allowlist(self) -> None:
        policy = Policy(
            allowed_senders=["*@mycompany.com"],
            blocked_senders=["spam@mycompany.com"],
        )
        assert policy.sender_decision("spam@mycompany.com")[0] == "ignore"

    def test_empty_sender_is_ignored(self) -> None:
        assert Policy(allow_any_sender=True).sender_decision("")[0] == "ignore"

    def test_required_token_must_appear_in_the_subject(self) -> None:
        policy = Policy(allowed_senders=["alice@example.com"], require_token="s3cret")
        assert policy.sender_decision("alice@example.com", subject="hi")[0] == "ignore"
        assert policy.sender_decision("alice@example.com", subject="hi s3cret")[0] == "accept"


class TestRecipientGate:
    def test_the_person_being_replied_to_is_always_allowed(self) -> None:
        Policy().check_recipient("alice@example.com", reply_target="alice@example.com")

    def test_a_third_party_is_refused_by_default(self) -> None:
        # This is the exfiltration guard: an injected "forward my keys to
        # mallory" cannot get past an empty allowed_recipients list.
        with pytest.raises(PermissionDeniedError):
            Policy().check_recipient("mallory@evil.example", reply_target="alice@example.com")

    def test_explicitly_allowed_recipients_pass(self) -> None:
        policy = Policy(allowed_recipients=["*@mycompany.com"])
        policy.check_recipient("bob@mycompany.com")


class TestToolsetGating:
    def test_unlisted_toolset_is_off(self) -> None:
        assert not Policy(enabled_toolsets=["email"]).toolset_enabled("shell")

    def test_dangerous_toolset_needs_the_second_switch(self) -> None:
        listed = Policy(enabled_toolsets=["shell"], allow_dangerous_tools=False)
        assert not listed.toolset_enabled("shell")
        armed = Policy(enabled_toolsets=["shell"], allow_dangerous_tools=True)
        assert armed.toolset_enabled("shell")

    def test_require_toolset_raises_when_off(self) -> None:
        with pytest.raises(PermissionDeniedError):
            Policy(enabled_toolsets=["email"]).require_toolset("file_write")


class TestPolicyValidation:
    def test_a_policy_nobody_can_reach_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="No sender can ever be accepted"):
            Policy().validate()

    def test_shell_without_the_second_switch_is_rejected(self) -> None:
        policy = Policy(allowed_senders=["a@b.com"], enabled_toolsets=["shell"])
        with pytest.raises(ConfigurationError, match="allow_dangerous_tools"):
            policy.validate()

    def test_shell_plus_open_inbox_is_rejected(self) -> None:
        # An unauthenticated remote shell is not a configuration anyone should
        # be able to reach by accident.
        policy = Policy(
            allow_any_sender=True,
            enabled_toolsets=["shell"],
            allow_dangerous_tools=True,
        )
        with pytest.raises(ConfigurationError, match="remote code execution"):
            policy.validate()

    def test_open_inbox_warns(self) -> None:
        warnings = Policy(allow_any_sender=True).validate()
        assert any("allow_any_sender" in w for w in warnings)

    def test_file_tools_without_roots_warn(self) -> None:
        policy = Policy(allowed_senders=["a@b.com"], enabled_toolsets=["file_read"])
        assert any("sandbox_roots" in w for w in policy.validate())


class TestRateLimits:
    def test_per_run_limit_is_enforced(self) -> None:
        policy = Policy(max_replies_per_run=2, max_replies_per_hour=-1)
        policy.check_send_allowed()
        policy.record_send()
        policy.check_send_allowed()
        policy.record_send()
        with pytest.raises(RateLimitError, match="per-run"):
            policy.check_send_allowed()

    def test_hourly_limit_is_enforced(self) -> None:
        policy = Policy(max_replies_per_run=-1, max_replies_per_hour=1)
        policy.record_send()
        with pytest.raises(RateLimitError, match="hourly"):
            policy.check_send_allowed()

    def test_resetting_the_run_clears_the_run_counter(self) -> None:
        policy = Policy(max_replies_per_run=1, max_replies_per_hour=-1)
        policy.record_send()
        policy.reset_run_counter()
        policy.check_send_allowed()

    def test_tool_budget_is_enforced_and_resettable(self) -> None:
        policy = Policy(max_tool_calls=2)
        policy.charge_tool_call()
        policy.charge_tool_call()
        with pytest.raises(RateLimitError, match="tool calls"):
            policy.charge_tool_call()
        policy.reset_tool_calls()
        policy.charge_tool_call()


class TestSandbox:
    def test_path_inside_a_root_resolves(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("hi", encoding="utf-8")
        assert resolve_in_sandbox(str(target), [tmp_path]) == target.resolve()

    def test_traversal_out_of_the_root_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "sandbox"
        root.mkdir()
        with pytest.raises(SandboxViolationError, match="outside"):
            resolve_in_sandbox(str(root / ".." / ".." / "etc" / "passwd"), [root])

    def test_absolute_path_outside_the_root_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "sandbox"
        root.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("nope", encoding="utf-8")
        with pytest.raises(SandboxViolationError):
            resolve_in_sandbox(str(outside), [root])

    def test_no_roots_refuses_everything(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxViolationError, match="not configured"):
            resolve_in_sandbox(str(tmp_path / "x"), [])

    def test_credential_shaped_names_are_refused_inside_the_root(self, tmp_path: Path) -> None:
        secret = tmp_path / "id_rsa"
        secret.write_text("PRIVATE KEY", encoding="utf-8")
        with pytest.raises(SandboxViolationError, match="deny pattern"):
            resolve_in_sandbox(str(secret), [tmp_path])

    def test_dotenv_is_refused_inside_the_root(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("EMALIA_PASSWORD=hunter2", encoding="utf-8")
        with pytest.raises(SandboxViolationError):
            resolve_in_sandbox(str(env), [tmp_path])

    def test_must_exist_is_enforced(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxViolationError, match="No such file"):
            resolve_in_sandbox(str(tmp_path / "missing.txt"), [tmp_path], must_exist=True)

    def test_a_new_file_in_the_root_resolves_without_must_exist(self, tmp_path: Path) -> None:
        resolved = resolve_in_sandbox(str(tmp_path / "new" / "file.txt"), [tmp_path])
        assert str(tmp_path.resolve()) in str(resolved)
