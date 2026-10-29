"""P0-T18：BrowserSkill owner 唯一性、撤销与迟到结果丢弃的契约层。

为什么需要这一层
================
T14 静态预检与 T17 真机取证确认了两件事，二者叠加正好构成本任务的动机：

1. `bsk` daemon 的 recoverable start 请求**本身不带 fencing**。同一
   `<expiry-ms>:<uuid>` 请求在被 `session stop` 之前可以被任意进程重复
   `session start --request-id`，daemon 只是幂等地返回**同一个** session
   （实测见 `docs/poc/evidence/poc05-recovery.md` §4.2）。所以「两个 owner 抢同一
   会话时恰好一个生效」无法由 wire 保证，必须由本模块的 CAS 保证。
2. `session stop` / `session request --cancel` 之后，daemon 才对旧 session 返回
   `not_found`；这是**断开后**的兜底，不是**断开前**的闸。撤销必须先在本地把 epoch
   推进并置 revoked，再尽力通知 daemon。

因此本模块只做三件在 wire 之上叠加的事：

- `LeaseRegistry`：纯内存、可单测的 **owner epoch CAS**。epoch 严格 +1；旧 epoch
  一律 `StaleEpoch`；撤销后一律 `SessionRevoked`；租约过期 `LeaseExpired`；
  两个 owner 抢同一 subject 只有一个成功，另一个 `EpochConflict`。
- `BrowserskillRecovery`：把 CAS 套进真实 `bsk` 流程（prepare → start → execute →
  stop/cancel），并负责**迟到结果丢弃**：`apply_result()` 先按 epoch/revoked 校验，
  旧 epoch 的结果只进 `dropped` 账本，永远不写当前会话的状态。
- 错误码：`STALE_EPOCH` / `SESSION_REVOKED` / `LEASE_EXPIRED` / `EPOCH_CONFLICT`
  / `START_REJECTED`，与 `browser_contracts` 的错误码风格一致（稳定大写串 + 明细）。

明确不做的
==========
- 不做跨进程/跨网关的分布式锁：Phase 0 只有单网关进程，多实例 fencing 见
  Backlog P0-T23；本模块的 `lease_epoch` 是**进程内** CAS，`docs/poc/evidence/
  poc05-recovery.md` §7 会把这一限制写成残余风险，不伪装成分布式保证。
- 不猜任何未核对的 `bsk` 子命令：只用 T17 逐条 `--help` 对过签名、
  且 T18 实测过的 `session request --prepare/--cancel/--claim`、
  `session start --request-id`、`session stop`、`snapshot`。
- 不重放已发送写动作：`apply_result` 只做「丢弃或应用」，没有 retry 分支。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

from browser_contracts import BrowserAction

from .adapter import BrowserskillAdapter
from .cli import BskCli, BskCliError, parse_json, redact

# ---------------------------------------------------------------- 常量

STALE_EPOCH: Final[str] = "STALE_EPOCH"
"""调用方持有的 epoch 落后于当前 epoch：它的写入/结果必须被丢弃。"""

SESSION_REVOKED: Final[str] = "SESSION_REVOKED"
"""会话已被撤销：任何动作、任何 epoch 都不再被接受。"""

LEASE_EXPIRED: Final[str] = "LEASE_EXPIRED"
"""租约已过期：owner 必须先 `takeover()` 才能继续，不能静默续命。"""

EPOCH_CONFLICT: Final[str] = "EPOCH_CONFLICT"
"""另一个 owner 持有同一 epoch 的活动租约，或调用方 epoch 超前（CAS 失败）。"""

START_REJECTED: Final[str] = "START_REJECTED"
"""recoverable start 被 daemon 拒绝（取消、daemon 重启、请求过期等）。"""

MAX_REQUEST_TTL_MS: Final[int] = 600_000
"""`bsk` 文档口径：`--request-id` 有效期「at most ten minutes」。"""

DEFAULT_LEASE_TTL_SECONDS: Final[float] = 60.0
"""owner 租约默认 60s：`bsk` 侧无心跳，过期后必须显式 takeover。"""

_REQUEST_STATE_PREPARED: Final[str] = "prepared"
_REQUEST_STATE_READY: Final[str] = "ready"
_REQUEST_STATE_ACTIVE: Final[str] = "active"
_REQUEST_STATE_CLOSED: Final[str] = "closed"
_REQUEST_STATE_UNKNOWN: Final[str] = "unknown"


# ---------------------------------------------------------------- 错误


class RecoveryError(RuntimeError):
    """恢复层错误：带稳定 `code` 与可序列化明细，便于写进证据与调用方分支。"""

    code: str = "RECOVERY_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or type(self).code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details:
            payload["details"] = redact(self.details)
        return payload


class StaleEpoch(RecoveryError):
    """旧 epoch 的写入/结果被拒；结果只能进 dropped 账本。"""

    code = STALE_EPOCH


class SessionRevoked(RecoveryError):
    """会话已撤销；新动作计数必须为 0。"""

    code = SESSION_REVOKED


class LeaseExpired(RecoveryError):
    """租约过期，需 takeover 而非复用旧 epoch。"""

    code = LEASE_EXPIRED


class EpochConflict(RecoveryError):
    """并发 owner 冲突或 epoch 超前。"""

    code = EPOCH_CONFLICT


class StartRejected(RecoveryError):
    """recoverable start 失败（携带 daemon 的原始 code/exit_code 供取证）。"""

    code = START_REJECTED


# ---------------------------------------------------------------- 请求 ID


def new_request_id(
    *,
    ttl_ms: int = MAX_REQUEST_TTL_MS,
    clock_ms: Callable[[], int] | None = None,
) -> str:
    """生成 `<expiry-unix-ms>:<uuid4>` 的 recoverable start 请求 ID。

    校验 `0 < ttl_ms <= MAX_REQUEST_TTL_MS`（daemon 只接受十分钟以内的过期时间）。
    """

    if not 0 < ttl_ms <= MAX_REQUEST_TTL_MS:
        raise ValueError(f"ttl_ms 必须在 (0, {MAX_REQUEST_TTL_MS}] 内，实际 {ttl_ms}")
    now_ms = (clock_ms or (lambda: int(time.time() * 1000)))()
    return f"{now_ms + ttl_ms}:{uuid.uuid4()}"


def parse_request_id(request_id: str) -> tuple[int, uuid.UUID]:
    """拆开请求 ID；形状不对直接 ValueError，不带病下发。"""

    raw = str(request_id)
    expiry, sep, tail = raw.partition(":")
    if not sep or not expiry.isdigit():
        raise ValueError(f"request_id 形状必须是 <expiry-unix-ms>:<uuid>，实际 {raw!r}")
    if int(expiry) <= 0:
        raise ValueError(f"request_id 的过期时间必须为正整数，实际 {expiry!r}")
    return int(expiry), uuid.UUID(tail)


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """已 `--prepare` 的 recoverable start 请求（daemon 侧 state=prepared）。"""

    request_id: str
    state: str
    expires_at_ms: int
    session_id: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> PreparedRequest:
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise StartRejected(f"prepare 返回缺少 request_id: {payload!r}")
        expiry_ms, _uuid = parse_request_id(request_id)
        session = payload.get("session")
        session_id = session.get("session_id") if isinstance(session, Mapping) else None
        return cls(
            request_id=request_id,
            state=str(payload.get("state") or ""),
            expires_at_ms=expiry_ms,
            session_id=session_id if isinstance(session_id, str) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "state": self.state,
            "expires_at_ms": self.expires_at_ms,
            "session_id": self.session_id,
        }


# ---------------------------------------------------------------- 租约


@dataclass(frozen=True, slots=True)
class Lease:
    """一次 owner 持有的租约；`epoch` 是 fencing token。

    字段名对齐 `docs/tdd/05_Browser_Adapter.md` §3 的 `BrowserSession`：
    `owner_instance` / `lease_epoch` / `lease_expires_at`。
    """

    subject: str
    owner_id: str
    epoch: int
    issued_at: float
    expires_at: float
    request_id: str | None = None
    session_id: str | None = None
    revoked: bool = False
    revoke_reason: str | None = None

    def alive(self, now: float) -> bool:
        return not self.revoked and now < self.expires_at

    def remaining(self, now: float) -> float:
        return max(0.0, self.expires_at - now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "owner_instance": self.owner_id,
            "lease_epoch": self.epoch,
            "lease_issued_at": round(self.issued_at, 6),
            "lease_expires_at": round(self.expires_at, 6),
            "request_id": self.request_id,
            "session_id": self.session_id,
            "revoked": self.revoked,
            "revoke_reason": self.revoke_reason,
        }


@dataclass(slots=True)
class LeaseRegistry:
    """单进程内的 owner epoch CAS。

    语义（与 Backlog P0-T18 的量化验收一一对应）：

    - `claim(subject, expected_epoch=e)`：仅当 `e` 等于当前 epoch 且没有其他 owner 的
      活动租约时成功，新租约 epoch = e + 1。两个 owner 同时用 `e=0` 抢，只有一个成功，
      另一个拿到 `EpochConflict` 或 `StaleEpoch`——**恰好一个生效**。
    - `assert_current(subject, epoch)`：撤销 → `SessionRevoked`；epoch 落后 →
      `StaleEpoch`；epoch 超前 → `EpochConflict`；租约过期 → `LeaseExpired`。
    - `takeover(subject, expected_epoch=e)`：显式接管（旧 owner 已死），仍要求 CAS
      命中当前 epoch，成功后 epoch + 1，旧 owner 的后续写入一律 `StaleEpoch`。
    - `revoke(subject, reason)`：置 revoked 并推进 epoch；之后任何 epoch 都
      `SessionRevoked`，**撤销后新动作 0** 由 `BrowserskillRecovery.execute` 兑现。

    所有方法共享一把 `threading.RLock`，因为真机用例里双 owner 是**并发线程**抢同一
    subject；含 `bind()` 的别名表使 session_id 与 request_id 指向同一条租约。
    """

    clock: Callable[[], float] = time.monotonic
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _epochs: dict[str, int] = field(default_factory=dict, repr=False)
    _leases: dict[str, Lease] = field(default_factory=dict, repr=False)
    _revoked: dict[str, str] = field(default_factory=dict, repr=False)
    _alias: dict[str, str] = field(default_factory=dict, repr=False)
    _history: list[dict[str, Any]] = field(default_factory=list, repr=False)

    # -- 读 --------------------------------------------------------------
    def _key(self, subject: str) -> str:
        return self._alias.get(subject, subject)

    def epoch(self, subject: str) -> int:
        with self._lock:
            return self._epochs.get(self._key(subject), 0)

    def lease(self, subject: str) -> Lease | None:
        with self._lock:
            return self._leases.get(self._key(subject))

    def is_revoked(self, subject: str) -> bool:
        with self._lock:
            return self._key(subject) in self._revoked

    def revoke_reason(self, subject: str) -> str | None:
        with self._lock:
            return self._revoked.get(self._key(subject))

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._history]

    def snapshot(self) -> list[dict[str, Any]]:
        """证据用：当前所有 subject 的 epoch / 租约 / 撤销状态。"""

        with self._lock:
            now = self.clock()
            rows: list[dict[str, Any]] = []
            for key in sorted(set(self._epochs) | set(self._leases) | set(self._revoked)):
                lease = self._leases.get(key)
                rows.append(
                    {
                        "subject": key,
                        "epoch": self._epochs.get(key, 0),
                        "revoked": key in self._revoked,
                        "revoke_reason": self._revoked.get(key),
                        "lease": lease.to_dict() if lease is not None else None,
                        "lease_alive": lease.alive(now) if lease is not None else False,
                    }
                )
            return rows

    # -- 写 --------------------------------------------------------------
    def _record(self, event: str, **fields: Any) -> None:
        self._history.append({"event": event, **fields})

    def claim(
        self,
        subject: str,
        *,
        expected_epoch: int,
        owner_id: str,
        ttl: float = DEFAULT_LEASE_TTL_SECONDS,
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> Lease:
        """CAS 认领：`expected_epoch` 必须等于当前 epoch，成功则 epoch + 1。"""

        if ttl <= 0:
            raise ValueError(f"ttl 必须为正数，实际 {ttl}")
        with self._lock:
            key = self._key(subject)
            self._assert_not_revoked(key, subject)
            current = self._epochs.get(key, 0)
            self._assert_epoch(key, expected_epoch, current)
            now = self.clock()
            existing = self._leases.get(key)
            if existing is not None and existing.alive(now) and existing.owner_id != owner_id:
                self._record(
                    "claim_conflict",
                    subject=key,
                    epoch=current,
                    holder=existing.owner_id,
                    contender=owner_id,
                )
                raise EpochConflict(
                    f"subject {subject!r} 的 epoch {current} 已被 owner {existing.owner_id!r} 持有",
                    details={"holder": existing.owner_id, "contender": owner_id, "epoch": current},
                )
            lease = Lease(
                subject=key,
                owner_id=owner_id,
                epoch=current + 1,
                issued_at=now,
                expires_at=now + ttl,
                request_id=request_id or (existing.request_id if existing else None),
                session_id=session_id or (existing.session_id if existing else None),
            )
            self._epochs[key] = lease.epoch
            self._leases[key] = lease
            self._record(
                "claimed",
                subject=key,
                epoch=lease.epoch,
                owner=owner_id,
                previous_owner=existing.owner_id if existing else None,
            )
            return lease

    def bind(self, subject: str, session_id: str) -> Lease:
        """把 start 得到的 session_id 绑到同一租约上，并登记别名。"""

        if not session_id:
            raise ValueError("session_id 不能为空")
        with self._lock:
            key = self._key(subject)
            self._assert_not_revoked(key, subject)
            lease = self._leases.get(key)
            if lease is None:
                raise LeaseExpired(f"subject {subject!r} 尚无租约，不能 bind session")
            self._alias[subject] = key
            self._alias[session_id] = key
            updated = replace(lease, session_id=session_id)
            self._leases[key] = updated
            self._record("bound", subject=key, session_id=session_id, epoch=lease.epoch)
            return updated

    def assert_current(self, subject: str, epoch: int) -> Lease:
        """校验调用方仍持有当前 epoch 的活动租约。"""

        with self._lock:
            key = self._key(subject)
            self._assert_not_revoked(key, subject)
            current = self._epochs.get(key, 0)
            self._assert_epoch(key, epoch, current)
            lease = self._leases.get(key)
            if lease is None:
                raise LeaseExpired(
                    f"subject {subject!r} 没有活动租约（epoch {current}）",
                    details={"epoch": current},
                )
            now = self.clock()
            if not lease.alive(now):
                raise LeaseExpired(
                    f"subject {subject!r} 的租约在 epoch {lease.epoch} 过期",
                    details={
                        "epoch": lease.epoch,
                        "expired_by_seconds": round(now - lease.expires_at, 6),
                    },
                )
            return lease

    def takeover(
        self,
        subject: str,
        *,
        expected_epoch: int,
        owner_id: str,
        ttl: float = DEFAULT_LEASE_TTL_SECONDS,
        reason: str = "owner-lost",
    ) -> Lease:
        """显式接管：即使旧租约仍活着也推进 epoch（旧 owner 从此只能拿到 StaleEpoch）。"""

        if ttl <= 0:
            raise ValueError(f"ttl 必须为正数，实际 {ttl}")
        with self._lock:
            key = self._key(subject)
            self._assert_not_revoked(key, subject)
            current = self._epochs.get(key, 0)
            self._assert_epoch(key, expected_epoch, current)
            now = self.clock()
            previous = self._leases.get(key)
            lease = Lease(
                subject=key,
                owner_id=owner_id,
                epoch=current + 1,
                issued_at=now,
                expires_at=now + ttl,
                request_id=previous.request_id if previous else None,
                session_id=previous.session_id if previous else None,
            )
            self._epochs[key] = lease.epoch
            self._leases[key] = lease
            self._record(
                "took_over",
                subject=key,
                epoch=lease.epoch,
                owner=owner_id,
                previous_owner=previous.owner_id if previous else None,
                previous_epoch=previous.epoch if previous else None,
                reason=reason,
            )
            return lease

    def release(self, subject: str, *, epoch: int) -> None:
        """owner 主动交还：只允许当前 epoch 持有者释放，不推进 epoch。"""

        with self._lock:
            key = self._key(subject)
            lease = self._leases.get(key)
            if lease is None or lease.epoch != epoch:
                raise self._stale_or_conflict(key, epoch)
            self._leases.pop(key, None)
            self._record("released", subject=key, epoch=epoch, owner=lease.owner_id)

    def revoke(self, subject: str, *, reason: str) -> dict[str, Any]:
        """撤销：置 revoked、推进 epoch、清租约；幂等返回同一份记录。"""

        with self._lock:
            key = self._key(subject)
            now = self.clock()
            if key in self._revoked:
                existing = self._revoked[key]
                return {
                    "subject": key,
                    "revoked": True,
                    "already_revoked": True,
                    "reason": existing,
                    "epoch": self._epochs.get(key, 0),
                }
            self._revoked[key] = reason
            epoch = self._epochs.get(key, 0) + 1
            self._epochs[key] = epoch
            previous = self._leases.pop(key, None)
            self._record(
                "revoked",
                subject=key,
                epoch=epoch,
                reason=reason,
                previous_owner=previous.owner_id if previous else None,
                at=now,
            )
            return {
                "subject": key,
                "revoked": True,
                "already_revoked": False,
                "reason": reason,
                "epoch": epoch,
                "previous_owner": previous.owner_id if previous else None,
            }

    # -- 内部 ------------------------------------------------------------
    def _assert_not_revoked(self, key: str, subject: str) -> None:
        if key in self._revoked:
            raise SessionRevoked(
                f"subject {subject!r} 已撤销（原因：{self._revoked[key]}）",
                details={"reason": self._revoked[key], "epoch": self._epochs.get(key, 0)},
            )

    @staticmethod
    def _stale_or_conflict(key: str, epoch: int) -> RecoveryError:
        return StaleEpoch(f"epoch {epoch} 已不是当前 epoch（subject={key!r}）")

    def _assert_epoch(self, key: str, expected_epoch: int, current: int) -> None:
        if expected_epoch == current:
            return
        if expected_epoch < current:
            raise StaleEpoch(
                f"epoch {expected_epoch} 落后于当前 epoch {current}（subject={key!r}）",
                details={"expected_epoch": expected_epoch, "current_epoch": current},
            )
        raise EpochConflict(
            f"epoch {expected_epoch} 超前于当前 epoch {current}（subject={key!r}）",
            details={"expected_epoch": expected_epoch, "current_epoch": current},
        )


# ---------------------------------------------------------------- 恢复器


@dataclass(frozen=True, slots=True)
class Bootstrap:
    """一次 recoverable start 的结果：请求 → 会话 → 租约。"""

    request: PreparedRequest
    session_id: str
    lease: Lease
    session: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "session_id": self.session_id,
            "lease": self.lease.to_dict(),
            "session": redact(self.session),
        }


@dataclass(slots=True)
class BrowserskillRecovery:
    """真实 `bsk` 流程 + 本地 CAS：prepare → start → execute → stop/cancel/revoke。

    `cli` 必须是已定位好的 `BskCli`（真机用 `BskCli.autodetect()`，单测用替身）；
    `adapter` 默认为同一 `cli` 上的 `BrowserskillAdapter`，只做动作派发。
    """

    cli: BskCli
    registry: LeaseRegistry = field(default_factory=LeaseRegistry)
    owner_id: str = "owner-local"
    adapter: BrowserskillAdapter | None = None
    applied: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.adapter is None:
            self.adapter = BrowserskillAdapter(cli=self.cli)

    # -- 会话 ------------------------------------------------------------
    def prepare_request(
        self,
        *,
        ttl_ms: int = MAX_REQUEST_TTL_MS,
        name: str | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> PreparedRequest:
        """`session request <id> --prepare`：只登记，不建会话。"""

        request_id = new_request_id(ttl_ms=ttl_ms, clock_ms=clock_ms)
        payload = self._run_json(["session", "request", request_id, "--prepare"])
        prepared = PreparedRequest.from_payload(payload if isinstance(payload, Mapping) else {})
        if prepared.state != _REQUEST_STATE_PREPARED:
            raise StartRejected(
                f"prepare 未进入 prepared 态：state={prepared.state!r}",
                details={"state": prepared.state, "request_id": request_id, "name": name},
            )
        return prepared

    def recoverable_start(
        self,
        request: PreparedRequest | str,
        *,
        name: str | None = None,
        ttl: float = DEFAULT_LEASE_TTL_SECONDS,
        verify: bool = True,
    ) -> Bootstrap:
        """CAS 认领请求 → `session start --request-id` → 绑会话 → 可选回查状态。

        双 owner 抢同一 request 时：只有 CAS 胜者能走到 `bsk`；败者在 `claim` 处
        拿 `EpochConflict`/`StaleEpoch`，**不会**产生第二次 wire 调用。
        """

        prepared = request if isinstance(request, PreparedRequest) else PreparedRequest(
            request_id=str(request),
            state=_REQUEST_STATE_PREPARED,
            expires_at_ms=parse_request_id(str(request))[0],
        )
        lease = self.registry.claim(
            prepared.request_id,
            expected_epoch=0,
            owner_id=self.owner_id,
            ttl=ttl,
            request_id=prepared.request_id,
        )
        argv = ["session", "start", "--request-id", prepared.request_id, "--no-focus"]
        if name:
            argv.extend(["--name", name])
        try:
            payload = self._run_json(argv)
        except BskCliError as exc:
            self.registry.release(prepared.request_id, epoch=lease.epoch)
            raise self._start_rejected(exc, prepared) from exc
        session_id = payload.get("session_id") if isinstance(payload, Mapping) else None
        if not isinstance(session_id, str) or not session_id:
            self.registry.release(prepared.request_id, epoch=lease.epoch)
            raise StartRejected(
                f"session start 未返回 session_id: {payload!r}",
                details={"request_id": prepared.request_id, "payload": redact(payload)},
            )
        lease = self.registry.bind(prepared.request_id, session_id)
        if verify:
            observed = self.inspect_request(prepared.request_id)
            if observed.state not in {_REQUEST_STATE_READY, _REQUEST_STATE_ACTIVE}:
                raise StartRejected(
                    f"start 后请求状态异常：{observed.state!r}",
                    details={"request_id": prepared.request_id, "observed": observed.to_dict()},
                )
            if observed.session_id is not None and observed.session_id != session_id:
                raise StartRejected(
                    "start 返回的 session 与请求登记的 session 不一致",
                    details={
                        "request_id": prepared.request_id,
                        "returned": session_id,
                        "observed": observed.session_id,
                    },
                )
        return Bootstrap(
            request=prepared,
            session_id=session_id,
            lease=lease,
            session=dict(payload) if isinstance(payload, Mapping) else {},
        )

    def inspect_request(self, request_id: str) -> PreparedRequest:
        """`session request <id>`：读 daemon 侧状态（prepared/ready/active/closed/unknown）。"""

        payload = self._run_json(["session", "request", str(request_id)])
        if not isinstance(payload, Mapping):
            raise StartRejected(f"session request 返回非对象: {payload!r}")
        state = str(payload.get("state") or _REQUEST_STATE_UNKNOWN)
        session = payload.get("session")
        session_id = session.get("session_id") if isinstance(session, Mapping) else None
        expiry_ms, _uuid = parse_request_id(str(request_id))
        return PreparedRequest(
            request_id=str(request_id),
            state=state,
            expires_at_ms=expiry_ms,
            session_id=session_id if isinstance(session_id, str) else None,
        )

    def cancel_request(self, request_id: str) -> PreparedRequest:
        """`session request <id> --cancel`：撤销尚未消费的 start 请求。"""

        payload = self._run_json(["session", "request", str(request_id), "--cancel"])
        if not isinstance(payload, Mapping):
            raise StartRejected(f"cancel 返回非对象: {payload!r}")
        state = str(payload.get("state") or "")
        expiry_ms, _uuid = parse_request_id(str(request_id))
        if state != _REQUEST_STATE_CLOSED:
            raise StartRejected(
                f"cancel 后状态不是 closed：{state!r}",
                details={"request_id": str(request_id), "state": state},
            )
        return PreparedRequest(
            request_id=str(request_id),
            state=state,
            expires_at_ms=expiry_ms,
        )

    def claim_request(self, request_id: str) -> dict[str, Any]:
        """真实 `--claim`（不做包装语义）：返回 exit_code + 原始 payload 供取证。

        实测语义（`docs/poc/evidence/poc05-recovery.md` §4.3）：prepared 态不可认领
        （`cancelled` exit 2）；ready/active 态返回该请求并带上 session（exit 0），
        **不提供 fencing**——所以本模块的 owner 唯一性不依赖它。
        """

        exit_code, stdout, _stderr = self.cli.run(
            ["session", "request", str(request_id), "--claim"], check=False
        )
        payload: Any = {}
        if stdout.strip():
            try:
                payload = parse_json(stdout)
            except BskCliError:
                payload = {"raw": stdout.strip()[:400]}
        return {"exit_code": exit_code, "payload": redact(payload)}

    # -- 动作 ------------------------------------------------------------
    def execute(
        self,
        *,
        session_id: str,
        epoch: int,
        action: BrowserAction | str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """先过本地闸（撤销/epoch/租约），再派发到 `bsk`；撤销后 wire 调用为 0。"""

        lease = self.registry.assert_current(session_id, epoch)
        adapter = self._adapter()
        adapter.session_id = session_id
        payload = adapter.execute(action, dict(params or {}), expected_session=session_id)
        name = action.value if isinstance(action, BrowserAction) else str(action)
        self.calls.append(
            {
                "action": name,
                "session_id": session_id,
                "epoch": lease.epoch,
                "owner_id": self.owner_id,
            }
        )
        return payload

    def apply_result(
        self,
        *,
        session_id: str,
        epoch: int,
        action: BrowserAction | str,
        result: Any,
    ) -> dict[str, Any]:
        """应用一次动作结果：旧 epoch/已撤销/过期一律丢弃，只进 dropped 账本。

        返回值形状固定为 `{"applied": bool, "dropped_reason": <code>|None, ...}`，
        调用方不需要区分异常类型即可判断「这条结果还能不能写当前会话」。
        """

        name = action.value if isinstance(action, BrowserAction) else str(action)
        try:
            lease = self.registry.assert_current(session_id, epoch)
        except RecoveryError as exc:
            record = {
                "applied": False,
                "dropped_reason": exc.code,
                "session_id": session_id,
                "epoch": epoch,
                "action": name,
                "detail": str(exc),
            }
            self.dropped.append(record)
            return record
        record = {
            "applied": True,
            "dropped_reason": None,
            "session_id": session_id,
            "epoch": epoch,
            "applied_epoch": lease.epoch,
            "action": name,
            "result": result,
        }
        self.applied.append(record)
        return record

    # -- 撤销与接管 ------------------------------------------------------
    def revoke(self, *, session_id: str, reason: str) -> dict[str, Any]:
        """本地撤销（epoch+1、置 revoked）+ 尽力 `session stop`。

        「撤销后不可执行」由本地闸保证：即使 `session stop` 失败，`execute()` 也
        不会再发出动作；wire 侧 stop 只是尽力而为的清理，失败如实记录。
        """

        record = self.registry.revoke(session_id, reason=reason)
        wire: Any = None
        wire_error: dict[str, Any] | None = None
        try:
            wire = self._run_json(["session", "stop", session_id])
        except BskCliError as exc:
            wire_error = {
                "message": str(exc),
                "exit_code": exc.exit_code,
                "stdout": redact((exc.stdout or "").strip()[:400]),
            }
        record["wire"] = redact(wire) if wire is not None else None
        record["wire_error"] = wire_error
        return record

    def stop(self, *, session_id: str, epoch: int | None = None) -> Any:
        """正常停止：可选先校验 epoch，再 `session stop`。"""

        if epoch is not None:
            self.registry.assert_current(session_id, epoch)
        payload = self._run_json(["session", "stop", session_id])
        return redact(payload)

    def takeover(
        self,
        *,
        subject: str,
        expected_epoch: int,
        ttl: float = DEFAULT_LEASE_TTL_SECONDS,
        reason: str = "owner-lost",
    ) -> Lease:
        """接管：旧 owner 的 epoch 立即作废（其迟到结果只能被 dropped）。"""

        self.owner_id = self.owner_id
        return self.registry.takeover(
            subject,
            expected_epoch=expected_epoch,
            owner_id=self.owner_id,
            ttl=ttl,
            reason=reason,
        )

    def assert_current(self, *, subject: str, epoch: int) -> Lease:
        return self.registry.assert_current(subject, epoch)

    def status(self, subject: str) -> dict[str, Any]:
        lease = self.registry.lease(subject)
        return {
            "subject": subject,
            "epoch": self.registry.epoch(subject),
            "revoked": self.registry.is_revoked(subject),
            "revoke_reason": self.registry.revoke_reason(subject),
            "lease": lease.to_dict() if lease is not None else None,
            "applied": len(self.applied),
            "dropped": len(self.dropped),
        }

    # -- 内部 ------------------------------------------------------------
    def _adapter(self) -> BrowserskillAdapter:
        assert self.adapter is not None  # __post_init__ 已保证
        return self.adapter

    def _run_json(self, args: list[str]) -> Any:
        payload = self.cli.run_json(args)
        self.calls.append({"argv": redact(args), "kind": "cli"})
        return payload

    def _start_rejected(self, exc: BskCliError, prepared: PreparedRequest) -> StartRejected:
        payload: Any = {}
        stdout = (exc.stdout or "").strip()
        if stdout:
            try:
                payload = parse_json(stdout)
            except BskCliError:
                payload = {"raw": stdout[:400]}
        wire_code = payload.get("code") if isinstance(payload, Mapping) else None
        return StartRejected(
            f"recoverable start 被拒绝：{exc}（wire_code={wire_code!r}）",
            details={
                "request_id": prepared.request_id,
                "exit_code": exc.exit_code,
                "wire_code": wire_code,
                "payload": redact(payload),
            },
        )


def supported_recovery_actions() -> tuple[str, ...]:
    """本模块**必然**会派发的动作子集（evidence 与测试共用，避免与 adapter 漂移）。"""

    return tuple(sorted({BrowserAction.SNAPSHOT.value, BrowserAction.TAB_LIST.value}))


__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "EPOCH_CONFLICT",
    "LEASE_EXPIRED",
    "MAX_REQUEST_TTL_MS",
    "SESSION_REVOKED",
    "STALE_EPOCH",
    "START_REJECTED",
    "Bootstrap",
    "BrowserskillRecovery",
    "EpochConflict",
    "Lease",
    "LeaseExpired",
    "LeaseRegistry",
    "PreparedRequest",
    "RecoveryError",
    "SessionRevoked",
    "StaleEpoch",
    "StartRejected",
    "new_request_id",
    "parse_request_id",
    "supported_recovery_actions",
]
