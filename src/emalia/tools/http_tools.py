"""HTTP request tool.

Off by default. An agent that can be told what URL to fetch by an incoming
email is a server-side request forgery primitive, so this group blocks private
address space unless the operator opts back in.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable
from urllib.parse import urlparse

import requests

from emalia.errors import PermissionDeniedError, ToolExecutionError
from emalia.security.policy import Policy
from emalia.tools.base import ToolGroup, tool_result

__all__ = ["HttpTools"]

logger = logging.getLogger(__name__)

_ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def _resolves_to_private(host: str) -> bool:
    """Whether a hostname resolves to a private, loopback or link-local address.

    A literal check on the hostname is not enough: an attacker controls DNS for
    their own domain and can point it at ``169.254.169.254`` to reach a cloud
    metadata service. Resolving first is what closes that.

    Args:
        host: The hostname or IP literal from the URL.

    Returns:
        True when any resolved address is non-public, or when resolution
        fails, which is treated as unsafe.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return True
        if (
            parsed.is_private
            or parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_reserved
            or parsed.is_multicast
            or parsed.is_unspecified
        ):
            return True
    return False


class HttpTools(ToolGroup):
    """Outbound HTTP, gated by the policy.

    Attributes:
        allow_private_hosts: Permit requests that resolve to private or
            loopback addresses. Off by default.
        max_response_bytes: Stop reading a response past this size.
        timeout: Per-request timeout in seconds.
    """

    toolset = "http"

    def __init__(
        self,
        policy: Policy,
        *,
        allow_private_hosts: bool = False,
        max_response_bytes: int = 2 * 1024 * 1024,
        timeout: float = 30.0,
    ) -> None:
        """
        Args:
            policy: The policy to enforce.
            allow_private_hosts: Permit private and loopback destinations.
            max_response_bytes: Response size ceiling.
            timeout: Per-request timeout in seconds.
        """
        super().__init__(policy)
        self.allow_private_hosts = allow_private_hosts
        self.max_response_bytes = max_response_bytes
        self.timeout = timeout

    def tools(self) -> list[Callable[..., str]]:
        """The HTTP tools, as plain callables."""
        return [self._http_request()]

    def _http_request(self) -> Callable[..., str]:
        policy = self.policy
        allow_private = self.allow_private_hosts
        max_bytes = self.max_response_bytes
        timeout = self.timeout

        @tool_result(policy)
        def http_request(
            url: str,
            method: str = "GET",
            headers_json: str = "",
            body: str = "",
        ) -> str:
            """Make an HTTP request and return the response.

            Args:
                url: The full URL, which must use http or https.
                method: The HTTP method. One of GET, POST, PUT, PATCH, DELETE,
                    HEAD or OPTIONS.
                headers_json: Optional request headers as a JSON object, e.g.
                    ``{"Accept": "application/json"}``.
                body: Optional request body, sent as-is.

            Returns:
                The status line, the response headers, and the body, truncated
                to the output limit.
            """
            import json

            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                raise ToolExecutionError(
                    f"Only http and https URLs are allowed, got {parsed.scheme!r}."
                )
            if not parsed.hostname:
                raise ToolExecutionError(f"{url!r} has no hostname.")
            if not allow_private and _resolves_to_private(parsed.hostname):
                raise PermissionDeniedError(
                    f"{parsed.hostname} resolves to a private or loopback address. "
                    "Requests to internal networks are refused."
                )

            normalised = method.strip().upper()
            if normalised not in _ALLOWED_METHODS:
                raise ToolExecutionError(
                    f"method must be one of {', '.join(_ALLOWED_METHODS)}, got {method!r}."
                )

            headers: dict[str, str] = {}
            if headers_json.strip():
                try:
                    parsed_headers = json.loads(headers_json)
                except json.JSONDecodeError as exc:
                    raise ToolExecutionError(f"headers_json is not valid JSON: {exc}") from exc
                if not isinstance(parsed_headers, dict):
                    raise ToolExecutionError("headers_json must be a JSON object.")
                headers = {str(k): str(v) for k, v in parsed_headers.items()}

            try:
                response = requests.request(
                    normalised,
                    url,
                    headers=headers or None,
                    data=body.encode("utf-8") if body else None,
                    timeout=timeout,
                    stream=True,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                raise ToolExecutionError(f"Request to {url} failed: {exc}") from exc

            with response:
                # Redirects are followed above, so re-check where we landed:
                # a public URL can 302 to a metadata endpoint.
                final_host = urlparse(response.url).hostname
                if not allow_private and final_host and _resolves_to_private(final_host):
                    raise PermissionDeniedError(
                        f"The request redirected to {final_host}, a private address. Refused."
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(8192):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
                payload = b"".join(chunks).decode("utf-8", errors="replace")
                header_block = "\n".join(f"{k}: {v}" for k, v in response.headers.items())

            rendered = (
                f"{response.status_code} {response.reason} ({response.url})\n"
                f"{header_block}\n\n{payload}"
            )
            return rendered

        return http_request
