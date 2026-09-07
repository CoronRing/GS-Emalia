"""Configuration.

Resolution order, lowest precedence first: built-in defaults, `emalia.toml`,
`EMALIA_*` environment variables, then explicit constructor arguments.

Secrets are read only from the environment or a `.env` file. `emalia.toml` is
meant to be committable; nothing written there is confidential.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from emalia.errors import ConfigurationError
from emalia.mail.accounts import MailAccount
from emalia.security.policy import Policy, normalise_patterns

__all__ = ["EmaliaConfig", "LLMSettings", "DEFAULT_CONFIG_NAMES"]

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_NAMES: tuple[str, ...] = ("emalia.toml", ".emalia.toml")

#: Environment variable each provider reads its key from. Emalia never handles
#: these itself; railtracks and the provider SDKs do. The mapping exists so
#: `emalia check` can tell you which one is missing.
_PROVIDER_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "azure": "AZURE_API_KEY",
    "huggingface": "HF_TOKEN",
    "ollama": "",
    "compatible": "",
}


@dataclass(slots=True)
class LLMSettings:
    """Which model the agent runs on.

    Attributes:
        provider: One of ``anthropic``, ``openai``, ``gemini``, ``azure``,
            ``ollama``, ``huggingface``, or ``compatible`` for any
            OpenAI-shaped endpoint.
        model: The model identifier the provider expects.
        api_base: Base URL, required for ``compatible`` and optional for
            ``ollama``.
        api_key_env: Override the environment variable the key is read from.
        temperature: Sampling temperature, or None for the provider default.
        max_tokens: Response ceiling, or None for the provider default.
    """

    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_base: str | None = None
    api_key_env: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    @property
    def key_env(self) -> str:
        """The environment variable holding this provider's API key."""
        if self.api_key_env:
            return self.api_key_env
        return _PROVIDER_KEY_ENV.get(self.provider.lower(), "")

    def key_present(self) -> bool:
        """Whether the required API key is set.

        Returns:
            True when a key is present, or when the provider needs none
            (a local Ollama server, for instance).
        """
        env = self.key_env
        return not env or bool(os.environ.get(env))


@dataclass(slots=True)
class EmaliaConfig:
    """Everything one Emalia instance needs.

    Attributes:
        account: The mailbox to run as.
        policy: What the instance is allowed to do.
        llm: Which model to run on.
        instance_name: The name the agent answers to, used in the system
            prompt and the outgoing footer.
        poll_interval: Seconds between IMAP polls.
        batch_size: Most messages handled per poll.
        max_concurrent: Messages processed in parallel. One is the safe
            default; the agent's tools touch shared mail connections.
        state_dir: Where the seen-message set and the audit log live.
        attachment_dir: Where inbound attachments are saved.
        footer: Appended to every outgoing body. None uses a generated one.
        extra_instructions: Appended to the system prompt. The place to give
            the agent house rules without forking the package.
        dry_run: Do everything except actually send. The replies are logged.
    """

    account: MailAccount
    policy: Policy = field(default_factory=Policy)
    llm: LLMSettings = field(default_factory=LLMSettings)

    instance_name: str = "Emalia"
    poll_interval: float = 30.0
    batch_size: int = 5
    max_concurrent: int = 1

    state_dir: Path = field(default_factory=lambda: Path(".emalia"))
    attachment_dir: Path | None = None
    footer: str | None = None
    extra_instructions: str = ""
    dry_run: bool = False

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir).expanduser().resolve()
        if self.attachment_dir is not None:
            self.attachment_dir = Path(self.attachment_dir).expanduser().resolve()
        if self.poll_interval < 1:
            raise ConfigurationError("poll_interval must be at least 1 second.")
        if self.batch_size < 1:
            raise ConfigurationError("batch_size must be at least 1.")
        if self.max_concurrent < 1:
            raise ConfigurationError("max_concurrent must be at least 1.")

    @property
    def effective_footer(self) -> str:
        """The footer actually appended to outgoing mail."""
        if self.footer is not None:
            return self.footer
        return f"-- \nSent by {self.instance_name}, an automated assistant."

    @property
    def seen_path(self) -> Path:
        """Where the processed-message set is persisted."""
        return self.state_dir / "seen.json"

    @property
    def audit_path(self) -> Path:
        """Where the JSONL audit log is written."""
        return self.state_dir / "audit.jsonl"

    # -- loading --------------------------------------------------------------

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        *,
        env_prefix: str = "EMALIA_",
        **overrides: Any,
    ) -> EmaliaConfig:
        """Build a config from a TOML file, the environment, and overrides.

        Args:
            config_path: Path to a TOML file. When None, the first of
                `DEFAULT_CONFIG_NAMES` found in the current directory is used,
                and it is fine for none to exist.
            env_prefix: Prefix for environment variables.
            **overrides: Values that win over everything else. Keys match the
                dataclass fields.

        Returns:
            The resolved config.

        Raises:
            ConfigurationError: If a named config file is missing, the TOML is
                malformed, or required credentials are absent.
        """
        data = _load_toml(config_path)

        account = MailAccount.from_env(env_prefix)
        account_overrides = data.get("account", {})
        if account_overrides:
            from dataclasses import replace

            # Only non-secret endpoint fields may come from the file.
            allowed = {
                "imap_host",
                "imap_port",
                "smtp_host",
                "smtp_port",
                "smtp_security",
                "display_name",
                "timeout",
                "username",
            }
            unknown = set(account_overrides) - allowed
            if unknown:
                raise ConfigurationError(
                    f"[account] in the config file may not set {sorted(unknown)}. "
                    "Credentials belong in the environment or a .env file."
                )
            account = replace(account, **account_overrides)

        policy = _policy_from(data.get("policy", {}), env_prefix)
        llm = _llm_from(data.get("llm", {}), env_prefix)

        general = {k: v for k, v in data.items() if k not in ("account", "policy", "llm")}
        general.update(_general_from_env(env_prefix))
        general.update(overrides)

        return cls(account=account, policy=policy, llm=llm, **general)


def _load_toml(config_path: str | Path | None) -> dict[str, Any]:
    """Read a TOML config, or return an empty mapping when none is present."""
    if config_path is not None:
        path = Path(config_path).expanduser()
        if not path.exists():
            raise ConfigurationError(f"Config file not found: {path}")
    else:
        path = next(
            (Path(name) for name in DEFAULT_CONFIG_NAMES if Path(name).exists()),
            Path(),
        )
        if not path.name:
            return {}

    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"{path} is not valid TOML: {exc}") from exc


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _policy_from(data: Mapping[str, Any], env_prefix: str) -> Policy:
    """Build a policy from the config table, then apply environment overrides."""
    fields: dict[str, Any] = dict(data)

    for key in ("allowed_senders", "blocked_senders", "allowed_recipients", "enabled_toolsets"):
        if key in fields:
            fields[key] = normalise_patterns(fields[key])

    env_map = {
        "allowed_senders": f"{env_prefix}ALLOWED_SENDERS",
        "blocked_senders": f"{env_prefix}BLOCKED_SENDERS",
        "allowed_recipients": f"{env_prefix}ALLOWED_RECIPIENTS",
        "enabled_toolsets": f"{env_prefix}TOOLSETS",
    }
    for key, env_name in env_map.items():
        raw = os.environ.get(env_name)
        if raw:
            fields[key] = normalise_patterns(raw)

    roots_raw = os.environ.get(f"{env_prefix}SANDBOX_ROOTS")
    if roots_raw:
        fields["sandbox_roots"] = [p.strip() for p in roots_raw.split(os.pathsep) if p.strip()]
    if "sandbox_roots" in fields:
        fields["sandbox_roots"] = [Path(p) for p in fields["sandbox_roots"]]

    for key, env_name in (
        ("allow_any_sender", f"{env_prefix}ALLOW_ANY_SENDER"),
        ("allow_dangerous_tools", f"{env_prefix}ALLOW_DANGEROUS_TOOLS"),
    ):
        value = _env_bool(env_name)
        if value is not None:
            fields[key] = value

    token = os.environ.get(f"{env_prefix}TOKEN")
    if token:
        fields["require_token"] = token

    for key, env_name in (
        ("max_replies_per_run", f"{env_prefix}MAX_REPLIES_PER_RUN"),
        ("max_replies_per_hour", f"{env_prefix}MAX_REPLIES_PER_HOUR"),
        ("max_tool_calls", f"{env_prefix}MAX_TOOL_CALLS"),
    ):
        raw = os.environ.get(env_name)
        if raw:
            fields[key] = int(raw)

    unknown = set(fields) - {f for f in Policy.__dataclass_fields__ if not f.startswith("_")}
    if unknown:
        raise ConfigurationError(f"Unknown [policy] keys: {sorted(unknown)}")

    return Policy(**fields)


def _llm_from(data: Mapping[str, Any], env_prefix: str) -> LLMSettings:
    """Build LLM settings from the config table, then environment overrides."""
    fields: dict[str, Any] = dict(data)
    for key, env_name in (
        ("provider", f"{env_prefix}LLM_PROVIDER"),
        ("model", f"{env_prefix}LLM_MODEL"),
        ("api_base", f"{env_prefix}LLM_API_BASE"),
        ("api_key_env", f"{env_prefix}LLM_API_KEY_ENV"),
    ):
        raw = os.environ.get(env_name)
        if raw:
            fields[key] = raw
    raw_temp = os.environ.get(f"{env_prefix}LLM_TEMPERATURE")
    if raw_temp:
        fields["temperature"] = float(raw_temp)
    raw_max = os.environ.get(f"{env_prefix}LLM_MAX_TOKENS")
    if raw_max:
        fields["max_tokens"] = int(raw_max)

    unknown = set(fields) - set(LLMSettings.__dataclass_fields__)
    if unknown:
        raise ConfigurationError(f"Unknown [llm] keys: {sorted(unknown)}")
    return LLMSettings(**fields)


def _general_from_env(env_prefix: str) -> dict[str, Any]:
    """Read the top-level config fields the environment may override."""
    values: dict[str, Any] = {}
    if name := os.environ.get(f"{env_prefix}INSTANCE_NAME"):
        values["instance_name"] = name
    if raw := os.environ.get(f"{env_prefix}POLL_INTERVAL"):
        values["poll_interval"] = float(raw)
    if raw := os.environ.get(f"{env_prefix}BATCH_SIZE"):
        values["batch_size"] = int(raw)
    if raw := os.environ.get(f"{env_prefix}MAX_CONCURRENT"):
        values["max_concurrent"] = int(raw)
    if raw := os.environ.get(f"{env_prefix}STATE_DIR"):
        values["state_dir"] = Path(raw)
    if raw := os.environ.get(f"{env_prefix}ATTACHMENT_DIR"):
        values["attachment_dir"] = Path(raw)
    dry = _env_bool(f"{env_prefix}DRY_RUN")
    if dry is not None:
        values["dry_run"] = dry
    return values
