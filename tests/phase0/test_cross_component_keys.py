"""W5 门禁修缺口：``tool_registry.compute_intent_key`` 与 ``tool_ledger.intent_key`` 逐字节一致。

背景：T19 的注册表曾用字符串 ``schema_version="tool-intent/1"``、T06 账本用整数
``schema_version=1``，同一语义调用算出不同 key，跨组件（T19 契约 / T06 账本）去重会
**静默失效**。修复后 registry 直接委托 T06 实现，本文件把这两个入口钉在一起：

1. 同一语义调用 → 两个入口产出完全相同的 64 位十六进制字符串；
2. 逐一扰动 8 个绑定字段 → 两侧同步变化且仍相等；
3. 参数键顺序不影响结果；``1`` vs ``"1"`` 等类型差异两侧行为一致（都不做隐式转换）；
4. 公开 API（名字、参数名与顺序）保持不变，``SCHEMA_VERSION`` 与 T06 常量同源。

只使用真实模块函数，无 mock、无 skip。
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

PROJECT = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import contracts  # noqa: E402
import tool_ledger as ledger  # noqa: E402
import tool_registry as registry  # noqa: E402

WS = UUID("0a0a0a0a-0000-4000-8000-0000000000aa")
PRINCIPAL = UUID("a11a11a1-0000-4000-8000-0000000000aa")
POLICY = UUID("a9a9a9a9-0000-4000-8000-0000000000aa")
RUN = UUID("22222222-0000-4000-8000-0000000000aa")
ARTIFACT = UUID("c0dec0de-0000-4000-8000-0000000000aa")

ARGUMENTS: dict[str, Any] = {"path": "/idem/count", "note": "跨组件一致"}
RESOURCE_REFS = (("source", "50050050-0000-4000-8000-0000000000aa"),)
ARGUMENTS_HASH = ledger.arguments_hash(ARGUMENTS)
SCOPE_HASH = ledger.scope_hash(
    workspace_id=WS,
    principal_id=PRINCIPAL,
    operation="site.read_counter",
    resource_refs=RESOURCE_REFS,
)


def _scope() -> contracts.WorkspaceScope:
    principal = contracts.synthesize_principal(
        workspace_id=WS,
        principal_id=PRINCIPAL,
        membership_subject_ref="subject-w5-intent",
        kind="service",
        role="owner",
    )
    return contracts.issue_scope(principal, policy_snapshot_id=POLICY)


def _base_fields() -> dict[str, Any]:
    return {
        "schema_version": ledger.INTENT_SCHEMA_VERSION,
        "run_id": RUN,
        "response_artifact_id": ARTIFACT,
        "call_ordinal": 0,
        "name": "site.read_counter",
        "tool_version": "1.0.0",
        "arguments_hash": ARGUMENTS_HASH,
        "scope_hash": SCOPE_HASH,
    }


def _registry_key(**overrides: Any) -> str:
    fields = {**_base_fields(), **overrides}
    return registry.compute_intent_key(**fields)


def _ledger_key(**overrides: Any) -> str:
    fields = {**_base_fields(), **overrides}
    return ledger.intent_key(**fields)


# ---------------------------------------------------------------------------
# 1) 同一语义调用 → 同一字符串
# ---------------------------------------------------------------------------
def test_same_semantic_call_yields_identical_key() -> None:
    registry_key = _registry_key()
    ledger_key = _ledger_key()

    assert registry_key == ledger_key
    assert len(registry_key) == 64
    assert set(registry_key) <= set("0123456789abcdef")


def test_registry_prepare_intent_key_equals_ledger_recomputation() -> None:
    """端到端：registry 造出的 intent.key 必须能被 T06 用同一组字段复算出来。"""

    intent, validated = registry.prepare_intent(
        registry.build_phase0_registry(),
        scope=_scope(),
        resource_refs=(),
        response_artifact_id=ARTIFACT,
        call_ordinal=2,
        name="site.read_counter",
        arguments={"path": "/idem/count"},
        run_id=RUN,
    )
    assert intent.schema_version == ledger.INTENT_SCHEMA_VERSION

    ledger_key = ledger.intent_key(
        schema_version=ledger.INTENT_SCHEMA_VERSION,
        run_id=RUN,
        response_artifact_id=ARTIFACT,
        call_ordinal=intent.call_ordinal,
        name=intent.name,
        tool_version=intent.tool_version,
        arguments_hash=intent.arguments_hash,
        scope_hash=intent.scope_hash,
    )
    assert ledger_key == intent.intent_key
    # 参数哈希也同源：registry 与 ledger 对同一参数给出同一 arguments_hash。
    assert validated.arguments_hash == ledger.arguments_hash({"path": "/idem/count"})


# ---------------------------------------------------------------------------
# 2) 逐一扰动 8 个绑定字段：两侧同步变化且仍相等
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("run_id", uuid4()),
        ("response_artifact_id", uuid4()),
        ("call_ordinal", 1),
        ("name", "site.idem_write"),
        ("tool_version", "1.0.1"),
        ("arguments_hash", "1" * 64),
        ("scope_hash", "2" * 64),
    ],
    ids=[
        "schema_version",
        "run_id",
        "response_artifact_id",
        "call_ordinal",
        "name",
        "tool_version",
        "arguments_hash",
        "scope_hash",
    ],
)
def test_each_bound_field_changes_both_sides_synchronously(field: str, value: object) -> None:
    baseline_registry = _registry_key()
    baseline_ledger = _ledger_key()
    assert baseline_registry == baseline_ledger

    mutated_registry = _registry_key(**{field: value})
    mutated_ledger = _ledger_key(**{field: value})

    assert mutated_registry == mutated_ledger, field
    assert mutated_registry != baseline_registry, field  # registry 侧确实变了
    assert mutated_ledger != baseline_ledger, field  # ledger 侧同步变了


# ---------------------------------------------------------------------------
# 3) 键序 / 类型行为两侧一致
# ---------------------------------------------------------------------------
def test_argument_key_order_does_not_change_key() -> None:
    reordered = {"note": "跨组件一致", "path": "/idem/count"}
    nested_one = {"tool": "site.read_counter", "params": {"b": 2, "a": 1}}
    nested_two = {"params": {"a": 1, "b": 2}, "tool": "site.read_counter"}

    assert ledger.arguments_hash(reordered) == ledger.arguments_hash(ARGUMENTS)
    assert registry.compute_arguments_hash(reordered) == registry.compute_arguments_hash(ARGUMENTS)
    assert ledger.arguments_hash(nested_one) == ledger.arguments_hash(nested_two)

    hash_one = ledger.arguments_hash(ARGUMENTS)
    hash_two = ledger.arguments_hash(reordered)
    assert _registry_key(arguments_hash=hash_one) == _registry_key(arguments_hash=hash_two)
    assert _ledger_key(arguments_hash=hash_one) == _ledger_key(arguments_hash=hash_two)


@pytest.mark.parametrize("schema_version", ["1", "tool-intent/1", 1.0, None])
def test_non_integer_schema_version_is_rejected_on_both_sides(schema_version: object) -> None:
    with pytest.raises(registry.ToolContractError) as registry_error:
        _registry_key(schema_version=schema_version)
    with pytest.raises(ledger.LedgerError):
        _ledger_key(schema_version=schema_version)
    assert registry_error.value.code == "TOOL_CONTRACT_ERROR"


def test_bool_schema_version_is_not_silently_converted() -> None:
    """``True`` 是 int 子类：两侧都按 ``true`` 序列化（不转成 1），且结果相等。"""

    registry_key = _registry_key(schema_version=True)
    ledger_key = _ledger_key(schema_version=True)
    assert registry_key == ledger_key
    assert registry_key != _registry_key(schema_version=1)


def test_argument_type_is_not_implicitly_converted() -> None:
    int_form = {"attempt": 1}
    str_form = {"attempt": "1"}
    assert ledger.arguments_hash(int_form) != ledger.arguments_hash(str_form)
    assert registry.compute_arguments_hash(int_form) != registry.compute_arguments_hash(str_form)

    int_registry = _registry_key(arguments_hash=ledger.arguments_hash(int_form))
    int_ledger = _ledger_key(arguments_hash=ledger.arguments_hash(int_form))
    str_registry = _registry_key(arguments_hash=ledger.arguments_hash(str_form))
    str_ledger = _ledger_key(arguments_hash=ledger.arguments_hash(str_form))
    assert int_registry == int_ledger
    assert str_registry == str_ledger
    assert int_registry != str_registry


def test_legacy_string_schema_version_would_have_diverged() -> None:
    """复现修复前的缺口：旧 registry 用字符串 ``"tool-intent/1"`` → key 与 T06 不同。"""

    legacy_payload = {
        **_base_fields(),
        "schema_version": "tool-intent/1",
        "run_id": str(RUN),
        "response_artifact_id": str(ARTIFACT),
    }
    legacy_key = hashlib.sha256(ledger.canonical_json(legacy_payload).encode("utf-8")).hexdigest()

    assert legacy_key != _ledger_key()  # 旧算法的 key 确实对不上 T06
    assert _registry_key() == _ledger_key()  # 修复后两侧一致


# ---------------------------------------------------------------------------
# 4) 公开 API 不破坏 + 无 Run 草稿边界
# ---------------------------------------------------------------------------
def test_public_api_signature_is_preserved() -> None:
    parameters = inspect.signature(registry.compute_intent_key).parameters
    assert list(parameters) == [
        "schema_version",
        "run_id",
        "response_artifact_id",
        "call_ordinal",
        "name",
        "tool_version",
        "arguments_hash",
        "scope_hash",
    ]
    assert all(item.kind is inspect.Parameter.KEYWORD_ONLY for item in parameters.values())
    assert parameters["schema_version"].default == ledger.INTENT_SCHEMA_VERSION
    assert registry.SCHEMA_VERSION == ledger.INTENT_SCHEMA_VERSION


def test_runless_draft_key_is_not_a_cross_component_key() -> None:
    """``run_id=None`` 只是未绑定 Run 的草稿；T06 账本拒绝它（key 必须绑定已持久 Run）。"""

    draft_one = _registry_key(run_id=None)
    draft_two = _registry_key(run_id=None)
    assert draft_one == draft_two
    assert len(draft_one) == 64
    assert draft_one != _registry_key()

    with pytest.raises(TypeError):
        ledger.intent_key(  # type: ignore[call-arg]
            schema_version=ledger.INTENT_SCHEMA_VERSION,
            response_artifact_id=ARTIFACT,
            call_ordinal=0,
            name="site.read_counter",
            tool_version="1.0.0",
            arguments_hash=ARGUMENTS_HASH,
            scope_hash=SCOPE_HASH,
        )
