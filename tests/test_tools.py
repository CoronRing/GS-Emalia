"""Tools: policy enforcement, output shape, and failure handling.

Every tool is a plain callable, so these run without a model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emalia.mail.models import Attachment, EmailAddress, EmailMessage
from emalia.security.policy import Policy
from emalia.tools.email_tools import EmailTools
from emalia.tools.file_tools import FileReadTools, FileWriteTools
from emalia.tools.http_tools import HttpTools
from emalia.tools.registry import build_groups, build_tools
from fakes import FakeMailClient


def tool_by_name(group: object, name: str) -> object:
    """Pull one tool out of a group by its function name."""
    for tool in group.tools():  # type: ignore[attr-defined]
        if tool.__name__ == name:
            return tool
    raise AssertionError(f"{name} is not in this group")


@pytest.fixture
def mailbox() -> FakeMailClient:
    return FakeMailClient(
        [
            EmailMessage(
                uid="12",
                message_id="<a@example.com>",
                subject="Quarterly report",
                sender=EmailAddress(address="alice@example.com", name="Alice"),
                to=[EmailAddress(address="bot@example.com")],
                text="Here is the report you asked for.",
                attachments=[
                    Attachment(filename="q1.csv", content_type="text/csv", data=b"a,b\n1,2\n")
                ],
            ),
            EmailMessage(
                uid="11",
                message_id="<b@example.com>",
                subject="Lunch",
                sender=EmailAddress(address="bob@example.com"),
                text="Sandwiches?",
            ),
        ]
    )


class TestEmailToolsReading:
    def test_list_inbox_renders_uids_and_subjects(self, mailbox: FakeMailClient) -> None:
        tools = EmailTools(mailbox, Policy(allow_any_sender=True))
        output = tool_by_name(tools, "list_inbox")()
        assert "uid=12" in output
        assert "Quarterly report" in output

    def test_read_email_includes_body_and_attachment_names(self, mailbox: FakeMailClient) -> None:
        tools = EmailTools(mailbox, Policy(allow_any_sender=True))
        output = tool_by_name(tools, "read_email")("12")
        assert "Here is the report" in output
        assert "q1.csv" in output

    def test_read_email_reports_a_missing_uid_as_text(self, mailbox: FakeMailClient) -> None:
        # A tool never raises at the model; it explains.
        tools = EmailTools(mailbox, Policy(allow_any_sender=True))
        output = tool_by_name(tools, "read_email")("999")
        assert "MessageNotFoundError" in output

    def test_list_folders_lists_the_mailbox_names(self, mailbox: FakeMailClient) -> None:
        tools = EmailTools(mailbox, Policy(allow_any_sender=True))
        assert "Archive" in tool_by_name(tools, "list_folders")()

    def test_mark_email_rejects_an_unknown_state(self, mailbox: FakeMailClient) -> None:
        tools = EmailTools(mailbox, Policy(allow_any_sender=True))
        assert "state must be one of" in tool_by_name(tools, "mark_email")("12", "sideways")

    def test_mark_email_sets_the_flag(self, mailbox: FakeMailClient) -> None:
        tools = EmailTools(mailbox, Policy(allow_any_sender=True))
        tool_by_name(tools, "mark_email")("12", "read")
        assert "\\Seen" in mailbox.flags["12"]


class TestEmailToolsSending:
    def test_send_to_the_reply_target_is_allowed(self, mailbox: FakeMailClient) -> None:
        policy = Policy(allow_any_sender=True)
        tools = EmailTools(mailbox, policy, reply_target="alice@example.com")
        result = tool_by_name(tools, "send_email")("alice@example.com", "hi", "body")
        assert "Sent" in result
        assert mailbox.sent[0]["to"] == ["alice@example.com"]

    def test_send_to_a_third_party_is_refused(self, mailbox: FakeMailClient) -> None:
        # The exfiltration guard, exercised through the tool the model sees.
        policy = Policy(allow_any_sender=True)
        tools = EmailTools(mailbox, policy, reply_target="alice@example.com")
        result = tool_by_name(tools, "send_email")("mallory@evil.example", "hi", "body")
        assert "PermissionDeniedError" in result
        assert mailbox.sent == []

    def test_send_respects_the_rate_limit(self, mailbox: FakeMailClient) -> None:
        policy = Policy(allow_any_sender=True, max_replies_per_run=1, max_replies_per_hour=-1)
        tools = EmailTools(mailbox, policy, reply_target="alice@example.com")
        send = tool_by_name(tools, "send_email")
        assert "Sent" in send("alice@example.com", "one", "body")
        assert "RateLimitError" in send("alice@example.com", "two", "body")

    def test_dry_run_does_not_send(self, mailbox: FakeMailClient) -> None:
        policy = Policy(allow_any_sender=True)
        tools = EmailTools(mailbox, policy, reply_target="alice@example.com", dry_run=True)
        result = tool_by_name(tools, "send_email")("alice@example.com", "hi", "body")
        assert result.startswith("[dry run]")
        assert mailbox.sent == []

    def test_send_needs_a_recipient(self, mailbox: FakeMailClient) -> None:
        tools = EmailTools(mailbox, Policy(allow_any_sender=True))
        assert "at least one recipient" in tool_by_name(tools, "send_email")("", "hi", "b")

    def test_attachment_outside_the_sandbox_is_refused(
        self, mailbox: FakeMailClient, tmp_path: Path
    ) -> None:
        outside = tmp_path / "secret.txt"
        outside.write_text("x", encoding="utf-8")
        policy = Policy(allow_any_sender=True, sandbox_roots=[tmp_path / "allowed"])
        (tmp_path / "allowed").mkdir()
        tools = EmailTools(mailbox, policy, reply_target="alice@example.com")
        result = tool_by_name(tools, "send_email")(
            "alice@example.com", "hi", "body", "", str(outside)
        )
        assert "SandboxViolationError" in result
        assert mailbox.sent == []

    def test_forward_to_a_third_party_is_refused(self, mailbox: FakeMailClient) -> None:
        tools = EmailTools(mailbox, Policy(allow_any_sender=True), reply_target="alice@example.com")
        result = tool_by_name(tools, "forward_email")("12", "mallory@evil.example")
        assert "PermissionDeniedError" in result


class TestFileReadTools:
    def test_reads_a_file_inside_the_sandbox(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hello world", encoding="utf-8")
        tools = FileReadTools(Policy(sandbox_roots=[tmp_path]))
        assert "hello world" in tool_by_name(tools, "read_file")("notes.txt")

    def test_refuses_a_path_outside_the_sandbox(self, tmp_path: Path) -> None:
        root = tmp_path / "sandbox"
        root.mkdir()
        (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
        tools = FileReadTools(Policy(sandbox_roots=[root]))
        result = tool_by_name(tools, "read_file")(str(tmp_path / "outside.txt"))
        assert "SandboxViolationError" in result
        assert "secret" not in result

    def test_refuses_credential_files_inside_the_sandbox(self, tmp_path: Path) -> None:
        (tmp_path / "id_rsa").write_text("PRIVATE", encoding="utf-8")
        tools = FileReadTools(Policy(sandbox_roots=[tmp_path]))
        result = tool_by_name(tools, "read_file")("id_rsa")
        assert "deny pattern" in result
        assert "PRIVATE" not in result

    def test_max_lines_is_honoured(self, tmp_path: Path) -> None:
        (tmp_path / "long.txt").write_text("\n".join(str(i) for i in range(100)), encoding="utf-8")
        tools = FileReadTools(Policy(sandbox_roots=[tmp_path]))
        output = tool_by_name(tools, "read_file")("long.txt", 5)
        assert "stopped after 5" in output

    def test_binary_file_is_described_not_dumped(self, tmp_path: Path) -> None:
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        tools = FileReadTools(Policy(sandbox_roots=[tmp_path]))
        assert "binary file" in tool_by_name(tools, "read_file")("blob.bin")

    def test_lists_a_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        tools = FileReadTools(Policy(sandbox_roots=[tmp_path]))
        output = tool_by_name(tools, "list_directory")(str(tmp_path))
        assert "a.txt" in output
        assert "sub/" in output

    def test_searches_by_name(self, tmp_path: Path) -> None:
        (tmp_path / "meeting-notes.md").write_text("x", encoding="utf-8")
        tools = FileReadTools(Policy(sandbox_roots=[tmp_path]))
        assert "meeting-notes.md" in tool_by_name(tools, "search_files")("notes")

    def test_search_with_no_match_says_so(self, tmp_path: Path) -> None:
        tools = FileReadTools(Policy(sandbox_roots=[tmp_path]))
        assert "No entry matching" in tool_by_name(tools, "search_files")("nothinghere")

    def test_file_info_reports_size_and_kind(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("12345", encoding="utf-8")
        tools = FileReadTools(Policy(sandbox_roots=[tmp_path]))
        output = tool_by_name(tools, "file_info")("a.txt")
        assert "size: 5 bytes" in output
        assert "kind: file" in output

    def test_output_is_truncated_at_the_policy_limit(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("x" * 5000, encoding="utf-8")
        tools = FileReadTools(Policy(sandbox_roots=[tmp_path], max_output_chars=100))
        output = tool_by_name(tools, "read_file")("big.txt")
        assert "truncated" in output
        assert len(output) < 400


class TestFileWriteTools:
    def test_writes_inside_the_sandbox(self, tmp_path: Path) -> None:
        tools = FileWriteTools(Policy(sandbox_roots=[tmp_path]))
        result = tool_by_name(tools, "write_file")(str(tmp_path / "out.txt"), "content")
        assert "Wrote" in result
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "content"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        tools = FileWriteTools(Policy(sandbox_roots=[tmp_path]))
        tool_by_name(tools, "write_file")(str(tmp_path / "a" / "b" / "c.txt"), "x")
        assert (tmp_path / "a" / "b" / "c.txt").exists()

    def test_appends_when_asked(self, tmp_path: Path) -> None:
        target = tmp_path / "log.txt"
        tools = FileWriteTools(Policy(sandbox_roots=[tmp_path]))
        write = tool_by_name(tools, "write_file")
        write(str(target), "one")
        write(str(target), "two", True)
        assert target.read_text(encoding="utf-8") == "onetwo"

    def test_refuses_to_write_outside_the_sandbox(self, tmp_path: Path) -> None:
        root = tmp_path / "sandbox"
        root.mkdir()
        tools = FileWriteTools(Policy(sandbox_roots=[root]))
        result = tool_by_name(tools, "write_file")(str(tmp_path / "escape.txt"), "x")
        assert "SandboxViolationError" in result
        assert not (tmp_path / "escape.txt").exists()

    def test_refuses_to_delete_a_directory(self, tmp_path: Path) -> None:
        (tmp_path / "tree").mkdir()
        tools = FileWriteTools(Policy(sandbox_roots=[tmp_path]))
        result = tool_by_name(tools, "delete_file")(str(tmp_path / "tree"))
        assert "Only single files" in result
        assert (tmp_path / "tree").exists()

    def test_deletes_a_single_file(self, tmp_path: Path) -> None:
        target = tmp_path / "gone.txt"
        target.write_text("x", encoding="utf-8")
        tools = FileWriteTools(Policy(sandbox_roots=[tmp_path]))
        assert "Deleted" in tool_by_name(tools, "delete_file")(str(target))
        assert not target.exists()


class TestHttpTools:
    def test_refuses_a_non_http_scheme(self) -> None:
        tools = HttpTools(Policy())
        assert "http and https" in tool_by_name(tools, "http_request")("file:///etc/passwd")

    def test_refuses_localhost_by_default(self) -> None:
        # Reaching loopback from an email-driven agent is a request forgery.
        tools = HttpTools(Policy())
        assert "private or loopback" in tool_by_name(tools, "http_request")("http://127.0.0.1/")

    def test_refuses_the_cloud_metadata_address(self) -> None:
        tools = HttpTools(Policy())
        result = tool_by_name(tools, "http_request")("http://169.254.169.254/latest/meta-data/")
        assert "private or loopback" in result

    def test_refuses_an_unknown_method(self) -> None:
        tools = HttpTools(Policy(), allow_private_hosts=True)
        result = tool_by_name(tools, "http_request")("http://127.0.0.1/", "TRACE")
        assert "method must be one of" in result

    def test_rejects_malformed_headers_json(self) -> None:
        tools = HttpTools(Policy(), allow_private_hosts=True)
        result = tool_by_name(tools, "http_request")("http://127.0.0.1/", "GET", "not json")
        assert "not valid JSON" in result


class TestRegistry:
    def test_only_enabled_groups_are_built(self, mailbox: FakeMailClient) -> None:
        policy = Policy(allow_any_sender=True, enabled_toolsets=["email"])
        groups = build_groups(policy, client=mailbox)
        assert [g.toolset for g in groups] == ["email"]

    def test_dangerous_groups_stay_out_without_the_second_switch(self) -> None:
        policy = Policy(
            allow_any_sender=True,
            enabled_toolsets=["file_read", "shell", "python"],
        )
        assert [g.toolset for g in build_groups(policy)] == ["file_read"]

    def test_email_group_is_skipped_without_a_client(self) -> None:
        policy = Policy(allow_any_sender=True, enabled_toolsets=["email", "file_read"])
        assert [g.toolset for g in build_groups(policy)] == ["file_read"]

    def test_tools_have_unique_names(self, mailbox: FakeMailClient) -> None:
        policy = Policy(
            allow_any_sender=True,
            enabled_toolsets=["email", "file_read", "file_write", "http"],
        )
        tools = build_tools(policy, client=mailbox)
        names = [t.__name__ for t in tools]
        assert len(names) == len(set(names))

    def test_duplicate_extra_tool_name_is_rejected(self, mailbox: FakeMailClient) -> None:
        def read_file(path: str) -> str:
            """A colliding tool.

            Args:
                path: ignored.

            Returns:
                Nothing useful.
            """
            return ""

        policy = Policy(allow_any_sender=True, enabled_toolsets=["file_read"])
        with pytest.raises(ValueError, match="Duplicate tool name"):
            build_tools(policy, extra=[read_file])

    def test_every_tool_has_a_docstring_and_annotations(self, mailbox: FakeMailClient) -> None:
        # railtracks builds the tool schema from these; a tool without them is
        # invisible or unusable to the model.
        policy = Policy(
            allow_any_sender=True,
            enabled_toolsets=["email", "file_read", "file_write", "http"],
        )
        for tool in build_tools(policy, client=mailbox):
            assert tool.__doc__, f"{tool.__name__} has no docstring"
            takes_args = bool(tool.__code__.co_varnames[: tool.__code__.co_argcount])
            assert not takes_args or "Args:" in tool.__doc__
            # The tool modules use postponed annotations, so these are strings.
            assert tool.__annotations__.get("return") in (str, "str")
