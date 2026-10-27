# Wave W4 记录（P0-T06 / T08 / T11 / T16 / T20 / T22）

> 按 `docs/plans/2026-10-27-phase0-execution-plan.md` §4.5.5 建立；门禁与提交决策在波次末填写。

## 1. 波次声明

```yaml
wave: W4
branch: feature/w4-t06-t08-t11-t16-t20-t22
base: feature/w3-t05-t07-t10-t15-t19@32f0035   # W3 已推送 origin；dev 仍是其祖先（merge-base 校验通过）
started_at: 2026-10-27T20:40+08:00
parallel_tasks: [P0-T06, P0-T08, P0-T11, P0-T16, P0-T20, P0-T22]
N: 6
serial_tasks: []
blocked_tasks: []
per_task:
  - task: P0-T06
    write_scope:
      - experiments/phase0/approval_probe.py
      - experiments/phase0/tool_ledger.py
      - experiments/phase0/db/migrations/002_approvals.sql
      - tests/phase0/test_approval_effects.py
    resource_slot: {pg_schema: p0_t06, redis_db: 6}
  - task: P0-T08
    write_scope:
      - experiments/phase0/job_probe.py
      - experiments/phase0/job_reconciler.py
      - tests/phase0/test_delivery_recovery.py
    resource_slot: {pg_schema: p0_t08, redis_db: 5}
  - task: P0-T11
    write_scope:
      - experiments/phase0/parsing_assets.py
      - experiments/phase0/locator_map.py
      - experiments/phase0/simple_reader.py
      - tests/phase0/test_parser_golden.py
    resource_slot: {}
  - task: P0-T16
    write_scope:
      - experiments/phase0/playwright_probe.py
      - experiments/phase0/network_policy/
      - tests/phase0/test_playwright_security.py
    resource_slot: {port: 5610}
  - task: P0-T20
    write_scope:
      - experiments/phase0/context_budget.py
      - experiments/phase0/budget_ledger.py
      - experiments/phase0/db/migrations/005_budget.sql
      - tests/phase0/test_budget_context.py
    resource_slot: {pg_schema: p0_t20}
  - task: P0-T22
    write_scope:
      - experiments/phase0/dag_contracts.py
      - experiments/phase0/planner_probe.py
      - tests/phase0/test_dag_validation.py
    resource_slot: {}
```

### 1.1 N 的计算依据（§4.5.2 三层规则）

| 层 | 结论 |
| --- | --- |
| 依赖层 | T06←T05(PARTIAL，fencing 结论已给)、T08←T07(PASS 限域)、T11←T10(PASS 限域)+T03、T16←T15(PASS 限域)、T20←T19(PASS 限域)、T22←T19。前置均无 FAIL；PARTIAL 项只允许做「不依赖未证假设」的限域实验，不得把上限写成已达成 |
| 写入权层 | 六份写权不相交；`contracts.py`/`scope.py`/`001`/`003`/`conftest.py`/`fixtures/manifest.json`/`pyproject.toml`/`uv.lock`/`compose.yaml`/`.env.example` 只读 |
| 资源层 | PG schema `p0_t06`/`p0_t08`/`p0_t20` 互斥；Redis DB 6/5；端口 T16 独占 5610；core 容器运行中，无人 `up/down` |

→ **N = 6**，无 `serial_tasks`。

## 1.2 本波次已知约束

| 约束 | 内容 |
| --- | --- |
| C-04（NEEDS_POC） | 稳定工具意图/审批：T06 负责落审批与调用账本；不得把 ADR 标为接受，只能给证据 |
| C-01/C-08/C-09 | T08 消费必须复用 `experiments/phase0/db/migrations/003_jobs_outbox.sql` 的五态与 epoch CAS，不得另造状态机 |
| C-10（NEEDS_POC） | T11 的 `locator_map` 只做「定位符 → 原文片段」映射，`asset_bindings` 唯一归属留给 T12/T13 |
| 迁移编号 | `002`→T06、`005`→T20；`003` 已由 T07 占用只读；禁止新建其他编号迁移 |
| 依赖 | 不得改实验 `pyproject.toml`/`uv.lock`；确需新包用 `uv run --with` 并按需记版本（注意：`--with` 不得用于跑全套测试） |
| 浏览器 | T16 真浏览器若不可用（Playwright 未安装/无浏览器二进制），网络策略仍必须用纯 Python 单测覆盖，真浏览器部分记 `NOT_RUN`，不得 skip 冒充 |
| 测试 | 必须 `-c experiments/phase0/pyproject.toml`；本轮起可直接跑全套（依赖已进 venv） |
| Git | 波次内禁止 `git add/commit/push` |
| 终端 | `pwsh` 工具不可用；用 `terminal_open` 会串行阻塞，优先「脚本写 `%TEMP%` + 单行调用 + 日志文件」 |

## 2. 状态表

| 任务 | 状态 | 负责人 | 备注 |
| --- | --- | --- | --- |
| P0-T06 | 已交报告（待门禁） | w3-t05-checkpoint | `002_approvals.sql`(455) + `tool_ledger.py`(551) + `approval_probe.py`(2073) + 测试(578，20 例)，共 3657 行；**20 passed（75.47s）/ ruff 通过**；`--rounds 50`：重放 50 轮 `downstream_applied=1 / replayed_rounds=49 / receipts=1`；过期/篡改/跨 workspace/撤销 4/4 拒绝且零下游调用；三崩溃窗口（executing 后 / 下游成功后 / receipt 后）各 50 轮真实 `os._exit`，`violations=[]`，`after_executing_commit` 记 50 条 `unknown_outcome`（不伪称 0 次执行）；账本 append-only 由触发器强制；两条幂等路线结论分离。**偏离**：002 落独立 schema `p0_t06`（同 003 理由） |
| P0-T08 | 已交报告（待门禁） | w3-t07-outbox | `job_probe.py`(1495) + `job_reconciler.py`(381) + 测试(903，13 例)，共 2779 行；**13 passed（`-o addopts=` 复跑；`-q`+项目 addopts 会变成 `-qq` 不出汇总行）/ ruff 通过**；100 Job×10 消息 → applied=100 / duplicate=900；旧 generation `STALE_EXECUTION_EPOCH`；退避 1/2/4s → 死信 `RETRY_EXHAUSTED`；对账 repair_count=1 且并发不双修；断 Redis 显式 `BROKER_UNAVAILABLE` 且 PEL 不丢；五态与 `003` 枚举逐字核对。**偏离**：DDL 写在代码里（写权只给三文件、不能建 004）、真实 Celery NOT_RUN（未准入 celery）、未实现 XAUTOCLAIM/PEL 重投 |
| P0-T11 | 已交报告（待门禁） | w3-t10-docreader | `simple_reader.py`(859) + `locator_map.py`(311) + `parsing_assets.py`(925) + 测试(570)，共 2665 行；**70 passed / 本任务 ruff 通过**；50/50 golden 命中（每格式 10/10），offset-free 定位 50/50（page/paragraph/heading/table_cell/code_block 五类），单字符插入 0 破损；19 例空/损坏/无可见文本全部显式失败；**素材全为合成样本**（`is_real_sample=False`，真实样本与真实镜像 NOT_RUN）；与 T10 的 `ReadResponse.error` 语义跨引用已测 |
| P0-T16 | 已交报告（待门禁） | w1-t14-browserskill | `network_policy/`(9 文件) + `playwright_probe.py` + 测试，共 2489 行；**109 passed / ruff 通过**；26 条 SSRF 向量 + 每跳重定向/限流/预算全过；**Playwright 未安装 → 真浏览器 NOT_RUN**（显式失败不 skip）；5610 端口已释放 |
| P0-T20 | 已交报告（待门禁） | w2-t03-scope | `context_budget.py`(946) + `budget_ledger.py`(1288) + `005_budget.sql`(316) + 测试(1100，27 例)，共 3650 行；**27 passed（10.39s）/ ruff 通过**；日志实测 `budget-race rounds=100 winners=100 rejected=100 overdraft=0`、`crash-recovery recovered_from_pg=true`、无价格时 `settle_unavailable` 按 maximum 记 `provisional=true` 且 `zeroed=false`；迁移 43 CHECK / 4 触发器 / 9 索引、台账 sha=文件 sha、重复 apply → `DuplicateSchema`；`p0_t20` 零残留。**偏离**：005 落独立 schema；tokenizer 是启发式；「每 retry 费用可归集」需 T21 决定重试与预留的接口 |
| P0-T22 | 已交报告（待门禁） | w1-t02-env | `dag_contracts.py`(799) + `planner_probe.py`(461) + 测试(501)，共 1761 行；**51 passed / 本任务 ruff 通过**；32 节点/72 边/66 点、最长链 12 节点 11 跳 24 点、核心链 23 点与 `poc/04` 一致；28 个非法计划全拒；RAG 路径 planner 调用 0；**澄清 Mermaid 缺边为误报（72/72 一致）**；真实模型 plan NOT_RUN |

## 3. 门禁检查（已完成 2026-10-27T21:30–21:36 +08:00）

### 3.1 逐项结论

| 检查 | 结论 | 证据 |
| --- | --- | --- |
| 各任务四项报告交齐 | ✅ 6/6 | T06/T08/T11/T16/T20/T22 均按提示词 06 四段汇报 |
| 越界写入核对 | ✅ 无越界 | Windows git 视角：只有 `docs/IMPLEMENTATION_STATUS.md`、`docs/plans/2026-10-27-phase0-execution-plan.md` 被改动（Lead 本轮），其余全部 `??` 新增；受保护文件（`pyproject.toml`/`uv.lock`/`compose.yaml`/`contracts.py`/`scope.py`/`001`/`003`/`conftest.py`/`manifest.json`）零改动 |
| 全量 ruff | ✅ `All checks passed!` | 首跑 1 个 `I001`（`job_reconciler.py:20` import 排序）→ 门禁 `ruff check --fix` 修净后全量复跑 exit 0 |
| 全量 pytest（单进程） | ✅ **655 passed / 0 failed / 0 skipped** in **233.77 s** | `%TEMP%\w4_gate_out.txt` + `experiments/phase0/.pytest_cache/w4_junit.xml` |
| 秘密扫描 | ✅ 0 命中 | 19 个新交付文件按 `ghp_`/`sk-`/`AKIA`/私钥/明文 password 模式扫描 |
| 端口与临时目录 | ✅ 干净 | 5500/5501/5600/5610 均无 `LISTENING`；仓库根无 `tmp*`/`.t0*` 残留 |
| 追加 IMPLEMENTATION_STATUS | ✅ | 「本轮：W4 波次执行」小节 |
| 询问用户是否提交 | ⏳ | 见 §4 |

### 3.2 全量测试拆解（655 = 365 + 290）

| 来源 | 用例数 | 说明 |
| --- | --- | --- |
| W1–W3 既有 | 365 | environment 12 / control 49 / faults 25 / scope 16 / checkpoint 11 / outbox 20 / docreader 17 / browser 117 / gateway 98 |
| P0-T06 本轮新增 | 20 | `test_approval_effects.py` |
| P0-T08 | 13 | `test_delivery_recovery.py` |
| P0-T11 | 70 | `test_parser_golden.py`（50 golden + 20 用例） |
| P0-T16 | 109 | `test_playwright_security.py`（26 条 SSRF 向量） |
| P0-T20 | 27 | `test_budget_context.py` |
| P0-T22 | 51 | `test_dag_validation.py`（28 个非法计划参数化） |
| **合计** | **655** | `655 passed in 233.77s` |

### 3.3 交付哈希与行数（门禁实测，authoritative）

```text
4eb2329b6b706d9233bd579cb0c128d40c71e6e8b8baf73243b2d873fb1fc830  experiments/phase0/approval_probe.py      2073 行
c13ccbcb09be62eeef5b8a27720b9e2bf7248412b84c710a19511c1dfeb2446d  experiments/phase0/tool_ledger.py          551 行
28aaad2f0dcb5c0ace9418bc7e4f9bd98adb226655ca97ddfc9069aba9eb61fb  experiments/phase0/db/migrations/002_approvals.sql  455 行
2c846fd8179cfae5d3f96a7cce422a944f1e53b974a4c63724d33f5d3ad8dab4  tests/phase0/test_approval_effects.py     578 行
bd578d05eec65fdb204b3ae5fb134c2f3608f2eff8853969eedd789f41ed438f  experiments/phase0/job_probe.py            1495 行
a96c019180939f65df05531f25cda56a2851c1ad7462ecaaaa212d77cc00550a  experiments/phase0/job_reconciler.py        382 行
44e43a9d590a388961af4a9b6494fb65491d7c8df34c240bda4c91a52729f798  tests/phase0/test_delivery_recovery.py     903 行
2d7c64626014232134807f1754d47c8b6f55196db76451dc83bdec7dbfe2efcf  experiments/phase0/simple_reader.py         859 行
b6159d14fb48e79999f0f036d4441dd5f82102a4b8d3dba287e2619198d12be5  experiments/phase0/locator_map.py           311 行
223237ba35f29b2b74de0a5982ab1330374eae8b995ace0a466ca30d6319e867  experiments/phase0/parsing_assets.py        925 行
e3b52a6f8ddb59e606ce35f95af55ed557fb9284e07255a0e2eeb07f939ae818  tests/phase0/test_parser_golden.py         570 行
5d66fe0d769da5047affa9c658d9a2b58710c7405f0b261d3f77a2202526e969  experiments/phase0/playwright_probe.py      354 行
eb703b39c746cf2f98886654feed08a60502c02a27673e643aaac45e4052fc05  tests/phase0/test_playwright_security.py    866 行
d834e003573147e77eb6a8812512f5aef5fc8d760aa9e943789d39b461c641b7  experiments/phase0/context_budget.py        946 行
49bd5eb87243314daf993c7e04a3ca13e2238d5bb5e513302442f7ee35698250  experiments/phase0/budget_ledger.py        1288 行
ef28c4971398a70b82f6574b84811f2fbc0805a2b664e86daf83b535ebb61bd1  experiments/phase0/db/migrations/005_budget.sql  316 行
f7e5b4f4c77c1c0b4fa3963b6518a36bd50f203322d85a9ce028486bfd2ad998  tests/phase0/test_budget_context.py       1100 行
fdb15ae4af5fd0b9a2301e88c9d42e1bbd565766de0306ca87d4ebec46a109c1  experiments/phase0/dag_contracts.py         799 行
6346d064c604bd3e56d590d0aa783a3e27d8aaa78fe177eaea59c2e02a23c28e  experiments/phase0/planner_probe.py         461 行
7d57fa1a23a8756b2bafcd1c3ec0428a95e1812210a31ae6748eead8ffd2311a  tests/phase0/test_dag_validation.py         501 行
```

`network_policy/`（8 文件 1182 行）：`__init__.py` `01a6d713…`(53)、`errors.py` `bf0f7644…`(62)、`targets.py` `9be1ac4b…`(231)、`addresses.py` `b4db9d18…`(80)、`dns.py` `efa14b61…`(113)、`limits.py` `0e8da27c…`(107)、`engine.py` `158d6843…`(436)、`flow.py` `d652f6d4…`(187)。

本轮新增代码/测试合计 **15 733 行 + 1182 行**（`network_policy/`）。

### 3.4 波次判定

| 任务 | 判定 | 依据 | 限域 / 未完成项 |
| --- | --- | --- | --- |
| P0-T06 | **PASS（限域）** | 20 passed；50 轮重放 `downstream_applied=1 / replayed=49`；三崩溃窗口各 50 轮真实 `os._exit`，`violations=[]`，`after_executing_commit` 记 50 条 `unknown_outcome`；账本 append-only 由触发器强制 | `002` 落独立 schema `p0_t06`（同 003 理由）；未压测两个执行者真并发进入 `executing`；`TRUNCATE` 边界需 `REVOKE` + 独立写入角色 |
| P0-T08 | **PASS（限域）** | 13 passed；100 Job×10 消息 → `applied=100 / duplicate=900`；旧 generation `STALE_EXECUTION_EPOCH`；退避 1/2/4s → `dead_letter`；对账 `repair_count=1` 且并发不双修；断 Redis 显式失败且 PEL 不丢；五态与 `003` 逐字核对 | DDL 写在 `job_probe.py`（写权不能建 004）；真实 Celery `NOT_RUN`（未准入 celery）；未实现 `XAUTOCLAIM`/PEL 重投；`visibility_timeout` 未经真实 broker 验证 |
| P0-T11 | **PASS（限域）** | 70 passed；50/50 golden（每格式 10/10）；offset-free 定位 50/50；单字符插入 0 破损；19 例空/损坏/无可见文本显式失败；与 T10 `ReadResponse.error` 跨引用已测 | 素材全为**合成样本**（`is_real_sample=false`）；真实样本与真实 DocReader 镜像 `NOT_RUN`；PDF/DOCX/HTML 窄适配，不外推真实解析质量 |
| P0-T16 | **PASS（限域）** | 109 passed；26 条 SSRF 向量；每跳（含重定向链）/协议/端口/DNS rebinding/体量/时长/重定向次数全覆盖；`navigate_back/forward/reload` 也过策略且 transport 零调用；限流与并发可观测 | **Playwright 未安装 → 真浏览器 `NOT_RUN`**（显式 `PlaywrightUnavailable`，未 skip）；HTTPS pin+SNI 路径未实测；`allow_loopback_endpoints` 是测试逃生门，需 T29 启动期断言为空 |
| P0-T20 | **PASS（限域）** | 27 passed；四区段裁剪确定性（30 次同指纹）；无价格 `settle_unavailable` 按 maximum 记账 `provisional=true / zeroed=false`；100 轮双竞争 `winners=100 / rejected=100 / overdraft=0`；DB CHECK 兜底；崩溃后从 PG 恢复 | `005` 落独立 schema；tokenizer 是启发式（CJK 1 字/token）；真实价格快照 `NOT_RUN`；「每 retry 费用可归集」需 T21 决定 gateway 重试与预留的接口 |
| P0-T22 | **PASS（限域）** | 51 passed；32 节点/72 边/66 点、最长链 12 节点 11 跳 24 点、核心链 23 点与 `poc/04` 一致；28 个非法计划全拒且首错可定位；RAG 路径 planner 调用 0；**澄清 Mermaid 缺边为误报（72/72 一致）** | 真实模型 plan `NOT_RUN`（本任务不需联网）；默认上限 32/64/8 与 Phase 0 样例图 72 边口径冲突，需评审定 profile 名 |

## 4. 提交决策

- 用户答复：**待询问**
- 拟提交（待确认后执行，仅本地）：
  1. `feat(phase0): add W4 probes - approvals (T06), delivery recovery (T08), parser golden (T11), network policy (T16), budget ledger (T20), dag validation (T22)`
  2. `docs(phase0): record W4 gate result`
- 不提交：`.idea/`、`CLAUDE.md`、`docs/plans/README.md`、`docs/teacher/`
