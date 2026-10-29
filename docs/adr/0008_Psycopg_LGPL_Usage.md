# ADR-0008：psycopg（LGPL-3.0）的合规使用方式

- 日期：2026-10-29
- 状态：**提议**（合规声明需法律确认；工程约束由本 ADR 先固定）
- 依据：`experiments/phase0/uv.lock`（`psycopg` 3.3.6、`psycopg-binary` 3.3.6、`psycopg-pool` 3.3.3）；`P0_Core_Gate_Review.md:298`；`05_Phase0_Acceptance.md:73`

## 背景

P0 的 PostgreSQL 访问层使用 `psycopg[binary]`（`experiments/phase0/pyproject.toml` 声明 `psycopg[binary]>=3.1`，锁定 3.3.6），解析出三个分发包：`psycopg`、`psycopg-binary`、`psycopg-pool` 3.3.3。psycopg 3 采用 **LGPL-3.0**，`psycopg-binary` 为预编译的二进制轮子（内含 libpq 等），因此「动态链接 + 不修改 + 可替换」是唯一可辩护的合规路径。T31 已登记该事项为「合规使用未定」（`P0_Core_Gate_Review.md:298`），T32 归入 G5 许可门。

## 决策提议

**接受 LGPL-3.0 的动态链接合规路径**，并把约束固化为工程规则：

1. 仅以 **未修改的分发包** 形式依赖 psycopg（含 `psycopg[binary]` 轮子），禁止把其源码内联进本项目源码树、禁止修改后分发。
2. 发布物必须附带：psycopg 及其依赖的许可文本、来源 URL、版本号，以及「可替换」说明（用户可用自编译 libpq 替换 `psycopg-binary`）。
3. CI 增加许可清单门禁：从 `uv.lock` 生成组件×许可表，出现 `unknown`/`UNKNOWN` 即失败（对应 `05:73`「未知许可不进入发布候选」）。
4. 文档与注释中不得声称本项目「不依赖 LGPL」；相反，必须在 NOTICE 明示。

裁决权：许可文本与「可替换声明」的措辞由法务确认；工程侧不自行改写许可条款。

## 替代方案

1. 改用 `psycopg` 的纯 Python 实现（去掉 `[binary]`）：仍是 LGPL-3.0，只是不再分发二进制，合规面略小，但部署需系统 libpq —— 可作为发布形态的备选。
2. 改用 asyncpg（MIT）：许可最干净，但需重写连接/事务层，且 T03 的 RLS 会话变量、T08 的幂等用例、T29 的安全用例都要重跑，成本远高于许可收益。
3. 继续使用但不做任何声明：违反 `05:73`，拒绝。

## 后果与验证条件

后果：LGPL 组件进入发布物是**可接受但需披露**的状态；`psycopg-binary` 的存在意味着发布物内含第三方二进制，SBOM 必须覆盖（当前全仓 SBOM 尚不存在，见 `Phase0_Full_Gate_Review.md` §5.2）。验证条件：(1) 发布候选含 NOTICE 与可替换声明；(2) 许可门禁脚本存在且在 CI/门禁中执行，`unknown` 计数为 0；(3) 若改用 asyncpg，需另立 ADR 并重跑 T03/T08/T29。

实现契约：[01](../tdd/01_System_Architecture.md) 依赖清单、`05_Phase0_Acceptance.md` G5、`docs/poc/evidence/Phase0_Full_Gate_Review.md` §5.2。
