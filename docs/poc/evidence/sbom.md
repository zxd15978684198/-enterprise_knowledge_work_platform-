# PyKnora SBOM 与许可清单（Python 依赖层）

| 项 | 值 |
| --- | --- |
| 文件 | `docs/poc/evidence/sbom.md` |
| 波次 / 任务 | W15 任务 A（`docs/poc/evidence/wave-w15-record.md:15`） |
| 依据 | T31 §2.4 D5（`docs/poc/evidence/P0_Core_Gate_Review.md:160`）、ADR-0007、ADR-0008 |
| 执行日期 | 2026-10-29 |
| 基线 | 分支 `feature/w14-m1-m2-m3`，HEAD `1bcad95`；本文件此前不存在（`Test-Path` 返回 `False`，`git ls-files docs/poc/evidence/sbom.md` 无输出） |
| 写权 | 仅本文件；本轮未执行任何 git 写操作 |
| 覆盖范围 | **Python 依赖闭包**（`experiments/phase0`）+ 三项风险许可项的事实登记 |
| 不覆盖 | DocReader 镜像内部组件（已有 `docreader_sbom.md`）、apt/基础镜像层、模型权重、前端依赖（当前不存在） |

## 1. 范围与口径

1. 事实源优先级：`experiments/phase0/uv.lock`（锁定版本）→ 已安装 `.venv` 的 `*.dist-info/METADATA`（许可字段实读）→ 仓内既有证据（镜像与扩展的许可）。三者冲突时以「实读」为准并记录差异。
2. 版本列**全部来自本轮实读**：包版本取自 `uv.lock` 的 `version` 行，并与 `.venv` 中 `dist-info` 的 `Version` 逐项比对，**58/58 一致、0 例漂移**（见 §3）。
3. 根项目 `pyproject.toml:10` 的 `dependencies = []`；根 `uv.lock` 在本轮开始时存在（8 行、仅 `name = "pyknora"` 一个包、未被 git 跟踪），本轮结束时已不存在（`Test-Path` 为 False，`git ls-files`/`check-ignore` 均无记录）——两种状态下结论一致：**根骨架无第三方依赖**，因此全仓 SBOM 的实际内容就是实验工程的依赖闭包。
4. `experiments/phase0` 是 `package = false` 的独立 uv 项目（`experiments/phase0/pyproject.toml:25-26`），其 `dev` 组虽非运行时依赖，但属于**必须锁定与审计的构建/测试供应链**，本文件一并纳入并显式标注 `dev`。
5. 镜像类组件（Redis 服务端、PostgreSQL、pgvector 扩展）**不是** Python 包，不出现在 `uv.lock`；本文件只登记其许可事实与 ADR 对照，不把镜像内 apt 层当作已审。

## 2. 生成方式与复现命令

### 2.1 技术路线

不使用 `pip-licenses`、`cyclonedx-py` 等第三方工具：它们需要额外注入依赖（会改变 `sys.prefix`，与 `tests/phase0/test_environment.py` 的隔离断言冲突），且本轮写权只允许一个文档文件。改用**只读事实提取**：从 `uv.lock` 取版本，从已安装 `dist-info/METADATA` 取许可，两条独立数据源互校。

许可字段识别顺序（PEP 639 优先，向旧元数据回退）：`License-Expression:`（SPDX 表达式，最权威）→ `License:`（自由文本，需归一）→ `Classifier: License :: ...`（仅分类名，精度最低；只有 2 个包落到这一档）。

### 2.2 实际执行的命令与真实输出

```powershell
# 1) 锁定状态校验（EXIT=0 才可用于计数）
uv lock --check --project experiments/phase0
#   -> Resolved 60 packages in 0.82ms      EXIT=0

# 2) 直接依赖树（只读 lock，不联网）
uv tree --project experiments/phase0 --depth 1
#   -> 12 条直接依赖：psycopg[binary] 3.3.6 / redis 8.1.0 / sqlalchemy[asyncio] 2.1.6
#      + dev 9 项（grpcio, grpcio-tools, langgraph, langgraph-checkpoint-postgres,
#        pytest, pytest-asyncio, pytest-timeout, pyyaml, ruff），尾行 Resolved 60 packages

# 3) 构建上下文
uv --version                                    # uv 0.12.24 (5411378eb 2026-10-08 x86_64-pc-windows-msvc)
uv run --project experiments/phase0 python -V   # Python 3.12.13

# 4) 统计 uv.lock 的 [[package]]：60 条（含虚拟项目），第三方 59 条
# 5) 遍历 dist-info/METADATA（脚本见 §2.3）：58 条，来源分布 LE 38 / L 18 / C 2
# 6) lock 与安装集差异：仅 httpx2-jsfetch 1.0 未装（uv.lock:214，emscripten-only）

# 7) 环境能力探针（决定哪些事实只能转引仓内证据）
docker version --format '{{.Server.Version}}'
#   -> failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine
#      → Docker daemon 未运行，本轮无法重读镜像内许可文本
```

退出码：除第 7 条 `docker version` 为 1 外，其余命令与 PowerShell 提取脚本均为 0；`uv run ... python -V` 会触发 uv 环境检查，但 `.venv/` 属 gitignore 产物，未改变任何 tracked 文件。

### 2.3 许可提取脚本（复现用）

```powershell
$out = @()
Get-ChildItem experiments/phase0/.venv/Lib/site-packages -Directory |
  Where-Object { $_.Name -like '*.dist-info' } | ForEach-Object {
    $L = Get-Content (Join-Path $_.FullName 'METADATA')
    $name = ($L | Where-Object { $_ -match '^Name: ' }    | Select-Object -First 1).Substring(6).Trim()
    $ver  = ($L | Where-Object { $_ -match '^Version: ' } | Select-Object -First 1).Substring(9).Trim()
    $lexp = @($L | Where-Object { $_ -match '^License-Expression: ' })
    $lic  = @($L | Where-Object { $_ -match '^License: ' })
    $cls  = @($L | Where-Object { $_ -match '^Classifier: License ::' })
    $src  = if ($lexp.Count) { 'License-Expression' } elseif ($lic.Count) { 'License' }
            elseif ($cls.Count) { 'Classifier' } else { 'NONE' }
    $out += [pscustomobject]@{ Name = $name; Ver = $ver; Src = $src }
  }
$out | Sort-Object Name
```

### 2.4 复现步骤

先 `uv sync --project experiments/phase0 --frozen` 对齐 `.venv`（本轮未执行；现有 58 个 dist-info 与 lock 一致），再跑 §2.2 的命令 1、2、4 与 §2.3 脚本得到 58 行「包 / 版本 / 许可来源」，最后与 §4、§5 逐行对照（`MISMATCH` 必须为 0）；镜像类事实（pgvector/Redis）按 §6 的转引方式复核，需 Docker daemon 可用。

## 3. 依赖计数

| 口径 | 数值 | 溯源 |
| --- | --- | --- |
| `pyproject.toml` 声明的依赖条目 | **12**（3 runtime + 9 dev） | `experiments/phase0/pyproject.toml:6-10`、`:12-23` |
| `uv.lock` 锁定条目总数 | **60** | `uv.lock` 中 `[[package]]` 计数（含虚拟项目） |
| 其中虚拟项目自身 | 1（`pyknora-phase0` 0.1.0） | `uv.lock:573-614` |
| **锁定第三方包** | **59** | 60 − 1 |
| extras 展开新增 | **2**（`psycopg-binary`、`greenlet`） | `uv.lock:484-487`、`:777-780` |
| **直接依赖**（声明 12 + extras 2） | **14** | §4 |
| **传递依赖** | **44** | 58 − 14，见 §5 |
| 本平台（win32 / CPython 3.12.13）实际安装 | **58** | `.venv` 中 `*.dist-info` 目录计数 |
| 锁定但本平台不安装 | 1（`httpx2-jsfetch` 1.0） | `uv.lock:214`（`sys_platform == 'emscripten'`） |
| 许可可判定 | **58 / 58（100%）**，无 `NONE` | §2.3 脚本输出 |
| 许可来源分布 | License-Expression 38 / License 18 / Classifier 2 | 同上 |
| 版本与 lock 一致 | 58 / 58，`MISMATCH=0` | 本轮比对输出 |

### 3.1 许可分布（按元数据原值去重，共 58 项）

MIT 26、BSD-3-Clause 11、Apache-2.0 8、BSD-2-Clause 3、LGPL-3.0-only 3、MPL-2.0 1、PSF-2.0 1；
OR/AND 组合 5 项：`MIT AND PSF-2.0`（greenlet）、`MPL-2.0 AND (Apache-2.0 OR MIT)`（orjson）、
`Apache-2.0 OR MIT`（ormsgpack）、`Apache-2.0 OR BSD-2-Clause`（packaging）、`MIT OR Apache-2.0`（sniffio）。

口径说明：单一许可 53 项、组合 5 项，合计 58。组合项的义务按**所选条款**确定，不是「全部叠加」，但发布物声明必须写明实际选择。

### 3.2 extras 与平台 marker（解释 59 与 58 的差）

- `psycopg[binary]` → `psycopg-binary`（`uv.lock:484-487`，marker `implementation_name != 'pypy'`），这是**预编译二进制轮子**，是 psycopg 合规面的关键（见 §6.2）；`psycopg` 在 win32 还额外拉入 `tzdata`（`uv.lock:477`）。
- `sqlalchemy[asyncio]` → `greenlet`（`uv.lock:777-780`）。
- `httpx2` 的 `httpx2-jsfetch` 只在 `sys_platform == 'emscripten'` 生效（`uv.lock:214`），因此 Windows/CPython 安装集比 lock 少 1 条。**跨平台口径的 SBOM 仍应保留这 1 条**，否则同一 lock 在 emscripten 目标上会漏审计。

## 4. 直接依赖清单（12 声明 + 2 extras = 14）

| # | 包 | 声明位置 | 版本 | `uv.lock` 行 | 许可 | 许可来源 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | psycopg（`[binary]`） | `pyproject.toml:7` | 3.3.6 | 471 | **LGPL-3.0-only** | License-Expression |
| 2 | redis | `pyproject.toml:8` | 8.1.0 | 675 | MIT | License-Expression |
| 3 | sqlalchemy（`[asyncio]`） | `pyproject.toml:9` | 2.1.6 | 755 | MIT | License-Expression |
| 4 | pyyaml | `pyproject.toml:14`（dev） | 6.0.3 | 657 | MIT | License（文本） |
| 5 | pytest | `pyproject.toml:15`（dev） | 9.1.1 | 616 | MIT | License-Expression |
| 6 | pytest-asyncio | `pyproject.toml:16`（dev） | 1.4.1 | 632 | Apache-2.0 | License-Expression |
| 7 | ruff | `pyproject.toml:17`（dev） | 0.12.12 | 711 | MIT | Classifier（仅有分类名） |
| 8 | grpcio | `pyproject.toml:18`（dev） | 1.85.0 | 113 | Apache-2.0 | License-Expression |
| 9 | grpcio-tools | `pyproject.toml:19`（dev） | 1.85.0 | 134 | Apache-2.0 | License-Expression（dist-info 内**无** LICENSE 文件） |
| 10 | langgraph-checkpoint-postgres | `pyproject.toml:20`（dev） | 3.2.0 | 335 | MIT | License-Expression |
| 11 | pytest-timeout | `pyproject.toml:21`（dev） | 2.4.0 | 645 | MIT | License（文本） |
| 12 | langgraph | `pyproject.toml:22`（dev） | 1.2.18 | 305 | MIT | License-Expression |
| E1 | psycopg-binary | extras：`uv.lock:484-487` | 3.3.6 | 489 | **LGPL-3.0-only** | License-Expression |
| E2 | greenlet | extras：`uv.lock:777-780` | 3.5.6 | 95 | MIT AND PSF-2.0 | License-Expression |

许可文件实读补充：`psycopg-3.3.6.dist-info/licenses/LICENSE.txt` 首行为 `GNU LESSER GENERAL PUBLIC LICENSE / Version 3, 29 June 2007`，与元数据 `LGPL-3.0-only` 一致。

## 5. 传递依赖清单（44）

来源标记：LE = `License-Expression`，L = `License`，C = 仅 `Classifier`。

| 包 | 版本 | `uv.lock` 行 | 许可 | 源 |
| --- | --- | --- | --- | --- |
| annotated-types | 0.8.0 | 5 | MIT | LE |
| anyio | 4.15.1 | 14 | MIT | LE |
| certifi | 2026.10.23 | 27 | MPL-2.0 | L |
| charset-normalizer | 3.5.2 | 36 | MIT | L |
| colorama | 0.4.6 | 77 | BSD-2-Clause（Classifier 只写 `BSD License`；`licenses/LICENSE.txt` 实读为 2 条款） | C+文件 |
| distro | 1.9.0 | 86 | Apache-2.0 | L |
| h11 | 0.16.0 | 157 | MIT | L |
| httpcore | 1.0.9 | 166 | BSD-3-Clause | LE |
| httpcore2 | 2.16.0 | 179 | BSD-3-Clause | LE |
| httpx | 0.28.1 | 192 | BSD-3-Clause | L |
| httpx2 | 2.16.0 | 207 | BSD-3-Clause | LE |
| idna | 3.20 | 233 | BSD-3-Clause | LE |
| iniconfig | 2.3.1 | 242 | MIT | LE |
| jsonpatch | 1.38 | 251 | BSD-3-Clause | LE |
| jsonpointer | 3.6 | 263 | BSD-3-Clause（`License` 字段写 `Modified BSD License`；文件实读 3 条款） | L+文件 |
| langchain-core | 1.7.0 | 272 | MIT | L |
| langchain-protocol | 0.0.20 | 293 | MIT | L |
| langgraph-checkpoint | 4.3.1 | 322 | MIT | LE |
| langgraph-prebuilt | 1.1.1 | 350 | MIT | LE |
| langgraph-sdk | 0.4.10 | 363 | MIT | LE |
| langsmith | 0.14.13 | 379 | MIT | L |
| orjson | 3.13.0 | 404 | MPL-2.0 AND (Apache-2.0 OR MIT) | LE |
| ormsgpack | 1.13.0 | 422 | Apache-2.0 OR MIT | LE |
| packaging | 26.3 | 438 | Apache-2.0 OR BSD-2-Clause | LE |
| pluggy | 1.7.0 | 447 | MIT | L |
| protobuf | 7.36.2 | 456 | BSD-3-Clause（`3-Clause BSD License`） | L |
| psycopg-pool | 3.3.3 | 507 | **LGPL-3.0-only** | LE |
| pydantic | 2.14.1 | 519 | MIT | LE |
| pydantic-core | 2.50.1 | 534 | MIT | LE |
| Pygments | 2.21.0 | 564 | BSD-2-Clause | LE |
| requests | 2.34.2 | 684 | Apache-2.0 | L |
| requests-toolbelt | 1.0.0 | 699 | Apache-2.0（`Apache 2.0`） | L |
| setuptools | 84.2.1 | 737 | MIT | LE |
| sniffio | 1.3.1 | 746 | MIT OR Apache-2.0 | L |
| tenacity | 9.2.1 | 782 | Apache-2.0 | LE |
| truststore | 0.10.5 | 791 | MIT | LE |
| typing-extensions | 4.16.0 | 800 | PSF-2.0 | LE |
| typing-inspection | 0.4.4 | 809 | MIT | LE |
| tzdata | 2026.5 | 821 | Apache-2.0 | L |
| urllib3 | 2.8.0 | 830 | MIT | LE |
| uuid-utils | 0.19.0 | 839 | BSD-3-Clause | LE |
| websockets | 17.3 | 863 | BSD-3-Clause | LE |
| xxhash | 4.0.2 | 898 | BSD-2-Clause | L |
| zstandard | 0.25.0 | 937 | BSD-3-Clause | LE |

### 5.1 锁定但本平台不安装（1，必须保留在 SBOM 里）

| 包 | 版本 | `uv.lock` 行 | 许可 | 状态 |
| --- | --- | --- | --- | --- |
| httpx2-jsfetch | 1.0 | 224-231 | **待验证：本平台不安装，无 dist-info 可实读** | 仅 emscripten 目标生效（`uv.lock:214`） |

## 6. 风险许可项与 ADR 对照

### 6.1 Redis：客户端 MIT，服务端镜像 RSALv2/SSPLv1（ADR-0007）

**实测事实（本轮）**

| 组件 | 标识 | 许可事实 | 来源 |
| --- | --- | --- | --- |
| Python 客户端包 | `redis` 8.1.0 | `License-Expression: MIT`，`Classifier: OSI Approved :: MIT License`；该包在 lock 中**无任何依赖**（`uv.lock:675-682`，纯客户端） | `.venv/Lib/site-packages/redis-8.1.0.dist-info/METADATA` |
| 服务端镜像 | `redis:7.4-alpine@sha256:6ba19c50b56777f5c11c58eff8ddb69d0d5f27ba131cffb63f180ccfd43c66af` | RSALv2 / SSPLv1（ADR-0007 陈述） | `experiments/phase0/compose.yaml:55` |

**与 ADR-0007 对照**

- ADR 状态为**提议**（`docs/adr/0007_Redis_License_Scope.md:3`），三选一未裁决：A 接受 SSPL/RSALv2 边界（限内部自建）、B 换 Valkey（BSD-3-Clause）、C 剥离 Redis（`:15-21`）。工程建议 B。ADR 依据段写「Python 客户端为 `redis` 8.1.0」，与本轮实读**完全吻合**；ADR 同时要求「本项在裁决前不得写已完成」（`:33`），本轮不改变该状态。
- **必须避免的误读**：SBOM 表格中的 `redis 8.1.0 = MIT` 只覆盖 Python 客户端，**不能**据此说明 Redis 服务端许可干净。真正的义务对象是 `compose.yaml:55` 的镜像；客户端 MIT 与服务端 RSALv2/SSPLv1 是两个不同主体。

**未验证部分**：镜像内许可文本本轮**未重读**（Docker daemon 不可用，见 §2.2 第 7 条）。`redis:7.4-alpine` 是否确实携带 RSALv2+SSPLv1 双许可文件，属于**转引 ADR 陈述**，不是本轮实读。

### 6.2 psycopg 三包 LGPL-3.0-only（ADR-0008）

**实测事实（本轮）**

| 包 | 版本 | 许可 | 许可来源 | `uv.lock` 行 |
| --- | --- | --- | --- | --- |
| psycopg | 3.3.6 | LGPL-3.0-only | METADATA `License-Expression`；`licenses/LICENSE.txt` 为 LGPL v3 正文 | 471 |
| psycopg-binary | 3.3.6 | LGPL-3.0-only | METADATA `License-Expression`；含 `win_amd64` 等平台预编译轮子 | 489-505 |
| psycopg-pool | 3.3.3 | LGPL-3.0-only | METADATA `License-Expression` | 507 |

**引入路径有两条**（必须同时登记，不能只按直接依赖算）：

1. 直接：`pyproject.toml:7` 的 `psycopg[binary]>=3.1`；
2. 传递：`langgraph-checkpoint-postgres` 显式依赖 `psycopg` 与 `psycopg-pool`（`uv.lock:342-343`）。

**与 ADR-0008 对照**

| ADR-0008 条目 | 本轮核对结果 |
| --- | --- |
| 依据段版本 `psycopg` 3.3.6 / `psycopg-binary` 3.3.6 / `psycopg-pool` 3.3.3（`0008:5`） | **完全一致** |
| 定性「psycopg 3 采用 LGPL-3.0」（`0008:9`） | 一致，且元数据更精确为 **LGPL-3.0-only** |
| 规则 1：仅以未修改分发包依赖，禁止内联源码（`0008:15`） | 可核查：lock 中三个包 `source` 均为 PyPI registry（唯一非 registry 源是虚拟项目自身 `uv.lock:576`），仓内无 vendored 副本 |
| 规则 2：发布物须附许可文本、来源 URL、版本号与「可替换」说明（`0008:16`） | **未落地**：仓根无 `NOTICE`/`LICENSE`/`COPYING` 文件（本轮目录扫描为空） |
| 规则 3：CI 许可清单门禁，出现 `unknown` 即失败（`0008:17`） | **未落地**：`experiments/phase0/*.py` 与 `tests/phase0` 中无 pip-licenses/license gate 脚本；本文件是手工产物，不是门禁 |
| 规则 4：不得声称「不依赖 LGPL」，须在 NOTICE 明示（`0008:18`） | **未落地**：无 NOTICE |
| 「可替换」论证（`:16`、`:24`） | 前提成立：`psycopg-binary` 是独立分发包，可改为纯 Python `psycopg` + 系统 libpq 替换（ADR 替代方案 1） |

结论：LGPL-3.0 的**事实基础**与 ADR 一致且更严格（only），但 ADR 的四条工程规则**只有第 1 条当前可验证通过**，第 2/3/4 条仍是待做项。这不改变「LGPL 可接受但需披露」的定性，改变的是「披露材料是否存在」。

### 6.3 pgvector：PostgreSQL License（ADR-0010）

**事实**

| 项 | 内容 | 来源 |
| --- | --- | --- |
| 形态 | **数据库扩展**，不是 Python 包；不在 `uv.lock`（59 条中无 pgvector） | §3 |
| 版本 | `vector` 0.8.7（`vector.control: default_version = '0.8.7'`） | `docs/poc/evidence/pgvector-probe.md:19` |
| 许可 | **PostgreSQL License**（容器内 `/usr/share/doc/pgvector/LICENSE` 实读） | `pgvector-probe.md:24`、`:304-308` |
| 载体镜像 | `pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d` | `experiments/phase0/compose.yaml:23` |
| Python 侧 | 项目**故意不引入** pgvector-python 适配器（`experiments/phase0/ann_bench.py:179` 明写「不经 pgvector-python 适配器，降低依赖」）；`ann_bench.py:179` 用文本字面量 `[a,b,c]` | 同左 |

**与 ADR-0010 对照**

- ADR-0010 状态为**已接受**（`docs/adr/0010_Pgvector_Core_Profile.md:4`），但正文只把许可列为「副作用」——「本镜像的汇总许可亦需登记」（`:11`），**未给出许可结论**。本文件即为该登记的落点。
- 许可定性（PostgreSQL License，类 BSD/MIT 宽松、可商用可分发、需保留版权声明）与 `pgvector-probe.md:308` 的结论一致；该结论是**转引**（探针在 2026-10-27 实读），不是本轮实读。
- ADR-0010 的验证条件不包含许可项，故本 SBOM 不声称 ADR-0010 的验证条件因此新增或满足。

**本轮未复核**：Docker daemon 不可用 → 镜像内 `LICENSE` 未重读；镜像的**汇总许可**（image-level license）仍无声明，与 DocReader 镜像的 `NOASSERTION` 同类问题。

### 6.4 次级许可项（不阻塞，但发布物必须处理）

| 项 | 事实 | 义务 |
| --- | --- | --- |
| `certifi` 2026.10.23 | MPL-2.0（`License` 字段） | 文件级弱 copyleft：修改 MPL 文件才需公开；分发需保留许可与来源 |
| `orjson` 3.13.0 | `MPL-2.0 AND (Apache-2.0 OR MIT)` | 组合式：MPL 部分单独成立，其余可择 Apache-2.0 或 MIT；声明需写明选择 |
| `grpcio-tools` 1.85.0 | `License-Expression: Apache-2.0`，但 dist-info **无 LICENSE 文件**（只有 `METADATA`/`RECORD`/`WHEEL` 等 7 个文件） | 许可可判定（元数据声明）；发布物需另附 Apache-2.0 全文，不能指望该包自带的许可文件 |
| `greenlet` 3.5.6 | `MIT AND PSF-2.0`（双许可叠加，非选择） | 两项义务同时适用，需同时附两份文本 |
| `colorama` 0.4.6 | Classifier 仅写 `BSD License`（无版本），`licenses/LICENSE.txt` 实读为 **2 条款** | 已由文件正文收敛，无需再猜；义务为保留版权与免责声明 |
| `jsonpointer` 3.6 | `License` 字段写 `Modified BSD License`，文件实读 **3 条款** | 同上，按 BSD-3-Clause 处理 |

## 7. 结论

### 7.1 分项结论

| 维度 | 结论 | 依据 |
| --- | --- | --- |
| Python 依赖许可可判定性 | **不阻塞**：58/58 有许可声明，`unknown` 计数为 0 | §3、§5 |
| 许可精度 | 基本可用：38 项为 SPDX 表达式；2 项仅有分类名（`colorama`、`ruff`），其中 `colorama` 已由许可正文收敛 | §2.3、§6.4 |
| Redis 服务端 | **阻塞（未裁决）**：RSALv2/SSPLv1，ADR-0007 仍为提议，A/B/C 未选 | §6.1 |
| psycopg 合规 | **条件阻塞**：LGPL-3.0-only 本身可接受，但披露材料（NOTICE、可替换声明、许可门禁）三项均不存在 | §6.2 |
| pgvector | **不阻塞**：PostgreSQL License 为宽松许可；登记缺失已由本文件补齐（转引 probe） | §6.3 |
| 全仓 SBOM 是否存在 | **本轮补齐**：此前 T31 登记为「不存在」（`P0_Core_Gate_Review.md:108`、`:160`；`Phase0_Full_Gate_Review.md:138`） | 本文件 |
| 对 P1 准入 | **不阻断 P1 开发；阻断 P1 发布/交付候选** | `docs/poc/05_Phase0_Acceptance.md:73`：「未知许可不进入发布候选」 |

### 7.2 是否阻塞 P1 准入（直接回答）

判定：**P1 准入为「条件阻塞」**，不是全阻塞也不是通过。

1. 开发态准入（写代码、跑实验、扩依赖）：**不阻塞**。所有 Python 依赖许可均可判定，无 unknown；新增依赖走同一套复现命令即可增量审计。
2. 发布态准入（对外交付、镜像分发、托管服务）：**阻塞**。三条理由全部指向「未裁决」而非「未知许可」：Redis 服务端许可边界未裁决（ADR-0007 提议态，`0007:23` 自写「P0 不得在未裁决状态下标记发布候选」）、psycopg 披露材料三缺三（NOTICE / 可替换声明 / 许可门禁，`0008:16-18`）、pgvector 镜像汇总许可未声明（`0010:11` 只提出要求）；`05:73`「未知许可不进入发布候选」是最终判据。
3. 因此本文件的正确读法是：**把「许可未知」降级为「许可已知、边界待裁决」**。这是 P1 准入的一个真实进展，但不是通过。

### 7.3 需要用户/法务决策的点

| 编号 | 决策点 | 选项/建议 | 归属 | 不决策的后果 |
| --- | --- | --- | --- | --- |
| Q1 | Redis 许可边界 | ADR-0007 的 A（内部自建，接受 SSPL/RSALv2）/ B（换 Valkey，推荐）/ C（剥离 Redis） | 产品 + 法务 | 发布候选永久无法签署；ADR-0007 停在提议 |
| Q2 | LGPL-3.0 披露措辞与「可替换」声明的文本 | 由法务定稿，工程侧只提供事实（§6.2） | 法务 | NOTICE 无法生成；ADR-0008 条件 2 不满足 |
| Q3 | pgvector 许可结论是否回写 ADR-0010（或在后续 ADR 中单列） | 建议在 ADR-0010 追加一节「许可」引用本文件，避免只靠 probe 记录 | 架构 + 用户 | 已接受 ADR 缺许可结论，审计链断在证据文件 |
| Q4 | 许可门禁（ADR-0008 条件 3）是否列为 P1 准入前强制项、由谁实现 | 建议列为强制项，实现为只读脚本：解析 `uv.lock` + dist-info，`unknown>0` 即失败 | 用户 + 工程 | 本 SBOM 是一次性手工产物，下次依赖变更即静默过期 |

## 8. 待验证清单

| 编号 | 项 | 待验证：缺什么 | 影响 | 建议取证 |
| --- | --- | --- | --- | --- |
| V1 | `redis:7.4-alpine@sha256:6ba19c50…` 镜像内许可文本 | 待验证：镜像未拉取 / Docker daemon 未运行，无法读 `/usr/share/doc/redis*/` 或镜像内 LICENSE | §6.1 的 RSALv2/SSPLv1 目前**只有 ADR 转引**，无本轮实读 | daemon 可用后 `docker run --rm --entrypoint sh <digest> -c 'ls /usr/share/doc; cat /usr/share/doc/redis*/LICENSE*'` |
| V2 | pgvector 扩展许可本轮复核 | 待验证：同上，本轮未重读 `/usr/share/doc/pgvector/LICENSE` | §6.3 结论依赖 2026-10-27 的 probe 记录 | 同上，目标文件 `/usr/share/doc/pgvector/LICENSE` |
| V3 | `pgvector/pgvector:pg17` 镜像汇总许可 | 待验证：镜像未声明 image-level license（与 DocReader 的 `NOASSERTION` 同类） | 发布物无法引用镜像整体许可 | 查镜像 OCI `annotations`/上游仓库 LICENSE + 构建脚本 |
| V4 | `httpx2-jsfetch` 1.0 许可 | 待验证：本平台不安装（emscripten-only），无 dist-info 可读；仓内无缓存元数据 | §5.1 唯一 unknown | 在 emscripten 目标上 `uv sync` 后读 METADATA，或离线取 sdist 内 PKG-INFO |
| V5 | `grpcio-tools` 1.85.0 许可文件 | 待验证：dist-info 内无 LICENSE 文件（仅元数据声明 Apache-2.0） | 发布物附许可文本时需另找来源 | 取 sdist 内 `LICENSE`，或复用 `grpcio` 包的 Apache-2.0 全文 |
| V6 | 根项目 `uv.lock`（本轮内消失） | 待验证：会话开始时为未跟踪的 8 行文件，结束时 `Test-Path`=False 且 git 无跟踪/忽略记录，删除方与原因未登记 | 若误提交或误删会让「根骨架无依赖」的表述产生歧义 | 由 Lead 登记该文件的去留与操作来源，本任务无权处理 |
| V7 | Redis 客户端与服务端许可的**产品口径** | 待验证：Q1 未裁决，无法确定「客户端 MIT + 服务端 SSPL」在对外文档中如何表述 | 对外许可声明措辞 | 随 Q1 一并定稿 |
| V8 | 许可门禁脚本 | 待验证：仓内不存在（ADR-0008 条件 3 要求） | SBOM 无法持续有效 | 见 Q4，需另行立项（本任务写权不含代码） |
| V9 | 镜像内 apt/基础镜像层（alpine、Debian pgdg）组件许可 | 待验证：不在本轮范围，且 daemon 不可用 | 发布镜像时仍需覆盖 | 沿用 `docreader_sbom.md` 的 apt 盘点方法（744/971 有 `License:` 字段的口径） |
| V10 | 模型权重与数据许可 | 待验证：仓内无权重文件清单 | `05:73` 的「模型权重/数据许可」项 | 由模型网关/数据侧任务补齐，不属本 SBOM |

## 9. 口径限制与已知偏差

1. **平台限定**：58 项安装集对应 `win32` + CPython 3.12.13。Linux/容器目标可能多出平台专属轮子（如 `psycopg-binary` 的 manylinux 轮子），但**包集合与版本不变**（同一 lock），差异只在二进制选择，不影响许可结论。`ruff` 仅有 `OSI Approved :: MIT License` 分类名、无 SPDX 表达式；MIT 无版本歧义，故按 MIT 记录，不列待验证。
2. **dev 组纳入口径**：`ruff`/`pytest` 等不会进入运行时发布物。若将来只对运行时做许可声明，应从 §4 中排除 `pyproject.toml:14-22` 的 9 项，届时直接依赖为 3 + 2 extras = 5，传递项需重算（不能直接相减）。
3. **边界与可维护性**：本文件不构成法律意见（条款解释与对外声明归法务，与 ADR-0007/0008 同一定位）；未执行 `uv sync`、未跑 pytest、未构建镜像、未生成 CycloneDX/SPDX 机器可读制品，它是**人读清单**。所有数字都可由 §2.2/§2.3 重建；`uv.lock` 一变，§3 计数与 §4/§5 表格必须同批复核，否则立即过期——这正是 V8/Q4 要解决的问题。
