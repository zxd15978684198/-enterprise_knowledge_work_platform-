"""P0-T27 来源质量、去重与人工 Review 契约（纯契约层 + 真实 PostgreSQL）。

对照任务卡 ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T27（L456–471）与验收口径
``docs/poc/05_Phase0_Acceptance.md`` PoC-08：

1. **候选追加史不可变**：候选/事实/证据/事件/审阅/发布全部只允许 INSERT；改状态 =
   追加一行事件；``UPDATE`` / ``DELETE`` 由 ``007_candidates.sql`` 的 BEFORE 触发器
   拒绝（SQLSTATE ``23514``）。本模块**不提供**任何原地改状态的函数。
2. **去重**：同 source 同 URL 同内容（``dedup_key``）与同 source 同内容指纹
   （``content_hash``）都只留 1 个候选；同内容跨 source 保留各自候选，只并进同一
   duplicate cluster；跨 workspace 的 UNIQUE 键互不相交，不泄露另一空间是否抓过同一
   内容。
3. **来源质量评分可复现**：``score_quality`` 是纯函数，只吃整数/枚举/时间差，无随机、
   无 wall clock、无浮点；同一输入两次调用逐字节一致。分值构成写进
   ``quality_components``（jsonb）+ ``quality_algorithm`` 版本号，旧候选不会被新算法
   静默重算。
4. **撤销/过期后不可入库**：来源 ``revoked`` / ``expired`` 后，该来源下候选一律禁止
   promote；证据过期（``phase0_evidence_is_valid`` 口径）同样禁止。
5. **缺来源/缺证据不得发布（0 例外）**：发布入口只接 ``review_decision_id``，且数据库
   触发器 ``phase0_promotion_guard()`` 会再核一次——绕过本模块直插也过不去。证据的
   ``verified`` 默认 ``False``（deny-by-default）：未显式声明"已核验"的证据一律不算有效。
6. **人工 Review 契约**：``pending → approved/rejected`` 只允许 ``kind='human'`` 主体
   （表级 CHECK + 触发器双重拦截）；身份/时间/理由三要素 NOT NULL；重复审批幂等
   （唯一键 + ``ON CONFLICT DO NOTHING``）；同一候选至多一条 ``approved``。
7. **与 T24 对齐**：``needs_review`` 只能通过本层人工出口，``open_review_from_replan``
   只认 T24 的 ``REPLAN_NEEDS_REVIEW`` 码；其它码一律拒绝（不得把自动重规划当人工出口），
   且没有审阅决定的候选永远 promote 不了。

证据边界（T14 结论，本任务直接引用而不重新探测）
------------------------------------------------
参考实现的浏览器层**没有任何 SSRF/域名/速率代码**（``docs/poc/evidence/
browserskill_protocol_manifest.md`` §9.3 U2），所以"来源可信"不可能从采集侧继承，
只能由本层（评分 + 人工出口）建立。T16 的 ``network_policy`` 提供"这一跳是否被策略
放行"的事后事实，本模块只把它记成 ``fetch_policy``（``allow`` / ``deny`` /
``not_checked``）并据此硬拒绝 ``deny``，**不**重复实现网络策略。

本模块不写的边界
----------------
* 不写向量、不写 chunk、不切可见性（T12/T13 所有）。
* 不实现 HTTP 抓取；采集结果由调用方以 ``CandidateDraft`` 传入。
* 不实现工具审批（Approval）；``ReviewDecision`` 只管知识入库，跨层混用按 code 拒绝。
* 不建 RLS/角色：本 schema 不在 HTTP 面暴露（生产落库时由 T03/T25 补 policy）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

import locator_map

__all__ = [
    "AUTHORITY_EXACT",
    "AUTHORITY_SUFFIX",
    "BLOCKED_DOMAINS",
    "CODE_NEEDS_REVIEW",
    "DECISIONS",
    "EVENT_TYPES",
    "MIGRATION_ID",
    "MIGRATION_PATH",
    "MIN_AUTO_ADMIT_SCORE",
    "NEAR_DUPLICATE_MAX_DISTANCE",
    "ORIGINS",
    "QUALITY_ALGORITHM",
    "QUALITY_WEIGHTS",
    "R_ALREADY_PROMOTED",
    "R_AUTO_REPLAN_BYPASS",
    "R_AWAITING_REVIEW",
    "R_BLOCKED_DOMAIN",
    "R_CANDIDATE_MISSING",
    "R_CANDIDATE_STATE_BLOCKED",
    "R_DECISION_CONFLICT",
    "R_DECISION_MISMATCH",
    "R_DECISION_NOT_APPROVED",
    "R_DECISION_REQUIRED",
    "R_DUPLICATE_CONTENT",
    "R_EVIDENCE_EXPIRED",
    "R_EVIDENCE_MISSING",
    "R_EVIDENCE_UNVERIFIABLE",
    "R_FACTS_MISSING",
    "R_FETCH_DENIED",
    "R_KB_MISMATCH",
    "R_LICENSE_FORBIDDEN",
    "R_LOCATOR_INVALID",
    "R_MIGRATION_DIGEST_MISMATCH",
    "R_NEAR_DUPLICATE",
    "R_NOT_FROM_REPLAN",
    "R_PII_DETECTED",
    "R_REASON_REQUIRED",
    "R_REPLAN_NEEDS_REVIEW",
    "R_REVIEW_TIME_IN_FUTURE",
    "R_REVIEWER_DISABLED",
    "R_REVIEWER_NOT_HUMAN",
    "R_REVIEWER_UNKNOWN",
    "R_SCORE_BELOW_THRESHOLD",
    "R_SOURCE_EXPIRED",
    "R_SOURCE_MISSING",
    "R_SOURCE_NOT_APPROVED",
    "R_SOURCE_REVOKED",
    "REDIS_DB",
    "SCHEMA",
    "SOURCE_EVENTS",
    "SOURCE_STATES_BLOCKING",
    "STATES_BLOCKING_PROMOTION",
    "CandidateDraft",
    "CandidateReviewError",
    "CandidateReviewStore",
    "CandidateSnapshot",
    "EvidenceSpec",
    "FactSpec",
    "NeedsReviewSignal",
    "PromotionOutcome",
    "QualityComponent",
    "QualityInput",
    "QualityScore",
    "ReviewOutcome",
    "ReviewRequest",
    "ReviewTicket",
    "SourceRecord",
    "SourceSpec",
    "SubmissionOutcome",
    "canonical_url",
    "content_hash",
    "dedup_key",
    "detect_pii",
    "domain_of",
    "ensure_schema",
    "evidence_is_valid",
    "hamming64",
    "normalise_locator",
    "quote_hash",
    "reset_schema",
    "score_quality",
    "simhash64",
    "url_hash",
]

# ---------------------------------------------------------------------------
# 资源槽与迁移身份（wave record §1：pg_schema=p0_t27 / redis_db=12）
# ---------------------------------------------------------------------------
SCHEMA: Final = "p0_t27"
REDIS_DB: Final = 12
MIGRATION_ID: Final = "007_candidates"
MIGRATION_PATH: Final = Path(__file__).resolve().parent / "db" / "migrations" / "007_candidates.sql"

#: 评分算法版本：算法或权重改动必须升版本；旧候选的分数不随新版本重算。
QUALITY_ALGORITHM: Final = "t27-quality-v1"

#: 允许的动作集合（全部来自 007 DDL 的 CHECK，不是随手写的字面量）。
SOURCE_EVENTS: Final = (
    "captured",
    "approved",
    "rejected",
    "revoked",
    "expired",
    "refreshed",
)
SOURCE_STATES_BLOCKING: Final = ("revoked", "expired", "rejected")
EVENT_TYPES: Final = (
    "captured",
    "admitted",
    "rejected",
    "needs_review",
    "review_requested",
    "approved",
    "declined",
    "promoted",
    "promotion_blocked",
    "expired",
    "superseded",
)
STATES_BLOCKING_PROMOTION: Final = (
    "rejected",
    "expired",
    "superseded",
    "declined",
    "promotion_blocked",
    "promoted",
)
DECISIONS: Final = ("approved", "rejected")
ORIGINS: Final = ("ingest", "research", "replan_needs_review")

#: 低于该分不自动入候选库，只转人工（不是"低分自动批准"的反面：本层从不自动批准）。
MIN_AUTO_ADMIT_SCORE: Final = 60

#: 近重复只做提示：汉明距离 ≤ 3 记一条 near_duplicate hint，不自动合并、不改分数。
NEAR_DUPLICATE_MAX_DISTANCE: Final = 3

#: 质量分权重（整数，合计 100；分项见 ``score_quality``）。
QUALITY_WEIGHTS: Final = {
    "authority": 35,
    "primacy": 20,
    "freshness": 20,
    "license": 10,
    "evidence": 15,
}

#: 权威性精确表（命中最优先，与后缀表互斥）。
AUTHORITY_EXACT: Final = {
    "eur-lex.europa.eu": 35,
    "nist.gov": 35,
    "ietf.org": 35,
    "rfc-editor.org": 35,
    "w3.org": 34,
    "docs.python.org": 32,
    "postgresql.org": 32,
    "arxiv.org": 30,
    "doi.org": 30,
    "developer.mozilla.org": 28,
    "pgvector.github.io": 28,
}
#: 权威性后缀表（按顺序第一个匹配生效；未命中记 8 分"未知域名"）。
AUTHORITY_SUFFIX: Final = (
    (".gov", 34),
    (".edu", 32),
    (".int", 32),
    (".org", 20),
    (".io", 16),
    (".dev", 16),
    (".com", 12),
    (".net", 12),
)
#: 硬拒绝域名（控制站之外的内网/元数据地址，防止"来源"其实就是 SSRF 回连）。
BLOCKED_DOMAINS: Final = (
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "169.254.169.254",
    "metadata.google.internal",
)

#: T24 的人工出口码（只认这一个）。
CODE_NEEDS_REVIEW: Final = "REPLAN_NEEDS_REVIEW"

# ---------------------------------------------------------------------------
# 稳定原因码（证据与断言只引用这些值，不比对中文）
# ---------------------------------------------------------------------------
R_SOURCE_MISSING: Final = "SOURCE_MISSING"
R_SOURCE_NOT_APPROVED: Final = "SOURCE_NOT_APPROVED"
R_SOURCE_REVOKED: Final = "SOURCE_REVOKED"
R_SOURCE_EXPIRED: Final = "SOURCE_EXPIRED"
R_LICENSE_FORBIDDEN: Final = "SOURCE_LICENSE_FORBIDDEN"
R_FETCH_DENIED: Final = "SOURCE_FETCH_DENIED"
R_BLOCKED_DOMAIN: Final = "SOURCE_DOMAIN_BLOCKED"
R_PII_DETECTED: Final = "PII_DETECTED"
R_FACTS_MISSING: Final = "FACTS_MISSING"
R_EVIDENCE_MISSING: Final = "EVIDENCE_MISSING"
R_EVIDENCE_UNVERIFIABLE: Final = "EVIDENCE_UNVERIFIABLE"
R_EVIDENCE_EXPIRED: Final = "EVIDENCE_EXPIRED"
R_SCORE_BELOW_THRESHOLD: Final = "QUALITY_BELOW_THRESHOLD"
R_NEAR_DUPLICATE: Final = "NEAR_DUPLICATE_HINT"
R_REPLAN_NEEDS_REVIEW: Final = "ORIGIN_REPLAN_NEEDS_REVIEW"
R_AWAITING_REVIEW: Final = "AWAITING_HUMAN_REVIEW"
R_DUPLICATE_CONTENT: Final = "DUPLICATE_CONTENT"
R_LOCATOR_INVALID: Final = "LOCATOR_INVALID"
R_CANDIDATE_MISSING: Final = "CANDIDATE_MISSING"
R_CANDIDATE_STATE_BLOCKED: Final = "CANDIDATE_STATE_BLOCKED"
R_REVIEWER_UNKNOWN: Final = "REVIEWER_UNKNOWN"
R_REVIEWER_NOT_HUMAN: Final = "REVIEWER_NOT_HUMAN"
R_REVIEWER_DISABLED: Final = "REVIEWER_DISABLED"
R_REASON_REQUIRED: Final = "REVIEW_REASON_REQUIRED"
R_REVIEW_TIME_IN_FUTURE: Final = "REVIEW_TIME_IN_FUTURE"
R_DECISION_REQUIRED: Final = "REVIEW_DECISION_REQUIRED"
R_DECISION_NOT_APPROVED: Final = "REVIEW_DECISION_NOT_APPROVED"
R_DECISION_MISMATCH: Final = "REVIEW_DECISION_MISMATCH"
R_DECISION_CONFLICT: Final = "REVIEW_DECISION_CONFLICT"
R_KB_MISMATCH: Final = "REVIEW_KB_MISMATCH"
R_ALREADY_PROMOTED: Final = "ALREADY_PROMOTED"
R_NOT_FROM_REPLAN: Final = "CANDIDATE_NOT_FROM_REPLAN"
R_AUTO_REPLAN_BYPASS: Final = "AUTO_REPLAN_BYPASS_FORBIDDEN"
R_MIGRATION_DIGEST_MISMATCH: Final = "MIGRATION_DIGEST_MISMATCH"

#: 所有已登记原因码（测试用它确认"码在枚举里"，防止拼写漂移）。
REASON_CODES: Final = tuple(
    value for name, value in sorted(globals().items()) if name.startswith("R_")
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_FACT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_WORD = re.compile(r"[0-9a-z]+")
_WS = re.compile(r"\s+")

#: PII 探测（合成 fixture 用；真实 PII 检测在 T21/T22 的脱敏层）。
_PII_PATTERNS: Final = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("cn_mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("cn_id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
)


# ---------------------------------------------------------------------------
# 错误：稳定 code + detail，便于证据断言
# ---------------------------------------------------------------------------
class CandidateReviewError(RuntimeError):
    """候选/审阅/发布链上的契约错误；``code`` 是稳定枚举，不随文案变化。"""

    code: str = "CANDIDATE_REVIEW_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.detail: dict[str, Any] = dict(detail or {})


def fail(
    code: str, message: str, **detail: Any
) -> CandidateReviewError:
    """构造带 code/detail 的错误（调用处 ``raise fail(...)``）。"""

    return CandidateReviewError(message, code=code, detail=detail)


# ---------------------------------------------------------------------------
# 纯函数：URL / 指纹 / 相似度 / PII
# ---------------------------------------------------------------------------
def domain_of(url: str) -> str:
    """从 URL 取小写 host（不含端口/userinfo）；非法输入抛 code。"""

    parts = urlsplit(str(url).strip())
    host = (parts.hostname or "").lower()
    if not host:
        raise fail(R_LOCATOR_INVALID, f"URL 缺少 host: {url!r}")
    return host


def canonical_url(raw: str) -> str:
    """把 URL 规范化成来源身份。

    规则（显式、可复算）：
    * 只接受 http/https；
    * scheme/host 小写，去掉默认端口（80/443），去掉 fragment；
    * 保留 path 语义（空 path 记为 ``/``），**保留** query 且逐字节不动——
      "canonical" 不等于"删参数"，语义 query（分页/版本/语言）不能盲删；
    * 拒绝 userinfo（``user@host``），避免把凭据带进来源表与日志。
    """

    text = str(raw).strip()
    if not text or len(text) > 2048:
        raise fail(R_LOCATOR_INVALID, "URL 为空或超过 2048 字符")
    if "@" in text.split("//", 1)[-1].split("/", 1)[0]:
        raise fail(R_LOCATOR_INVALID, "URL 不允许携带 userinfo")
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise fail(R_LOCATOR_INVALID, f"只接受 http/https，得到 {scheme!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise fail(R_LOCATOR_INVALID, "URL 缺少 host")
    port = parts.port
    netloc = host
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    if port is not None and not default_port:
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def url_hash(canonical: str) -> str:
    """来源身份键：canonical URL 的 sha256（64 hex）。"""

    return hashlib.sha256(canonical_url(canonical).encode("utf-8")).hexdigest()


def content_hash(payload: str | bytes) -> str:
    """内容指纹：原始字节的 sha256（不做换行归一，避免掩盖真实差异）。"""

    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    return hashlib.sha256(data).hexdigest()


def dedup_key(url_digest: str, content_digest: str) -> str:
    """幂等键：同 URL 同内容重复投递必须落到同一个候选。"""

    for value, name in ((url_digest, "url_hash"), (content_digest, "content_hash")):
        if not _HEX64.match(value):
            raise fail(R_LOCATOR_INVALID, f"{name} 必须是 64 位小写 hex")
    return hashlib.sha256(f"{url_digest}:{content_digest}".encode()).hexdigest()


def quote_hash(quote: str) -> str:
    """证据引文指纹：折叠空白后取 sha256（同一句不同缩进视为同一证据）。"""

    collapsed = _WS.sub(" ", str(quote)).strip()
    if not collapsed:
        raise fail(R_LOCATOR_INVALID, "证据引文不得为空")
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


def _tokens(text: str) -> list[str]:
    return _WORD.findall(str(text).lower())


def simhash64(text: str) -> str:
    """确定性 64 位 simhash（16 hex）。

    已定型：token→blake2b(8B)→按 3-gram 加权投票。零随机、零依赖，
    同文本在任何进程/任何顺序下都得到同一个值。
    """

    tokens = _tokens(text)
    if not tokens:
        return hashlib.blake2b(b"", digest_size=8).hexdigest()
    width = 3
    if len(tokens) < width:
        shingles = [" ".join(tokens)]
    else:
        shingles = [" ".join(tokens[i : i + width]) for i in range(len(tokens) - width + 1)]
    counters = [0] * 64
    for shingle in shingles:
        digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(64):
            counters[bit] += 1 if (value >> bit) & 1 else -1
    result = 0
    for bit, counter in enumerate(counters):
        if counter > 0:
            result |= 1 << bit
    return f"{result:016x}"


def hamming64(left: str, right: str) -> int:
    """两个 simhash 的汉明距离（输入必须是 16 hex）。"""

    for value in (left, right):
        if not _HEX16.match(value):
            raise fail(R_LOCATOR_INVALID, f"simhash 必须是 16 位小写 hex: {value!r}")
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def detect_pii(text: str) -> tuple[str, ...]:
    """返回命中的 PII 类别（排序去重）；空 tuple = 未发现。"""

    hits: set[str] = set()
    for name, pattern in _PII_PATTERNS:
        if pattern.search(str(text)):
            hits.add(name)
    return tuple(sorted(hits))


# ---------------------------------------------------------------------------
# 质量评分：纯函数 + 可复现构成
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QualityComponent:
    """单个评分分项：权重、原始值、实际贡献。"""

    name: str
    weight: int
    value: int
    contribution: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "name": self.name,
            "weight": self.weight,
            "value": self.value,
            "contribution": self.contribution,
        }


@dataclass(frozen=True, slots=True)
class QualityInput:
    """评分的全部输入（没有隐藏状态，因此可复现）。"""

    domain: str
    is_primary: bool
    is_reprint: bool
    license: str
    observed_at: datetime
    published_at: datetime | None
    evidenced_critical: int
    critical_total: int

    def __post_init__(self) -> None:
        if self.license not in ("allowed", "unknown", "forbidden"):
            raise fail(R_LOCATOR_INVALID, f"未知 license: {self.license!r}")
        if self.evidenced_critical < 0 or self.critical_total < 0:
            raise fail(R_LOCATOR_INVALID, "证据计数不得为负")
        if self.evidenced_critical > self.critical_total:
            raise fail(R_LOCATOR_INVALID, "有效证据数不得超过关键事实数")


@dataclass(frozen=True, slots=True)
class QualityScore:
    """评分结果：总分 + 算法版本 + 逐项构成。"""

    total: int
    algorithm: str
    components: tuple[QualityComponent, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "algorithm": self.algorithm,
            "components": [component.as_dict() for component in self.components],
        }

    def component(self, name: str) -> QualityComponent:
        for item in self.components:
            if item.name == name:
                return item
        raise KeyError(name)


def _authority_value(domain: str) -> int:
    host = (domain or "").lower().strip(".")
    if host in AUTHORITY_EXACT:
        return AUTHORITY_EXACT[host]
    for suffix, value in AUTHORITY_SUFFIX:
        if host.endswith(suffix):
            return value
    return 8


def _freshness_value(observed_at: datetime, published_at: datetime | None) -> int:
    if published_at is None:
        # 没有发布时间不是"新"，也不是"旧"：给中间偏低的常量，并保持确定性。
        return 4
    if published_at > observed_at:
        # 未来发布时间 = 不可信时间戳；不奖励，也不抛错（记录分项即可）。
        return 0
    age_days = (observed_at - published_at).days
    if age_days <= 7:
        return 20
    if age_days <= 30:
        return 14
    if age_days <= 180:
        return 8
    if age_days <= 365:
        return 4
    return 0


def _evidence_value(evidenced_critical: int, critical_total: int) -> int:
    if critical_total <= 0:
        return 0
    return (QUALITY_WEIGHTS["evidence"] * evidenced_critical) // critical_total


def score_quality(item: QualityInput) -> QualityScore:
    """确定性评分（0–100）。

    构成（写进 ``quality_components``，可被测试逐项复算）：

    * authority（35）：精确域名表 > 后缀表 > 未知 8 分；
    * primacy（20）：一手证据 20 分；**转载/聚合站一手标记归零**，防止转载冒充一手；
    * freshness（20）：按发布时间距观测时间的整数天数分桶；发布时间未知记 4 分；
    * license（10）：allowed 10 / unknown 4 / forbidden 0；
    * evidence（15）：有效证据覆盖关键事实的比例，整数下取整。

    全整数运算、无浮点、无随机、无 wall clock（时间由调用方传入），因此同一输入
    两次评分逐字段相同。
    """

    authority = QualityComponent(
        name="authority",
        weight=QUALITY_WEIGHTS["authority"],
        value=_authority_value(item.domain),
        contribution=_authority_value(item.domain),
    )
    primacy_value = 0
    if item.is_primary and not item.is_reprint:
        primacy_value = QUALITY_WEIGHTS["primacy"]
    primacy = QualityComponent(
        name="primacy",
        weight=QUALITY_WEIGHTS["primacy"],
        value=primacy_value,
        contribution=primacy_value,
    )
    freshness_value = _freshness_value(item.observed_at, item.published_at)
    freshness = QualityComponent(
        name="freshness",
        weight=QUALITY_WEIGHTS["freshness"],
        value=freshness_value,
        contribution=freshness_value,
    )
    license_value = {"allowed": 10, "unknown": 4, "forbidden": 0}[item.license]
    license_component = QualityComponent(
        name="license",
        weight=QUALITY_WEIGHTS["license"],
        value=license_value,
        contribution=license_value,
    )
    evidence_values = _evidence_value(item.evidenced_critical, item.critical_total)
    evidence = QualityComponent(
        name="evidence",
        weight=QUALITY_WEIGHTS["evidence"],
        value=evidence_values,
        contribution=evidence_values,
    )
    components = (authority, primacy, freshness, license_component, evidence)
    total = sum(component.contribution for component in components)
    return QualityScore(total=total, algorithm=QUALITY_ALGORITHM, components=components)


# ---------------------------------------------------------------------------
# 输入数据结构
# ---------------------------------------------------------------------------
def _require_uuid(value: Any, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise fail(R_LOCATOR_INVALID, f"{name} 必须是 UUID，得到 {type(value).__name__}")
    return value


def _require_hash(value: str, name: str) -> str:
    if not _HEX64.match(str(value)):
        raise fail(R_LOCATOR_INVALID, f"{name} 必须是 64 位小写 hex")
    return str(value)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """待注册来源（``SourceSpec.create`` 负责派生 canonical/hash/domain）。"""

    workspace_id: UUID
    source_id: UUID
    canonical_url: str
    url_hash: str
    domain: str
    source_type: str
    license: str
    is_primary: bool
    is_reprint: bool
    channel: str
    fetch_policy: str
    content_hash: str
    published_at: datetime | None
    first_seen_at: datetime
    expires_at: datetime | None

    @classmethod
    def create(
        cls,
        workspace_id: UUID,
        url: str,
        *,
        source_type: str = "url",
        license: str = "allowed",
        is_primary: bool = True,
        is_reprint: bool = False,
        channel: str = "http",
        fetch_policy: str = "allow",
        content: str | bytes = b"",
        published_at: datetime | None = None,
        first_seen_at: datetime | None = None,
        expires_at: datetime | None = None,
        source_id: UUID | None = None,
    ) -> SourceSpec:
        if source_type not in ("url", "upload"):
            raise fail(R_LOCATOR_INVALID, f"未知 source_type: {source_type!r}")
        if license not in ("allowed", "unknown", "forbidden"):
            raise fail(R_LOCATOR_INVALID, f"未知 license: {license!r}")
        if channel not in ("http", "browser", "upload"):
            raise fail(R_LOCATOR_INVALID, f"未知 channel: {channel!r}")
        if fetch_policy not in ("allow", "deny", "not_checked"):
            raise fail(R_LOCATOR_INVALID, f"未知 fetch_policy: {fetch_policy!r}")
        canonical = canonical_url(url)
        seen = first_seen_at or datetime.now(UTC)
        return cls(
            workspace_id=_require_uuid(workspace_id, "workspace_id"),
            source_id=source_id or uuid4(),
            canonical_url=canonical,
            url_hash=url_hash(canonical),
            domain=domain_of(canonical),
            source_type=source_type,
            license=license,
            is_primary=is_primary,
            is_reprint=is_reprint,
            channel=channel,
            fetch_policy=fetch_policy,
            content_hash=content_hash(content),
            published_at=published_at,
            first_seen_at=seen,
            expires_at=expires_at,
        )


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """已注册来源的只读投影（含当前状态）。"""

    workspace_id: UUID
    source_id: UUID
    canonical_url: str
    url_hash: str
    domain: str
    license: str
    is_primary: bool
    is_reprint: bool
    channel: str
    fetch_policy: str
    content_hash: str
    state: str


@dataclass(frozen=True, slots=True)
class FactSpec:
    """候选内的结构化关键事实（只存指纹，不存正文）。"""

    fact_id: str
    text: str
    is_critical: bool = True

    def __post_init__(self) -> None:
        if not _FACT_ID.match(self.fact_id):
            raise fail(R_LOCATOR_INVALID, f"非法 fact_id: {self.fact_id!r}")
        if not str(self.text).strip():
            raise fail(R_LOCATOR_INVALID, f"事实 {self.fact_id!r} 正文为空")

    @property
    def fact_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceSpec:
    """逐事实证据：T11 locator + 引文指纹 + 观测/失效时间。"""

    fact_id: str
    locator: Mapping[str, Any]
    quote: str
    observed_at: datetime
    expires_at: datetime | None = None
    #: deny-by-default：调用方必须显式声明"这条证据已核验"，不得靠缺省值获得可信。
    verified: bool = False
    source_version_id: UUID | None = None

    def __post_init__(self) -> None:
        if not _FACT_ID.match(self.fact_id):
            raise fail(R_LOCATOR_INVALID, f"非法 fact_id: {self.fact_id!r}")
        if len(str(self.quote)) > 4000:
            raise fail(R_LOCATOR_INVALID, "证据引文超过 4000 字符上限")
        if self.expires_at is not None and self.expires_at <= self.observed_at:
            raise fail(R_LOCATOR_INVALID, "证据 expires_at 必须晚于 observed_at")

    @property
    def normalised_locator(self) -> dict[str, Any]:
        return normalise_locator(self.locator)

    @property
    def quote_hash(self) -> str:
        return quote_hash(self.quote)


def normalise_locator(locator: Mapping[str, Any]) -> dict[str, Any]:
    """用 T11 ``locator_map.Locator`` 严格校验并序列化 locator。

    直接复用 T11 的 schema（kind/format + 该 kind 使用的字段、禁止偏移字段），
    避免本层再造一套定位符定义。
    """

    if not isinstance(locator, Mapping):
        raise fail(R_LOCATOR_INVALID, "locator 必须是 mapping")
    try:
        return locator_map.Locator.from_dict(locator).to_dict()
    except locator_map.LocatorError as exc:
        raise fail(R_LOCATOR_INVALID, f"locator 非法: {exc}", locator=dict(locator)) from exc


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    """一次受控采集 + 抽取的完整输入（提交后即成不可变候选事实）。"""

    workspace_id: UUID
    source_id: UUID
    source_version_id: UUID
    content: str
    facts: tuple[FactSpec, ...]
    evidence: tuple[EvidenceSpec, ...] = ()
    origin: str = "ingest"
    run_id: UUID | None = None
    extraction_profile: str = "t27-profile-v1"
    canonical_url_override: str | None = None

    def __post_init__(self) -> None:
        if self.origin not in ORIGINS:
            raise fail(R_LOCATOR_INVALID, f"未知 origin: {self.origin!r}")
        if not self.facts:
            raise fail(R_FACTS_MISSING, "候选必须至少包含 1 条结构化事实")
        known = {fact.fact_id for fact in self.facts}
        for item in self.evidence:
            if item.fact_id not in known:
                raise fail(
                    R_LOCATOR_INVALID,
                    f"证据指向不存在的事实 {item.fact_id!r}",
                )

    @property
    def content_hash(self) -> str:
        return content_hash(self.content)

    @property
    def extraction_profile_hash(self) -> str:
        return hashlib.sha256(self.extraction_profile.encode()).hexdigest()

    @property
    def critical_facts(self) -> tuple[FactSpec, ...]:
        return tuple(fact for fact in self.facts if fact.is_critical)


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    """候选提交结果（重复投递时 ``created=False`` 且计数不变）。"""

    candidate_id: UUID
    created: bool
    status: str
    reason_code: str
    quality: QualityScore
    cluster_id: UUID
    duplicate_of: UUID | None
    near_duplicate_of: UUID | None
    near_duplicate_distance: int | None
    detail: str = ""

    @property
    def needs_review(self) -> bool:
        return self.status == "needs_review"


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """人工审阅请求：身份 + 时间 + 理由三要素 + 目标 KB。

    ``kb_id`` 必须在决策时确定：发布时 ``ReviewDecision.kb_id`` 必须等于发布目标，
    否则会出现「批准到 A 库、发布到 B 库」的越权路径。
    """

    workspace_id: UUID
    candidate_id: UUID
    reviewer_principal_id: UUID
    kb_id: UUID
    decision: str
    reason_code: str
    reason_text: str
    decided_at: datetime

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise fail(R_LOCATOR_INVALID, f"未知 decision: {self.decision!r}")


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    review_decision_id: UUID
    candidate_id: UUID
    decision: str
    created: bool
    status: str


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    promotion_id: UUID
    candidate_id: UUID
    review_decision_id: UUID
    kb_id: UUID
    evidence_count: int
    created: bool


@dataclass(frozen=True, slots=True)
class NeedsReviewSignal:
    """T24 ``needs_review`` 的出参（本层只认 ``REPLAN_NEEDS_REVIEW``）。"""

    code: str
    run_id: UUID
    task_id: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReviewTicket:
    """人工评审单：``requires_human`` 恒为 True——本层没有自动出口。"""

    ticket_id: UUID
    candidate_id: UUID
    code: str
    requires_human: bool = True


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    """候选当前状态的只读快照（状态由事件推导，不在表里改）。"""

    candidate_id: UUID
    workspace_id: UUID
    state: str
    source_state: str
    quality_total: int
    critical_facts: int
    valid_evidence: int
    approved_decision: UUID | None
    promotion_id: UUID | None


@dataclass(frozen=True, slots=True)
class _Gate:
    """准入闸门结论（内部类型；不进公开 API）。"""

    decision: str
    reason_code: str
    detail: str = ""
    fact_ids: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 数据库连接与迁移
# ---------------------------------------------------------------------------
def connect(dsn: str | None = None) -> psycopg.Connection[Any]:
    """连接实验库；DSN 只经 T03 的 ``scope.read_dsn()`` 读取，不落日志。"""

    if dsn is None:
        import scope  # 延迟导入：T03 的 DSN 读取是唯一凭据来源

        dsn = scope.read_dsn()
    return psycopg.connect(dsn, row_factory=dict_row)


def _ensure_clean(conn: psycopg.Connection[Any]) -> None:
    """调用方遗留只读事务时 ``transaction()`` 会退化成 SAVEPOINT（T23 教训）。"""

    if conn.info.transaction_status != TransactionStatus.IDLE:
        conn.commit()


def _migration_bytes() -> bytes:
    return MIGRATION_PATH.read_bytes()


def migration_digest() -> str:
    """迁移文件字节的 sha256（台账与证据用它复验，而不是信任文件名）。"""

    return hashlib.sha256(_migration_bytes()).hexdigest()


def _ledger_exists(conn: psycopg.Connection[Any]) -> bool:
    row = conn.execute(
        "select to_regclass(%s) as oid", (f"{SCHEMA}.phase0_schema_migrations",)
    ).fetchone()
    return row is not None and row["oid"] is not None


def ensure_schema(conn: psycopg.Connection[Any]) -> str:
    """幂等应用 ``007_candidates.sql``，返回台账里的 sha256。

    首次应用：执行迁移 + 写台账一行。
    重复应用：只核对台账 sha256 与当前文件一致；不一致直接失败（不静默漂移）。
    """

    _ensure_clean(conn)
    digest = migration_digest()
    try:
        with conn.transaction():
            if _ledger_exists(conn):
                row = conn.execute(
                    f"select sha256 from {SCHEMA}.phase0_schema_migrations "
                    "where migration_id = %s",
                    (MIGRATION_ID,),
                ).fetchone()
                if row is None or row["sha256"] != digest:
                    raise fail(
                        R_MIGRATION_DIGEST_MISMATCH,
                        "台账记录的迁移摘要与磁盘文件不一致（拒绝静默漂移）",
                        recorded=None if row is None else row["sha256"],
                        current=digest,
                    )
                return digest
            conn.execute(_migration_bytes().decode("utf-8"))
            conn.execute(
                f"insert into {SCHEMA}.phase0_schema_migrations "
                "(migration_id, filename, sha256) values (%s, %s, %s)",
                (MIGRATION_ID, MIGRATION_PATH.name, digest),
            )
            return digest
    finally:
        _ensure_clean(conn)


def reset_schema(conn: psycopg.Connection[Any]) -> None:
    """仅供测试清理：删除本 schema 的全部对象。"""

    _ensure_clean(conn)
    try:
        with conn.transaction():
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    finally:
        _ensure_clean(conn)


# ---------------------------------------------------------------------------
# 会话与时间
# ---------------------------------------------------------------------------
def _db_now(conn: psycopg.Connection[Any]) -> datetime:
    row = conn.execute(f"select {SCHEMA}.phase0_now() as now").fetchone()
    assert row is not None
    return row["now"]


def _pin_now(conn: psycopg.Connection[Any], at: datetime) -> None:
    """把本 schema 的"现在"钉在 ``at``（事务内本地 GUC，仅影响有效性判定）。"""

    conn.execute("select set_config('p0_t27.now', %s, true)", (at.isoformat(),))


def evidence_is_valid(spec: EvidenceSpec, at: datetime) -> bool:
    """与 SQL ``phase0_evidence_is_valid`` 同口径的有效证据判定。"""

    if not spec.verified:
        return False
    if spec.observed_at > at:
        return False
    return spec.expires_at is None or spec.expires_at > at


def _record(row: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if row is None:
        raise fail(R_CANDIDATE_MISSING, f"{name} 不存在")
    return row


# ---------------------------------------------------------------------------
# 存储层：来源 / 候选 / 审阅 / 发布
# ---------------------------------------------------------------------------
class CandidateReviewStore:
    """P0-T27 的唯一写入口。

    所有写操作都在单个 ``transaction()`` 内完成；跨表不变量（候选+事实+证据+事件
    同时出现）靠事务保证，被拒的提交不会留下半份候选。
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    # -- 来源 ---------------------------------------------------------------
    def register_source(self, spec: SourceSpec) -> SourceRecord:
        """注册来源（workspace+url 唯一）；重复注册返回既有记录。

        只对**首次**注册追加 ``captured`` 事件，重复调用不产生事件噪声。
        """

        with self._conn.transaction():
            inserted = self._conn.execute(
                f"""
                insert into {SCHEMA}.phase0_sources (
                    workspace_id, source_id, canonical_url, url_hash, domain,
                    source_type, license, is_primary, is_reprint, channel,
                    fetch_policy, content_hash, published_at, first_seen_at, expires_at
                ) values (
                    %(workspace_id)s, %(source_id)s, %(canonical_url)s, %(url_hash)s,
                    %(domain)s, %(source_type)s, %(license)s, %(is_primary)s,
                    %(is_reprint)s, %(channel)s, %(fetch_policy)s, %(content_hash)s,
                    %(published_at)s, %(first_seen_at)s, %(expires_at)s
                )
                on conflict (workspace_id, url_hash) do nothing
                returning source_id
                """,
                {
                    "workspace_id": spec.workspace_id,
                    "source_id": spec.source_id,
                    "canonical_url": spec.canonical_url,
                    "url_hash": spec.url_hash,
                    "domain": spec.domain,
                    "source_type": spec.source_type,
                    "license": spec.license,
                    "is_primary": spec.is_primary,
                    "is_reprint": spec.is_reprint,
                    "channel": spec.channel,
                    "fetch_policy": spec.fetch_policy,
                    "content_hash": spec.content_hash,
                    "published_at": spec.published_at,
                    "first_seen_at": spec.first_seen_at,
                    "expires_at": spec.expires_at,
                },
            ).fetchone()
            if inserted is not None:
                self._append_source_event(
                    spec.workspace_id,
                    spec.source_id,
                    "captured",
                    actor_kind="service",
                    actor_ref="collector",
                    reason_code="SOURCE_CAPTURED",
                )
        return self.source(spec.workspace_id, spec.url_hash)

    def source(self, workspace_id: UUID, url_digest: str) -> SourceRecord:
        row = self._conn.execute(
            f"""
            select workspace_id, source_id, canonical_url, url_hash, domain, license,
                   is_primary, is_reprint, channel, fetch_policy, content_hash,
                   {SCHEMA}.phase0_source_state(workspace_id, source_id) as state
            from {SCHEMA}.phase0_sources
            where workspace_id = %s and url_hash = %s
            """,
            (workspace_id, url_digest),
        ).fetchone()
        record = _record(row, "来源")
        return SourceRecord(
            workspace_id=record["workspace_id"],
            source_id=record["source_id"],
            canonical_url=record["canonical_url"],
            url_hash=record["url_hash"],
            domain=record["domain"],
            license=record["license"],
            is_primary=record["is_primary"],
            is_reprint=record["is_reprint"],
            channel=record["channel"],
            fetch_policy=record["fetch_policy"],
            content_hash=record["content_hash"],
            state=record["state"],
        )

    def source_state(self, workspace_id: UUID, source_id: UUID) -> str:
        row = self._conn.execute(
            f"select {SCHEMA}.phase0_source_state(%s, %s) as state",
            (workspace_id, source_id),
        ).fetchone()
        assert row is not None
        return str(row["state"])

    def append_source_event(
        self,
        workspace_id: UUID,
        source_id: UUID,
        event_type: str,
        *,
        actor_kind: str,
        actor_ref: str,
        reason_code: str,
        detail: str = "",
    ) -> UUID:
        if event_type not in SOURCE_EVENTS:
            raise fail(R_LOCATOR_INVALID, f"未知来源事件: {event_type!r}")
        with self._conn.transaction():
            return self._append_source_event(
                workspace_id,
                source_id,
                event_type,
                actor_kind=actor_kind,
                actor_ref=actor_ref,
                reason_code=reason_code,
                detail=detail,
            )

    def _append_source_event(
        self,
        workspace_id: UUID,
        source_id: UUID,
        event_type: str,
        *,
        actor_kind: str,
        actor_ref: str,
        reason_code: str,
        detail: str = "",
    ) -> UUID:
        event_id = uuid4()
        self._conn.execute(
            f"""
            insert into {SCHEMA}.phase0_source_events (
                workspace_id, source_id, event_id, event_type, actor_kind,
                actor_ref, reason_code, detail
            ) values (%s, %s, %s, %s, %s, %s, %s, nullif(%s, ''))
            """,
            (
                workspace_id,
                source_id,
                event_id,
                event_type,
                actor_kind,
                actor_ref,
                reason_code,
                detail,
            ),
        )
        return event_id

    # -- 候选准入 -----------------------------------------------------------
    def submit_candidate(
        self, draft: CandidateDraft, *, at: datetime | None = None
    ) -> SubmissionOutcome:
        """提交候选：硬拒绝抛错（不落库），软拒绝落 ``needs_review``。

        硬拒绝（**不**留候选，避免同内容被永久锁死）：
        license=forbidden、fetch_policy=deny、域名黑名单、PII、无关键事实、
        关键事实缺可用证据。
        """

        with self._conn.transaction():
            moment = at or _db_now(self._conn)
            if at is not None:
                _pin_now(self._conn, at)
            source_row = self._conn.execute(
                f"""
                select s.*, {SCHEMA}.phase0_source_state(s.workspace_id, s.source_id) as state
                from {SCHEMA}.phase0_sources as s
                where s.workspace_id = %s and s.source_id = %s
                """,
                (draft.workspace_id, draft.source_id),
            ).fetchone()
            if source_row is None:
                raise fail(
                    R_SOURCE_MISSING,
                    "候选必须挂已注册来源（缺来源不得入库）",
                    source_id=str(draft.source_id),
                )

            canonical = canonical_url(
                draft.canonical_url_override or str(source_row["canonical_url"])
            )
            digest_url = url_hash(canonical)
            digest_content = draft.content_hash
            quality = self._score(draft, source_row, moment)
            hard = self._hard_gate(draft, source_row, quality, moment)
            if hard.decision == "rejected":
                raise fail(
                    hard.reason_code,
                    hard.detail or "候选被准入闸门拒绝",
                    source_id=str(draft.source_id),
                    fact_ids=list(hard.fact_ids),
                )

            cluster_id, duplicate_of, near_of, near_distance = self._resolve_duplicates(
                draft, digest_content
            )
            reason_code, detail = self._soft_gate(
                draft, quality, near_of, source_row, hard
            )
            candidate_id = uuid4()
            inserted = self._conn.execute(
                f"""
                insert into {SCHEMA}.phase0_candidates (
                    workspace_id, candidate_id, source_id, source_version_id, run_id,
                    canonical_url, url_hash, content_hash, extraction_profile_hash,
                    dedup_key, cluster_id, duplicate_of_candidate_id, is_duplicate,
                    simhash64, near_duplicate_of, near_duplicate_distance,
                    quality_score, quality_components, quality_algorithm,
                    evidence_set_hash, critical_fact_count, origin, fetch_policy
                ) values (
                    %(workspace_id)s, %(candidate_id)s, %(source_id)s,
                    %(source_version_id)s, %(run_id)s, %(canonical_url)s, %(url_hash)s,
                    %(content_hash)s, %(profile_hash)s, %(dedup_key)s, %(cluster_id)s,
                    %(duplicate_of)s, %(is_duplicate)s, %(simhash)s, %(near_of)s,
                    %(near_distance)s, %(quality)s, %(components)s, %(algorithm)s,
                    %(evidence_hash)s, %(critical)s, %(origin)s, %(fetch_policy)s
                )
                on conflict do nothing
                returning candidate_id
                """,
                {
                    "workspace_id": draft.workspace_id,
                    "candidate_id": candidate_id,
                    "source_id": draft.source_id,
                    "source_version_id": draft.source_version_id,
                    "run_id": draft.run_id,
                    "canonical_url": canonical,
                    "url_hash": digest_url,
                    "content_hash": digest_content,
                    "profile_hash": draft.extraction_profile_hash,
                    "dedup_key": dedup_key(digest_url, digest_content),
                    "cluster_id": cluster_id,
                    "duplicate_of": duplicate_of,
                    "is_duplicate": duplicate_of is not None,
                    "simhash": simhash64(draft.content),
                    "near_of": near_of,
                    "near_distance": near_distance,
                    "quality": quality.total,
                    "components": json.dumps(quality.as_dict(), sort_keys=True),
                    "algorithm": quality.algorithm,
                    "evidence_hash": self._evidence_set_hash(draft),
                    "critical": len(draft.critical_facts),
                    "origin": draft.origin,
                    "fetch_policy": source_row["fetch_policy"],
                },
            ).fetchone()

            if inserted is None:
                existing = self._existing_candidate(
                    draft.workspace_id, draft.source_id, digest_content
                )
                return SubmissionOutcome(
                    candidate_id=existing["candidate_id"],
                    created=False,
                    status=str(existing["state"]),
                    reason_code=R_DUPLICATE_CONTENT,
                    quality=quality,
                    cluster_id=existing["cluster_id"],
                    duplicate_of=existing["duplicate_of_candidate_id"],
                    near_duplicate_of=existing["near_duplicate_of"],
                    near_duplicate_distance=existing["near_duplicate_distance"],
                    detail="同 source 同内容已存在候选；重复投递不新增",
                )

            self._insert_facts_and_evidence(candidate_id, draft)
            status = "needs_review" if reason_code in (
                R_SCORE_BELOW_THRESHOLD,
                R_NEAR_DUPLICATE,
                R_REPLAN_NEEDS_REVIEW,
            ) else "review_requested"
            self._append_candidate_event(
                draft.workspace_id,
                candidate_id,
                status,
                actor_kind="service",
                actor_ref="candidate_review",
                reason_code=reason_code,
                detail=detail,
                dedup_key=f"submit:{candidate_id}",
            )
            return SubmissionOutcome(
                candidate_id=candidate_id,
                created=True,
                status=status,
                reason_code=reason_code,
                quality=quality,
                cluster_id=cluster_id,
                duplicate_of=duplicate_of,
                near_duplicate_of=near_of,
                near_duplicate_distance=near_distance,
                detail=detail,
            )

    def _score(
        self,
        draft: CandidateDraft,
        source_row: Mapping[str, Any],
        moment: datetime,
    ) -> QualityScore:
        critical = draft.critical_facts
        evidenced = {
            fact.fact_id
            for fact in critical
            if any(
                item.fact_id == fact.fact_id and evidence_is_valid(item, moment)
                for item in draft.evidence
            )
        }
        return score_quality(
            QualityInput(
                domain=str(source_row["domain"]),
                is_primary=bool(source_row["is_primary"]),
                is_reprint=bool(source_row["is_reprint"]),
                license=str(source_row["license"]),
                observed_at=moment,
                published_at=source_row["published_at"],
                evidenced_critical=len(evidenced),
                critical_total=len(critical),
            )
        )

    def _hard_gate(
        self,
        draft: CandidateDraft,
        source_row: Mapping[str, Any],
        quality: QualityScore,
        moment: datetime,
    ) -> _Gate:
        """硬拒绝：任一命中即抛错，**不**因高分而放行。"""

        if str(source_row["license"]) == "forbidden":
            return _Gate("rejected", R_LICENSE_FORBIDDEN, "来源许可为 forbidden")
        if str(source_row["fetch_policy"]) == "deny":
            return _Gate("rejected", R_FETCH_DENIED, "T16 网络策略记录该来源被拒")
        host = str(source_row["domain"]).lower()
        if host in BLOCKED_DOMAINS:
            return _Gate("rejected", R_BLOCKED_DOMAIN, f"域名在黑名单：{host}")
        pii = detect_pii(draft.content)
        if pii:
            return _Gate(
                "rejected", R_PII_DETECTED, f"候选正文命中 PII：{','.join(pii)}"
            )
        gap_code, fact_ids = _evidence_gap(draft, moment)
        if gap_code is not None:
            return _Gate(
                "rejected",
                gap_code,
                f"关键事实缺少可用证据：{','.join(fact_ids)}",
                fact_ids=fact_ids,
            )
        return _Gate("pending", R_AWAITING_REVIEW, "")

    def _soft_gate(
        self,
        draft: CandidateDraft,
        quality: QualityScore,
        near_of: UUID | None,
        source_row: Mapping[str, Any],
        hard: _Gate,
    ) -> tuple[str, str]:
        if hard.decision == "rejected":
            return hard.reason_code, hard.detail
        if quality.total < MIN_AUTO_ADMIT_SCORE:
            return (
                R_SCORE_BELOW_THRESHOLD,
                f"质量分 {quality.total} < {MIN_AUTO_ADMIT_SCORE}，转人工",
            )
        if near_of is not None:
            return R_NEAR_DUPLICATE, f"与 {near_of} 近似重复（仅提示，不合并）"
        if draft.origin == "replan_needs_review":
            return R_REPLAN_NEEDS_REVIEW, "T24 needs_review 出口，必须人工处理"
        if str(source_row["state"]) != "approved":
            return (
                R_SOURCE_NOT_APPROVED,
                f"来源状态 {source_row['state']}，需人工确认后方可入库",
            )
        return R_AWAITING_REVIEW, "等待人工审阅"

    def _resolve_duplicates(
        self, draft: CandidateDraft, digest_content: str
    ) -> tuple[UUID, UUID | None, UUID | None, int | None]:
        """精确重复并簇 + 近似重复提示。

        * 同 workspace 同 content_hash 共享一个 cluster（UNIQUE 保证）；
        * 跨 source 的重复**保留各自候选**（TDD06 §2），只标 duplicate_of=链首；
        * 近似重复只写提示列，绝不自动合并。
        """

        existing_cluster = self._conn.execute(
            f"""
            select cluster_id from {SCHEMA}.phase0_duplicate_clusters
            where workspace_id = %s and content_hash = %s
            """,
            (draft.workspace_id, digest_content),
        ).fetchone()
        if existing_cluster is None:
            cluster_id = uuid4()
            self._conn.execute(
                f"""
                insert into {SCHEMA}.phase0_duplicate_clusters
                    (workspace_id, cluster_id, content_hash)
                values (%s, %s, %s)
                on conflict (workspace_id, content_hash) do nothing
                """,
                (draft.workspace_id, cluster_id, digest_content),
            )
            cluster_id = self._conn.execute(
                f"select cluster_id from {SCHEMA}.phase0_duplicate_clusters "
                "where workspace_id = %s and content_hash = %s",
                (draft.workspace_id, digest_content),
            ).fetchone()["cluster_id"]
        else:
            cluster_id = existing_cluster["cluster_id"]

        head = self._conn.execute(
            f"""
            select candidate_id from {SCHEMA}.phase0_candidates
            where workspace_id = %s and content_hash = %s
            order by created_at, candidate_id
            limit 1
            """,
            (draft.workspace_id, digest_content),
        ).fetchone()
        duplicate_of = None if head is None else head["candidate_id"]

        simhash = simhash64(draft.content)
        candidates = self._conn.execute(
            f"""
            select candidate_id, simhash64 from {SCHEMA}.phase0_candidates
            where workspace_id = %s and content_hash <> %s
            """,
            (draft.workspace_id, digest_content),
        ).fetchall()
        near_of: UUID | None = None
        near_distance: int | None = None
        for row in candidates:
            distance = hamming64(simhash, str(row["simhash64"]))
            if distance <= NEAR_DUPLICATE_MAX_DISTANCE and (
                near_distance is None or distance < near_distance
            ):
                near_of = row["candidate_id"]
                near_distance = distance
        return cluster_id, duplicate_of, near_of, near_distance

    def _existing_candidate(
        self, workspace_id: UUID, source_id: UUID, digest_content: str
    ) -> Mapping[str, Any]:
        row = self._conn.execute(
            f"""
            select c.candidate_id, c.cluster_id, c.duplicate_of_candidate_id,
                   c.near_duplicate_of, c.near_duplicate_distance,
                   {SCHEMA}.phase0_candidate_state(c.workspace_id, c.candidate_id) as state
            from {SCHEMA}.phase0_candidates as c
            where c.workspace_id = %s and c.source_id = %s and c.content_hash = %s
            """,
            (workspace_id, source_id, digest_content),
        ).fetchone()
        return _record(row, "候选")

    def _evidence_set_hash(self, draft: CandidateDraft) -> str:
        payload = [
            {
                "fact_id": item.fact_id,
                "locator": item.normalised_locator,
                "quote_hash": item.quote_hash,
                "source_version_id": str(item.source_version_id or draft.source_version_id),
            }
            for item in draft.evidence
        ]
        payload.sort(key=lambda entry: (entry["fact_id"], entry["quote_hash"]))
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _insert_facts_and_evidence(self, candidate_id: UUID, draft: CandidateDraft) -> None:
        for fact in draft.facts:
            self._conn.execute(
                f"""
                insert into {SCHEMA}.phase0_candidate_facts (
                    workspace_id, candidate_id, fact_id, fact_hash, is_critical
                ) values (%s, %s, %s, %s, %s)
                """,
                (
                    draft.workspace_id,
                    candidate_id,
                    fact.fact_id,
                    fact.fact_hash,
                    fact.is_critical,
                ),
            )
        for item in draft.evidence:
            self._conn.execute(
                f"""
                insert into {SCHEMA}.phase0_candidate_evidence (
                    workspace_id, evidence_id, candidate_id, fact_id, source_version_id,
                    locator, quote_hash, quote_length, observed_at, expires_at, verified
                ) values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                """,
                (
                    draft.workspace_id,
                    uuid4(),
                    candidate_id,
                    item.fact_id,
                    item.source_version_id or draft.source_version_id,
                    json.dumps(item.normalised_locator, sort_keys=True),
                    item.quote_hash,
                    len(item.quote),
                    item.observed_at,
                    item.expires_at,
                    item.verified,
                ),
            )

    # -- 候选事件/快照 -------------------------------------------------------
    def candidate_state(self, workspace_id: UUID, candidate_id: UUID) -> str:
        row = self._conn.execute(
            f"select {SCHEMA}.phase0_candidate_state(%s, %s) as state",
            (workspace_id, candidate_id),
        ).fetchone()
        assert row is not None
        return str(row["state"])

    def _append_candidate_event(
        self,
        workspace_id: UUID,
        candidate_id: UUID,
        event_type: str,
        *,
        actor_kind: str,
        actor_ref: str,
        reason_code: str,
        detail: str = "",
        dedup_key: str | None = None,
    ) -> UUID:
        if event_type not in EVENT_TYPES:
            raise fail(R_LOCATOR_INVALID, f"未知候选事件: {event_type!r}")
        event_id = uuid4()
        self._conn.execute(
            f"""
            insert into {SCHEMA}.phase0_candidate_events (
                workspace_id, candidate_id, event_id, event_type, actor_kind,
                actor_ref, reason_code, dedup_key, detail
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, nullif(%s, ''))
            on conflict (workspace_id, dedup_key) do nothing
            """,
            (
                workspace_id,
                candidate_id,
                event_id,
                event_type,
                actor_kind,
                actor_ref,
                reason_code,
                dedup_key,
                detail,
            ),
        )
        return event_id

    def history(self, workspace_id: UUID, candidate_id: UUID) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            f"""
            select event_id, event_type, actor_kind, actor_ref, reason_code, detail,
                   recorded_at
            from {SCHEMA}.phase0_candidate_events
            where workspace_id = %s and candidate_id = %s
            order by event_seq
            """,
            (workspace_id, candidate_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def decisions(self, workspace_id: UUID, candidate_id: UUID) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            f"""
            select review_decision_id, reviewer_principal_id, reviewer_kind, decision,
                   reason_code, reason_text, decided_at
            from {SCHEMA}.phase0_review_decisions
            where workspace_id = %s and candidate_id = %s
            order by decided_at, review_decision_id
            """,
            (workspace_id, candidate_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self, workspace_id: UUID, candidate_id: UUID) -> CandidateSnapshot:
        row = self._conn.execute(
            f"""
            select c.candidate_id, c.workspace_id, c.quality_score,
                   (select count(*) from {SCHEMA}.phase0_candidate_facts as f
                    where f.workspace_id = c.workspace_id and f.candidate_id = c.candidate_id
                      and f.is_critical) as critical_facts,
                   (select count(*) from {SCHEMA}.phase0_candidate_evidence as e
                    where e.workspace_id = c.workspace_id and e.candidate_id = c.candidate_id
                      and {SCHEMA}.phase0_evidence_is_valid(
                          e.verified, e.expires_at, e.observed_at))
                       as valid_evidence,
                   {SCHEMA}.phase0_candidate_state(c.workspace_id, c.candidate_id) as state,
                   {SCHEMA}.phase0_source_state(c.workspace_id, c.source_id) as source_state,
                   (select d.review_decision_id from {SCHEMA}.phase0_review_decisions as d
                    where d.workspace_id = c.workspace_id and d.candidate_id = c.candidate_id
                      and d.decision = 'approved' limit 1) as approved_decision,
                   (select p.promotion_id from {SCHEMA}.phase0_promotions as p
                    where p.workspace_id = c.workspace_id and p.candidate_id = c.candidate_id
                    limit 1) as promotion_id
            from {SCHEMA}.phase0_candidates as c
            where c.workspace_id = %s and c.candidate_id = %s
            """,
            (workspace_id, candidate_id),
        ).fetchone()
        record = _record(row, "候选")
        return CandidateSnapshot(
            candidate_id=record["candidate_id"],
            workspace_id=record["workspace_id"],
            state=record["state"],
            source_state=record["source_state"],
            quality_total=record["quality_score"],
            critical_facts=record["critical_facts"],
            valid_evidence=record["valid_evidence"],
            approved_decision=record["approved_decision"],
            promotion_id=record["promotion_id"],
        )

    def count_candidates(self, workspace_id: UUID) -> int:
        row = self._conn.execute(
            f"select count(*) as total from {SCHEMA}.phase0_candidates where workspace_id = %s",
            (workspace_id,),
        ).fetchone()
        assert row is not None
        return int(row["total"])

    # -- 人工审阅 -----------------------------------------------------------
    def submit_review(self, request: ReviewRequest) -> ReviewOutcome:
        """记录一条人工审阅决定；重复提交幂等，冲突拒绝。

        拦截顺序是刻意的（先身份、再要素、后冲突），保证错误码稳定：
        REVIEWER_UNKNOWN → REVIEWER_NOT_HUMAN → REVIEWER_DISABLED →
        REVIEW_REASON_REQUIRED → REVIEW_TIME_IN_FUTURE → DECISION_CONFLICT。
        """

        with self._conn.transaction():
            candidate = self._conn.execute(
                f"""
                select c.candidate_id, c.workspace_id,
                       {SCHEMA}.phase0_candidate_state(c.workspace_id, c.candidate_id) as state
                from {SCHEMA}.phase0_candidates as c
                where c.workspace_id = %s and c.candidate_id = %s
                """,
                (request.workspace_id, request.candidate_id),
            ).fetchone()
            record = _record(candidate, "候选")
            workspace_id = record["workspace_id"]

            principal = self._conn.execute(
                f"select kind, status from {SCHEMA}.phase0_principals "
                "where workspace_id = %s and principal_id = %s",
                (workspace_id, request.reviewer_principal_id),
            ).fetchone()
            if principal is None:
                raise fail(R_REVIEWER_UNKNOWN, "审阅主体不存在")
            if str(principal["kind"]) != "human":
                raise fail(
                    R_REVIEWER_NOT_HUMAN,
                    "只有人工主体可以产出 ReviewDecision",
                    kind=str(principal["kind"]),
                )
            if str(principal["status"]) != "active":
                raise fail(R_REVIEWER_DISABLED, "审阅主体已停用")
            if not str(request.reason_code).strip() or not str(request.reason_text).strip():
                raise fail(R_REASON_REQUIRED, "审批必须同时给出理由码与理由文本")
            if request.decided_at > datetime.now(UTC):
                raise fail(R_REVIEW_TIME_IN_FUTURE, "审批时间不得晚于当前时刻")

            existing = self._conn.execute(
                f"""
                select review_decision_id, decision, reviewer_principal_id, reason_code
                from {SCHEMA}.phase0_review_decisions
                where workspace_id = %s and candidate_id = %s
                """,
                (workspace_id, request.candidate_id),
            ).fetchall()
            for row in existing:
                same = (
                    row["reviewer_principal_id"] == request.reviewer_principal_id
                    and row["decision"] == request.decision
                    and row["reason_code"] == request.reason_code
                )
                if same:
                    return ReviewOutcome(
                        review_decision_id=row["review_decision_id"],
                        candidate_id=request.candidate_id,
                        decision=str(row["decision"]),
                        created=False,
                        status=str(record["state"]),
                    )
                if row["decision"] == "approved" and request.decision == "rejected":
                    raise fail(
                        R_DECISION_CONFLICT,
                        "已批准的候选不能改判为 rejected（回退请新建候选）",
                        approved_decision=str(row["review_decision_id"]),
                    )

            decision_id = uuid4()
            try:
                self._conn.execute(
                    f"""
                    insert into {SCHEMA}.phase0_review_decisions (
                        workspace_id, review_decision_id, candidate_id, source_version_id,
                        kb_id, reviewer_principal_id, reviewer_kind, decision,
                        reason_code, reason_text, decided_at
                    )
                    select %(workspace_id)s, %(decision_id)s, c.candidate_id,
                           c.source_version_id, %(kb_id)s, %(reviewer)s, 'human',
                           %(decision)s, %(reason_code)s, %(reason_text)s, %(decided_at)s
                    from {SCHEMA}.phase0_candidates as c
                    where c.workspace_id = %(workspace_id)s
                      and c.candidate_id = %(candidate_id)s
                    """,
                    {
                        "workspace_id": workspace_id,
                        "decision_id": decision_id,
                        "candidate_id": request.candidate_id,
                        "kb_id": request.kb_id,
                        "reviewer": request.reviewer_principal_id,
                        "decision": request.decision,
                        "reason_code": request.reason_code,
                        "reason_text": request.reason_text,
                        "decided_at": request.decided_at,
                    },
                )
            except psycopg.errors.UniqueViolation as exc:
                raise fail(
                    R_DECISION_CONFLICT,
                    "同一候选已存在 approved 决定（批准是终态）",
                ) from exc

            event_type = "approved" if request.decision == "approved" else "declined"
            self._append_candidate_event(
                workspace_id,
                request.candidate_id,
                event_type,
                actor_kind="human",
                actor_ref=str(request.reviewer_principal_id),
                reason_code=request.reason_code,
                detail=request.reason_text,
                dedup_key=f"review:{decision_id}",
            )
            return ReviewOutcome(
                review_decision_id=decision_id,
                candidate_id=request.candidate_id,
                decision=request.decision,
                created=True,
                status=event_type,
            )

    # -- 发布 ---------------------------------------------------------------
    def promote(
        self,
        workspace_id: UUID,
        candidate_id: UUID,
        *,
        review_decision_id: UUID,
        kb_id: UUID | None = None,
        at: datetime | None = None,
    ) -> PromotionOutcome:
        """把已批准候选发布入库；重复发布返回既有事实（不再新增行）。"""

        target_kb = kb_id or kb_id_for_workspace(workspace_id)
        with self._conn.transaction():
            if at is not None:
                _pin_now(self._conn, at)
            moment = at or _db_now(self._conn)
            snapshot_row = self._conn.execute(
                f"""
                select c.candidate_id, c.source_id,
                       {SCHEMA}.phase0_candidate_state(c.workspace_id, c.candidate_id) as state,
                       {SCHEMA}.phase0_source_state(c.workspace_id, c.source_id) as source_state
                from {SCHEMA}.phase0_candidates as c
                where c.workspace_id = %s and c.candidate_id = %s
                """,
                (workspace_id, candidate_id),
            ).fetchone()
            record = _record(snapshot_row, "候选")
            state = str(record["state"])
            source_state = str(record["source_state"])

            # 幂等优先：同一候选已发布时直接返回既有事实，不再评估后续闸门。
            existing = self._conn.execute(
                f"select promotion_id from {SCHEMA}.phase0_promotions "
                "where workspace_id = %s and candidate_id = %s",
                (workspace_id, candidate_id),
            ).fetchone()
            if existing is not None:
                return PromotionOutcome(
                    promotion_id=existing["promotion_id"],
                    candidate_id=candidate_id,
                    review_decision_id=review_decision_id,
                    kb_id=target_kb,
                    evidence_count=self._valid_evidence_count(workspace_id, candidate_id),
                    created=False,
                )

            # 审批三要素先于状态闸门：错误码要指向根因（未批准 / 不属于该候选 / KB 不符）。
            decision = self._conn.execute(
                f"""
                select review_decision_id, decision, candidate_id, kb_id
                from {SCHEMA}.phase0_review_decisions
                where workspace_id = %s and review_decision_id = %s
                """,
                (workspace_id, review_decision_id),
            ).fetchone()
            if decision is None:
                raise fail(R_DECISION_REQUIRED, "发布必须携带 review_decision_id")
            if str(decision["decision"]) != "approved":
                raise fail(R_DECISION_NOT_APPROVED, "只有 approved 决定可以发布")
            if decision["candidate_id"] != candidate_id:
                raise fail(R_DECISION_MISMATCH, "ReviewDecision 不属于该候选")
            if decision["kb_id"] != target_kb:
                raise fail(R_KB_MISMATCH, "ReviewDecision 的目标 KB 与发布目标不一致")

            if state in STATES_BLOCKING_PROMOTION:
                raise fail(
                    R_CANDIDATE_STATE_BLOCKED,
                    f"候选状态 {state} 禁止发布",
                    state=state,
                )
            if source_state in SOURCE_STATES_BLOCKING:
                code = {
                    "revoked": R_SOURCE_REVOKED,
                    "expired": R_SOURCE_EXPIRED,
                    "rejected": R_SOURCE_NOT_APPROVED,
                }[source_state]
                raise fail(
                    code,
                    f"来源状态 {source_state}，该来源下候选一律禁止发布",
                    source_state=source_state,
                )
            if source_state != "approved":
                raise fail(
                    R_SOURCE_NOT_APPROVED,
                    f"来源状态 {source_state}，需人工确认后方可发布",
                    source_state=source_state,
                )

            gap_code, fact_ids = self._evidence_gap_for(workspace_id, candidate_id, moment)
            if gap_code is not None:
                raise fail(
                    gap_code,
                    f"关键事实缺少有效证据：{','.join(fact_ids)}",
                    fact_ids=list(fact_ids),
                )

            count = self._valid_evidence_count(workspace_id, candidate_id)
            promotion_id = uuid4()
            self._conn.execute(
                f"""
                insert into {SCHEMA}.phase0_promotions (
                    workspace_id, promotion_id, candidate_id, review_decision_id,
                    kb_id, evidence_count, promoted_at
                ) values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    workspace_id,
                    promotion_id,
                    candidate_id,
                    review_decision_id,
                    target_kb,
                    count,
                    moment,
                ),
            )
            self._append_candidate_event(
                workspace_id,
                candidate_id,
                "promoted",
                actor_kind="service",
                actor_ref="candidate_review",
                reason_code="PROMOTED",
                dedup_key=f"promote:{candidate_id}",
            )
            return PromotionOutcome(
                promotion_id=promotion_id,
                candidate_id=candidate_id,
                review_decision_id=review_decision_id,
                kb_id=target_kb,
                evidence_count=count,
                created=True,
            )

    def _valid_evidence_count(self, workspace_id: UUID, candidate_id: UUID) -> int:
        row = self._conn.execute(
            f"""
            select count(*) as total from {SCHEMA}.phase0_candidate_evidence as e
            where e.workspace_id = %s and e.candidate_id = %s
              and {SCHEMA}.phase0_evidence_is_valid(e.verified, e.expires_at, e.observed_at)
            """,
            (workspace_id, candidate_id),
        ).fetchone()
        assert row is not None
        return int(row["total"])

    def _evidence_gap_for(
        self, workspace_id: UUID, candidate_id: UUID, moment: datetime
    ) -> tuple[str | None, tuple[str, ...]]:
        del moment  # 有效性由 SQL 的 phase0_now()（可被 SET LOCAL 钉住）判定
        rows = self._conn.execute(
            f"""
            select f.fact_id,
                   count(e.evidence_id) as total,
                   count(e.evidence_id) filter (where e.verified) as verified_count,
                   count(e.evidence_id) filter (
                       where {SCHEMA}.phase0_evidence_is_valid(
                           e.verified, e.expires_at, e.observed_at)
                   ) as valid_count
            from {SCHEMA}.phase0_candidate_facts as f
            left join {SCHEMA}.phase0_candidate_evidence as e
              on e.workspace_id = f.workspace_id
             and e.candidate_id = f.candidate_id
             and e.fact_id = f.fact_id
            where f.workspace_id = %s and f.candidate_id = %s and f.is_critical
            group by f.fact_id
            order by f.fact_id
            """,
            (workspace_id, candidate_id),
        ).fetchall()
        missing: list[str] = []
        unverified: list[str] = []
        expired: list[str] = []
        for row in rows:
            if int(row["valid_count"]) > 0:
                continue
            missing.append(str(row["fact_id"]))
            if int(row["verified_count"]) == 0:
                unverified.append(str(row["fact_id"]))
            else:
                expired.append(str(row["fact_id"]))
        if not missing:
            return None, ()
        if len(unverified) == len(missing):
            return R_EVIDENCE_UNVERIFIABLE, tuple(missing)
        if len(expired) == len(missing):
            return R_EVIDENCE_EXPIRED, tuple(missing)
        return R_EVIDENCE_MISSING, tuple(missing)

    # -- T24 桥接 -----------------------------------------------------------
    def open_review_from_replan(
        self, workspace_id: UUID, candidate_id: UUID, signal: NeedsReviewSignal
    ) -> ReviewTicket:
        """把 T24 的 ``needs_review`` 转成人工评审单。

        只认 ``REPLAN_NEEDS_REVIEW``；其它码（如 ``REPLAN_APPLIED``）一律拒绝——
        否则自动重规划就能冒充人工出口。
        """

        if signal.code != CODE_NEEDS_REVIEW:
            raise fail(
                R_AUTO_REPLAN_BYPASS,
                f"只接受 {CODE_NEEDS_REVIEW}，得到 {signal.code!r}",
                code=signal.code,
            )
        with self._conn.transaction():
            row = self._conn.execute(
                f"""
                select candidate_id, origin from {SCHEMA}.phase0_candidates
                where workspace_id = %s and candidate_id = %s
                """,
                (workspace_id, candidate_id),
            ).fetchone()
            record = _record(row, "候选")
            if str(record["origin"]) != "replan_needs_review":
                raise fail(
                    R_NOT_FROM_REPLAN,
                    "该候选不来自 T24 needs_review 出口",
                    origin=str(record["origin"]),
                )
            ticket_id = self._append_candidate_event(
                workspace_id,
                candidate_id,
                "needs_review",
                actor_kind="machine",
                actor_ref="replanner",
                reason_code=signal.code,
                detail=signal.reason,
                dedup_key=f"replan:{signal.run_id}:{signal.task_id}",
            )
            return ReviewTicket(
                ticket_id=ticket_id,
                candidate_id=candidate_id,
                code=signal.code,
            )


#: 合成 KB id（与 workspace 一一对应；生产由调用方显式给出 target KB）。
def kb_id_for_workspace(workspace_id: UUID) -> UUID:
    """确定性合成 KB id：``uuid5(workspace, 'kb')``，避免测试里散落魔法常量。"""

    return kb_uuid(workspace_id, "kb")


def kb_uuid(namespace: UUID, name: str) -> UUID:
    from uuid import NAMESPACE_URL, uuid5

    return uuid5(NAMESPACE_URL, f"p0-t27:{namespace}:{name}")


# ---------------------------------------------------------------------------
# 内部：证据缺口（提交期的纯函数版本，与 SQL 同口径）
# ---------------------------------------------------------------------------
def _evidence_gap(
    draft: CandidateDraft, moment: datetime
) -> tuple[str | None, tuple[str, ...]]:
    """按关键事实检查证据：缺失 / 全部未核验 / 全部过期 三分类。

    分类顺序是保守的：**没有任何证据行**记 ``EVIDENCE_MISSING``（不是"未核验"），
    只有"有行但全未核验"才算 ``EVIDENCE_UNVERIFIABLE``；混合缺口统一记为
    ``EVIDENCE_MISSING``，避免把"根本没做证据"粉饰成"证据待核验"。
    """

    gaps: list[str] = []
    unverified: list[str] = []
    expired: list[str] = []
    for fact in draft.critical_facts:
        items = [item for item in draft.evidence if item.fact_id == fact.fact_id]
        if any(evidence_is_valid(item, moment) for item in items):
            continue
        gaps.append(fact.fact_id)
        if not items:
            continue
        if all(not item.verified for item in items):
            unverified.append(fact.fact_id)
        elif all(
            item.expires_at is not None and item.expires_at <= moment for item in items
        ):
            expired.append(fact.fact_id)
    if not gaps:
        return None, ()
    if len(unverified) == len(gaps):
        return R_EVIDENCE_UNVERIFIABLE, tuple(gaps)
    if len(expired) == len(gaps):
        return R_EVIDENCE_EXPIRED, tuple(gaps)
    return R_EVIDENCE_MISSING, tuple(gaps)


# ---------------------------------------------------------------------------
# 内部：追加史复验清单（全部只允许 INSERT，供测试逐表验证）
# ---------------------------------------------------------------------------
#: 供测试复验「追加史」约束用的目标表名（全部只允许 INSERT）。
APPEND_ONLY_TABLES: Final = (
    "phase0_sources",
    "phase0_source_events",
    "phase0_duplicate_clusters",
    "phase0_candidates",
    "phase0_candidate_facts",
    "phase0_candidate_evidence",
    "phase0_candidate_events",
    "phase0_review_decisions",
    "phase0_promotions",
)
