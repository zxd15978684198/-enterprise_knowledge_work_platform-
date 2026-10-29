"""P0-T02 环境 preflight（真实断言；禁止 skip/xfail/空测试冒充通过）。

对照 docs/poc/03_Phase0_Task_Backlog.md 的 P0-T02「量化验收」：
核心包与镜像 100% 有精确版本/hash、环境可隔离重建、零真实秘密、
未就绪 profile 必须显式标注而不是被静默忽略。

运行方式（显式 config，避免落到仓库根 pyproject.toml 的 rootdir）：
    uv run --project experiments/phase0 pytest \\
        -c experiments/phase0/pyproject.toml tests/phase0/test_environment.py -q
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import psycopg
import redis
import sqlalchemy
import yaml

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "experiments" / "phase0"
TESTS = ROOT / "tests" / "phase0"
PYPROJECT = PROJECT / "pyproject.toml"
ENV_EXAMPLE = PROJECT / ".env.example"
COMPOSE = PROJECT / "compose.yaml"
UV_LOCK = PROJECT / "uv.lock"
EVIDENCE = ROOT / "docs" / "poc" / "evidence" / "environment.md"

# 核心依赖：SQLAlchemy（async PG 方言）+ psycopg 3（sync/async 双覆盖）+ Redis 客户端
# + 测试与静态检查工具。精确版本由 uv.lock 锁定，这里只校验「已锁定且已安装」。
LOCKED_CORE = (
    "sqlalchemy",
    "psycopg",
    "psycopg-binary",
    "redis",
    "pyyaml",
    "pytest",
    "pytest-asyncio",
    "ruff",
)

REQUIRED_ENV_KEYS = (
    "PHASE0_COMPOSE_PROJECT",
    "PHASE0_PROFILE",
    "PHASE0_PG_HOST",
    "PHASE0_PG_HOST_PORT",
    "PHASE0_PG_USER",
    "PHASE0_PG_PASSWORD",
    "PHASE0_PG_DB",
    "PHASE0_PG_SSLMODE",
    "PHASE0_PG_DSN",
    "PHASE0_REDIS_HOST",
    "PHASE0_REDIS_HOST_PORT",
    "PHASE0_REDIS_DB",
    "PHASE0_REDIS_URL",
)

SECRET_PATTERNS = (
    r"AKIA[0-9A-Z]{16}",
    r"ASIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"ghp_[A-Za-z0-9]{20,}",
    r"sk-[A-Za-z0-9]{20,}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
)

DIGEST_IMAGE = re.compile(r"^[^\s:@]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")
# 精确版本：至少两段数字（packaging 这类项目就是 "26.3"），允许 PEP 440 后缀。
EXACT_VERSION = re.compile(r"^\d+(\.\d+)+")


def read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


# --------------------------------------------------------------------------- #
# 1. 解释器：必须是实验项目自己的 3.12 venv（隔离，而不是根 .venv 或系统 Python）
# --------------------------------------------------------------------------- #
def test_interpreter_is_python_312_in_isolated_phase0_venv() -> None:
    assert sys.version_info[:2] == (3, 12), f"实验项目固定 Python 3.12，实际 {sys.version.split()[0]}"
    assert sys.implementation.name == "cpython", f"实现应为 CPython，实际 {sys.implementation.name}"
    assert sys.prefix != sys.base_prefix, "必须运行在虚拟环境中"
    assert Path(sys.prefix).resolve() == (PROJECT / ".venv").resolve(), (
        f"必须使用 experiments/phase0/.venv，实际 sys.prefix={sys.prefix}"
    )


# --------------------------------------------------------------------------- #
# 2. uv.lock：每个包都有精确版本 + sha256 制品 hash（不得出现 latest/通配）
# --------------------------------------------------------------------------- #
def test_uv_lock_pins_exact_versions_and_artifact_hashes() -> None:
    lock = read_toml(UV_LOCK)
    assert lock["version"] >= 1, "uv.lock 缺少 version 字段"
    packages = {pkg["name"]: pkg for pkg in lock["package"]}
    for name in LOCKED_CORE:
        assert name in packages, f"uv.lock 缺少核心包 {name}"

    hashed = 0
    for name, pkg in packages.items():
        source = pkg.get("source", {})
        assert "version" not in pkg or EXACT_VERSION.match(pkg["version"]), (
            f"{name} 版本不是精确版本：{pkg.get('version')}"
        )
        if "registry" not in source:
            continue  # 本项目自身是 virtual 包，无下载制品
        assert pkg.get("version"), f"{name} 来自 registry 却没有 version"
        artifacts = []
        if "sdist" in pkg:
            artifacts.append(pkg["sdist"])
        artifacts.extend(pkg.get("wheels", []))
        assert artifacts, f"{name} 来自 registry 却没有 sdist/wheel 制品记录"
        for artifact in artifacts:
            assert artifact.get("hash", "").startswith("sha256:"), (
                f"{name} 制品缺少 sha256 hash：{artifact}"
            )
        hashed += 1
    assert hashed >= len(LOCKED_CORE), f"带制品 hash 的 registry 包只有 {hashed} 个"


# --------------------------------------------------------------------------- #
# 3. 已安装版本 == 锁定版本（防止 .venv 与 uv.lock 漂移）
# --------------------------------------------------------------------------- #
def test_installed_versions_match_uv_lock() -> None:
    lock = {pkg["name"]: pkg["version"] for pkg in read_toml(UV_LOCK)["package"]}
    drift = {
        name: (lock[name], importlib.metadata.version(name))
        for name in LOCKED_CORE
        if importlib.metadata.version(name) != lock[name]
    }
    assert not drift, f"已安装版本与 uv.lock 不一致（lock, installed）：{drift}"


# --------------------------------------------------------------------------- #
# 4. 核心包可导入且主版本符合实验设计
# --------------------------------------------------------------------------- #
def test_core_packages_import_and_major_versions() -> None:
    assert sqlalchemy.__version__.startswith("2."), sqlalchemy.__version__
    assert psycopg.__version__.startswith("3."), psycopg.__version__
    assert int(redis.__version__.split(".")[0]) >= 5, redis.__version__
    assert yaml.__version__.startswith("6."), yaml.__version__


# --------------------------------------------------------------------------- #
# 5. 项目声明：requires-python / 隔离 / ruff / pytest testpaths
# --------------------------------------------------------------------------- #
def test_pyproject_declares_isolated_project_and_real_config() -> None:
    data = read_toml(PYPROJECT)
    assert data["project"]["requires-python"] == ">=3.12,<3.13"
    assert data["tool"]["uv"]["package"] is False, "实验项目不打包安装"
    runtime = data["project"]["dependencies"]
    for prefix in ("sqlalchemy[asyncio]", "psycopg[binary]", "redis"):
        assert any(dep.startswith(prefix) for dep in runtime), f"runtime 依赖缺少 {prefix}"
    for browser in ("browserskill", "playwright"):
        assert not any(browser in dep.lower() for dep in runtime), f"T02 不得预装 {browser}"

    ruff = data["tool"]["ruff"]
    assert ruff["target-version"] == "py312"
    assert isinstance(ruff["line-length"], int) and ruff["line-length"] > 0
    assert data["tool"]["ruff"]["lint"]["select"], "ruff select 规则集不得为空"

    ini = data["tool"]["pytest"]["ini_options"]
    assert ini["testpaths"] == ["../../tests/phase0"], "testpaths 必须指向真实测试目录"


# --------------------------------------------------------------------------- #
# 6. 根项目未被污染（T02 不得把实验依赖塞进根环境）
# --------------------------------------------------------------------------- #
def test_root_project_dependencies_untouched() -> None:
    root = read_toml(ROOT / "pyproject.toml")
    assert root["project"]["dependencies"] == [], "T02 不得修改根项目依赖"
    assert "ruff" not in root.get("tool", {}), "ruff 配置不得写进根 pyproject.toml"
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"


# --------------------------------------------------------------------------- #
# 7. .env.example：字段齐全、端口一致、不含真实秘密
# --------------------------------------------------------------------------- #
def test_env_example_fields_ports_and_no_secrets() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    values = env_example_values()
    missing = [key for key in REQUIRED_ENV_KEYS if key not in values]
    assert not missing, f".env.example 缺少字段：{missing}"
    assert values["PHASE0_PG_HOST_PORT"] == "5433"
    assert values["PHASE0_REDIS_HOST_PORT"] == "6380"
    assert values["PHASE0_REDIS_DB"] == "0"
    assert values["PHASE0_PG_HOST"] == values["PHASE0_REDIS_HOST"] == "127.0.0.1"

    for pattern in SECRET_PATTERNS:
        assert not re.search(pattern, text), f".env.example 命中疑似秘密模式 {pattern}"
    password = values["PHASE0_PG_PASSWORD"]
    assert len(password) <= 32, "占位密码不应是长随机串"
    assert not re.fullmatch(r"[A-Za-z0-9+/=]{24,}", password), "占位密码不应像真实凭据"

    # compose.yaml 引用的环境变量必须在 .env.example 里有定义（不缺键、不藏值）
    compose_text = COMPOSE.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\$\{(PHASE0_[A-Z0-9_]+)", compose_text))
    assert referenced, "compose.yaml 未引用任何 PHASE0_* 变量"
    assert referenced <= set(values), f"compose 引用了 .env.example 未定义的变量：{referenced - set(values)}"


# --------------------------------------------------------------------------- #
# 8. compose.yaml：core 服务健康检查 + 镜像 digest 固定 + loopback 绑定
# --------------------------------------------------------------------------- #
def test_compose_core_services_are_digest_pinned_and_loopback_only() -> None:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert data["name"] == "${PHASE0_COMPOSE_PROJECT:-pyknora-phase0}"
    services = data["services"]
    for name in ("postgres", "redis"):
        service = services[name]
        image = service["image"]
        assert DIGEST_IMAGE.match(image), f"{name} 镜像必须 `tag@sha256:<digest>` 固定，实际 {image}"
        assert not image.endswith(":latest")
        assert "core" in service["profiles"], f"{name} 必须属于 core profile"
        assert "healthcheck" in service, f"{name} 必须声明 healthcheck"
        assert service["healthcheck"]["test"], f"{name} healthcheck 缺少 test"
        assert service["volumes"], f"{name} 缺少持久化卷"
    # 端口必须是 127.0.0.1:${...}:<container> 形式，且映射到约定端口
    assert services["postgres"]["ports"] == [
        "127.0.0.1:${PHASE0_PG_HOST_PORT:-5433}:5432"
    ], services["postgres"]["ports"]
    assert services["redis"]["ports"] == [
        "127.0.0.1:${PHASE0_REDIS_HOST_PORT:-6380}:6379"
    ], services["redis"]["ports"]
    assert set(data.get("volumes", {})) == {"phase0-pgdata", "phase0-redisdata"}


# --------------------------------------------------------------------------- #
# 8.1 postgres 镜像必须自带 pgvector（T26 检索/引用依赖；不允许运行时 pip/编译）
# --------------------------------------------------------------------------- #
def test_postgres_image_ships_pgvector_extension() -> None:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    image = data["services"]["postgres"]["image"]
    repository, _, tag = image.partition(":")
    assert repository == "pgvector/pgvector", (
        f"postgres 必须使用自带 pgvector 的官方镜像，实际仓库 {repository!r}；"
        "理由与证据见 docs/poc/evidence/fix-w1-t02-pgvector-record.md"
    )
    major_tag = tag.split("@", 1)[0]
    assert major_tag == "pg17", (
        f"pgvector 镜像的 PG major 必须与既有数据卷一致（pg17），实际 {major_tag!r}"
    )
    # 仍是 tag@sha256 固定，不允许 latest 或裸 tag
    assert DIGEST_IMAGE.match(image), f"pgvector 镜像也必须 digest 固定，实际 {image}"


# --------------------------------------------------------------------------- #
# 9. 未就绪 profile 必须显式标注（queue/parser/browser/control-site），不虚构镜像
# --------------------------------------------------------------------------- #
def test_not_ready_profiles_are_declared_not_implemented() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    for profile in ("core", "queue", "parser", "browser"):
        assert re.search(rf"#\s*profile:\s*{profile}\b", text), f"缺少 profile {profile} 的说明"
    assert text.count("not_ready") >= 3, "queue/parser/browser 至少要各有一条 not_ready 说明"
    assert "control-site" in text, "control-site 本任务不实现，必须显式记 not_ready"


# --------------------------------------------------------------------------- #
# 10. 工作目录结构与证据契约
# --------------------------------------------------------------------------- #
def test_workspace_layout_and_evidence_contract() -> None:
    for path in (PROJECT, TESTS, PROJECT / ".venv", PYPROJECT, UV_LOCK, COMPOSE, ENV_EXAMPLE):
        assert path.exists(), f"缺少 {path}"
    assert Path(__file__).resolve() == (TESTS / "test_environment.py").resolve()
    assert EVIDENCE.is_file(), "缺少 docs/poc/evidence/environment.md 证据文件"
    evidence = EVIDENCE.read_text(encoding="utf-8")
    for needle in ("P0-T02", "w1-t02-env", "@sha256:", "uv.lock"):
        assert needle in evidence, f"证据文件缺少 {needle}"


# --------------------------------------------------------------------------- #
# 11. 负向演练：缺必需环境变量时 compose 必须显式失败（不静默回退默认值）
# --------------------------------------------------------------------------- #
def run_compose(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """在隔离环境变量下调用 compose，便于断言“缺变量即失败”。"""
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), *args],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )


def clean_env() -> dict[str, str]:
    """移除全部 PHASE0_* 变量，模拟“使用者没给任何变量”的干净环境。"""
    return {key: value for key, value in os.environ.items() if not key.startswith("PHASE0_")}


def test_missing_required_env_var_fails_explicitly(tmp_path: Path) -> None:
    empty_env_file = tmp_path / "empty.env"
    empty_env_file.write_text("", encoding="utf-8")

    result = run_compose(
        ["--profile", "core", "--env-file", str(empty_env_file), "config", "-q"],
        env=clean_env(),
    )

    assert result.returncode != 0, (
        "缺少必需变量时 compose 仍成功退出（会静默使用默认值），不满足 T02 注入场景；"
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "PYKNORA_T02_MISSING_PHASE0_PG_PASSWORD" in combined, (
        f"失败信息未给出可诊断的标记：{combined!r}"
    )


# --------------------------------------------------------------------------- #
# 12. 正向对照：用 .env.example 提供变量时 compose config 必须成功
# --------------------------------------------------------------------------- #
def test_compose_config_succeeds_with_env_example() -> None:
    result = run_compose(
        ["--profile", "core", "--env-file", str(ENV_EXAMPLE), "config", "-q"],
        env=clean_env(),
    )

    assert result.returncode == 0, (
        f"提供 .env.example 后 compose config 仍失败：stdout={result.stdout!r} stderr={result.stderr!r}"
    )
