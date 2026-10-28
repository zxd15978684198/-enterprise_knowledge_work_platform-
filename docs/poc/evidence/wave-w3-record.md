# Wave W3 记录（P0-T05 / T07 / T10 / T15 / T19）

> 按 `docs/plans/2026-10-27-phase0-execution-plan.md` §4.5.5 建立，波次结束时填写门禁。

## 1. 波次声明（启动时填写）

```yaml
wave: W3
branch: feature/w3-t05-t07-t10-t15-t19
base: feature/w2-t03-t04@f86765e   # W2 尚未合入 dev（用户选择「先推分支、不合并」），W3 直接以 W2 顶点为基线
base_note: dev 仍停在 6ff7b72；W3 需要 T03/T04 产物，故不从 dev 起分支
started_at: 2026-10-27T19:30+08:00
parallel_tasks: [P0-T05, P0-T07, P0-T10, P0-T15, P0-T19]
N: 5
serial_tasks: []
blocked_tasks: []
per_task:
  - task: P0-T05
    agent: w3-t05-checkpoint
    write_scope:
      - experiments/phase0/checkpoint_probe.py
      - tests/phase0/test_checkpoint_owner.py
      - docs/poc/evidence/poc01-owner.md
    resource_slot: {pg_schema: p0_t05, redis_db: 4}
  - task: P0-T07
    agent: w3-t07-outbox
    write_scope:
      - experiments/phase0/outbox_probe.py
      - experiments/phase0/db/migrations/003_jobs_outbox.sql
      - tests/phase0/test_outbox_transaction.py
    resource_slot: {pg_schema: p0_t07, redis_db: 3}
  - task: P0-T10
    agent: w3-t10-docreader
    write_scope:
      - experiments/phase0/docreader_probe/
      - tests/phase0/test_docreader_wire.py
    resource_slot: {port: 5600}
  - task: P0-T15
    agent: w1-t14-browserskill   # 复用 inactive 成员（同角色，T14 上下文），因团队上限 8 无法新建
    reuse_note: spawn w3-t15-browser 失败 `Error: Team member limit 8 reached`
    write_scope:
      - experiments/phase0/browser_contracts.py
      - tests/phase0/test_browser_contracts.py
    resource_slot: {}
  - task: P0-T19
    agent: w2-t03-scope   # 复用 inactive 成员（已读 contracts.py/Scope 约定，适合做 registry 的 Scope 约束）
    reuse_note: spawn w3-t19-gateway 失败 `Error: Team member limit 8 reached`
    write_scope:
      - experiments/phase0/model_gateway.py
      - experiments/phase0/tool_registry.py
      - tests/phase0/test_gateway_registry.py
    resource_slot: {}
```

### 1.1 N 的计算依据（§4.5.2 三层规则）

| 层 | 结论 |
| --- | --- |
| 依赖层 | 五项前置（T03、T04、T09）均已就绪：T03/T04 **PASS**，T09 **PARTIAL**（缺镜像 digest/SBOM，故 T10 只能做 proto+本地 stub，不能在证据里声称镜像已验证） |
| 写入权层 | 五份写权完全不相交；`contracts.py`/`scope.py`/`001`/`002`/`conftest.py`/`fixtures/manifest.json`/`pyproject.toml`/`uv.lock`/`compose.yaml`/`.env.example` 全部只读 |
| 资源层 | PG 用独立 schema（`p0_t05`/`p0_t07`），Redis 用 DB 4/3，端口 T10 独占 5600；core 容器（5433/6380）已运行 3 小时 healthy，无人 `up/down` |

→ **N = 5**，无 `serial_tasks`。

### 1.1.1 成员名额处置（木已成舟记录）

团队上限 8（含 Lead），本轮 W1/W2 已有 5 个 inactive 成员占位，故 `spawn_teammate` 第 4、5 个直接返回 `Error: Team member limit 8 reached`。
处置：改用 `send_message` 把两个任务交给 **inactive 成员复用**（T15 → `w1-t14-browserskill`，T19 → `w2-t03-scope`），而不是让 Lead 串行接管——这样 N 仍为 5，且 T15 复用了唯一读过 BSK manifest 的上下文。
教训：以后波次开跑前先 `list_agents` 核名额，`spawn` 失败不要静默降级 N。

## 1.2 本波次已知约束

| 约束 | 内容 |
| --- | --- |
| C-03（NEEDS_POC） | checkpoint 单写者与 fencing：本波次正是解它，不得反向把 ADR 标为接受 |
| C-04（NEEDS_POC） | 稳定工具意图/审批：T19 只能给「最小 Registry + 稳定 intent key」骨架，审批链路属 T06/T21 |
| C-05（NEEDS_POC） | BrowserSkill 真协议：T15 按 T14 清单建模 port 与能力位，不得编造 upstream schema；Windows 单列 |
| C-01/C-08/C-09 | T07 的 Outbox 五态、死信、epoch CAS 必须与契约一致；事务外 publish 不可省 |
| 迁移编号 | `002` 归 T06、`003` 归 T07、`004` 归 T12、`005` 归 T20、`006` 归 T23、`007` 归 T27；T05 不得新建编号迁移，只能在自己 schema 内建表 |
| 依赖 | 新增 pip 包一律 `uv run --with <pkg>` 临时注入并记录版本；**不得改**实验 `pyproject.toml`/`uv.lock`（T02 写权） |
| 测试 | pytest 必须 `-c experiments/phase0/pyproject.toml`；禁止 `skip`/`xfail` 冒充通过；未跑的记 `NOT_RUN` |
| Git | 波次内禁止 `git add/commit/push/branch/switch`，由 Lead 在门禁处理 |
| 终端 | `pwsh` 工具不可用（`0xC0000142`）；用 `terminal_open`/`terminal_send` 的原生 PowerShell 持久会话，命令单行，脚本写 `%TEMP%` 再调用，输出落日志再用 read 读 |

## 2. 状态表（执行中更新）

| 任务 | 状态 | 负责人 | 备注 |
| --- | --- | --- | --- |
| P0-T05 | 已完成（**PARTIAL**） | w3-t05-checkpoint | 11 passed / ruff 通过；fenced `stale_writes_accepted=0` vs 对照 50；**原生 `PostgresSaver` 在锁连接被杀后 `put` 仍成功** → 原生路径无法 fence 独立 saver 连接，结论「**单活 runner**，不声称安全横扩」，未自改 ADR/契约 |
| P0-T07 | 已完成（PASS 限域） | w3-t07-outbox | 20 passed / ruff 通过；含 50 轮 commit/rollback + 断 Redis 恢复、4 worker SKIP LOCKED 不重复、事务内 publish 被抓；**迁移 003 落在 schema `p0_t07` 而非 `p0_t03` 增量**（并行 schema 槽导致，生产需换前缀并补 RLS/GRANT） |
| P0-T10 | 已完成（PASS 限域） | w3-t10-docreader | 17 passed / ruff 通过；grpcio 1.85.0 + protobuf 7.36.2（`--with` 临时注入，未改 pyproject）；4 RPC 真 TCP；发送期异常实测 `UNKNOWN`；真实镜像与服务 = **NOT_RUN**（T09 digest UNKNOWN） |
| P0-T15 | 已完成（PASS 限域） | w1-t14-browserskill | 117 passed / ruff 通过；单入口 `execute` + 31 动作枚举 + U1–U9 能力位 + `AUTOMATIC_RETRY=False`；网络策略只留前置接口（T16 落地） |
| P0-T19 | 已完成（PASS 限域） | w2-t03-scope | 98 passed / ruff 通过（首次 20 errors 已折行修净）；`call()` 同步版、`stream()` 记 NOT_RUN；真实厂商兼容性 NOT_RUN（禁联网） |

## 3. 门禁检查（已完成 2026-10-27T20:05–20:12 +08:00）

### 3.1 逐项结论

| 检查 | 结论 | 证据 |
| --- | --- | --- |
| 各任务四项报告交齐 | ✅ 5/5 | T05/T07/T10/T15/T19 均按提示词 06 四段汇报 |
| 越界写入核对 | ✅ 无越界 | `git status --porcelain` 18 行全部为 `??`；`git diff --numstat` = `added=0 deleted=0 files=0`（无 tracked 文件被改） |
| 全量 ruff | ✅ `All checks passed!` | 首跑 1 个 `I001`（`outbox_probe.py:62` import 块未排序）→ `ruff check --fix` 修净后复跑 exit 0（`%TEMP%\w3_gate2.log`） |
| 逐文件 pytest | ✅ 365 passed / 0 failed | 见 §3.2 |
| 全量单进程 pytest（补充实测） | ✅ `tests=365 failures=0 errors=0 skipped=0` | `junitxml` 实测 `time="128.327"`，见 §3.6；此前「挂起」系 `--with` overlay 所致，已定位 |
| 秘密扫描 | ✅ `secret_hit_count=0` | 11 个新交付文件按 `ghp_`/`sk-`/`AKIA`/私钥/明文 password 模式扫描 |
| 端口与临时目录 | ✅ 干净 | 5500/5501/5600 `listeners=0`；仓库根无 `.t*`/`tmp*` 残留 |
| `git status` / `git diff` 审阅 | ✅ | `ahead_of_dev=4`（W2 四笔），工作树仅新增未跟踪交付物 |
| 追加 IMPLEMENTATION_STATUS | ✅ | 「本轮：W3 波次执行」小节 |
| 询问用户是否提交 | ⏳ | 见 §4 |

### 3.2 逐文件测试结果（真实命令与输出）

统一命令形态（`<extra>` 见表）：

```powershell
uv run --project experiments/phase0 <extra> pytest -c experiments/phase0/pyproject.toml tests/phase0/<file> -q
```

| 测试文件 | `<extra>` | 用例数 | 结果 | exit |
| --- | --- | --- | --- | --- |
| `test_environment.py` | （无，保持 venv 隔离） | 12 | `............` | 0 |
| `test_control_site.py` | `--with grpcio --with grpcio-tools --with langgraph-checkpoint-postgres --with pytest-timeout` | 49 | `.................................................` | 0 |
| `test_faults.py` | 同上 | 25 | `.........................` | 0 |
| `test_scope.py` | 同上 | 16 | `................` | 0 |
| `test_checkpoint_owner.py` | 同上（需 langgraph） | 11 | `...........` | 0 |
| `test_outbox_transaction.py` | 同上 | 20 | `....................` | 0 |
| `test_docreader_wire.py` | 同上（需 grpcio） | 17 | `.................` | 0 |
| `test_browser_contracts.py` | 同上 | 117 | `[100%]` 全通过 | 0 |
| `test_gateway_registry.py` | 同上 | 98 | `[100%]` 全通过 | 0 |
| **合计** | — | **365** | **0 failed / 0 skipped** | — |

日志：`C:\Users\Administrator\AppData\Local\Temp\w3_gate2.log`、`w3_gate.log`、`w3_close.log`、`envclean.log`。

### 3.3 未执行 / 偏离 / 缺口

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 全量单进程 pytest（9 文件一次跑） | **PASS（20:29 复跑）** | 20:05 首次跑时日志停住、CPU 冻结，当时判为阻塞；20:27–20:29 在测试依赖正式准入后**不带 `--with`** 复跑，全量一次跑通：`tests=365 failures=0 errors=0 skipped=0 time=128.327`（junit）。**根因**：`--with` overlay 与模块级 side effect 交互导致进程卡在收尾；依赖进 venv 后不再复现 |
| 测试依赖准入 | **已准入（用户授权，T02 写权内变更）** | `experiments/phase0/pyproject.toml` dev 组新增 `grpcio>=1.85.0`、`grpcio-tools>=1.85.0`、`langgraph-checkpoint-postgres>=3.2.0`、`pytest-timeout>=2.4.0`（+4 行，`uv.lock` 同步重锁）。`test_environment.py` 12 用例在新 venv 下仍全绿 |
| T05 原生 saver fencing | **FAIL（原生路径）** | 锁连接被杀后原生 `put` 仍成功 → 只能单活 runner |
| T07 迁移前缀 | **偏离待裁** | 落 `p0_t07`，生产需并入 `p0_t03` 并补 policy/GRANT |
| T10 真实 DocReader | **NOT_RUN** | 无镜像 digest/许可（T09 遗留），只有本地 stub |
| T19 真实厂商 API / `stream()` | **NOT_RUN** | 禁联网；`stream()` 实现为同步 `call()`，T21 前须先定 port |
| T15 U1–U9 与 daemon 对齐 | **PARTIAL** | 能力位是模块常量，未与真实扩展握手对齐（T17 负责） |

### 3.4 交付哈希（门禁实测，authoritative）

```text
2e21422e25bbd51630c9b616b125533435173b50380e49ff44722a7beb49d73c  experiments/phase0/checkpoint_probe.py  1674 行
01663a473d85fb0c9705812f715e4a6b4a158efbee3090a6f60bc47bde65dcf9  tests/phase0/test_checkpoint_owner.py   262 行
8e6582443f18c357e5841d5fdb49296449255b0d7ecbe1e5ff438c9adfd415e2  docs/poc/evidence/poc01-owner.md        195 行
f45966577b02fbb412c732ef600aa6abac84b7cd4293ab80022a66d753dadb00  experiments/phase0/outbox_probe.py      1267 行
6d6c22b0279b6e764227e62f320f2d9068caaba5a9a07c98af80aad9058e0b55  experiments/phase0/db/migrations/003_jobs_outbox.sql  295 行
0b1104c428352c173086ea41c20f8b912e2ab7bb60cd28d589d080fb10aeae4e  tests/phase0/test_outbox_transaction.py  968 行
c15897ef7a71e5f7c01bca86d27ebb1396ecdfbfa85b0d8625318b77d3b68805  tests/phase0/test_docreader_wire.py      894 行
8f5d92b8b4661044e02813c6085cc59aa47527e31e17731d492d356cb7385a51  experiments/phase0/browser_contracts.py 1622 行
022a150f3d7ca76327d04a8a96842ef26edd8befc2d2a6c026d61e9c62cedd62  tests/phase0/test_browser_contracts.py  1038 行
07dbb13595b6618dbeb5bd40c3fcb3f6cf700f421d8afeb6a4ee0f6c29493e50  experiments/phase0/model_gateway.py      926 行
c66367164a46c2ec42d5f6b37b337b3e6b6e45067bd631d16d8542a3a4dee2b8  experiments/phase0/tool_registry.py      734 行
4a7776b7ad8996e05f3a3c365223612b6b64d52af03606c56e4b4d488f122540  tests/phase0/test_gateway_registry.py    992 行
33d4135cfcd2ec95f8d160d6cefdd54be669167e8b8fb69ef81095d218a738d0  experiments/phase0/docreader_probe/proto/docreader.proto  4689 B
```

`docreader_probe/` 其余文件：`__init__.py` `5f4d7764…`、`client.py` `9a82118d…`、`gen_proto.py` `14fb2f45…`、`server.py` `c1b7f560…`、`wire_evidence.json` `7b20b68a…`、`NOTICE.md` `e578b84a…`、`generated/docreader_pb2.py` `4d22e874…`、`generated/docreader_pb2_grpc.py` `24f747ed…`、`generated/PROVENANCE.json` `efa2e026…`。

> ⚠️ **行数偏差**：agent 自报行数（如 `checkpoint_probe.py` 1881、`browser_contracts.py` 1812、`outbox_probe.py` 1437）与门禁实测（1674 / 1622 / 1267）不一致。**以本表 sha256 与实测行数为准**，agent 自报值仅作参考。

### 3.5 波次判定

| 任务 | 判定 | 依据 |
| --- | --- | --- |
| P0-T05 | **PARTIAL** | 单写者/fencing 机制与 50 轮失锁证据成立；但原生 saver 无法 fence → 只能单活 runner，横向安全未证 |
| P0-T07 | **PASS（限域）** | 事务原子性、SKIP LOCKED、epoch CAS、五态/死信全部实测；限域点在迁移前缀与生产 RLS/GRANT |
| P0-T10 | **PASS（限域）** | proto + 语义 stub 的真 wire 行为（错误码/上限/取消/超时）实测；限域点在无真实服务与镜像 |
| P0-T15 | **PASS（限域）** | 契约、动作枚举、能力位、拒绝语义全绿；限域点在未与 daemon 对齐 |
| P0-T19 | **PASS（限域）** | Gateway/Registry/intent key 全绿；限域点在无真实供应商与 `stream()` |

## 3.6 全量单进程复跑（20:29，测试依赖准入后）

真实命令（无 `--with`，依赖已在实验 venv 内）：

```powershell
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0 -q `
  --timeout=120 --timeout-method=thread -p no:cacheprovider `
  --junitxml=experiments/phase0/.pytest_cache/junit.xml
```

```text
........................................................................ [ 19%]
........................................................................ [ 39%]
........................................................................ [ 59%]
........................................................................ [ 78%]
........................................................................ [ 98%]
.....                                                                    [100%]
EXIT=0
junit: tests="365" failures="0" errors="0" skipped="0" time="128.327" timestamp="2026-10-27T20:29:09+08:00"
```

- junit 落在 `.pytest_cache/`（gitignored），不入库。
- 20:26 与 20:27 两次尝试把 `--junitxml` 指向 `%TEMP%` 均报 `PermissionError: [Errno 13]`（pytest 子进程无该目录写权），不影响用例结果；改到仓库内 gitignored 目录后一次成功。
- 结论：**W3 全量 365 用例单进程通过**，此前「挂起」不再复现；「不能用 `--with` 跑全套」升级为纪律写入 §1.2。

## 4. 提交决策与结果

- 用户答复（2026-10-27 ask_user_question）：**提交 W3 两笔 + 由我决定是否推送**；随后用户明确「推送并开启 W4」。
- 实际提交：
  1. `ed669af` `feat(phase0): add W3 probes - checkpoint owner (T05), outbox (T07), docreader wire (T10), browser contracts (T15), model gateway (T19)`（24 文件 / +14336）
  2. `32f0035` `docs(phase0): record W3 gate result and full-suite rerun`（amend 两次：补 `.env.example` 例外、删计划文件 EOF 空行）
- 推送结果（2026-10-27 20:39）：`git push -u origin feature/w3-t05-t07-t10-t15-t19` → 远端顶点 **`32f0035`**；**未合并 `dev`**。
- 未提交项：`.idea/`、`CLAUDE.md`、`docs/plans/README.md`、`docs/teacher/`（开工前既有未跟踪）。
