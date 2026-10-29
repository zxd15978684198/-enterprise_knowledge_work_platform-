"""P0-T28 编排层：Research Graph（LangGraph）+ 三读取通道 + 预算/审批/终态。

分工：``research_probe`` 拥有 schema ``p0_t28`` 的事实写入（run/来源/证据/候选/审批/Job/事件）；
本模块只做**控制流与 port 组合**：

* Planner（T22）+ 依赖调度（T23）→ DAG 分解与 ready 任务领取；
* 三读取通道：本地固定搜索、T04 控制站 HTTP、B-SK JS 适配（未开 ``BSK_LIVE`` 时用确定性替身）、
  用户浏览器快照适配；
* 审批：LangGraph 原生 ``interrupt()`` 暂停，``Command(resume=...)`` 恢复；审批决策落
  ``p0_t28.phase0_approvals``，执行前用 T19 ``tool_registry.assert_executable`` 复核
  intent/审批人/过期/撤销；
* 预算：T20 ``budget_ledger`` 预留-结算；额度不足 → 终态 ``BUDGET_EXCEEDED``，不发布任何候选；
* 终态：``research_probe.finalize_run`` 在一次事务里写 run 终态 + 唯一 terminal 事件。

限域：没有真实 chat 模型，``ModelPort`` 是确定性替身；``BSK_LIVE`` 未开时 JS 通道用替身，
报告中一律标 NOT_RUN，不冒充真机。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, TypedDict

import psycopg

import budget_ledger
import control_site
import dag_contracts
import planner_probe
import research_probe as rp
import task_scheduler as ts
import tool_registry as tr
from contracts import Principal, ResourceRef, WorkspaceScope, issue_scope

GRAPH_VERSION: Final = "research.v1"
REVISION: Final = 1
MAX_GRAPH_STEPS: Final = 64
DEFAULT_TOP_K: Final = 3
EXTRACT_MAX_CHARS: Final = 900
ANSWER_MAX_QUOTE: Final = 120
EXTERNAL_IDEM_PREFIX: Final = "t28-research"
PLAN_SCHEMA_VERSION: Final = 1
APPROVAL_TTL_SECONDS: Final = 900

TAG_RE: Final = re.compile(r"<[^>]+>")
WHITESPACE_RE: Final = re.compile(r"\s+")

#: 检索/抓取用的本地固定语料（真实搜索 API 一律 NOT_RUN；ADR/契约未定来源）。
LOCAL_CORPUS: Final[tuple[dict[str, str], ...]] = (
    {
        "url": "https://research.example.test/handbook/travel",
        "license": "CC-BY-4.0",
        "body": (
            "差旅报销标准：一线城市住宿上限 600 元，需要发票与行程单。"
            "差旅审批由部门负责人完成。"
        ),
    },
    {
        "url": "https://research.example.test/handbook/expense",
        "license": "CC-BY-4.0",
        "body": (
            "费用报销流程：提交报销单、附发票、部门审批、财务复核；"
            "单笔超过 5000 元需二级审批。"
        ),
    },
    {
        "url": "https://research.example.test/policy/security",
        "license": "CC-BY-SA-4.0",
        "body": "安全策略：外部链接必须经过域名白名单校验，禁止携带凭据访问第三方站点。",
    },
    {
        "url": "https://research.example.test/policy/retention",
        "license": "CC-BY-4.0",
        "body": "数据保留策略：审计日志保留 180 天，原始文档快照保留 90 天，到期自动归档。",
    },
)

USER_BROWSER_SNAPSHOT: Final = (
    "用户浏览器快照（fixture）：知识库导入流程要求先上传原始文件、再解析分块、最后写索引；"
    "导入失败时保留 staged 版本，不影响当前活动版本。"
)


class ResearchFlowError(rp.ResearchError):
    code = "RESEARCH_FLOW_ERROR"


class PlanInvalid(ResearchFlowError):
    code = "PLAN_INVALID"


class ChannelUnavailable(ResearchFlowError):
    code = "SOURCE_UNAVAILABLE"


class ToolDenied(ResearchFlowError):
    code = "TOOL_DENIED"


# ---------------------------------------------------------------------------
# 读取通道
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchedDocument:
    url: str
    body: str
    license: str
    snapshot: Mapping[str, Any] = field(default_factory=dict)


class SearchPort(Protocol):
    def search(self, query: str, *, limit: int = DEFAULT_TOP_K) -> Sequence[FetchedDocument]:
        ...


class LocalSearchAdapter:
    """本地固定搜索适配：按 token 重合度排序，不做网络请求（真实搜索 API NOT_RUN）。"""

    backend: Final = "local-fixed-fixture (真实搜索 API NOT_RUN)"

    def search(self, query: str, *, limit: int = DEFAULT_TOP_K) -> Sequence[FetchedDocument]:
        from retrieval_probe import tokenize

        terms = set(tokenize(query))
        ranked: list[tuple[int, dict[str, str]]] = []
        for doc in LOCAL_CORPUS:
            body_terms = set(tokenize(doc["body"]))
            url_terms = set(tokenize(doc["url"]))
            overlap = len(terms & body_terms) + len(terms & url_terms)
            ranked.append((overlap, doc))
        ranked.sort(key=lambda item: (-item[0], item[1]["url"]))
        picked = [doc for score, doc in ranked[:limit] if score > 0] or [
            doc for _score, doc in ranked[:1]
        ]
        return tuple(
            FetchedDocument(url=doc["url"], body=doc["body"], license=doc["license"],
                            snapshot={"backend": self.backend, "query": query})
            for doc in picked
        )


def _strip_html(text: str) -> str:
    plain = TAG_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", plain).strip()


class HttpChannel:
    """T04 控制站 HTTP 通道：真实 loopback HTTP（这是本任务唯一真实出网形态）。"""

    backend: Final = "control-site-http"

    def __init__(self, site: Any) -> None:
        self.site = site

    def fetch(self, *, path: str = "/static/page.html") -> FetchedDocument:
        url = f"{self.site.base_url}{path}"
        result = control_site.request("GET", url, timeout=5.0)
        if result.error or result.status != 200:
            raise ChannelUnavailable(
                f"HTTP 通道失败：status={result.status} error={result.error}",
            )
        body = _strip_html(result.body.decode("utf-8", errors="replace"))
        return FetchedDocument(
            url=url, body=body, license="CC-BY-4.0",
            snapshot={"backend": self.backend, "status": result.status,
                      "bytes": len(result.body), "path": path},
        )


class JsChannel:
    """B-SK JS 通道。

    ``BSK_LIVE=1`` 时本模块**不**伪造真机结果：直接把 ``live_available`` 标成 False 并抛
    ``NotSupported`` 语义（由调用方记 NOT_RUN）；未开时用控制站 JS 页 + 确定性提取替身。
    """

    backend: Final = "browserskill-deterministic-stand-in (BSK_LIVE 未开/真机 NOT_RUN)"

    def __init__(self, site: Any, *, live: bool | None = None) -> None:
        self.site = site
        self.live_requested = bool(os.environ.get("BSK_LIVE")) if live is None else live

    def fetch(self, *, path: str = "/static/js.html") -> FetchedDocument:
        result = control_site.request("GET", f"{self.site.base_url}{path}", timeout=5.0)
        if result.error or result.status != 200:
            raise ChannelUnavailable(f"JS 通道页面加载失败：status={result.status}")
        text = _strip_html(result.body.decode("utf-8", errors="replace"))
        rendered = f"JS 渲染结果（确定性替身）：{text[:EXTRACT_MAX_CHARS]}"
        return FetchedDocument(
            url=f"{self.site.base_url}{path}", body=rendered, license="CC-BY-4.0",
            snapshot={"backend": self.backend, "live_requested": self.live_requested,
                      "live_run": "NOT_RUN"},
        )


class UserBrowserChannel:
    """用户浏览器快照通道（fixture 文本；真实登录/授权 NOT_RUN）。"""

    backend: Final = "user-browser-snapshot-fixture"

    def __init__(self, snapshot: str = USER_BROWSER_SNAPSHOT) -> None:
        self.snapshot = snapshot

    def fetch(self) -> FetchedDocument:
        return FetchedDocument(
            url="browser://user-snapshot/fixture",
            body=self.snapshot,
            license="user-provided",
            snapshot={"backend": self.backend, "length": len(self.snapshot)},
        )


# ---------------------------------------------------------------------------
# ModelPort 替身
# ---------------------------------------------------------------------------


class ResearchModelPort(Protocol):
    """研究链需要的两个模型能力：产出计划草案、合成答案（真实 chat 模型 NOT_RUN）。"""

    profile_id: str

    def plan_payload(self, goal: str, *, budget_mode: str = "normal") -> Mapping[str, Any]:
        ...

    def compose_answer(self, goal: str, quotes: Sequence[str]) -> str:
        ...


class DeterministicResearchModel:
    """确定性替身：按 goal 生成 6 节点 DAG（3 读取通道 + analyze + ingest），答案只引用证据。"""

    profile_id: Final = "fake.research.v1"

    def plan_payload(self, goal: str, *, budget_mode: str = "normal") -> Mapping[str, Any]:
        nodes = [
            {"task_id": "t1-search", "kind": "search", "depends_on": [], "weight": 1},
            {"task_id": "t2-http", "kind": "fetch_http", "depends_on": [], "weight": 1},
            {"task_id": "t3-js", "kind": "browse_js", "depends_on": [], "weight": 1},
            {"task_id": "t4-browser", "kind": "user_browser", "depends_on": [], "weight": 1},
            {"task_id": "t5-analyze", "kind": "analyze",
             "depends_on": ["t1-search", "t2-http", "t3-js", "t4-browser"], "weight": 1},
            {"task_id": "t6-ingest", "kind": "ingest", "depends_on": ["t5-analyze"], "weight": 1},
        ]
        if budget_mode == "tight":
            # 预算紧张时少跑一个读取通道，仍然保证 DAG 合法（依赖不指向被删节点）。
            nodes = [node for node in nodes if node["task_id"] not in ("t4-browser", "t5-analyze",
                                                                       "t6-ingest")]
            nodes = [
                {"task_id": "t1-search", "kind": "search", "depends_on": [], "weight": 1},
                {"task_id": "t2-http", "kind": "fetch_http", "depends_on": [], "weight": 1},
                {"task_id": "t3-js", "kind": "browse_js", "depends_on": [], "weight": 1},
            ]
        return {
            "task_graph_revision": 1,
            "goal": goal,
            "budget_mode": budget_mode,
            "nodes": nodes,
        }

    def compose_answer(self, goal: str, quotes: Sequence[str]) -> str:
        if not quotes:
            return "现有已授权资料不足以回答该研究目标（证据不足），因此不作推测性结论。"
        cited = "；".join(f"[{index}] {quote[:ANSWER_MAX_QUOTE]}" for index, quote in
                          enumerate(quotes, start=1))
        return f"围绕「{goal}」的证据摘要：{cited}"


class ScriptedResearchModel(DeterministicResearchModel):
    """可注入剧本的替身：用于 PLAN_INVALID 等负例（不伪装成真实模型）。"""

    profile_id: Final = "fake.research.scripted.v1"

    def __init__(self, *, plan: Mapping[str, Any] | None = None,
                 answer: str | None = None) -> None:
        self._plan = plan
        self._answer = answer

    def plan_payload(self, goal: str, *, budget_mode: str = "normal") -> Mapping[str, Any]:
        if self._plan is not None:
            return self._plan
        return super().plan_payload(goal, budget_mode=budget_mode)

    def compose_answer(self, goal: str, quotes: Sequence[str]) -> str:
        if self._answer is not None:
            return self._answer
        return super().compose_answer(goal, quotes)


# ---------------------------------------------------------------------------
# 运行上下文
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetProfile:
    limits: budget_ledger.BudgetLimits
    price: budget_ledger.PriceSnapshot


NORMAL_BUDGET: Final = BudgetProfile(
    limits=budget_ledger.BudgetLimits(
        tokens=20_000, cost_microunits=5_000_000, pages=24, bytes=1_000_000, concurrency=2
    ),
    price=budget_ledger.PriceSnapshot(
        model_profile_id="fake.research.v1",
        input_microunits_per_million=1_000,
        output_microunits_per_million=2_000,
    ),
)
TIGHT_BUDGET: Final = BudgetProfile(
    limits=budget_ledger.BudgetLimits(
        tokens=10, cost_microunits=1_000, pages=1, bytes=1_000, concurrency=1
    ),
    price=budget_ledger.PriceSnapshot(
        model_profile_id="fake.research.v1",
        input_microunits_per_million=1_000,
        output_microunits_per_million=2_000,
    ),
)


@dataclass
class ResearchContext:
    dsn: str
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    goal: str
    principal: Principal
    scope: WorkspaceScope
    model: ResearchModelPort
    planner: planner_probe.ProbePlanner
    search: SearchPort
    http: HttpChannel
    js: JsChannel
    browser: UserBrowserChannel
    registry: tr.ToolRegistry
    budget: BudgetProfile
    approval_ttl_seconds: int = APPROVAL_TTL_SECONDS
    diagnostics: list[str] = field(default_factory=list)
    intent_keys: dict[str, str] = field(default_factory=dict)

    @property
    def state_key(self) -> str:
        return str(self.run_id)


class ResearchState(TypedDict, total=False):
    status: str
    plan_hash: str
    claimed: dict[str, Any] | None
    fetched: int
    analyzed: int
    candidate_id: str | None
    approval_id: str | None
    approved: bool
    published: int
    steps: int
    error_code: str | None
    diagnostics: list[str]
    terminal_event_id: str | None


# ---------------------------------------------------------------------------
# 事务辅助
# ---------------------------------------------------------------------------


@contextmanager
def _tx(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    conn = rp.connect(dsn=dsn)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _budget(ctx: ResearchContext) -> budget_ledger.BudgetLedger:
    return budget_ledger.BudgetLedger(rp.connect(dsn=ctx.dsn))


def _reserve_tool(ctx: ResearchContext, *, purpose: str, pages: int, byte_budget: int) -> uuid.UUID:
    ledger = _budget(ctx)
    reservation = ledger.reserve_tool_call(
        scope=ctx.scope, run_id=ctx.run_id, purpose_key=purpose,
        max_bytes=byte_budget, max_pages=pages, concurrency=1,
    )
    return reservation.reservation_id


def _settle_tool(ctx: ResearchContext, *, reservation_id: uuid.UUID, pages: int, nbytes: int,
                 key: str) -> None:
    ledger = _budget(ctx)
    ledger.settle(
        reservation_id=reservation_id,
        actual=budget_ledger.Cost(pages=pages, bytes=nbytes),
        settlement_key=key,
    )


def _reserve_model(ctx: ResearchContext, *, purpose: str, prompt_tokens: int,
                   max_output_tokens: int) -> uuid.UUID:
    ledger = _budget(ctx)
    reservation = ledger.reserve_model_call(
        scope=ctx.scope, run_id=ctx.run_id, purpose_key=purpose,
        prompt_tokens=prompt_tokens, max_output_tokens=max_output_tokens,
        price=ctx.budget.price,
    )
    return reservation.reservation_id


def _settle_model(ctx: ResearchContext, *, reservation_id: uuid.UUID, prompt_tokens: int,
                  output_tokens: int, key: str) -> None:
    ledger = _budget(ctx)
    ledger.settle(
        reservation_id=reservation_id,
        actual=budget_ledger.Cost(tokens=prompt_tokens + output_tokens),
        settlement_key=key,
        cost_source="estimated",
    )


# ---------------------------------------------------------------------------
# Graph 节点
# ---------------------------------------------------------------------------


def _node_plan(state: ResearchState, ctx: ResearchContext) -> dict[str, Any]:
    with _tx(ctx.dsn) as conn:
        run = rp.read_run(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
        if run.plan_hash is not None:
            return {"status": run.status, "plan_hash": run.plan_hash, "steps": run.step}
        if run.status == "queued":
            rp.set_run_status(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id,
                              status="running")
    payload = ctx.model.plan_payload(ctx.goal, budget_mode=_mode(ctx))
    try:
        graph = ctx.planner.plan(payload)
    except dag_contracts.ContractViolation as exc:
        raise PlanInvalid(str(exc)) from exc
    plan_payload = dag_contracts.payload_from_graph(graph)
    plan_hash = hashlib.sha256(
        json.dumps(plan_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    reservation = _reserve_model(ctx, purpose="plan", prompt_tokens=64, max_output_tokens=128)
    _settle_model(ctx, reservation_id=reservation, prompt_tokens=64, output_tokens=0,
                  key=f"{ctx.run_id}:plan")
    with _tx(ctx.dsn) as conn:
        rp.set_run_status(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id,
                          status="running", plan_hash=plan_hash)
        ts.register_graph(
            conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, revision=REVISION,
            graph=graph, fixed_job_kinds=rp.FIXED_JOB_KINDS,
        )
    return {"status": "running", "plan_hash": plan_hash, "steps": 0}


def _node_schedule(state: ResearchState, ctx: ResearchContext) -> dict[str, Any]:
    steps = int(state.get("steps", 0)) + 1
    if steps > MAX_GRAPH_STEPS:
        raise rp.ResearchError("研究步数超过上限，判定为不收敛", code="STEP_LIMIT")
    with _tx(ctx.dsn) as conn:
        states = ts.task_states(conn, ctx.workspace_id, ctx.run_id, REVISION)
        if not states:
            raise rp.ResearchError("没有登记的任务图", code="PLAN_MISSING")
        terminal = all(item.status in ts.TASK_TERMINAL for item in states.values())
        if terminal:
            return {"status": "ready_to_finalize", "steps": steps, "claimed": None}
        claimed = ts.claim_ready(
            conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, revision=REVISION,
            owner_id="research-probe", limit=1,
        )
        if not claimed:
            running = [item for item in states.values() if item.status == "running"]
            if running:
                return {"status": "waiting", "steps": steps, "claimed": None}
            raise rp.ResearchError("ready 任务为空且无 running 任务（DAG 卡死）", code="DAG_STUCK")
        task = claimed[0]
        kind = states[task.task_id].kind
        rp.bump_step(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
        rp.append_event(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id,
                        event_type="step_progress",
                        payload={"task_id": task.task_id, "kind": kind, "epoch": task.epoch})
    return {"status": "dispatching", "steps": steps,
            "claimed": {"task_id": task.task_id, "kind": kind, "epoch": task.epoch}}


def _node_fetch(state: ResearchState, ctx: ResearchContext) -> dict[str, Any]:
    claimed = state.get("claimed") or {}
    task_id = str(claimed["task_id"])
    kind = str(claimed["kind"])
    reservation = _reserve_tool(ctx, purpose=f"fetch:{task_id}", pages=1, byte_budget=64 * 1024)
    try:
        documents, channel = _fetch_documents(ctx, kind)
    except ChannelUnavailable as exc:
        ctx.diagnostics.append(f"channel_unavailable:{kind}:{exc}")
        with _tx(ctx.dsn) as conn:
            ts.fail_task(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, revision=REVISION,
                         task_id=task_id, epoch=int(claimed["epoch"]),
                         error_code="SOURCE_UNAVAILABLE")
        raise
    nbytes = sum(len(doc.body.encode("utf-8")) for doc in documents)
    _settle_tool(ctx, reservation_id=reservation, pages=1, nbytes=nbytes, key=f"{task_id}:fetch")
    source_ids: list[str] = []
    evidence_ids: list[str] = []
    with _tx(ctx.dsn) as conn:
        for doc in documents:
            source = rp.record_source(
                conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, task_id=task_id,
                channel=channel, url=doc.url, body=doc.body, license_name=doc.license,
                snapshot={**dict(doc.snapshot), "corpus_version": 1},
            )
            evidence = rp.record_evidence(
                conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id,
                source_id=source.source_id, quote=doc.body[:200], locator=f"line:{channel}:1",
            )
            source_ids.append(str(source.source_id))
            evidence_ids.append(str(evidence.evidence_id))
        ts.complete_task(
            conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, revision=REVISION,
            task_id=task_id, epoch=int(claimed["epoch"]),
            fact={"channel": channel, "sources": source_ids, "evidence": evidence_ids},
            owner_id="research-probe",
        )
    return {"status": "dispatching", "claimed": None, "fetched": int(state.get("fetched", 0)) + 1}


def _fetch_documents(ctx: ResearchContext, kind: str) -> tuple[Sequence[FetchedDocument], str]:
    if kind == "search":
        return ctx.search.search(ctx.goal), "search"
    if kind == "fetch_http":
        return (ctx.http.fetch(),), "http"
    if kind == "browse_js":
        return (ctx.js.fetch(),), "js"
    if kind == "user_browser":
        return (ctx.browser.fetch(),), "user_browser"
    raise rp.ResearchError(f"任务 kind={kind} 不是读取通道", code="KIND_INVALID")


def _node_analyze(state: ResearchState, ctx: ResearchContext) -> dict[str, Any]:
    """把三条通道抓到的证据合成一个候选（无证据 → 明确失败，不发布）。"""

    claimed = state.get("claimed") or {}
    task_id = str(claimed["task_id"])
    reservation = _reserve_model(ctx, purpose="analyze", prompt_tokens=128, max_output_tokens=256)
    try:
        with _tx(ctx.dsn) as conn:
            sources = rp.list_sources(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
            evidence_rows = conn.execute(
                f"SELECT * FROM {rp.SCHEMA}.phase0_evidence WHERE workspace_id = %s AND run_id = %s"
                " ORDER BY created_at, evidence_id",
                (ctx.workspace_id, ctx.run_id),
            ).fetchall()
        evidence_ids = [row["evidence_id"] for row in evidence_rows]
        if not evidence_ids:
            raise rp.ResearchError("没有可用证据，拒绝生成无证据候选", code="NO_EVIDENCE")
        quotes = [str(row["quote"]) for row in evidence_rows[:3]]
        body = ctx.model.compose_answer(ctx.goal, quotes)
        title = f"研究结论：{ctx.goal[:60]}"
        with _tx(ctx.dsn) as conn:
            candidate = rp.build_candidate(
                conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, task_id=task_id,
                title=title, body=body, evidence_ids=evidence_ids[:3], requires_approval=True,
            )
            ts.complete_task(
                conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, revision=REVISION,
                task_id=task_id, epoch=int(claimed["epoch"]),
                fact={"candidate_id": str(candidate.candidate_id),
                      "evidence_count": len(evidence_ids[:3]),
                      "channels": sorted({source.channel for source in sources})},
                owner_id="research-probe",
            )
    finally:
        _settle_model(ctx, reservation_id=reservation, prompt_tokens=128, output_tokens=64,
                      key=f"{task_id}:analyze")
    return {"status": "dispatching", "claimed": None, "analyzed": int(state.get("analyzed", 0)) + 1,
            "candidate_id": str(candidate.candidate_id)}


def _node_approve_intent_name() -> str:
    return "site.idem_write"


def _tool_intent(ctx: ResearchContext, *, candidate_id: uuid.UUID, body: str) -> tr.ToolIntent:
    registry = ctx.registry
    arguments = {
        "idempotency_key": f"{EXTERNAL_IDEM_PREFIX}:{ctx.run_id}:{candidate_id}",
        "body": {"text": body[:512]},
    }
    intent, _validated = tr.prepare_intent(
        registry,
        scope=ctx.scope,
        resource_refs=[ResourceRef(workspace_id=ctx.workspace_id, resource_kind="source",
                                  resource_id=candidate_id)],
        response_artifact_id=uuid.uuid5(
            uuid.NAMESPACE_URL, f"p0-t28:{ctx.run_id}:{candidate_id}"
        ),
        call_ordinal=1,
        name=_node_approve_intent_name(),
        arguments=arguments,
        run_id=ctx.run_id,
    )
    return intent


def _node_approve(state: ResearchState, ctx: ResearchContext) -> dict[str, Any]:
    """原生 interrupt：第一次到这里暂停；恢复后幂等读取审批事实再继续。"""

    candidate_id = uuid.UUID(str(state.get("candidate_id")))
    with _tx(ctx.dsn) as conn:
        candidate = rp._lock_candidate(
            conn, workspace_id=ctx.workspace_id, candidate_id=candidate_id
        )
    intent = _tool_intent(ctx, candidate_id=candidate_id, body=candidate.body)
    with _tx(ctx.dsn) as conn:
        approval = rp.record_approval_request(
            conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, candidate_id=candidate_id,
            intent_key=intent.intent_key, ttl_seconds=ctx.approval_ttl_seconds,
        )
        run = rp.set_run_status(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id,
                                status="waiting_approval")
        del run
    ctx.intent_keys[str(candidate_id)] = intent.intent_key
    with _tx(ctx.dsn) as conn:
        current = rp.read_approval(conn, workspace_id=ctx.workspace_id,
                                   approval_id=approval.approval_id)
    if current.status == "pending":
        from langgraph.types import interrupt

        decision = interrupt(
            {
                "type": "approval_required",
                "approval_id": str(approval.approval_id),
                "candidate_id": str(candidate_id),
                "intent_key": intent.intent_key,
                "summary": f"研究任务需要人工批准外部写：{ctx.goal[:80]}",
            }
        )
        _record_decision(ctx, approval=current, decision=decision)
        with _tx(ctx.dsn) as conn:
            current = rp.read_approval(conn, workspace_id=ctx.workspace_id,
                                       approval_id=approval.approval_id)
    if current.status != "approved":
        with _tx(ctx.dsn) as conn:
            ts.fail_task(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, revision=REVISION,
                         task_id=str((state.get("claimed") or {})["task_id"]),
                         epoch=int((state.get("claimed") or {})["epoch"]),
                         error_code="APPROVAL_REJECTED")
            rp.set_run_status(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id,
                              status="running")
        return {"status": "dispatching", "approved": False, "claimed": None,
                "approval_id": str(approval.approval_id)}
    with _tx(ctx.dsn) as conn:
        rp.set_run_status(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, status="running")
    return {"status": "dispatching", "approved": True, "claimed": state.get("claimed"),
            "approval_id": str(approval.approval_id)}


def _record_decision(ctx: ResearchContext, *, approval: rp.ApprovalRecord, decision: Any) -> None:
    if isinstance(decision, Mapping):
        verdict = str(decision.get("decision", "")).lower()
        approver_raw = decision.get("approver_principal_id")
        reason = decision.get("reason")
    else:
        verdict = str(decision).lower()
        approver_raw = None
        reason = None
    if verdict not in ("approve", "approved", "reject", "rejected"):
        raise rp.ResearchError(f"非法审批恢复值: {decision!r}", code="APPROVAL_DECISION_INVALID")
    approver = uuid.UUID(str(approver_raw)) if approver_raw else ctx.principal.principal_id
    with _tx(ctx.dsn) as conn:
        rp.decide_approval(
            conn, workspace_id=ctx.workspace_id, approval_id=approval.approval_id,
            decision="approved" if verdict.startswith("approve") else "rejected",
            approver_principal_id=approver, reason=str(reason) if reason is not None else None,
        )


def _node_publish(state: ResearchState, ctx: ResearchContext) -> dict[str, Any]:
    """审批通过后：外部幂等写（T19 门禁）→ 一个事务写发布事实 + Job + 事件。"""

    claimed = state.get("claimed") or {}
    task_id = str(claimed["task_id"])
    candidate_id = uuid.UUID(str(state.get("candidate_id")))
    approval_id = uuid.UUID(str(state.get("approval_id")))
    with _tx(ctx.dsn) as conn:
        candidate = rp._lock_candidate(conn, workspace_id=ctx.workspace_id,
                                       candidate_id=candidate_id)
        approval = rp.read_approval(conn, workspace_id=ctx.workspace_id, approval_id=approval_id)
    if approval.status != "approved":
        raise rp.ApprovalMismatch(f"审批状态 {approval.status}，拒绝执行外部写")
    intent = _tool_intent(ctx, candidate_id=candidate_id, body=candidate.body)
    if intent.intent_key != approval.intent_key:
        raise rp.ApprovalMismatch("审批 intent_key 与当前调用不匹配")
    tr.assert_executable(
        intent,
        approval=tr.ApprovalRef(
            approval_id=approval.approval_id, intent_key=approval.intent_key,
            status="approved", expires_at=approval.expires_at,
            approver_principal_id=approval.approver_principal_id,
        ),
    )
    # 外部副作用先带稳定幂等键执行：控制站保证同 key 只生效一次。
    idempotency_key = f"{EXTERNAL_IDEM_PREFIX}:{ctx.run_id}:{candidate_id}"
    payload = json.dumps(
        {"key": idempotency_key, "text": candidate.body[:512]}, ensure_ascii=False
    ).encode("utf-8")
    result = control_site.request(
        "POST", f"{ctx.http.site.base_url}/idem/write", body=payload,
        headers={"Content-Type": "application/json", "Idempotency-Key": idempotency_key},
        timeout=5.0,
    )
    if result.error or result.status != 200:
        raise ToolDenied(f"控制站幂等写失败：status={result.status} error={result.error}")
    with _tx(ctx.dsn) as conn:
        published = rp.publish_candidate(
            conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, candidate_id=candidate_id,
            approval=approval,
        )
        ts.complete_task(
            conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, revision=REVISION,
            task_id=task_id, epoch=int(claimed["epoch"]),
            fact={"candidate_id": str(candidate_id), "job_id": str(published.job.job_id)},
            owner_id="research-probe",
        )
    return {"status": "dispatching", "claimed": None,
            "published": int(state.get("published", 0)) + (0 if published.replayed else 1)}


def _node_finalize(state: ResearchState, ctx: ResearchContext) -> dict[str, Any]:
    with _tx(ctx.dsn) as conn:
        candidates = rp.list_candidates(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
        published = [item for item in candidates if item.status == "published"]
        quotes = [item.body[:ANSWER_MAX_QUOTE] for item in published]
        citations = rp.list_citations(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
    answer = ctx.model.compose_answer(ctx.goal, quotes)
    answer_kind = "grounded" if published and citations else "insufficient_evidence"
    with _tx(ctx.dsn) as conn:
        outcome = rp.finalize_run(
            conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id,
            answer_kind=answer_kind, content=answer,
        )
    return {"status": "succeeded", "terminal_event_id":
            str(outcome.event_id) if outcome.event_id else None}


def _mode(ctx: ResearchContext) -> str:
    return "tight" if ctx.budget is TIGHT_BUDGET else "normal"


# ---------------------------------------------------------------------------
# 图装配 / 运行
# ---------------------------------------------------------------------------


def build_graph(ctx: ResearchContext, *, checkpointer: Any) -> Any:
    from langgraph.graph import END, START, StateGraph

    graph: Any = StateGraph(ResearchState)
    graph.add_node("plan", lambda state: _node_plan(state, ctx))
    graph.add_node("schedule", lambda state: _node_schedule(state, ctx))
    graph.add_node("fetch", lambda state: _node_fetch(state, ctx))
    graph.add_node("analyze", lambda state: _node_analyze(state, ctx))
    graph.add_node("approve", lambda state: _node_approve(state, ctx))
    graph.add_node("publish", lambda state: _node_publish(state, ctx))
    graph.add_node("finalize", lambda state: _node_finalize(state, ctx))
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "schedule")
    graph.add_conditional_edges("schedule", _route_schedule,
                                {"dispatch": "fetch_dispatch", "finalize": "finalize"})
    graph.add_node("fetch_dispatch", lambda state: state)
    graph.add_conditional_edges(
        "fetch_dispatch", _route_dispatch,
        {"fetch": "fetch", "analyze": "analyze", "approve": "approve"},
    )
    graph.add_edge("fetch", "schedule")
    graph.add_edge("analyze", "schedule")
    graph.add_conditional_edges("approve", _route_approve,
                                {"publish": "publish", "schedule": "schedule"})
    graph.add_edge("publish", "schedule")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def _route_schedule(state: ResearchState) -> str:
    if state.get("status") == "ready_to_finalize":
        return "finalize"
    if state.get("claimed"):
        return "dispatch"
    return "finalize"


def _route_dispatch(state: ResearchState) -> str:
    kind = (state.get("claimed") or {}).get("kind")
    if kind == "analyze":
        return "analyze"
    if kind == "ingest":
        return "approve"
    return "fetch"


def _route_approve(state: ResearchState) -> str:
    return "publish" if state.get("approved") else "schedule"


@dataclass(frozen=True)
class ResearchOutcome:
    run: rp.RunState
    status: str
    interrupted: bool
    interactions: tuple[Mapping[str, Any], ...]
    published: int
    candidates: int
    citations: int
    events: int
    terminal_count: int
    channels: tuple[str, ...]
    replayed: bool = False
    diagnostics: tuple[str, ...] = ()
    error_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run.run_id),
            "status": self.status,
            "interrupted": self.interrupted,
            "interactions": [dict(item) for item in self.interactions],
            "published": self.published,
            "candidates": self.candidates,
            "citations": self.citations,
            "events": self.events,
            "terminal_count": self.terminal_count,
            "channels": list(self.channels),
            "replayed": self.replayed,
            "diagnostics": list(self.diagnostics),
            "error_code": self.error_code,
        }


def make_context(
    *,
    dsn: str,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    goal: str,
    principal: Principal,
    site: Any,
    model: ResearchModelPort | None = None,
    budget_mode: str = "normal",
    policy_snapshot_id: uuid.UUID | None = None,
) -> ResearchContext:
    return ResearchContext(
        dsn=dsn,
        workspace_id=workspace_id,
        run_id=run_id,
        goal=goal,
        principal=principal,
        scope=issue_scope(principal, policy_snapshot_id=policy_snapshot_id or uuid.uuid4()),
        model=model or DeterministicResearchModel(),
        planner=planner_probe.ProbePlanner(),
        search=LocalSearchAdapter(),
        http=HttpChannel(site),
        js=JsChannel(site),
        browser=UserBrowserChannel(),
        registry=tr.build_phase0_registry(),
        budget=TIGHT_BUDGET if budget_mode == "tight" else NORMAL_BUDGET,
    )


def _invoke(ctx: ResearchContext, graph_input: Any) -> dict[str, Any]:
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.errors import GraphInterrupt

    saver_conn = rp._saver_connection(ctx.dsn)
    try:
        app = build_graph(ctx, checkpointer=PostgresSaver(saver_conn))
        config = {"configurable": {"thread_id": ctx.state_key}}
        try:
            result = dict(app.invoke(graph_input, config=config))
        except GraphInterrupt:
            snapshot = app.get_state(config)
            return {"__interrupted__": True, "values": dict(snapshot.values),
                    "interrupts": tuple(getattr(item, "value", item)
                                        for item in (snapshot.interrupts or ()))}
        if "__interrupt__" in result:
            interrupts = result.get("__interrupt__") or ()
            raw = tuple(getattr(item, "value", item) for item in interrupts)
            values = {key: value for key, value in result.items() if key != "__interrupt__"}
            return {"__interrupted__": True, "values": values, "interrupts": raw}
        return result
    finally:
        saver_conn.close()


def _outcome(
    ctx: ResearchContext, state: Mapping[str, Any], *, interrupted: bool
) -> ResearchOutcome:
    with _tx(ctx.dsn) as conn:
        run = rp.read_run(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
        candidates = rp.list_candidates(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
        citations = rp.list_citations(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
        events = rp.list_events(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
        sources = rp.list_sources(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
        terminal = sum(1 for event in events if event.event_type in rp.TERMINAL_EVENT_TYPES)
    return ResearchOutcome(
        run=run,
        status=run.status,
        interrupted=interrupted,
        interactions=tuple(state.get("interrupts") or ()),
        published=sum(1 for item in candidates if item.status == "published"),
        candidates=len(candidates),
        citations=len(citations),
        events=len(events),
        terminal_count=terminal,
        channels=tuple(sorted({source.channel for source in sources})),
        diagnostics=tuple(ctx.diagnostics),
        error_code=run.error_code,
    )


def run_research(ctx: ResearchContext) -> ResearchOutcome:
    """跑到第一个审批中断或终态；重复调用已终态 run 返回复放结果。"""

    with _tx(ctx.dsn) as conn:
        run = rp.create_run(
            conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, goal=ctx.goal,
            budget_mode=_mode(ctx),
        )
        ledger = budget_ledger.BudgetLedger(conn)
        ledger.ensure(scope=ctx.scope, run_id=ctx.run_id, limits=ctx.budget.limits)
    if run.terminal:
        return _replayed(ctx)
    try:
        state = _invoke(ctx, {"status": "queued", "steps": 0, "fetched": 0, "analyzed": 0,
                              "published": 0})
    except (budget_ledger.BudgetExceededError, budget_ledger.LedgerConfigError) as exc:
        ctx.diagnostics.append(f"budget_denied:{exc}")
        with _tx(ctx.dsn) as conn:
            rp.fail_run(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id,
                        error_code="BUDGET_EXCEEDED", detail=str(exc)[:200])
        return _outcome(ctx, {}, interrupted=False)
    except (PlanInvalid, rp.ApprovalMismatch, rp.ResearchError) as exc:
        code = getattr(exc, "code", "RESEARCH_ERROR")
        ctx.diagnostics.append(f"failure:{code}:{exc}")
        with _tx(ctx.dsn) as conn:
            rp.fail_run(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id,
                        error_code=code, detail=str(exc)[:200])
        return _outcome(ctx, {}, interrupted=False)
    if state.get("__interrupted__"):
        dict(state.get("values") or {})
        return _outcome(ctx, {"interrupts": state.get("interrupts") or ()}, interrupted=True)
    return _outcome(ctx, state, interrupted=False)


def resume_research(ctx: ResearchContext, decision: Mapping[str, Any]) -> ResearchOutcome:
    """用审批决定恢复；重复恢复不会产生第二次外部副作用（幂等）。"""

    from langgraph.types import Command

    with _tx(ctx.dsn) as conn:
        run = rp.read_run(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)
    if run.terminal:
        return _replayed(ctx)
    state = _invoke(ctx, Command(resume=dict(decision)))
    if state.get("__interrupted__"):
        return _outcome(ctx, {"interrupts": state.get("interrupts") or ()}, interrupted=True)
    return _outcome(ctx, state, interrupted=False)


def _replayed(ctx: ResearchContext) -> ResearchOutcome:
    outcome = _outcome(ctx, {}, interrupted=False)
    return ResearchOutcome(
        run=outcome.run, status=outcome.status, interrupted=False, interactions=(),
        published=outcome.published, candidates=outcome.candidates,
        citations=outcome.citations, events=outcome.events,
        terminal_count=outcome.terminal_count, channels=outcome.channels,
        replayed=True, diagnostics=outcome.diagnostics, error_code=outcome.error_code,
    )


def cancel_research(ctx: ResearchContext, *, reason: str = "user_cancelled") -> rp.FinalizeOutcome:
    with _tx(ctx.dsn) as conn:
        return rp.cancel_run(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, reason=reason)


def read_external_writes(site: Any) -> Mapping[str, Mapping[str, Any]]:
    """读控制站全量幂等写计数（服务端记账，不信客户端）。"""

    result = control_site.request("GET", f"{site.base_url}/_control/state", timeout=5.0)
    if result.error or result.status != 200:
        raise ResearchFlowError(f"读取控制站状态失败：status={result.status}")
    payload = json.loads(result.body.decode("utf-8"))
    idem = payload.get("idem") if isinstance(payload, Mapping) else None
    if not isinstance(idem, Mapping):
        raise ResearchFlowError(f"控制站状态缺少 idem 段：{payload!r}")
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in idem.items():
        raw = dict(value) if isinstance(value, Mapping) else {}
        applied = raw.get("applied_calls", raw.get("applied", 0))
        normalized[str(key)] = {
            "applied_calls": int(applied),
            "attempts": int(raw.get("attempts", 0)),
            "raw": raw,
        }
    return normalized


def count_external_writes(site: Any, *, key: str) -> Mapping[str, Any]:
    """读控制站的幂等写计数（用于证明重复恢复不产生第二次外部副作用）。"""

    result = control_site.request(
        "GET", f"{site.base_url}/idem/count?key={key}", timeout=5.0
    )
    if result.error or result.status != 200:
        raise ResearchFlowError(f"读取控制站计数失败：status={result.status}")
    payload = json.loads(result.body.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise ResearchFlowError(f"计数响应不是对象：{payload!r}")
    return payload


# ---------------------------------------------------------------------------
# 30 个研究任务 + 批量证据
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchTask:
    task_id: str
    goal: str
    expect: str  # succeeded | approval_rejected | budget_exceeded | browser_down | plan_invalid
    budget_mode: str = "normal"
    approval: str = "approve"
    inject: str | None = None


def seed_tasks() -> tuple[ResearchTask, ...]:
    """30 个研究任务：正路径 12、审批拒绝 6、预算 4、浏览器断线 4、非法计划 4。"""

    positives = (
        "差旅报销上限与发票要求是什么",
        "费用报销流程需要几级审批",
        "外部链接访问的安全策略要求",
        "审计日志与原始文档快照保留多久",
        "知识库导入失败时活动版本是否受影响",
        "差旅与费用报销的区别",
        "安全白名单是否允许携带凭据",
        "原始文档快照的归档周期",
        "二级审批的金额门槛",
        "部门负责人与财务复核的职责边界",
        "导入流程的分块与索引顺序",
        "报销单必须附带的材料",
    )
    tasks: list[ResearchTask] = []
    for index, goal in enumerate(positives, start=1):
        tasks.append(ResearchTask(task_id=f"p{index:02d}", goal=goal, expect="succeeded"))
    for index in range(1, 7):
        tasks.append(ResearchTask(task_id=f"r{index:02d}", goal=f"需要人工否决的研究议题 {index}",
                                  expect="approval_rejected", approval="reject"))
    for index in range(1, 5):
        tasks.append(ResearchTask(task_id=f"b{index:02d}", goal=f"预算受限的研究议题 {index}",
                                  expect="budget_exceeded", budget_mode="tight"))
    for index in range(1, 5):
        tasks.append(ResearchTask(task_id=f"d{index:02d}", goal=f"浏览器断线的研究议题 {index}",
                                  expect="browser_down", inject="browser_down"))
    for index in range(1, 5):
        tasks.append(ResearchTask(task_id=f"i{index:02d}", goal=f"非法计划的研究议题 {index}",
                                  expect="plan_invalid", inject="plan_invalid"))
    return tuple(tasks)


def run_batch(
    *,
    dsn: str,
    site: Any,
    workspace_id: uuid.UUID,
    principal: Principal,
    tasks: Sequence[ResearchTask] | None = None,
) -> dict[str, Any]:
    """顺序跑 30 个任务（每个 run 独立 thread/checkpoint），返回真实行与分类。"""

    rows: list[dict[str, Any]] = []
    for task in tasks or seed_tasks():
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"p0-t28:{workspace_id}:{task.task_id}")
        model: ResearchModelPort = DeterministicResearchModel()
        if task.inject == "plan_invalid":
            model = ScriptedResearchModel(plan={"task_graph_revision": 1, "nodes": "not-a-list"})
        ctx = make_context(
            dsn=dsn, workspace_id=workspace_id, run_id=run_id, goal=task.goal,
            principal=principal, site=site, model=model, budget_mode=task.budget_mode,
        )
        row: dict[str, Any] = {"task_id": task.task_id, "expect": task.expect, "goal": task.goal}
        try:
            if task.inject == "browser_down":
                ctx.http = HttpChannel(site)
                ctx.js = _BrokenChannel(site)  # type: ignore[assignment]
            outcome = run_research(ctx)
            if outcome.interrupted:
                decision = {"decision": task.approval, "approver_principal_id": str(
                    principal.principal_id), "reason": f"batch:{task.task_id}"}
                outcome = resume_research(ctx, decision)
            row.update(outcome.as_dict())
            row["actual"] = _classify(task, outcome)
            prefix = f"{EXTERNAL_IDEM_PREFIX}:{run_id}:"
            counters = read_external_writes(site) if site is not None else {}
            keys = sorted(key for key in counters if key.startswith(prefix))
            row["external_keys"] = keys
            row["external_applied"] = sum(
                int(counters[key].get("applied_calls", 0)) for key in keys
            )
        except Exception as exc:  # noqa: BLE001 - 批量证据需要记录真实异常而中止整批
            row.update({"status": "error", "actual": "error",
                        "error": f"{type(exc).__name__}: {exc}"[:300]})
        rows.append(row)
    actuals = [row["actual"] for row in rows]
    return {
        "schema_version": rp.EVIDENCE_SCHEMA_VERSION,
        "graph_version": GRAPH_VERSION,
        "task_count": len(rows),
        "rows": rows,
        "summary": {
            "succeeded": actuals.count("succeeded"),
            "approval_rejected": actuals.count("approval_rejected"),
            "budget_exceeded": actuals.count("budget_exceeded"),
            "browser_down": actuals.count("browser_down"),
            "plan_invalid": actuals.count("plan_invalid"),
            "unexpected": sum(1 for row in rows if row["actual"] != row["expect"]),
        },
    }


def _classify(task: ResearchTask, outcome: ResearchOutcome) -> str:
    if task.expect == "plan_invalid":
        return "plan_invalid" if outcome.error_code == "PLAN_INVALID" else "succeeded"
    if task.expect == "budget_exceeded":
        return "budget_exceeded" if outcome.error_code == "BUDGET_EXCEEDED" else "succeeded"
    if task.expect == "browser_down":
        return "browser_down" if outcome.error_code == "SOURCE_UNAVAILABLE" else "succeeded"
    if task.expect == "approval_rejected":
        rejected = outcome.status == "succeeded" and outcome.published == 0
        return "approval_rejected" if rejected else outcome.status
    return outcome.status


class _BrokenChannel:
    """断线通道替身：模拟浏览器在抓取阶段断连（用于 browser_down 负例）。"""

    def __init__(self, site: Any) -> None:
        self.site = site

    def fetch(self, *, path: str = "/static/js.html") -> FetchedDocument:
        raise ChannelUnavailable("浏览器连接被重置（fixture 断线）")


def verify_traceability(
    dsn: str, *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> dict[str, Any]:
    """逐条核对已发布候选的证据链：来源快照/时间/通道/许可/证据/审批/Job。"""

    with rp.connect(dsn=dsn) as conn:
        candidates = rp.list_candidates(conn, workspace_id=workspace_id, run_id=run_id)
        sources = rp.list_sources(conn, workspace_id=workspace_id, run_id=run_id)
        citations = rp.list_citations(conn, workspace_id=workspace_id, run_id=run_id)
        jobs = rp.list_jobs(conn, workspace_id=workspace_id, run_id=run_id)
        source_by_id = {source.source_id: source for source in sources}
        job_by_candidate = {job.candidate_id: job for job in jobs}
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.status != "published":
                continue
            evidence_ids = {item.evidence_id for item in citations
                            if item.candidate_id == candidate.candidate_id}
            evidence_rows = conn.execute(
                f"SELECT * FROM {rp.SCHEMA}.phase0_evidence WHERE workspace_id = %s"
                " AND evidence_id = ANY(%s)", (workspace_id, list(evidence_ids)),
            ).fetchall()
            problems: list[str] = []
            channel_set = set()
            license_set = set()
            for row in evidence_rows:
                source = source_by_id.get(row["source_id"])
                if source is None:
                    problems.append("evidence_source_missing")
                    continue
                channel_set.add(source.channel)
                license_set.add(source.license)
                if not source.body_sha256 or not source.snapshot:
                    problems.append("source_snapshot_missing")
                if source.fetched_at is None:
                    problems.append("source_time_missing")
            if not evidence_rows:
                problems.append("no_evidence")
            if candidate.requires_approval and candidate.approval_id is None:
                problems.append("approval_missing")
            job = job_by_candidate.get(candidate.candidate_id)
            if job is None:
                problems.append("job_missing")
            rows.append({
                "candidate_id": str(candidate.candidate_id),
                "channels": sorted(channel_set),
                "licenses": sorted(license_set),
                "evidence_count": len(evidence_rows),
                "approval_id": str(candidate.approval_id) if candidate.approval_id else None,
                "job_id": str(job.job_id) if job else None,
                "job_dedupe_key": job.dedupe_key if job else None,
                "problems": problems,
            })
    complete = [row for row in rows if not row["problems"]]
    return {
        "published_candidates": len(rows),
        "complete_chains": len(complete),
        "incomplete": len(rows) - len(complete),
        "rows": rows,
    }


def write_evidence(payload: Mapping[str, Any], path: Path | None = None) -> Path:
    target = path or rp.EVIDENCE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return target


__all__ = [
    "ANSWER_MAX_QUOTE",
    "BudgetProfile",
    "ChannelUnavailable",
    "DeterministicResearchModel",
    "EXTERNAL_IDEM_PREFIX",
    "FetchedDocument",
    "GRAPH_VERSION",
    "HttpChannel",
    "JsChannel",
    "LocalSearchAdapter",
    "NORMAL_BUDGET",
    "PlanInvalid",
    "REVISION",
    "ResearchContext",
    "ResearchFlowError",
    "ResearchModelPort",
    "ResearchOutcome",
    "ResearchState",
    "ResearchTask",
    "ScriptedResearchModel",
    "TIGHT_BUDGET",
    "ToolDenied",
    "UserBrowserChannel",
    "build_graph",
    "read_external_writes",
    "cancel_research",
    "count_external_writes",
    "make_context",
    "resume_research",
    "run_batch",
    "run_research",
    "seed_tasks",
    "verify_traceability",
    "write_evidence",
]
