# P0-T05 / P0-T09 收敛补口计划

- 日期：2026-10-28
- 状态：**提议**（未执行；需用户当轮授权）
- 对应任务：`docs/poc/03_Phase0_Task_Backlog.md:82-97`（P0-T05）、`:150-165`（P0-T09）
- 对应验收：`docs/poc/05_Phase0_Acceptance.md:52`（F12）、`:70`（G-P0 第 1 条）、`:73`（G-P0 第 4 条）、`:80`（G-FULL）
- 前置文档：`docs/poc/evidence/poc01-owner.md`、`docs/poc/evidence/docreader_manifest.md`、`docs/adr/0001_Runtime_Boundaries.md`

## 1. 目标与范围

把两项从「PARTIAL 且原因未闭合」推进到**可判定的终态**，二者路径不同：

| 任务 | 缺口性质 | 可达终态 |
| --- | --- | --- |
| P0-T05 | 假设被证伪：原生 `PostgresSaver` 无法 fence 独立 saver 连接 | `PASS（限域：单活）` 或 `FAIL`，二者都必须有明确依据；多实例项保留 PARTIAL |
| P0-T09 | 证据不完整：4 类 UNKNOWN 未记录 | `PASS`（4 类全部转为记录）或 `PARTIAL`（列出阻断与替代） |

范围外：不把任何 ADR 状态改为「已接受」；不宣称 G-FULL；不推进 W7（T25/T27/T30）；不修改 `references/WeKnora`。

## 2. 前置依赖与已确认输入

本节全部为 2026-10-28 本机会话实测，不是文档转述。

| 事实 | 实测结果 | 影响 |
| --- | --- | --- |
| Docker | 29.7.2，服务端可用 | 可拉镜像、可跑容器盘点 |
| 镜像加速 | 已配 4 个 mirror；`docker.1panel.live`、`docker.1ms.run` 的 `/v2/` API 可直读 | 绕过 Docker Hub 封锁 |
| 直连封锁 | `registry-1.docker.io`、`pypi.org` 均不通；`docker manifest inspect` 因直连失败 | 只能用 mirror；`pip` 装包需换源 |
| docreader 镜像 tags | 47 个，含 `latest`、`v0.8.3`、`amd64-latest`、`arm64-latest` | 镜像存在，可拉 |
| 镜像 digest | index `sha256:9af668ac0ac857b9dfe8430d0fe77c0e5ad2636dc2b5285cd5500a13d5a35266`；amd64 manifest `sha256:ee308c6d69f278619794bd5f42af1d78624efefb57dce345e5d00d12798b19fa`；config `sha256:2e53ece9…6f67df0` | UNKNOWN-1（digest）可关闭 |
| 镜像体积 | 18 层，压缩后 1503.6 MB（最大单层 740.1 MB） | 拉取成本可预估，可行 |
| 镜像 = 哪个提交 | `latest` 与 `v0.8.3` 同 digest；`v0.8.3` tag 指向 `3e22822c1`；审计基线 `662abaf9` = `v0.8.3-202-g662abaf9f` | **镜像比审计基线早 202 个提交**，不能声称「镜像即审计源」 |
| PyPI JSON | 清华 `/pypi/<name>/json` 返回 200 但 `name/version/license` 全空；直连 pypi.org 超时 | 许可元数据必须从镜像内取，不能靠 PyPI JSON |
| GitHub | `git ls-remote origin` 可用 | 可核对 tag/commit 对应关系 |
| 已有许可素材 | `references/WeKnora/licenses/` 10 个文件，含 `rapidocr-MODEL_LICENSES.md`(8560B) | OCR 权重许可有上游来源，仍需对镜像内实际文件核账 |

## 3. 步骤与涉及文件

### 3.1 P0-T09（S1–S6）

| 步 | 内容 | 文件（新建 N / 修改 M） |
| --- | --- | --- |
| S1 | 固定镜像坐标：`wechatopenai/weknora-docreader:v0.8.3@sha256:9af668ac…`，记录 amd64/arm64 平台 digest 与发布提交 `3e22822c1` | N `docs/poc/evidence/docreader_manifest_amendment-01.md` |
| S2 | `docker pull` 经 mirror，校验 `RepoDigests` 与 S1 一致；记录耗时与失败重试 | N 同上（续写） |
| S3 | 镜像内只读盘点：`dpkg-query -W`、`python3 -m pip list --format=json`、`importlib.metadata` 的 `License-Expression`/`Classifier`、`find / -name '*.onnx' -o -iname '*rapidocr*'` 并算 sha256、读 `/usr/share/doc/*/copyright` 的 License 字段 | N `docs/poc/evidence/docreader_image_inventory.json`；N `experiments/phase0/audit_docreader_image.ps1`（只读脚本） |
| S4 | 生成 SBOM：组件/版本/来源(apt\|pip\|model)/SPDX/许可出处/校验和；未知项单列并给阻断与替代（对应 `03:159`） | N `docs/poc/evidence/docreader_sbom.md` |
| S5 | 与基线交叉核对：`docreader/uv.lock`、`docreader/pyproject.toml` 的版本闭包对比镜像内实际版本，差异逐条编号（续 D-13…）；202 提交差写入限域声明 | N 同上 amendment（§4.6/§7 对应节） |
| S6 | 判定与状态回写：T09 → `PASS` 或 `PARTIAL`（含阻断清单）；新建本次波次记录，不追改 W1 记录 | N `docs/poc/evidence/wave-w7r-record.md`；M `docs/IMPLEMENTATION_STATUS.md` |

**T09 同样采旁挂式**：`docs/poc/evidence/docreader_manifest.md` 原文不动；新增 amendment 逐条对应其 §4.3/§4.5/§4.6/§7/§9，并显式声明「本节取代原文某节」，原文保留为历史记录。

**量化验收（T09）**

1. 4 类 UNKNOWN 全部有记录：digest 固定；pip 许可 100% 有 SPDX 或 `unknown(原因)`；apt 许可 100%；OCR 权重文件名+大小+sha256 全量。
2. 拉取后的 `RepoDigests` 与 S1 记录逐字一致。
3. 基线交叉核对差异表非空则逐条解释；镜像与基线 202 提交差必须有明文限域声明。
4. 判定只用 `PASS`/`PASS（限域）`/`PARTIAL`，不得把限域写成全量。

### 3.2 P0-T05

**路线 A（推荐）：单活正式化，不做新 PoC**

| 步 | 内容 | 文件 |
| --- | --- | --- |
| A1 | 补 §6.1-E 进程级证据：子进程持有 distributor + saver；主进程 `pg_terminate_backend` 锁连接后，经 IPC 触发子进程写一次；原生路径断言「写仍被接受」（FAIL 证据），替身 fenced 路径断言拒绝。**只追加测试函数，不改已有断言** | M `tests/phase0/test_checkpoint_owner.py`（仅追加）；N `docs/poc/evidence/poc01_owner_process_level.json` |
| A2 | 新增 ADR-0006（提议）：单活 runner 为 P0 唯一形态、禁止自动接管、多实例横扩不在 P0 范围；关联 ADR-0001 但不改其正文 | N `docs/adr/0006_Single_Active_Checkpoint_Runner.md` |
| A3 | 新增验收修订旁挂文件：重述 T05 判定、逐条引用原文件行号、声明多实例项仍 PARTIAL；**不改** `poc/03`、`poc/05`、`tdd/01`、`tdd/04`、`poc01-owner.md` | N `docs/poc/evidence/poc01-owner-amendment-01.md` |
| A4 | 重判：T05 → `PASS（限域：单活）`；G-P0 提交限域准入评审；G-FULL 仍不可宣称；只更新活文档 | M `docs/IMPLEMENTATION_STATUS.md` |

**为什么不需要改原文**：四处预案条款已经存在，T05 结论落在这四条写好的分支内，属于「按预案执行」，不是「改契约」：

| 依据 | 原文要点 |
| --- | --- |
| `docs/tdd/01_System_Architecture.md:166` | 「checkpointer 独立连接不天然 fenced……若不能证明，改为单活 runner，不能声称安全横扩」 |
| `docs/poc/05_Phase0_Acceptance.md:23` | 可延期但必须明示：「多实例未过只能 PARTIAL 单活继续」 |
| `docs/poc/03_Phase0_Task_Backlog.md:91` | 「否则明确 FAIL 或单活 PARTIAL，不隐藏失败」 |
| `docs/adr/README.md:15` | 「已接受记录发生变化时新增 ADR 并关联旧记录」 |

因此 `poc01-owner.md §7.1` 所说「提 ADR-0001 限域修订」，按仓库约定落地为新增 ADR-0006 并关联 0001，而不是改写 0001 正文。

**路线 B（备选，仅当多实例是硬需求）**：新增 PoC「checkpoint 持锁代理」——单 host 进程持锁且独占唯一 saver 连接，invocation 经 IPC 提交写，host 校验 epoch/lock 后落库，失锁即停。需 5 类测试（双 host 争锁、锁连接 kill、host 崩溃、旧 invocation 重连、进程级）。成本约 1.5 点，产出新证据文件。

**量化验收（T05 路线 A）**

1. 进程级测试在「锁连接被 kill、写进程仍存活」条件下产出明确断言结果，退出码为 0。
2. 证据文件记录进程 PID、锁连接 PID、写入结果、时间戳。
3. 新增三份文件（ADR-0006、amendment、状态文件）口径一致，且全文不出现「支持多实例」的表述；原文保持不变。
4. 判定字符串精确为 `PASS（限域：单活）`，并在同一段落写明「原多实例假设仍 PARTIAL」。

## 4. 验证命令

```powershell
# 镜像坐标与体积（不拉层）
Invoke-WebRequest 'https://docker.1panel.live/v2/wechatopenai/weknora-docreader/manifests/v0.8.3' `
  -Headers @{Accept='application/vnd.oci.image.index.v1+json'} -UseBasicParsing

# 拉取并核对
docker pull wechatopenai/weknora-docreader:v0.8.3
docker image inspect wechatopenai/weknora-docreader:v0.8.3 --format '{{json .RepoDigests}}'

# 镜像内盘点（只读）
docker run --rm --entrypoint sh wechatopenai/weknora-docreader:v0.8.3 -c "dpkg-query -W -f='\${Package}\t\${Version}\n'"

# T05 进程级证据
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_checkpoint_owner.py -q
uv run --project experiments/phase0 ruff check tests/phase0
```

证据落 `docs/poc/evidence/`，含命令、退出码、输出摘要、产物 sha256。

## 5. 风险、回滚与阻塞

| 风险 | 处置 |
| --- | --- |
| 1.5 GB 拉取失败/超时 | 依次换 `docker.1ms.run`、`docker.xuanyuan.me`；仍失败则只保留 digest（registry API 已可得），SBOM 降级为 lockfile+镜像外元数据，并在 §9 标注 weakened |
| 镜像与基线差 202 提交 | 不得声称「镜像=审计源」。默认接受为「发布制品盘点 + 差异限域」；若要求严格一致，改为从基线本地构建，但构建输入（apt/pip/Playwright/权重 URL）未固定，产物 digest 不可复现，需单独立项 |
| `references/WeKnora` 只读 | 审计脚本只读挂载，不修改参考源码 |
| ADR 仍是「提议」 | 只新增 0006 草案并关联 0001，不自标已接受；评审后由人改状态 |
| 口径漂移 | 所有判定字符串集中在一处生成，避免三处各写一套 |
| 波次越界 | 本计划为补口任务，不并入 W7；若用户授权，单独立声明 `wave-w7r-record.md` |

## 6. 写权与依赖

- T09 写域：`docs/poc/evidence/docreader_manifest_amendment-01.md`、`docreader_sbom.md`、`docreader_image_inventory.json`、`wave-w7r-record.md`（全为新建）、`experiments/phase0/audit_docreader_image.ps1`（新建）、`IMPLEMENTATION_STATUS.md`
- T05 写域：`tests/phase0/test_checkpoint_owner.py`（只追加函数）、`docs/poc/evidence/poc01-owner-amendment-01.md`、`poc01_owner_process_level.json`（新建）、`docs/adr/0006_*.md`（新建）、`IMPLEMENTATION_STATUS.md`
- 二者仅在 `IMPLEMENTATION_STATUS.md` 重叠，按 T09 → T05 顺序串行，或由 Lead 统一收口。
- 依赖：T09 解锁 T10/T11 运行层；T05 结论锁 W7 的 T30 多实例口径。

## 7. 明确不做

1. 不改 ADR 状态为「已接受」。
2. 不把「单活降级」写成「多实例安全」。
3. 不伪造 digest、不手填 SBOM 行、不用 PyPI 网页抓取冒充 SPDX。
4. 不在本计划内推进 G-FULL 或 W7。
5. **不修改任何原文**：`docs/poc/00–07`、`docs/poc/evidence/` 既有文件、`docs/tdd/00–08`、`docs/adr/0001–0005`、`tests/phase0` 既有断言，一律保持原样；修订只以新增文件表达，并在新文件中显式声明取代关系。
