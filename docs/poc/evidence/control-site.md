# P0-T04 证据：可控下游、控制站与故障断点 fixture

- 任务：P0-T04（Wave W2，执行者 `w2-t04-controlsite`）
- 状态：**控制站/屏障/向量/测试四条能力 PASS；故障场景（F1–F18）本身 NOT_RUN**（见 §9、§10）
- 证据生成时间：2026-10-27 19:0x（+08:00）
- Owner：P0-T04 执行者（`w2-t04-controlsite`）；Reviewer：Wave W2 Lead

> 本文只记录本任务真实跑过的命令与真实输出。**未运行的场景一律 `NOT_RUN`**，
> 不用「控制站能返回 503」替代「业务系统正确处理 503」这类断言。

## 1. 基线与环境

| 项 | 值 |
| --- | --- |
| 仓库 | `E:\project_month\pyknora` |
| 分支 | `feature/w2-t03-t04` |
| 基线 commit（HEAD，未提交） | `6ff7b72941b130fe1284af44c052e345466a8820`（`dev@6ff7b72`） |
| 工作区 `git diff` | 0 字节，`sha256 = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`（空 diff 的 sha256） |
| 工作区 `git status --porcelain`（取证末态） | `sha256 = 4aa06cb34f37065d1b8224bd5979993c48976ad6cf017d4466ace54e982a768a`，68 行 = 17 未跟踪 + 51 已跟踪（后者为**换行符翻转**，非本任务内容改动，见 §8） |
| Python | CPython 3.12.13（`experiments/phase0/.venv`） |
| 测试命令 | `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml ...` |
| Fixture manifest | `experiments/phase0/fixtures/manifest.json`，`sha256 = ed3f4fc4e061e13ef15c0e940af29734262b704a96776c25388813d6e649ddaa`（26 590 B，858 行，33 向量） |
| 控制站地址 | 只绑 `127.0.0.1`；默认端口 5500，测试用 0（随机空闲端口） |

依赖（均来自既有 `experiments/phase0` 环境，本任务**未新增任何依赖**）：
`psycopg 3.x`、`redis 5.x+`、`sqlalchemy[asyncio] 2.x`、`pyyaml`、`pytest`、`pytest-asyncio`、`ruff`。
控制站与 `faults.py` 只使用标准库：`http.server`、`socketserver`、`threading`、`json`、`hashlib`、`urllib.request`。

## 2. 交付物清单（行数 / 字节 / sha256）

| 文件 | 行数 | 字节 | sha256 |
| --- | --- | --- | --- |
| `experiments/phase0/control_site/__init__.py` | 36 | 1128 | `9a079f3006aaf70e12301da6ee5721256d48aa8450c4a814cad57a1e29e02e95` |
| `experiments/phase0/control_site/__main__.py` | 151 | 5297 | `04f2d02878b72cb12d5fc188006b51d0d21d27f7470add6d0bd4e5a487139ca3` |
| `experiments/phase0/control_site/client.py` | 186 | 6579 | `e50c5a18e4008cd9cd8d95c5f9bee9bed0e79de6b8ba0f3957a1d414f8abc138` |
| `experiments/phase0/control_site/handlers.py` | 677 | 27916 | `983b0f97450041d609bd7b07f13eed890eb10b3b4cae9bbc7990cfd0877cefc5` |
| `experiments/phase0/control_site/payloads.py` | 176 | 6561 | `76e288fb05c2115e6ac61c145462d2402fbeec6f7c65481bfc2ee5e9aaa64d55` |
| `experiments/phase0/control_site/server.py` | 242 | 9084 | `ef4ed40712c45623c127c6909524052b9c3304e0a7f5757cb7bbba14dd1181e4` |
| `experiments/phase0/control_site/store.py` | 352 | 13414 | `055a7353774d308159ed14586641bd8408570dcc1d8c358a2000538e483f1b23` |
| `experiments/phase0/faults.py` | 317 | 11914 | `9b30a2cbc3a65928fd8428f64b53dbb2acdb3d973bc353b2cd7da92dca495792` |
| `experiments/phase0/fixtures/manifest.json` | 858 | 26590 | `ed3f4fc4e061e13ef15c0e940af29734262b704a96776c25388813d6e649ddaa` |
| `tests/phase0/conftest.py` | 206 | 7607 | `5a817efa43ccf4d77c191df84d214050eae6148b5f790a025b419937e87bb16e` |
| `tests/phase0/test_control_site.py` | 376 | 15741 | `9cec7d1563308113d0ddadfe1acc4712f90e238665589dfd9c8fdc4db447e5d7` |
| `tests/phase0/test_faults.py` | 329 | 12795 | `6b8e3def954a33a19e8d27a0901c9faaaf480ff7ea0366901f7e117aa115afbd` |

未改动（属于其它任务）：`experiments/phase0/pyproject.toml`、`compose.yaml`、`.env.example`、`uv.lock`、
`contracts.py`、`scope.py`、`db/`、`tests/phase0/test_scope.py`、`tests/phase0/test_environment.py`。

## 3. 设计要点

- **控制站只做测试用计数器与故障节点**：内存态 `ControlStore`，零落盘、不连 PG/Redis、不写任何业务
  Run/Job 表；进程退出即状态消失。
- **幂等写**：`/idem/write` 以 `key` 去重，第二次起返回 `deduplicated=true` 且 `applied_calls` 恒为 1；
  **非幂等写**：`/nonidem/write` 每次 `applied_calls + 1`；`/idem/count`、`/nonidem/count` 只读查单。
- **屏障语义**（`faults.FaultBarrier`）：`idle → armed → hit → released|timeout`。`arm` 设定断点，
  服务端真正到达断点时 `hit()` 阻塞并唤醒 `wait_armed`；`release` 放行；到 `timeout` 自动放行并转
  `timeout`，不会把测试永久挂死。`release_all()` 在 `ControlSite.stop()` 与 fixture teardown 双重调用。
  **无固定 sleep 参与时序判断**，全部基于 `threading.Condition`。
- **故障类型覆盖**：`delay`（可控延迟，hit 后才返回 200）、`timeout`（hit 后主动断连/或可控延迟）、
  `server_error`（503）、`truncate`（声明 Content-Length 但只写部分字节 → 客户端 `IncompleteRead`）、
  `disconnect_after_success`（**副作用先落计数**，再 abortive close，客户端拿不到响应 → F3 型窗口）、
  `login_required`（人工登录页，`mode=wall` 时 401、默认 200 演示页）。
- **故障入口双门禁**：进程内默认 `allow_fault_injection=False`，`/fault/*` 与 `/_fault/*` 一律 404
  （与未知路径不可区分）；CLI 需 `--allow-fault-injection` **且** `PYKNORA_CONTROL_SITE_ALLOW_FAULTS=1`，
  且 `PYKNORA_ENV ∈ {production, prod}` 时**拒绝启动**。
- **到达日志脱敏**：`/_control/requests` 只记录敏感头的**名字**集合
  （`authorization`/`cookie`/`proxy-authorization`/`x-api-key`/`x-auth-token`/`set-cookie`），从不记录值。
- **载荷确定性**：页面/二进制由固定模板与 `sha256` 计数器模式生成，`PAYLOAD_ID` 固定；manifest 中 7 条
  内容向量的 `body_sha256` 取自**真实响应字节**，测试会重新请求并比对，防止 manifest 与实现漂移。

## 4. 命令与真实输出

### 4.1 ruff（用户指定路径集）

```powershell
experiments\phase0\.venv\Scripts\ruff.exe check experiments/phase0/control_site experiments/phase0/faults.py tests/phase0/conftest.py
```

```text
All checks passed!
RUFF_EXIT=0
```

（同一套规则另跑过包含 `tests/phase0/test_control_site.py tests/phase0/test_faults.py` 的路径集，同样 `All checks passed!`。）

### 4.2 pytest：本任务全部用例（两个文件）

```powershell
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml `
  tests/phase0/test_control_site.py tests/phase0/test_faults.py -v -o addopts= -p no:cacheprovider
```

```text
74 passed in 23.14s
```

（另一轮同命令 `74 passed in 24.10s`；计数一致。）

### 4.3 pytest：用户指定的 `-k` 选择（含 1 条 `test_scope.py` 用例，共 75 条）

```powershell
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0 -q -k "control or fault"
```

```text
EXITCODE=0
```

> `tests/phase0/pyproject.toml` 已带 `addopts="-q"`，命令行再给一个 `-q` 即 `-qq`，pytest 会**省掉末行统计**，
> 所以上面只有 75 个点与 `EXITCODE=0`。为拿到计数，去掉重复 `-q` 再跑同一选择：

```text
75 passed, 27 deselected in 30.25s
EXITCODE=0
```

`--collect-only` 显示该选择 = 74 条本任务用例 + 1 条 `tests/phase0/test_scope.py` 中命中关键字的用例；
后者只被读取执行，本任务未修改它。

### 4.4 故障入口门禁（真实起进程 + 真实 HTTP）

脚本 `.t04_tmp/gate_probe.ps1`（临时，收尾已删）四场景，`python -m control_site --port 5501`：

```text
A_HEALTH=True
A_FAULT_STATE=404          # 默认启动：/_fault/state 不可用
A_FAULT_DELAY=404          # 默认启动：/fault/delay 不可用
A_NORMAL_PAGE=200          # 普通页面正常
A_PORT_FREE=True
B_EXIT=2                   # 传 --allow-fault-injection 但未设环境变量 → 拒绝启动
B_STDERR=[control-site] 拒绝启用故障注入：需要显式设置 PYKNORA_CONTROL_SITE_ALLOW_FAULTS=1 才允许启用故障注入
B_PORT_FREE=True
C_HEALTH=True
C_FAULT_STATE=200          # flag + PYKNORA_CONTROL_SITE_ALLOW_FAULTS=1 才挂载
C_HEALTH_FAULTS=true       # /healthz 自报 fault_injection=true
C_PORT_FREE=True
D_EXIT=2                   # PYKNORA_ENV=production 时即使 flag+env 也拒绝
D_STDERR=[control-site] 拒绝启用故障注入：PYKNORA_ENV=production 禁止启用故障注入
D_PORT_FREE=True
GATE_DONE
```

（原始日志中文因 PowerShell 控制台 GBK 回显为乱码，上述文本取自 `control_site/__main__.py`
`_faults_allowed_by_env()` 的实际字面量。）

### 4.5 复用性：同一进程内 10 轮起停 + 每次 kill

`python -m control_site --self-check 10 --port 5501`（临时脚本，收尾已删；`kill_exit=1` 是 Windows
`taskkill /F` 的强制终止码，不是命令失败）：

```text
cycle healthy health_body                                                    write_status listen_before_kill kill_exit listen_after_kill port_free
----- ------- -----------                                                    ------------ ------------------ --------- --------------- ---------
    1    True {"fault_injection": false, "request_id": 2, ... "uptime_s": 0.391}  200            1           1               0      True
    2    True {"fault_injection": false, "request_id": 2, ... "uptime_s": 0.312}  200            1           1               0      True
    3    True {"fault_injection": false, "request_id": 2, ... "uptime_s": 0.328}  200            1           1               0      True
    4    True {"fault_injection": false, "request_id": 2, ... "uptime_s": 0.25}   200            1           1               0      True
    5    True {"fault_injection": false, "request_id": 2, ... "uptime_s": 0.282}  200            1           1               0      True
    6    True {"fault_injection": false, "request_id": 2, ... "uptime_s": 0.281}  200            1           1               0      True
    7    True {"fault_injection": false, "request_id": 2, ... "uptime_s": 0.265}  200            1           1               0      True
    8    True {"fault_injection": false, "request_id": 2, ... "uptime_s": 0.906}  200            1           1               0      True
    9    True {"fault_injection": false, "request_id": 2, ... "uptime_s": 0.25}   200            1           1               0      True
   10    True {"fault_injection": false, "request_id": 2, ... "uptime_s": 0.312}  200            1           1               0      True

ALL_PORT_FREE=True
ALL_HEALTHY=True
LISTEN_AFTER_TOTAL=0
STATE_FILES_IN_PHASE0=0
```

- `healthz` 文本里 `request_id=2` 是每轮健康检查的计数（`healthz` 自身不计数，见 §6 说明）。
- `STATE_FILES_IN_PHASE0=0`：10 轮结束后 `experiments/phase0` 下没有新增状态/数据文件。

### 4.6 实测发现并修复的真缺陷（测试有效性证据）

1. **`ControlStore.reset()` 自死锁**：`reset()` 持有不可重入 `threading.Lock` 时调用 `snapshot()`
   （再次 `with self._lock`）→ 调用线程永久阻塞；在 HTTP 线程里表现为「`/_control/reset` 请求永不返回」，
   在测试里表现为「pytest 进程静默停住、CPU≈0、faulthandler 定时 dump 不触发」。修复：`reset()` 内联
   构造 `after` 快照，不再重入加锁。
2. **`server_close()` 无界等待**：`shutdown()` 后仍有 keep-alive 线程时 `server_close()` 会等 join。
   修复：`ControlSite.stop()` 把 `server_close()` 放进 daemon 线程并带超时（`_close_timed_out` 标记），
   超时即放弃并记录，保证 teardown 不挂死。
3. **计数完成信号缺失**：只读查单在「请求已投递、处理未完成」时读到中间态。修复：`store` 增加
   `completed_count` 与 `wait_for_completed(count, timeout)`（条件变量），测试改为等待完成数而不是 sleep。
4. **`login_required` 200 演示页漏 `X-Phase0-Fault` 头**：只有 401 `mode=wall` 分支带该头，测试按
   故障名断言时失败。修复：两条分支统一带故障名头。

这三处正好说明 fixture 不是「自证成功」：它们都是先被测试/探针抓出、再修的。

## 5. 预期 vs 实际

| 验收点 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- |
| 幂等写重复 key | `applied_calls` 恒 1、`attempts` 递增、带 replay 头 | 与预期一致（用例 `test_idempotent_write_deduplicates_by_key`） | PASS |
| 非幂等写 | 每次 `applied_calls + 1` | 与预期一致（用例 `test_non_idempotent_write_counts_every_call`） | PASS |
| 只读查单 | 返回该 key 的 counts 与 `last_status` | 与预期一致 | PASS |
| 静态/JS/iframe/重定向(301,302)/下载(html,binary)/登录页 | 约定状态码与 Content-Type，301/302 带 `Location` | 与预期一致（含 manifest 7 条 sha256 逐字节比对） | PASS |
| `/healthz` | 200 + `fault_injection` 门态 + 计数 | 与预期一致 | PASS |
| 故障入口默认关闭 | `/fault/*`、`/_fault/*` 404 且与未知路径同形 | 404 `not_found`，与 `/nope` 响应体一致 | PASS |
| 生产默认不可启用 | flag 缺 env 拒绝、`PYKNORA_ENV=production` 拒绝 | exit 2 并给出拒绝原因（§4.4 B/D） | PASS |
| 六类故障 | delay/timeout/5xx/断流/成功断连/人工登录各自可复现且可释放 | 全部断言通过；`truncate` 触发客户端 `IncompleteRead`；`disconnect_after_success` 计数 +1 但客户端无响应 | PASS |
| 断点屏障 | `wait_armed` 确认服务端到达断点，`release` 放行，超时自动放行 | 与预期一致；无固定 sleep | PASS |
| 起停可重复 | 10 次重建后无遗留监听、无遗留数据 | §4.5：`port_free=True` ×10、`LISTEN_AFTER_TOTAL=0`、`STATE_FILES_IN_PHASE0=0` | PASS |
| 只绑回环 | 无对外监听 | 监听 `127.0.0.1`；fixture 与 CLI 均显式传 host | PASS |
| 不新增依赖 | pyproject 未改 | `git status` 中 `experiments/phase0/pyproject.toml` 无改动 | PASS |

## 6. 控制向量清单（manifest，33 条）

`experiments/phase0/fixtures/manifest.json`：`schema_version=1`、`task=P0-T04`、
`vector_count=33`、`hash_vector_count=7`、正/负样本 `21 / 12`；
`content_license.spdx = LicenseRef-PyKnora-Phase0-TestFixture`（合成测试素材，非第三方素材；
仓库无 LICENSE 文件，故用 `LicenseRef-` 形式声明）。每条向量含
`id / type / polarity / license / source / method / path / integrity / expected / consumers / fault_matrix`。

| ID | 类型 | 极性 | 请求 | 预期（摘要） |
| --- | --- | --- | --- | --- |
| CS-001 | static_page | 正 | `GET /` | 200, text/html, body sha256 固定 |
| CS-002 | static_page | 正 | `GET /static/page.html` | 200, body sha256 固定 |
| CS-003 | js_page | 正 | `GET /static/js.html` | 200，页面内含 `fetch(` 脚本 |
| CS-004 | js_page | 正 | `GET /js/beacon?key=js-beacon-1` | 200，JS beacon 目标，到达日志记录 key |
| CS-005 | iframe_page | 正 | `GET /static/iframe.html` | 200，内含 `<iframe src=...>` |
| CS-006 | iframe_page | 正 | `GET /static/frame.html` | 200，被 iframe 引用的内层页 |
| CS-007 | redirect | 正 | `GET /redirect/301` | 301 + `Location: /static/page.html` |
| CS-008 | redirect | 正 | `GET /redirect/302` | 302 + `Location: /static/page.html` |
| CS-009 | download_html | 正 | `GET /download/html` | 200, text/html, `Content-Disposition: attachment` |
| CS-010 | download_binary | 正 | `GET /download/binary` | 200, application/octet-stream, 4096 B, sha256 固定 |
| CS-011 | login_page | 正 | `GET /login` | 200 登录表单（人工参与） |
| CS-012 | login_page | 负 | `POST /login` 错误口令 | 401，不建立会话 |
| CS-013 | login_page | 正 | `POST /login` 正确口令 | 303 + `Set-Cookie`，随后 `/protected` 200 |
| CS-014 | counter_idempotent | 正 | `POST /idem/write {key}` | 200，`applied_calls=1`，重复同 key 不增加 |
| CS-015 | counter_non_idempotent | 正 | `POST /nonidem/write {key}` | 200，每次 `applied_calls + 1` |
| CS-016 | counter_read | 正 | `GET /idem/count?key=...` | 200，返回 counts 与 last_status |
| CS-017 | counter_read | 正 | `GET /nonidem/count?key=...` | 200，非幂等面查单 |
| CS-018 | counter_idempotent | 负 | `POST /idem/write {key:"bad key!"}` | 400 `invalid_key`，零副作用 |
| CS-019 | error_unknown | 负 | `GET /nope` | 404 `not_found` |
| CS-020 | counter_idempotent | 负 | `PUT /idem/write` | 405 + `Allow` 头 |
| CS-021 | fault_delay | 正 | `GET /fault/delay?key=...` | 屏障 hit 后 200（可控延迟） |
| CS-022 | fault_timeout | 负 | `GET /fault/timeout` | hit 后主动断连，客户端无响应（超时类） |
| CS-023 | fault_server_error | 负 | `GET /fault/server_error` | 503（未 arm 时基线 503） |
| CS-024 | fault_truncate | 负 | `GET /fault/truncate` | 声明长度但短写 → `IncompleteRead` |
| CS-025 | fault_disconnect_after_success | 负 | `GET /fault/disconnect_after_success?key=...` | 计数已 +1，客户端无 receipt（F3 窗口） |
| CS-026 | fault_login_required | 正 | `GET /fault/login_required` | 人工登录页（自动化不得绕过） |
| CS-027 | error_unknown | 负 | `GET /_fault/state`（默认关闭） | 404，与未知路径不可区分 |
| CS-028 | static_page | 负 | `GET /static/missing.html` | 404 |
| CS-029 | health | 正 | `GET /healthz` | 200 + `fault_injection` 门态 |
| CS-030 | login_page | 负 | `GET /protected`（未登录） | 401 `login_required` |
| CS-031 | counter_read | 负 | `GET /idem/count`（缺 key） | 400 `key_required` |
| CS-032 | fault_delay | 正 | `POST /_fault/arm {name:"delay"}` | 200（控制面；仅显式开启故障入口的站点） |
| CS-033 | fault_delay | 正 | `POST /_fault/release {name:"delay"}` | 200 并清理屏障 |

## 7. 故障矩阵映射（`docs/poc/05_Phase0_Acceptance.md` §3 F1–F18）

判定口径：**能力** = 本任务是否交付了可复现该窗口的控制站/屏障；**场景** = 该 F 行要求的业务断言
是否已在本任务内跑过。本任务不执行 F1–F18 的业务断言（它们属于 T05–T30）。

| F | 场景（05 §3） | 本任务提供的能力 | 能力 | 场景 |
| --- | --- | --- | --- | --- |
| F1 | DB 成功 Redis 失败 | 控制站本身不接 Redis；由其它任务的容器/代理注入，本任务提供 HTTP 侧断连基元 | 间接 | NOT_RUN |
| F2 | Job 重投 | `/nonidem/write` 暴露重复副作用；`disconnect_after_success` 造「结果未知」 | 部分 | NOT_RUN |
| F3 | 外部成功 receipt 前崩溃 | `disconnect_after_success`：**副作用已计数**、客户端拿不到响应；`/idem/count` 查 key 现状 | PASS（能力） | NOT_RUN |
| F4 | 批准后撤权/改参 | 需 provider 侧，本任务不提供 | 无 | NOT_RUN |
| F5 | Graph 节点重入 | `/_control/requests` 到达日志可用于断言「同 intent 只到达一次」 | 部分 | NOT_RUN |
| F6 | 旧版本晚于新版本 | 无（对象存储侧，非本任务） | 无 | NOT_RUN |
| F7 | 取消/删除后结果 | `disconnect_after_success` + `/nonidem/write` 可造「已发未回」 | 部分 | NOT_RUN |
| F8 | 核心成功富化失败 | 无（DB/Outbox 侧） | 无 | NOT_RUN |
| F9 | 浏览器 owner/结果未知 | `disconnect_after_success`、`delay` 屏障可模拟动作已发断连；无 WS/浏览器端 | 部分 | NOT_RUN |
| F10 | SSE/消息/游标失败 | `delay`/`truncate` 可造流式截断；SSE 端点未提供 | 部分 | NOT_RUN |
| F11 | 并行/重规划争预算 | 无（DB/Redis 侧） | 无 | NOT_RUN |
| F12 | 单锁连接丢失 | 无（PG 侧） | 无 | NOT_RUN |
| F13 | 无 RunJob 身份传播 | `/idem/write`、`/_control/requests` 可记录到达身份头**名**，不记录值 | 部分 | NOT_RUN |
| F14 | 对象缺失/FK 环 | 无（对象存储/DB 侧） | 无 | NOT_RUN |
| F15 | 浏览器/HTTP 每跳出站 | 已具备：重定向 301/302、iframe、下载（html/binary）、JS beacon、人工登录页、到达日志（最终端点） | **部分**：私网 IPv6、WS、DNS rebinding 端点未提供 | NOT_RUN |
| F16 | secret/PII/提示注入 | 到达日志只存敏感头**名字**、从不存值（有用例断言 canary 不落日志）；无提示注入文本页 | 部分 | NOT_RUN |
| F17 | DAG 旧 revision/失败 | 无（Graph/DAG 侧） | 无 | NOT_RUN |
| F18 | 还原/升级失配 | 无（备份/升级侧） | 无 | NOT_RUN |

**不得混淆**：上表「PASS（能力）」只表示**控制站能稳定复现该窗口**，不表示业务系统已正确处理该窗口。
F15 的 SSRF 负例仍必须由使用方记录「最终到达端点」（控制站 `/_control/requests` 的
`path`/`method`/`headers_present` 正是为此准备），「控制站返回 blocked」不构成网络未到达的证据。

## 8. 清理验证

| 检查 | 结果 |
| --- | --- |
| 10 轮起停后端口遗留 | `ALL_PORT_FREE=True`、`LISTEN_AFTER_TOTAL=0` |
| 10 轮起停后数据遗留 | `STATE_FILES_IN_PHASE0=0`；控制站状态全内存、进程退出即消失 |
| 临时脚本/日志目录 | 证据与临时物均放在 `.t04_tmp/`（未跟踪），取证完成后已删除 |
| 本任务起过的独立进程 | 全部 `taskkill /PID <pid> /T /F`（`kill_exit=1` 为强制终止码）；`Get-NetTCPConnection -LocalPort 5500/5501` 均为空 |
| 因诊断误产生的仓库根文件 | `uv run --with py-spy` 曾在仓库根生成 `.venv/`（gitignored）与 `uv.lock`（未跟踪）；两者均非本任务交付物，已删除 |
| 交付物以外的修改 | 无：`git status` 中本任务只新增 5 项（control_site/、faults.py、fixtures/、tests 三个文件、本证据文档） |

最终 `git status --porcelain`：**68 行**，`sha256 = 4aa06cb34f37065d1b8224bd5979993c48976ad6cf017d4466ace54e982a768a`。
其中 17 行未跟踪（本任务新交付物 + 既有未跟踪文件），51 行是已跟踪文件的**纯换行符差异**
（`git diff --numstat` 合计 `added=7932 / deleted=7932`，逐行成对；工作区为 CRLF、索引为 LF）。

```text
?? .idea/
?? CLAUDE.md
?? docs/plans/README.md
?? docs/poc/evidence/control-site.md
?? docs/poc/evidence/poc01-scope.md
?? docs/poc/evidence/wave-w2-record.md
?? docs/teacher/
?? experiments/phase0/contracts.py
?? experiments/phase0/control_site/
?? experiments/phase0/db/
?? experiments/phase0/faults.py
?? experiments/phase0/fixtures/
?? experiments/phase0/scope.py
?? tests/phase0/conftest.py
?? tests/phase0/test_control_site.py
?? tests/phase0/test_faults.py
?? tests/phase0/test_scope.py
```

未跟踪清单里的 `.idea/`、`CLAUDE.md`、`docs/plans/README.md`、`docs/poc/evidence/poc01-scope.md`、
`docs/poc/evidence/wave-w2-record.md`、`docs/teacher/`、`contracts.py`、`db/`、`scope.py`、`test_scope.py`
都不是本任务产物（分别属于用户本地目录与 T03）。本任务产物是本表的 control_site/、faults.py、fixtures/、
conftest.py、test_control_site.py、test_faults.py 与本证据文档。

### 8.1 观察到的异常（非本任务改动，交由 Lead 判定）

取证开始时（本任务代码尚未完成）`git status` 只有未跟踪文件；取证末态出现 51 个已跟踪文件的 `M`。
核对结论：**纯换行符翻转**，不是内容改动。

- 证据：`git diff --numstat | awk` 合计 `added=7932 deleted=7932`；逐文件插入数=删除数（如 `.gitignore` 21/21）。
- 证据：`file .gitignore AGENTS.md …` 报 `CRLF`，而本任务新写的文件（`faults.py`、`handlers.py`、`conftest.py`、
  本证据文档）均为 LF，与仓库 blob 一致。
- 影响：`git diff` 不再是空 diff，§1 的「dirty hash」因此改变；本任务**未运行任何 `git` 写操作**，
  也未修改这 51 个文件的内容。是否由编辑器/工具行的 CRLF 归一化引起，需要 Lead 确认后再决定如何处理
  （本任务按约束不碰 git，也不回滚他人改动）。

### 8.2 清理后回归

删除 `.t04_tmp/`、根 `uv.lock`、根 `.venv` 之后，用同一命令复跑本任务全部用例：

```text
74 passed in 23.31s
```

## 9. NOT_RUN / BLOCKED

| 项 | 状态 | 原因 |
| --- | --- | --- |
| F1–F18 的业务断言本身 | NOT_RUN | 需要 T05–T30 的业务实现与 PG/Redis/Graph 侧注入；本任务只交付 fixture |
| 私网 IPv6 / WebSocket / DNS rebinding 控制站端点（F15 要求之一） | NOT_RUN（能力缺口） | 控制站按约束只绑 `127.0.0.1`；这三类需真实网络栈或代理层，且本地无法安全模拟 rebinding |
| 浏览器/真实 Playwright 走查（F9/F15） | BLOCKED | 环境无 Playwright/FastAPI，且人工登录、验证码必须由用户处理；自动化不得绕过 |
| JUnit XML 报告落盘 | BLOCKED | 本机沙箱禁止子进程在 `%TEMP%` 建目录（`_pytest/junitxml.py` 打开临时文件失败），改用真实控制台输出入证 |
| 真网络出站/隔离证据 | NOT_RUN | 本任务所有 HTTP 只发生在 `127.0.0.1`，不能作为 SSRF 隔离证据；见 §7 说明 |
| 提示注入文本/PII canary 页面（F16 一半） | NOT_RUN（能力缺口） | 未在本次范围实现；敏感头「只记名字」已实现并有用例 |

## 10. 残余风险

1. **时序类断言仍可能受慢机影响**：屏障用条件变量 + 超时，理论上无 sleep，但 `timeout`/`truncate`
   在极慢环境下可能先撞客户端超时；用例已一律给 5–10 s 上限并在失败信息里打印屏障状态。
2. **`disconnect_after_success` 的平台差异**：abortive close 依赖 `SO_LINGER` 平台编码（Windows
   `HH`、POSIX `ii`），在 Linux CI 上需重跑本文件确认（当前只在 Windows 实测）。
3. **`truncate` 依赖客户端实现**：urllib 抛 `IncompleteRead`；换 `httpx`/`requests` 时会抛不同异常，
   使用方需按「响应不完整」而非具体异常类断言。
4. **F15 能力缺口**：IPv6/WS/DNS rebinding 未覆盖，使用 F15 的任务需自建或扩展本控制站（须另开任务，
   不要在 T04 文件里堆业务逻辑）。
5. **真实 `kill` 只测了 `taskkill /F`**：SIGKILL 之外的优雅终止（Ctrl-C）路径由 `stop()` 覆盖，
   但未在 CI 上验证。
6. **manifest 与实现可能漂移**：已有用例对 7 条内容向量逐字节比对；新增页若不更新 manifest，
   该用例会失败，这是预期行为。

## 11. 复现步骤

```powershell
# 1) 静态检查
experiments\phase0\.venv\Scripts\ruff.exe check experiments/phase0/control_site experiments/phase0/faults.py tests/phase0/conftest.py

# 2) 本任务用例
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml `
  tests/phase0/test_control_site.py tests/phase0/test_faults.py -v -o addopts=

# 3) 用户指定的选择
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0 -q -k "control or fault"

# 4) 手动起站（默认无故障入口）
uv run --project experiments/phase0 python -m control_site --port 5500
#    显式开故障入口（仅本机实验）：
$env:PYKNORA_CONTROL_SITE_ALLOW_FAULTS = "1"
uv run --project experiments/phase0 python -m control_site --port 5500 --allow-fault-injection

# 5) 复用性自检（不常驻）
uv run --project experiments/phase0 python -m control_site --self-check 10 --port 5501
```

## 12. 证据溯源

- ruff：`.t04_tmp/ruff4.log`（`RUN: All checks passed!`）
- pytest 用例明细：`.t04_tmp/v1.out`（74 passed）
- pytest `-k` 选择：`.t04_tmp/kq.log`、`.t04_tmp/kq2.log`
- 门禁四场景：`.t04_tmp/gate_probe.log`（含 A/B/C/D）
- 10 轮起停：`.t04_tmp/cycle10.log`
- 上述临时物在取证后已删除；本文保留了它们的原文输出，故结论可复核而无需保留脚本。
- manifest 校验：`tests/phase0/test_control_site.py::test_manifest_hash_matches_live_bytes` 等用例每次运行都会重算。

## 13. Owner 与复核

- 交付 Owner：`w2-t04-controlsite`（P0-T04 执行者）
- 待复核：Wave W2 Lead（复核后本文件状态再更新）
- 复核要点：①`pyproject.toml`/`compose.yaml`/`contracts.py`/`scope.py`/迁移文件确实未改；
  ②故障入口在默认配置下 404（§4.4 A）；③10 轮起停无遗留（§4.5）；④manifest 33 条含许可与来源。
