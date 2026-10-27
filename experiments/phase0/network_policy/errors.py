"""网络策略的拒绝原因与异常。

设计约束（P0-T16 / TDD05 §6）：

* 一律 **fail-closed**：解析不出来、DNS 查不到、判定不了，都按拒绝处理；
* 每个拒绝带稳定 ``Reason`` 码，测试与审计按码断言，不靠自然语言；
* 异常消息可以带 URL 片段，但**不得**带 userinfo/cookie 等凭据（调用方只传已脱敏 URL）。
"""

from __future__ import annotations

from enum import StrEnum


class Reason(StrEnum):
    """拒绝原因码（稳定词表，测试按此断言）。"""

    INVALID_URL = "invalid_url"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    USERINFO_NOT_ALLOWED = "userinfo_not_allowed"
    ENCODED_HOST = "encoded_host"
    BACKSLASH_IN_URL = "backslash_in_url"
    CONTROL_CHAR_IN_URL = "control_char_in_url"
    HOST_MISSING = "host_missing"
    DOMAIN_DENIED = "domain_denied"
    DOMAIN_NOT_ALLOWED = "domain_not_allowed"
    PORT_NOT_ALLOWED = "port_not_allowed"
    DNS_FAILURE = "dns_failure"
    DNS_REBINDING = "dns_rebinding"
    ADDRESS_BLOCKED = "address_blocked"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    BODY_TOO_LARGE = "body_too_large"
    HEADERS_TOO_LARGE = "headers_too_large"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    RATE_LIMITED = "rate_limited"
    CONCURRENCY_LIMITED = "concurrency_limited"
    NO_PAGE_CONTEXT = "no_page_context"
    TRUSTED_ENDPOINT_INVALID = "trusted_endpoint_invalid"
    UNSAFE_REDIRECT = "unsafe_redirect"


class PolicyViolation(Exception):
    """一次拒绝。``code`` 固定为 TDD05 §6 的 ``SSRF_BLOCKED``。"""

    code = "SSRF_BLOCKED"

    def __init__(
        self,
        reason: Reason,
        message: str,
        *,
        url: str | None = None,
        data: dict[str, object] | None = None,
    ) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message
        self.url = url
        self.data: dict[str, object] = dict(data or {})

    def __str__(self) -> str:
        return f"{self.code}: {self.reason}: {self.message}"
