"""P0-T19 窄工具 Registry：白名单解析、参数 schema 校验、稳定 intent key 与审批门。

契约来源
--------
* ``docs/poc/07_Contract_Closure.md`` C-04（第 88/89/91 条）：
  ``intent_key = SHA256(canonical(schema_version, run_id, response_artifact_id,
  call_ordinal, name, tool_version, arguments_hash, scope_hash))``；
  ``scope_hash`` 覆盖主体、具体资源、操作与授权范围；同 ordinal 参数漂移必须失败；
  改变绑定只能生成新意图，不得修改旧 ``tool_call_id`` 或挪用旧批准。
* ``docs/tdd/02_Agent_Harness.md`` §3：Registry 在装配时拒绝重复 name/version；
  工具 schema/授权改变即失效旧意图；§4：审批绑定 arguments_hash/scope_hash/tool_version。
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T19 卡：一个 provider + 三个受控工具的
  typed 调用契约；重名注册拒绝、未授权/截断参数执行 0。

**intent_key 单一实现（W5 门禁修缺口）**：``compute_intent_key`` 不再本地重写 C-04
公式，而是把 ``run_id`` 存在的调用**直接委托**给 T06 ``tool_ledger.intent_key``；
``schema_version`` 统一为 T06 的整数 ``INTENT_SCHEMA_VERSION``。历史版本本模块用
字符串 ``"tool-intent/1"``，导致 T19 与 T06 对同一调用算出不同 key、跨组件去重
静默失效；现在两侧逐字节一致。``run_id is None`` 只是未绑定 Run 的草稿键（T06 账本
拒绝 ``None``），不可作为跨组件持久键。

边界（本任务**不做**）
----------------------
* **没有执行器**：本模块只做 resolve → validate → intent → 审批门检查；
  真正的 ``ToolExecutor.execute`` 与 ApprovalManager 在 T21/T06。
* 不做策略引擎：``requires_approval`` 是工具声明的事实位，是否放行由 PolicyEngine 决定。
* 不落库：``ToolIntent`` 只是可持久化的 DTO；``tool_calls`` 表归 T21。
* 三个工具对齐 T04 控制站的路由（``/idem/count``、``/nonidem/count``、``/idem/write``、
  ``/fault/<name>``），但本模块**不发起任何 HTTP 或网络调用**。

deny 默认
---------
* 未知工具直接拒绝（``UnknownToolError``），不是「返回空列表」。
* 参数 schema 不完整、含不支持关键字、``additionalProperties`` 未显式 false、
  写/外部副作用工具未声明 ``requires_approval`` —— 全部在**注册时**拒绝。
* 模型/客户端提交 ``workspace_id`` 等作用域字段 → ``ToolScopeInjectionError``。
* 需要审批却没有有效批准、批准绑定不同 intent、批准过期 → 一律不得执行。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from contracts import ResourceRef, WorkspaceScope, compute_resource_scope_hash
from model_gateway import ToolDeclaration, canonical_json
from tool_ledger import INTENT_SCHEMA_VERSION
from tool_ledger import LedgerError as _ToolLedgerError
from tool_ledger import canonical_json as _ledger_canonical_json
from tool_ledger import intent_key as _ledger_intent_key

__all__ = [
    "APPROVAL_REQUIRED",
    "CANONICALIZATION_VERSION",
    "PHASE0_TOOL_SPECS",
    "SCHEMA_VERSION",
    "ApprovalExpiredError",
    "ApprovalMismatchError",
    "ApprovalRef",
    "ApprovalRequiredError",
    "ArgumentError",
    "ArgumentsTooLargeError",
    "DuplicateToolRegistrationError",
    "InvalidToolArgumentsError",
    "ToolContractError",
    "ToolIntent",
    "ToolRegistry",
    "ToolScopeInjectionError",
    "ToolSpec",
    "UnknownToolError",
    "UnknownToolVersionError",
    "ValidatedArguments",
    "assert_executable",
    "build_phase0_registry",
    "compute_arguments_hash",
    "compute_intent_key",
    "compute_scope_hash",
    "prepare_intent",
    "validate_arguments",
]

#: 与 T06 ``tool_ledger.INTENT_SCHEMA_VERSION`` 同源（整数）。旧版本这里是字符串
#: ``"tool-intent/1"``，导致同一调用在 T06 与本模块算出不同 intent_key（跨组件去重静默
#: 失效）；现已统一为同一整数常量，并由 ``compute_intent_key`` 直接委托 T06 实现。
SCHEMA_VERSION = INTENT_SCHEMA_VERSION
CANONICALIZATION_VERSION = "json-c14n/1"
APPROVAL_REQUIRED = "TOOL_APPROVAL_REQUIRED"

_SIDE_EFFECTS: tuple[str, ...] = ("none", "write", "external")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# 模型/客户端绝不能提交这些字段（作用域只能由服务端注入）
_SCOPE_ARGUMENT_KEYS: frozenset[str] = frozenset(
    {"workspace_id", "principal_id", "policy_snapshot_id", "scope", "scope_hash"}
)

_MAX_ARGUMENT_BYTES = 16 * 1024
_MAX_TOOL_NAME_LENGTH = 64


# ---------------------------------------------------------------------------
# 错误（稳定 code；证据只引用 code）
# ---------------------------------------------------------------------------
class ToolContractError(ValueError):
    """工具契约错误基类；``code`` 稳定，``field_path`` 便于定位。"""

    code: str = "TOOL_CONTRACT_ERROR"

    def __init__(self, message: str, *, field_path: str | None = None) -> None:
        super().__init__(message)
        self.field_path = field_path

    def __str__(self) -> str:
        if self.field_path:
            return f"{self.code}: {super().__str__()} (field={self.field_path})"
        return f"{self.code}: {super().__str__()}"


class ToolSchemaInvalidError(ToolContractError):
    code = "TOOL_SCHEMA_INVALID"


class DuplicateToolRegistrationError(ToolContractError):
    code = "TOOL_DUPLICATE_REGISTRATION"


class UnknownToolError(ToolContractError):
    code = "TOOL_UNKNOWN"


class UnknownToolVersionError(ToolContractError):
    code = "TOOL_VERSION_UNKNOWN"


class AmbiguousToolVersionError(ToolContractError):
    code = "TOOL_VERSION_AMBIGUOUS"


class InvalidToolArgumentsError(ToolContractError):
    code = "TOOL_ARGUMENTS_INVALID"

    def __init__(self, errors: Sequence[ArgumentError]) -> None:
        self.errors: tuple[ArgumentError, ...] = tuple(errors)
        summary = "; ".join(f"{item.field_path}: {item.code}" for item in self.errors[:5])
        super().__init__(f"{len(self.errors)} 处参数错误: {summary}")


class ArgumentsTooLargeError(ToolContractError):
    code = "TOOL_ARGUMENTS_TOO_LARGE"


class ToolScopeInjectionError(ToolContractError):
    code = "TOOL_SCOPE_INJECTION"


class ApprovalRequiredError(ToolContractError):
    code = "TOOL_APPROVAL_REQUIRED"


class ApprovalMismatchError(ToolContractError):
    code = "TOOL_APPROVAL_MISMATCH"


class ApprovalExpiredError(ToolContractError):
    code = "TOOL_APPROVAL_EXPIRED"


@dataclass(frozen=True, slots=True)
class ArgumentError:
    """单个参数错误；``code`` 固定枚举，便于跨任务断言。"""

    field_path: str
    code: Literal[
        "MISSING_REQUIRED",
        "UNKNOWN_FIELD",
        "TYPE_MISMATCH",
        "ENUM_MISMATCH",
        "RANGE_VIOLATION",
        "LENGTH_VIOLATION",
    ]
    message: str


# ---------------------------------------------------------------------------
# schema 子集（显式白名单；不支持的关键字在注册期 fail closed）
# ---------------------------------------------------------------------------
_OBJECT_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "description"})
_STRING_KEYS = frozenset({"type", "description", "enum", "minLength", "maxLength"})
_NUMBER_KEYS = frozenset({"type", "description", "enum", "minimum", "maximum"})
_BOOLEAN_KEYS = frozenset({"type", "description"})
_ARRAY_KEYS = frozenset({"type", "description", "items", "minItems", "maxItems"})
_JSON_TYPES: tuple[str, ...] = ("object", "string", "integer", "number", "boolean", "array")


def _schema_error(message: str, *, field_path: str) -> ToolSchemaInvalidError:
    return ToolSchemaInvalidError(message, field_path=field_path)


def _validate_schema_node(node: object, *, field_path: str) -> None:
    """递归校验工具参数 schema 是否落在受支持子集内（fail closed）。"""

    if not isinstance(node, dict):
        raise _schema_error("schema 节点必须是 object", field_path=field_path)
    type_name = node.get("type")
    if type_name not in _JSON_TYPES:
        raise _schema_error(f"不支持的 type={type_name!r}", field_path=field_path)
    allowed = {
        "object": _OBJECT_KEYS,
        "string": _STRING_KEYS,
        "integer": _NUMBER_KEYS,
        "number": _NUMBER_KEYS,
        "boolean": _BOOLEAN_KEYS,
        "array": _ARRAY_KEYS,
    }[type_name]
    unknown = sorted(set(node) - allowed)
    if unknown:
        raise _schema_error(f"不支持的 schema 关键字 {unknown}", field_path=field_path)

    if type_name == "object":
        properties = node.get("properties")
        if not isinstance(properties, dict) or not properties:
            raise _schema_error("object 需要非空 properties", field_path=field_path)
        if node.get("additionalProperties") is not False:
            raise _schema_error(
                "object 必须显式 additionalProperties=false（未知字段行为必须明确）",
                field_path=field_path,
            )
        required = node.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise _schema_error("required 必须是字符串数组", field_path=field_path)
        missing = sorted(set(required) - set(properties))
        if missing:
            raise _schema_error(f"required 含未声明字段 {missing}", field_path=field_path)
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise _schema_error("properties 的键必须是非空字符串", field_path=field_path)
            _validate_schema_node(child, field_path=f"{field_path}.{name}")
        return

    if type_name == "array":
        items = node.get("items")
        if items is None:
            raise _schema_error("array 必须声明 items（不接受任意元素）", field_path=field_path)
        _validate_schema_node(items, field_path=f"{field_path}[]")
        _validate_bounds(node, field_path=field_path, low_key="minItems", high_key="maxItems")
        return

    if "enum" in node:
        enum = node["enum"]
        if not isinstance(enum, list) or not enum:
            raise _schema_error("enum 必须是非空数组", field_path=field_path)
        for item in enum:
            _check_scalar_type(type_name, item, field_path=field_path)
    if type_name in ("integer", "number"):
        _validate_bounds(node, field_path=field_path, low_key="minimum", high_key="maximum")
    if type_name == "string":
        _validate_bounds(node, field_path=field_path, low_key="minLength", high_key="maxLength")


def _validate_bounds(
    node: Mapping[str, Any], *, field_path: str, low_key: str, high_key: str
) -> None:
    low = node.get(low_key)
    high = node.get(high_key)
    for key, value in ((low_key, low), (high_key, high)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _schema_error(f"{key} 必须是数字", field_path=field_path)
    if low is not None and high is not None and low > high:
        raise _schema_error(f"{low_key} 不得大于 {high_key}", field_path=field_path)


def _check_scalar_type(type_name: str, value: object, *, field_path: str) -> None:
    if type_name == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif type_name == "number":
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    elif type_name == "string":
        ok = isinstance(value, str)
    else:  # pragma: no cover - boolean 不应出现在 enum 校验里
        ok = isinstance(value, bool)
    if not ok:
        raise _schema_error(f"enum 值 {value!r} 与 type={type_name} 不符", field_path=field_path)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个 name+version 的工具定义；``schema_hash`` 由 schema 内容派生。"""

    name: str
    version: str
    description: str
    parameters: Mapping[str, Any]
    readonly: bool
    side_effect: Literal["none", "write", "external"]
    requires_approval: bool
    timeout_seconds: float = 30.0
    max_output_bytes: int = 65536

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise ToolSchemaInvalidError(f"工具名非法: {self.name!r}", field_path="name")
        if not isinstance(self.version, str) or not _VERSION_RE.match(self.version):
            raise ToolSchemaInvalidError(
                f"版本必须是 x.y.z: {self.version!r}", field_path="version"
            )
        if not isinstance(self.description, str) or not 1 <= len(self.description) <= 500:
            raise ToolSchemaInvalidError(
                "description 长度必须在 [1, 500]", field_path="description"
            )
        _validate_schema_node(self.parameters, field_path="parameters")
        if self.parameters.get("type") != "object":  # 工具参数根必须是 object
            raise ToolSchemaInvalidError(
                "工具参数根 schema 的 type 必须是 object", field_path="parameters"
            )
        if self.side_effect not in _SIDE_EFFECTS:
            raise ToolSchemaInvalidError(
                f"side_effect 非法: {self.side_effect!r}", field_path="side_effect"
            )
        if self.readonly != (self.side_effect == "none"):
            raise ToolSchemaInvalidError(
                "readonly 必须与 side_effect 一致（none ⇔ readonly=True）", field_path="readonly"
            )
        if self.side_effect != "none" and not self.requires_approval:
            raise ToolSchemaInvalidError(
                "写/外部副作用工具必须显式 requires_approval=True", field_path="requires_approval"
            )
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, int | float
        ):
            raise ToolSchemaInvalidError("timeout_seconds 必须是数字", field_path="timeout_seconds")
        if not 0.1 <= float(self.timeout_seconds) <= 600.0:
            raise ToolSchemaInvalidError(
                "timeout_seconds 必须在 [0.1, 600]", field_path="timeout_seconds"
            )
        if isinstance(self.max_output_bytes, bool) or not isinstance(self.max_output_bytes, int):
            raise ToolSchemaInvalidError(
                "max_output_bytes 必须是 int", field_path="max_output_bytes"
            )
        if not 1 <= self.max_output_bytes <= 1024 * 1024:
            raise ToolSchemaInvalidError(
                "max_output_bytes 必须在 [1, 1MiB]", field_path="max_output_bytes"
            )

    @property
    def schema_hash(self) -> str:
        """工具 schema 漂移检测：name/version/schema/规范化版本任一变化都会变。"""

        payload = {
            "name": self.name,
            "version": self.version,
            "parameters": self.parameters,
            "canonicalization_version": CANONICALIZATION_VERSION,
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def declaration(self) -> ToolDeclaration:
        return ToolDeclaration(
            name=self.name,
            version=self.version,
            description=self.description,
            parameters_schema=self.parameters,
            schema_hash=self.schema_hash,
        )


@dataclass(frozen=True, slots=True)
class ValidatedArguments:
    """通过 schema 校验的参数及其规范化事实（供 intent key 使用）。"""

    spec: ToolSpec
    arguments: Mapping[str, Any]
    canonical: str
    arguments_hash: str
    size_bytes: int


def validate_arguments(spec: ToolSpec, arguments: object) -> ValidatedArguments:
    """严格校验参数：缺必填/多未知字段/类型不符全部拒绝（不静默修正）。"""

    if not isinstance(arguments, Mapping):
        raise InvalidToolArgumentsError(
            [ArgumentError("<root>", "TYPE_MISMATCH", "arguments 必须是 JSON object")]
        )
    if not isinstance(spec, ToolSpec):
        raise ToolSchemaInvalidError("validate_arguments 需要 ToolSpec")
    for key in arguments:
        if key in _SCOPE_ARGUMENT_KEYS:
            raise ToolScopeInjectionError(
                f"参数含作用域字段 {key!r}；workspace 只能由服务端注入", field_path=str(key)
            )
    try:
        canonical = canonical_json(dict(arguments))
    except (TypeError, ValueError) as exc:
        raise InvalidToolArgumentsError(
            [ArgumentError("<root>", "TYPE_MISMATCH", f"参数不是合法 JSON 值: {exc}")]
        ) from exc
    size_bytes = len(canonical.encode("utf-8"))
    if size_bytes > _MAX_ARGUMENT_BYTES:
        raise ArgumentsTooLargeError(
            f"参数 {size_bytes} 字节超过上限 {_MAX_ARGUMENT_BYTES}", field_path="<root>"
        )
    errors: list[ArgumentError] = []
    _validate_value(spec.parameters, dict(arguments), path="", errors=errors)
    if errors:
        raise InvalidToolArgumentsError(errors)
    return ValidatedArguments(
        spec=spec,
        arguments=dict(arguments),
        canonical=canonical,
        arguments_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        size_bytes=size_bytes,
    )


def _validate_value(
    schema: Mapping[str, Any], value: object, *, path: str, errors: list[ArgumentError]
) -> None:
    type_name = schema["type"]
    if type_name == "object":
        if not isinstance(value, Mapping):
            errors.append(ArgumentError(path or "<root>", "TYPE_MISMATCH", "必须是 object"))
            return
        properties: Mapping[str, Any] = schema["properties"]
        required: Sequence[str] = schema.get("required", ())
        for name in required:
            if name not in value:
                errors.append(ArgumentError(name, "MISSING_REQUIRED", f"缺少必填字段 {name!r}"))
        for name, item in value.items():
            child_path = f"{path}.{name}" if path else str(name)
            if name not in properties:
                errors.append(
                    ArgumentError(child_path, "UNKNOWN_FIELD", f"未声明字段 {name!r} 一律拒绝")
                )
                continue
            _validate_value(properties[name], item, path=child_path, errors=errors)
        return

    if type_name == "array":
        if not isinstance(value, list):
            errors.append(ArgumentError(path, "TYPE_MISMATCH", "必须是 array"))
            return
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if min_items is not None and len(value) < min_items:
            errors.append(ArgumentError(path, "RANGE_VIOLATION", f"元素数少于 {min_items}"))
        if max_items is not None and len(value) > max_items:
            errors.append(ArgumentError(path, "RANGE_VIOLATION", f"元素数多于 {max_items}"))
        for index, item in enumerate(value):
            _validate_value(schema["items"], item, path=f"{path}[{index}]", errors=errors)
        return

    if type_name == "string":
        if not isinstance(value, str):
            errors.append(ArgumentError(path, "TYPE_MISMATCH", "必须是 string"))
            return
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if min_length is not None and len(value) < min_length:
            errors.append(ArgumentError(path, "LENGTH_VIOLATION", f"长度小于 {min_length}"))
        if max_length is not None and len(value) > max_length:
            errors.append(ArgumentError(path, "LENGTH_VIOLATION", f"长度大于 {max_length}"))
    elif type_name == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(ArgumentError(path, "TYPE_MISMATCH", "必须是 integer（bool 不算）"))
            return
        _validate_numeric_bounds(schema, value, path=path, errors=errors)
    elif type_name == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            errors.append(ArgumentError(path, "TYPE_MISMATCH", "必须是 number"))
            return
        _validate_numeric_bounds(schema, value, path=path, errors=errors)
    elif type_name == "boolean" and not isinstance(value, bool):
        errors.append(ArgumentError(path, "TYPE_MISMATCH", "必须是 boolean"))
        return

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append(ArgumentError(path, "ENUM_MISMATCH", f"取值不在 {list(enum)}"))


def _validate_numeric_bounds(
    schema: Mapping[str, Any], value: int | float, *, path: str, errors: list[ArgumentError]
) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None and value < minimum:
        errors.append(ArgumentError(path, "RANGE_VIOLATION", f"小于最小值 {minimum}"))
    if maximum is not None and value > maximum:
        errors.append(ArgumentError(path, "RANGE_VIOLATION", f"大于最大值 {maximum}"))


# ---------------------------------------------------------------------------
# Registry（白名单；装配期拒绝重复 name+version）
# ---------------------------------------------------------------------------
class ToolRegistry:
    """白名单工具注册表：未知工具/版本一律显式拒绝，不做「空结果」降级。"""

    __slots__ = ("_by_name", "_max_tools")

    def __init__(self, specs: Iterable[ToolSpec] = (), *, max_tools: int = 64) -> None:
        if (
            isinstance(max_tools, bool)
            or not isinstance(max_tools, int)
            or not 1 <= max_tools <= 1024
        ):
            raise ToolSchemaInvalidError("max_tools 必须在 [1, 1024]")
        self._by_name: dict[str, dict[str, ToolSpec]] = {}
        self._max_tools = max_tools
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if not isinstance(spec, ToolSpec):
            raise ToolSchemaInvalidError(f"只接受 ToolSpec，收到 {type(spec).__name__}")
        versions = self._by_name.setdefault(spec.name, {})
        if spec.version in versions:
            raise DuplicateToolRegistrationError(
                f"重复注册 {spec.name}@{spec.version}；Registry 拒绝覆盖已装配定义",
                field_path=spec.name,
            )
        total = sum(len(item) for item in self._by_name.values())
        if total >= self._max_tools:
            raise ToolSchemaInvalidError(f"工具数已达上限 {self._max_tools}，拒绝静默扩容")
        versions[spec.version] = spec

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            self._by_name[name][version]
            for name in sorted(self._by_name)
            for version in sorted(self._by_name[name])
        )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def versions(self, name: str) -> tuple[str, ...]:
        if not isinstance(name, str) or name not in self._by_name:
            raise UnknownToolError(
                f"未知工具 {name!r}；白名单只有 {list(self.names())}", field_path=str(name)
            )
        return tuple(sorted(self._by_name[name]))

    def resolve(self, *, scope: WorkspaceScope, name: str, version: str | None = None) -> ToolSpec:
        """按 scope 解析工具；scope 必须是服务端签发对象（此处只做类型与一致性校验）。"""

        if not isinstance(scope, WorkspaceScope):
            raise ToolScopeInjectionError("resolve 需要服务端签发的 WorkspaceScope")
        versions = self.versions(name)
        if version is None:
            if len(versions) != 1:
                raise AmbiguousToolVersionError(
                    f"工具 {name!r} 有多个版本 {list(versions)}，必须显式指定",
                    field_path=name,
                )
            version = versions[0]
        if version not in versions:
            raise UnknownToolVersionError(
                f"工具 {name!r} 没有版本 {version!r}；已注册 {list(versions)}", field_path=name
            )
        return self._by_name[name][version]

    def declarations(
        self, *, scope: WorkspaceScope, names: Sequence[str] | None = None
    ) -> tuple[ToolDeclaration, ...]:
        """授权后传给模型的 ToolSpec 子集（默认全部；名字不存在则整体拒绝）。"""

        if not isinstance(scope, WorkspaceScope):
            raise ToolScopeInjectionError("declarations 需要服务端签发的 WorkspaceScope")
        selected = self.names() if names is None else tuple(names)
        return tuple(self.resolve(scope=scope, name=name).declaration() for name in selected)


# ---------------------------------------------------------------------------
# 稳定 intent key（C-04 公式，逐字段 canonical）
# ---------------------------------------------------------------------------
def compute_arguments_hash(arguments: Mapping[str, Any]) -> str:
    """规范化参数哈希：排序键、无空白、禁 NaN；非 JSON 值直接拒绝。"""

    if not isinstance(arguments, Mapping):
        raise InvalidToolArgumentsError(
            [ArgumentError("<root>", "TYPE_MISMATCH", "arguments 必须是 object")]
        )
    canonical = canonical_json(dict(arguments))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_scope_hash(scope: WorkspaceScope, resource_refs: Sequence[ResourceRef]) -> str:
    """C-04：scope_hash 覆盖主体、具体资源、操作与授权范围（此处主体+资源）。"""

    if not isinstance(scope, WorkspaceScope):
        raise ToolScopeInjectionError("compute_scope_hash 需要服务端签发的 WorkspaceScope")
    resource_scope_hash = compute_resource_scope_hash(
        workspace_id=scope.workspace_id, resource_refs=resource_refs
    )
    payload = {
        "workspace_id": str(scope.workspace_id),
        "principal_id": str(scope.principal_id),
        "policy_snapshot_id": str(scope.policy_snapshot_id),
        "resource_scope_hash": resource_scope_hash,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_intent_key(
    *,
    schema_version: int = SCHEMA_VERSION,
    run_id: UUID | None,
    response_artifact_id: UUID,
    call_ordinal: int,
    name: str,
    tool_version: str,
    arguments_hash: str,
    scope_hash: str,
) -> str:
    """C-04 稳定意图键；与 T06 ``tool_ledger.intent_key`` **同一实现**。

    - 有 ``run_id`` 时直接委托 ``tool_ledger.intent_key``：字段集合、canonical JSON 与
      参数校验全部共用，因此 T19 契约与 T06 账本对同一调用得到逐字节相同的 key。
    - ``schema_version`` 统一为 T06 的整数 ``INTENT_SCHEMA_VERSION``（默认 1）；字符串
      （如旧的 ``"tool-intent/1"`` 或 ``"1"``）一律拒绝，不做隐式转换——两侧行为一致。
    - ``run_id is None`` 是**无 Run 草稿**：T06 账本明确拒绝 ``None``（key 必须绑定已持久
      Run），这里仅用同一 canonical 规则做本地预览哈希，不能当跨组件去重键；T21 注入
      真实 ``run_id`` 后自然走委托分支。
    """

    if run_id is not None:
        try:
            return _ledger_intent_key(
                schema_version=schema_version,
                run_id=run_id,
                response_artifact_id=response_artifact_id,
                call_ordinal=call_ordinal,
                name=name,
                tool_version=tool_version,
                arguments_hash=arguments_hash,
                scope_hash=scope_hash,
            )
        except _ToolLedgerError as exc:
            raise ToolContractError(str(exc), field_path="intent_key") from exc

    # 无 Run 草稿：镜像 T06 intent_key 的字段校验 + canonical JSON（不另写一套序列化）。
    if not isinstance(schema_version, int) or schema_version < 1:
        raise ToolContractError(
            f"schema_version 必须是 >= 1 的 int（与 T06 INTENT_SCHEMA_VERSION 同源），"
            f"收到 {schema_version!r}",
            field_path="schema_version",
        )
    if not isinstance(response_artifact_id, UUID):
        raise ToolContractError(
            "response_artifact_id 必须是 uuid.UUID", field_path="response_artifact_id"
        )
    if not isinstance(call_ordinal, int) or isinstance(call_ordinal, bool) or call_ordinal < 0:
        raise ToolContractError("call_ordinal 必须是 >= 0 的 int", field_path="call_ordinal")
    for label, value in (("name", name), ("tool_version", tool_version)):
        if not isinstance(value, str) or not value:
            raise ToolContractError(f"{label} 必须是非空字符串", field_path=label)
    for label, value in (("arguments_hash", arguments_hash), ("scope_hash", scope_hash)):
        if not isinstance(value, str) or _HEX64_RE.match(value) is None:
            raise ToolContractError(f"{label} 必须是 64 位 sha256 hex", field_path=label)
    payload = {
        "schema_version": schema_version,
        "run_id": None,
        "response_artifact_id": str(response_artifact_id),
        "call_ordinal": call_ordinal,
        "name": name,
        "tool_version": tool_version,
        "arguments_hash": arguments_hash,
        "scope_hash": scope_hash,
    }
    return hashlib.sha256(_ledger_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolIntent:
    """可持久化的稳定工具意图；同一语义重算必须得到同一 ``intent_key``。"""

    intent_key: str
    name: str
    tool_version: str
    workspace_id: UUID
    principal_id: UUID
    policy_snapshot_id: UUID
    arguments: Mapping[str, Any]
    arguments_hash: str
    scope_hash: str
    response_artifact_id: UUID
    call_ordinal: int
    run_id: UUID | None
    requires_approval: bool
    schema_hash: str
    schema_version: int = SCHEMA_VERSION
    canonicalization_version: str = CANONICALIZATION_VERSION

    def recompute_key(self) -> str:
        return compute_intent_key(
            schema_version=self.schema_version,
            run_id=self.run_id,
            response_artifact_id=self.response_artifact_id,
            call_ordinal=self.call_ordinal,
            name=self.name,
            tool_version=self.tool_version,
            arguments_hash=self.arguments_hash,
            scope_hash=self.scope_hash,
        )

    def verify(self) -> None:
        """重算并比对；任何字段漂移都视为意图损坏。"""

        if self.recompute_key() != self.intent_key:
            raise ToolContractError(
                "intent_key 与字段不一致（疑似换参/漂移）", field_path="intent_key"
            )


def prepare_intent(
    registry: ToolRegistry,
    *,
    scope: WorkspaceScope,
    resource_refs: Sequence[ResourceRef],
    response_artifact_id: UUID,
    call_ordinal: int,
    name: str,
    arguments: object,
    version: str | None = None,
    run_id: UUID | None = None,
) -> tuple[ToolIntent, ValidatedArguments]:
    """resolve → 参数校验 → 稳定 intent；返回 (intent, validated) 供执行/审批层使用。"""

    if not isinstance(response_artifact_id, UUID):
        raise ToolContractError(
            "response_artifact_id 必须是 UUID", field_path="response_artifact_id"
        )
    spec = registry.resolve(scope=scope, name=name, version=version)
    validated = validate_arguments(spec, arguments)
    scope_hash = compute_scope_hash(scope, resource_refs)
    intent_key = compute_intent_key(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        response_artifact_id=response_artifact_id,
        call_ordinal=call_ordinal,
        name=spec.name,
        tool_version=spec.version,
        arguments_hash=validated.arguments_hash,
        scope_hash=scope_hash,
    )
    intent = ToolIntent(
        intent_key=intent_key,
        name=spec.name,
        tool_version=spec.version,
        workspace_id=scope.workspace_id,
        principal_id=scope.principal_id,
        policy_snapshot_id=scope.policy_snapshot_id,
        arguments=validated.arguments,
        arguments_hash=validated.arguments_hash,
        scope_hash=scope_hash,
        response_artifact_id=response_artifact_id,
        call_ordinal=call_ordinal,
        run_id=run_id,
        requires_approval=spec.requires_approval,
        schema_hash=spec.schema_hash,
    )
    intent.verify()
    return intent, validated


# ---------------------------------------------------------------------------
# 审批门（只做「未批准不得执行」的拒绝路径；链路归 T06/T21）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ApprovalRef:
    """审批引用（T06 的 ApprovalManager 是唯一写入者；这里只读校验）。"""

    approval_id: UUID
    intent_key: str
    status: Literal["pending", "approved", "rejected", "consumed", "revoked"]
    expires_at: datetime | None = None
    approver_principal_id: UUID | None = None

    def is_usable(self, now: datetime) -> bool:
        if self.status != "approved":
            return False
        return self.expires_at is None or now < self.expires_at


def assert_executable(
    intent: ToolIntent,
    *,
    approval: ApprovalRef | None = None,
    now: datetime | None = None,
) -> UUID | None:
    """执行前门禁：返回可用 approval_id（不需要审批时为 None），否则抛错。"""

    if not isinstance(intent, ToolIntent):
        raise ToolContractError("assert_executable 需要 ToolIntent")
    if not intent.requires_approval:
        return None
    if approval is None:
        raise ApprovalRequiredError(
            f"工具 {intent.name} 需要审批；未批准不得执行（审批链路归 T06/T21）",
            field_path="approval",
        )
    if not isinstance(approval, ApprovalRef):
        raise ApprovalMismatchError("approval 必须是 ApprovalRef", field_path="approval")
    if approval.intent_key != intent.intent_key:
        raise ApprovalMismatchError(
            "批准绑定的是另一个 intent_key（换参/换范围必须新建意图与审批）",
            field_path="intent_key",
        )
    if approval.status != "approved":
        raise ApprovalMismatchError(
            f"批准状态为 {approval.status!r}，不得执行", field_path="approval.status"
        )
    moment = now or datetime.now(UTC)
    if approval.expires_at is not None and moment >= approval.expires_at:
        raise ApprovalExpiredError("批准已过期，不得执行", field_path="approval.expires_at")
    return approval.approval_id


# ---------------------------------------------------------------------------
# P0 三个受控工具（对齐 T04 控制站路由；本模块不执行）
# ---------------------------------------------------------------------------
READ_COUNTER = ToolSpec(
    name="site.read_counter",
    version="1.0.0",
    description="读取 T04 控制站的幂等/非幂等写入计数（只读，无外部副作用）。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "enum": ["/idem/count", "/nonidem/count"],
                "description": "计数端点",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    readonly=True,
    side_effect="none",
    requires_approval=False,
)

IDEM_WRITE = ToolSpec(
    name="site.idem_write",
    version="1.0.0",
    description="向 T04 控制站发起一次幂等写（POST /idem/write）；写操作必须审批。",
    parameters={
        "type": "object",
        "properties": {
            "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
            "body": {
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": 512}},
                "required": ["text"],
                "additionalProperties": False,
            },
        },
        "required": ["idempotency_key", "body"],
        "additionalProperties": False,
    },
    readonly=False,
    side_effect="write",
    requires_approval=True,
    timeout_seconds=10.0,
)

ERROR_PROBE = ToolSpec(
    name="site.error_probe",
    version="1.0.0",
    description="触发 T04 控制站故障入口，用于错误分类演练（外部副作用，必须审批）。",
    parameters={
        "type": "object",
        "properties": {
            "fault": {"type": "string", "enum": ["http_500", "http_429", "timeout"]},
            "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 30000},
        },
        "required": ["fault"],
        "additionalProperties": False,
    },
    readonly=False,
    side_effect="external",
    requires_approval=True,
    timeout_seconds=5.0,
)

PHASE0_TOOL_SPECS: tuple[ToolSpec, ...] = (READ_COUNTER, IDEM_WRITE, ERROR_PROBE)


def build_phase0_registry() -> ToolRegistry:
    """装配 P0 白名单：三个受控工具（只读 / 写 / 错误探针）。"""

    return ToolRegistry(PHASE0_TOOL_SPECS)
