# `docreader_manifest.md` 修订 01（旁挂增补，原文不动）

| 项 | 值 |
|---|---|
| amendment 编号 | **amendment-01** |
| 针对文件 | `docs/poc/evidence/docreader_manifest.md`（315 行，保持原样不修改） |
| 触发原因 | T09R：原文 §4.1/§4.5/§9 中「镜像 digest」「OCR 权重」「apt 许可」等项标为 `UNKNOWN`，原因是当时无 docker、无网络；本轮镜像已落地，实测数据可闭合其中大部分项 |
| 状态 | **PASS（限域）** —— 镜像可固定、四类 UNKNOWN 中三类闭合、一类部分闭合；限域声明见 §5 |
| 数据文件 | `docs/poc/evidence/docreader_sbom.md`（组件与许可明细）、`docs/poc/evidence/docreader_image_inventory.json`（原始盘点，sha256 `3c0d993a8fb4284ddb15f9826c58e35afc37695162bc27ccc38d67178772bb01`） |
| 生效方式 | 阅读原文 §4.3/§4.5/§4.6/§7/§9 时**必须同时读本文件对应章节**；两者冲突时以本文件的实测值为准（原文保留为「当时的静态审计记录」） |

---

## 1. 取代声明（显式，逐段）

本文件**取代** `docreader_manifest.md` 的下述内容。原文一个字不改，取代关系只在此声明：

| 原文位置 | 原文结论（摘要） | 本文件取代章节 | 取代后结论 |
|---|---|---|---|
| §4.3（L170-190）整表 + L190 结论句 | 26 条依赖；只有 4 个 OCR 包有仓内许可记录；「docreader 的 22 个非 OCR 依赖在仓内无任何许可记录」 | **§3 + §6**，明细见 `docreader_sbom.md §3` | 依赖集合按 **v0.8.3 = 22 条**（不是 26 条）；许可改由镜像内元数据判定：72 个 dist 中 **61 可判定 / 11 unknown**，apt **744/971 有 `License:` 字段 / 227 unknown** |
| §4.5（L199-206）整表，尤其 L206「权重具体文件/大小/校验和 `UNKNOWN`」 | 假设镜像内含 RapidOCR 权重，只是本轮拿不到 | **§4** | **镜像内不存在 RapidOCR 与任何 OCR 权重**；全盘 `*.onnx` 仅 4 个、最大 3.16 MB（magika 内容分类模型）。权重清单为**空集**，不是 `UNKNOWN` |
| §4.6（L208-215）四行可复现性缺口 | 镜像/基础镜像/apt/protoc 均无 digest 或版本固定 | **§7** | 逐行更新；新增 2 行（playwright webkit 未 pin、lock 文件可校验） |
| §7 D-01（L270） | 「镜像无 digest、tag 缺省 latest；镜像层不可复现」 | **§6 D-19 / D-22** | 镜像 digest 已取得并可从 registry 独立复核；**compose 侧仍用 tag**，属仓内配置缺口，非镜像缺陷 |
| §7 D-09（L278） | 「仅 4 个 OCR 包有记录；22 个 Python 依赖 + apt 包 + 权重文件清单均无；T10 前产出 SBOM（本轮 BLOCKED）」 | **§6 D-13/D-14/D-18 + `docreader_sbom.md`** | SBOM 已产出（旁挂）；OCR 权重项由「清单缺失」变为「不存在」；许可项由 BLOCKED 降为 PARTIAL |
| §9（L297-308）未确认项表 8 行 | 其中 5 行涉及镜像/依赖/权重 | **§8** | 逐行给出 CLOSED / PARTIAL / 仍 UNKNOWN 及证据位置 |

**不属于取代、只作追加事实的两处**（避免越权改动原文其他章节）：

| 原文位置 | 原文值 | 追加事实 |
|---|---|---|
| §4.1 表「镜像 digest」行（L158） | `UNKNOWN`（C 级） | `sha256:9af668ac0ac857b9dfe8430d0fe77c0e5ad2636dc2b5285cd5500a13d5a35266`（A 级，见 §2）。原文该行仍写 `UNKNOWN`，读时以本行为准 |
| §4.4（L196）「以上 apt 包在 LICENSE / THIRD_PARTY_NOTICES.md / licenses/ 中均无记录 → UNKNOWN」 | `UNKNOWN` | 该判断**仍然成立**（仓内确实无记录），但镜像内 `/usr/share/doc/<pkg>/copyright` 提供了另一路证据：971 包中 744 有 `License:` 字段。故 apt 许可从「全 UNKNOWN」细化为「744 可引用 Debian 短名 + 227 unknown」 |

---

## 2. 镜像坐标与拉取证据

### 2.1 坐标

| 项 | 值 |
|---|---|
| 引用 | `wechatopenai/weknora-docreader:v0.8.3` |
| **index digest** | `sha256:9af668ac0ac857b9dfe8430d0fe77c0e5ad2636dc2b5285cd5500a13d5a35266` |
| amd64 manifest | `sha256:ee308c6d69f278619794bd5f42af1d78624efefb57dce345e5d00d12798b19fa` |
| config | `sha256:2e53ece9f2a1caa9acef4b54e70cc9f079ef0c2f3103abca9f372e44a6f67df0` |
| arm64 manifest（未拉） | `sha256:3cbe152216ee0276adbf6d37069e52b333553f9b702c6575818d4c19225fcd44` |
| 层数 / 压缩大小 | 18 / 1,576,643,756 B（1503.6 MiB） |
| 解压后 | 5.67 GB（`docker images`） |
| created | `2026-10-14T07:57:12.189503576Z` |
| labels | `revision=3e22822c1d2397be730c458c7539d0eb8c113a24`、`version=v0.8.3`、`licenses=NOASSERTION`、`source=https://github.com/Tencent/WeKnora` |

### 2.2 三重校验（本地重算，不信任响应头）

| 对象 | 取法与校验 | 结果 |
|---|---|---|
| index | `GET https://docker.1panel.live/v2/wechatopenai/weknora-docreader/manifests/v0.8.3` → 1,609 B，`application/vnd.oci.image.index.v1+json`；对**原始字节**本地算 sha256 | `9af668ac…5266`，与响应头 `Docker-Content-Digest` **逐字一致** |
| amd64 manifest | 同一 URL + `Accept: application/vnd.oci.image.manifest.v1+json` → 3,740 B；本地重算 | `ee308c6d…19fa`，与 header 一致 |
| config | `GET …/blobs/sha256:2e53ece9…` → 13,038 B；本地重算 | `2e53ece9…7df0`，与 digest 一致 |

`docker image inspect` 落地后复核：`RepoDigests = ["wechatopenai/weknora-docreader@sha256:9af668ac0ac857b9dfe8430d0fe77c0e5ad2636dc2b5285cd5500a13d5a35266"]`，`Id` 同该 digest → **registry 侧与 docker 侧闭环一致**。

### 2.3 拉取流水（含失败）

| 时间（+08:00） | 动作 | 结果 |
|---|---|---|
| 11:08:26 | `docker pull wechatopenai/weknora-docreader:v0.8.3`（daemon 默认 mirror 顺序，首选 `docker.1panel.live`） | 18 层中 16 层 `Download complete`、12 层 `Pull complete`；11:20:24 后约 20 分钟无新行 |
| 11:36 | Lead 判定停滞 → 改用 `docker.1ms.run/…` | 11:41:41 起，仅剩 9 层；11:41:43 后无输出；WLAN 收包 0.6 MB/20 s ≈ 0.03 MB/s → 实质停滞 |
| 11:50 | 对最大层 `sha256:215e8f98e033…`（776,087,437 B）发 `Range: bytes=0-12582911` | 见 §9 表：只有 `docker.1panel.live` 返回 206 |
| 11:52:10 | kill 掉 1ms.run 任务，回退默认 `docker pull`（层缓存保留） | 实测 **54.8 MB/min ≈ 0.91 MB/s**，在真实下载 |
| **11:59:14** | 完成 | `Status: Downloaded newer image`，`Digest: sha256:9af668ac…5266` |

总 wall clock 11:08:26 → 11:59:14（含两次中断与一次 mirror 切换）；有效下载速率约 0.9 MB/s。

---

## 3. 依赖集合：原文 26 条 vs 镜像 22 条

`docreader/pyproject.toml` 在 **v0.8.3** 只有 **22** 个直接依赖；原文 §4.3 的 26 条是审计基线 `662abaf9f`（`v0.8.3-202-g662abaf9f`）的口径。镜像构建自 `v0.8.3`，**A 级证据**：镜像内 `/app/pyproject.toml` sha256 `757a432a66e826fbbc45f590bc41dd349cbafa2ecd1517596255a93b76d0259f` 与 `git show v0.8.3:docreader/pyproject.toml` 逐字节一致。

**镜像内不存在的 4 条（原文 §4.3 表头 4 行，全部是 OCR 相关）**：

| 依赖 | 原文声明 | 镜像实测 |
|---|---|---|
| `rapidocr` | `>=3.0`，Apache-2.0（锁 3.10.0） | **未安装**（`ModuleNotFoundError`），`uv.lock` 无此条目 |
| `onnxruntime` | `>=1.17`，MIT（锁 1.23.2） | **不是直接依赖**；作为 `magika 0.6.3` 的传递依赖安装 **1.20.1** |
| `opencv-python-headless` | `>=4.8`，Apache-2.0（锁 5.0.0.93） | **未安装** |
| `numpy` | `>=1.26`，BSD-3-Clause（锁 2.3.4） | **不是直接依赖**；作为传递依赖安装 **2.2.6** |

后两者（onnxruntime / numpy）在镜像里**存在但是传递依赖**，与原文「作为 OCR 直接依赖」的定性不同，故版本号也对不上（见 §6 D-15/D-16）。

### 3.1 与 `docreader/uv.lock` 的版本差异表

镜像内 `/app/uv.lock`（sha256 `57861b67…1f85`）与 `v0.8.3` 的 `docreader/uv.lock` **逐字节一致**，故本表等价于「`uv.lock` 声明集 vs 镜像实际安装集」的对账。

| 对照项 | `uv.lock`（v0.8.3） | 镜像（importlib.metadata） | 结论 |
|---|---:|---:|---|
| 条目 / 去重名 / dist | 80 / 78 / 72 | — / — / 72 | 交集 64；**版本不匹配 0 条** |

| 差异类别 | 数量 | 明细 | 解释 |
|---|---:|---|---|
| 命名写法不同（同一组件） | 8 对 | `ebooklib`↔`EbookLib`、`et-xmlfile`↔`et_xmlfile`、`justext`↔`jusText`、`lxml-html-clean`↔`lxml_html_clean`、`pdfminer-six`↔`pdfminer.six`、`pydantic-core`↔`pydantic_core`、`speechrecognition`↔`SpeechRecognition`、`typing-extensions`↔`typing_extensions` | lock 用 PEP 503 归一名，`importlib.metadata` 用 `Name` 原始名；**不是真差异** |
| marker 排除（Windows-only） | 2 | `colorama 0.4.6`、`pyreadline3 3.5.4` | `sys_platform != 'win32'` 分支不参与 linux 解析 |
| marker 排除（Python ≥3.13 回填） | 3 | `audioop-lts 0.2.2`、`standard-aifc 3.13.0`、`standard-chunk 3.13.0` | `requires-python >= 3.13` 分支；镜像 Python 3.10.18 |
| 项目自身（virtual） | 1 | `docreader 0.1.0`（`source = virtual`） | `uv sync` 不安装本项目本体；靠 `WorkingDir=/app` + `/app/docreader` 源码目录导入，`/app/.venv` 内无 `docreader` dist 属**预期** |
| 多平台多版本分支 | 2 | `numpy 2.2.6 / 2.3.4` → 镜像 **2.2.6**；`onnxruntime 1.20.1 / 1.23.2` → 镜像 **1.20.1** | 镜像走 `python < 3.11 && linux/amd64`；两套值都合法，选错端口径会写错版本（D-15/D-16 的根因） |

→ 结论：lock 与镜像**无版本冲突**；上表 14 条 `only_in_lock` 全部可解释，**没有一条**属于「镜像装错版本」。

---

## 4. 决定性发现：v0.8.3 镜像没有 OCR 能力

三条独立证据（任一成立即可否定，此处三条同时成立）：

1. `importlib.metadata.version('rapidocr')` → `ModuleNotFoundError: No module named 'rapidocr'`。
2. 镜像内 `/app/uv.lock` sha256 `57861b67cfcbbaeee7e8ae49b8f22985b6eae4d83ffd3c0b59f761181af1bf85` 与 `git show v0.8.3:docreader/uv.lock` **逐字节一致**，该 lock 80 条中**无 `rapidocr`**。
3. 全盘 `find / -xdev \( -name '*.onnx' -o -iname '*rapidocr*' \) -type f` 命中 4 个文件，**0 个路径含 `rapidocr`**：3 个是 onnxruntime 自带测试样例（670/130/103 B），1 个是 `magika/models/standard_v3_3/model.onnx`（3,163,737 B，sha256 `fe2d2eb4…9ec8c`）。

**因此原文三处判断需要限域修正**：

| 原文位置 | 原文 | 修正 |
|---|---|---|
| §4.5 L203-204 | 权重归 Baidu/PaddleOCR，Apache-2.0 分发；`licenses/rapidocr-MODEL_LICENSES.md` 在仓内 | 事实成立（**对基线/对 HEAD 成立**），但**该文件不在镜像内**（`/app/docreader/licenses` 不存在），且镜像内没有这些权重 |
| §4.5 L206 | 权重具体文件/大小/校验和 `UNKNOWN` | **改为空集（`NOT_PRESENT`）**：不是「没查到」，是「不存在」 |
| §6 PDF 行「主干可复用；OCR 依赖 `PreprocessImage` + RapidOCR 权重」 | 暗示该镜像可 OCR | **v0.8.3 镜像不能 OCR**。`PreprocessImage` RPC 的实现若依赖 rapidocr，则其调用必然失败；OCR 能力在 `v0.8.3` 之后 202 个提交内才引入，不能从 `662abaf9f` 反推镜像行为 |
| §7 D-09「OCR 权重清单」 | 许可缺口 | 该项**从许可问题降级**：镜像内无权重，故无第三方法律风险；转为**能力缺口**（T11 若需 OCR，必须换镜像 tag 或自建） |

`licenses/rapidocr-MODEL_LICENSES.md §6` 给出的三个期望 SHA-256（`PP-OCRv6_det_small.onnx` = `090f04ab…ff94f`、`PP-OCRv6_rec_small.onnx` = `6f327246…14884`、`ch_ppocr_mobile_v2.0_cls_mobile.onnx` = `e47acedf…6215c`）**在本镜像内无法校验**，因为没有这些文件。保留在原文作为「基线仓内的期望值」，但不得引用为本镜像的权重校验和。

---

## 5. 限域声明：镜像 ≠ 审计基线（202 个提交）

| 项 | 值 | 取法 |
|---|---|---|
| 审计基线 | `662abaf9f944e37689d04c42160034b2cdec6829` | `references/WeKnora` HEAD |
| 基线描述 | `v0.8.3-202-g662abaf9f` | `git describe --tags` |
| 距 v0.8.3 的提交数 | **202** | `git rev-list --count v0.8.3..HEAD` |
| `v0.8.3` 指向 | `3e22822c1d2397be730c458c7539d0eb8c113a24`（2026-10-14 15:20:30，`chore(release): finalize v0.8.3 website and documentation (#4285)`） | `git rev-list -n1 v0.8.3` |
| 镜像 revision label | `3e22822c1d2397be730c458c7539d0eb8c113a24` | 镜像 config |
| 镜像构建时间 | `2026-10-14T07:57:12Z`（+08:00 为 15:57），晚于 tag commit 约 37 分钟 | config |

**限域声明（必须与任何引用本文件的结论一起复述）**：

1. 本文件与 `docreader_sbom.md` 的**全部实测结论只对 amd64 的 `v0.8.3` 镜像成立**。
2. **禁止**把「镜像 = 审计基线」当成立：镜像比基线**早 202 个提交**。原文 §4.3 的 26 条依赖、§4.5 的 OCR 权重、§6 的 `PreprocessImage`/`stream.py`/`image_preprocess.py` 行为，都是 **HEAD 口径**；镜像里的 `main.py`/`parser/*` 没有这些改动。
3. **禁止**反向推断：不能从镜像「无 rapidocr」推断基线「无 OCR」，也不能从基线「有 rapidocr」推断镜像「能 OCR」。二者是不同代码。
4. `docker/Dockerfile.docreader` 在 `v0.8.3..HEAD` 之间**未改动**（`git diff --name-status` 无该文件），故原文 §4.2/§4.4 的构建步骤与 apt 清单**对镜像仍然成立** —— 这是少数可以跨基线引用的部分，且已由镜像 history 的 `apt-get install …` 行复核。
5. `v0.8.3..HEAD` 在 `docreader/ docker/Dockerfile.docreader licenses THIRD_PARTY_NOTICES.md` 范围内共 **60 个改动**，其中 `licenses/` 新增 10 个文件（numpy/onnxruntime/opencv×2/rapidocr×2 等）、`THIRD_PARTY_NOTICES.md` 从 67 行扩写 —— **这些许可材料全部晚于镜像**，不能在 SBOM 里当镜像的许可出处引用（本文件只在标注「基线/HEAD 口径」时提及）。

---

## 6. 差异清单（编号从 D-13 起；原文已用至 D-12）

| ID | 契约/事实项 | 原文（manifest）口径 | 镜像实测（本 amendment） | 差距性质 | 处置 |
|---|---|---|---|---|---|
| **D-13** | 直接依赖条数 | 26 条（基线 `662abaf9f`） | **22 条**（v0.8.3，`/app/pyproject.toml` 与 `git show v0.8.3:` 一致） | 基线 vs 镜像的版本错位 | 引用 §4.3 时必须注明口径；SBOM 以 22 条为根，再展开 lock 闭包 |
| **D-14** | OCR 能力与权重 | 假定有 RapidOCR 与权重（`UNKNOWN` 待查） | **无 rapidocr、无 OCR 权重**（三证据，§4） | 能力缺口 | T11 前决定：换 ≥ v0.8.3+202 的镜像，或自建含 rapidocr 的镜像；不得在 v0.8.3 上宣称 OCR |
| **D-15** | `numpy` 版本 | `THIRD_PARTY_NOTICES.md` 写 2.3.4 | 镜像 **2.2.6**（`python<3.11 && linux/amd64` 分支） | 通知文件写错平台分支 | 原文不得直接抄 2.3.4；SBOM 用 2.2.6 + 文件 sha256 `c002bd26de7d` |
| **D-16** | `onnxruntime` 版本与定性 | 通知文件写 1.23.2，且暗示为 OCR 直接依赖 | 镜像 **1.20.1**，由 `magika 0.6.3`（`markitdown` 传递依赖）引入 | 同上 + 定性错误 | 同上；并在 SBOM 里标 `transitive via magika` |
| **D-17** | lock ↔ 镜像一致性 | 原文未做（无镜像） | lock 80 条/78 名、镜像 72 dist、交集 64、**版本不匹配 0**；14 只在 lock（8 为命名差异、6 为 marker 排除）、8 只在镜像（同 8 个命名差异） | 新增证据（正向） | 作为可复现性 A 级证据引用；`docreader 0.1.0` 未安装本体属预期（`source = virtual`） |
| **D-18** | apt 许可覆盖 | §4.4 结论「均无记录 → UNKNOWN」 | 971 包，**744 有 `License:` 字段 / 227 无**，650 个不同短名；copyright 文件 971/971 存在 | 从「全不可知」细化为「大部分可引用 + 227 待判」 | 引用 §4.4 时改为「744 可引用 Debian 短名，227 unknown」；227 名单与阻断见 `docreader_sbom.md §7 U-2` |
| **D-19** | `grpc_health_probe` | §4.4 只记「另装 v0.4.24」 | 存在（11,457,876 B，commit `645566fc…df7`），**镜像内无许可文本** | 新增 unknown | 回上游 tag 取 LICENSE；SBOM `U-4` |
| **D-20** | Playwright WebKit 浏览器包 | §4.4 只记「`playwright install webkit` + `install-deps`」 | `/root/.cache/ms-playwright/webkit-2203`，**273 MB**，**缓存内 0 个 LICENSE/COPYING/NOTICE** | 未点名的第三方二进制大件（273 MB，约占总解压体积的 4.8%） | 计入 SBOM；许可回上游；`install webkit` 未 pin 版本 → 同时是漂移源 |
| **D-21** | 镜像内许可材料 | 原文认为 `licenses/` 在仓内即可代表 | **`/app/docreader/licenses` 不存在**；v0.8.3 仓内 `licenses/` 仅 5 个文件（OpenCC/Wails/go-m1cpu/go-sql-driver/sources.tsv），**不含任何 pip 组件**；`THIRD_PARTY_NOTICES.md` @ v0.8.3 仅 67 行、零提及 rapidocr/onnxruntime/opencv/numpy | 合规材料缺口 | 合规材料须外置（即本 SBOM + amendment）；不能依赖镜像自带 |
| **D-22** | 镜像固定方式 | `docker-compose.yml:421` 用 `${WEKNORA_VERSION:-latest}` | digest 已取得（§2），可写成 `@sha256:9af668ac…5266` | 配置缺口（非镜像缺陷） | T10 前把 compose 改为 digest 固定；本 amendment 记录 digest 供引用 |

---

## 7. 取代原文 §4.6「可复现性缺口」四行

| 缺口 | 原文现状 | 实测更新 | 建议 |
|---|---|---|---|
| docreader 镜像未按 digest 固定 | `${WEKNORA_VERSION:-latest}` | **digest 已取得**：`sha256:9af668ac…5266`（index）/ `ee308c6d…19fa`（amd64 manifest） | compose 改 `@sha256:9af668ac…5266`；在此之前禁止用 `latest` |
| 基础镜像未固定 | `python:3.10.18-bookworm` 无 digest | **仍无 digest**（config `Env.PYTHON_SHA256=ae665bc6…817f` 只固定了 Python 源码包，不固定基础层） | 记为 `U-6`；应用层已可字节复现，基础层不可 |
| apt 包未固定版本 | Dockerfile 无版本号 | **仍无版本号**，但现在可枚举 **971 个精确版本**（`docreader_image_inventory.json` 的 `container.apt.packages`） | 以本盘点结果为冻结清单；换基础镜像时用它做 diff |
| protoc 版本与生成物 | protoc 3.19.4 + protobuf≥6.33.5 并存，生成物版本未标注 | **本轮未涉及**（不属镜像盘点范围） | 维持原文结论 |

新增两行（原文四行之外的缺口）：

| 缺口 | 实测 | 建议 |
|---|---|---|
| pip 依赖集合由 lock 决定，但 lock 文件本身未校验 | `/app/uv.lock` sha256 `57861b67…1f85`，与 `v0.8.3` 逐字节一致 | 把该 sha256 写进 T10 的镜像准入清单；换 tag 即重算 |
| Playwright webkit 未 pin 版本、下载物无校验清单 | 镜像内 `webkit-2203`，273 MB，无许可文件 | 记录 revision 号；如需复现，锁 `PLAYWRIGHT_BROWSERS_PATH` 与版本 |

---

## 8. 取代原文 §9「未确认项」表

| 原文项 | 原结论 | 本 amendment 后 | 证据 |
|---|---|---|---|
| docreader 镜像 digest | `UNKNOWN` | **CLOSED** → `sha256:9af668ac0ac857b9dfe8430d0fe77c0e5ad2636dc2b5285cd5500a13d5a35266` | §2 三重校验 + `RepoDigests` |
| 22 个非 OCR Python 依赖的许可 | `UNKNOWN` | **PARTIAL** → 72 个 dist 中 61 判定 / 11 unknown | `docreader_sbom.md §3` |
| apt 包（libreoffice/openjdk-17/antiword 等）许可 | `UNKNOWN` | **PARTIAL** → 744/971 有 `License:` 字段；227 unknown（含 `antiword`） | `docreader_sbom.md §4` |
| OCR 权重文件名/大小/校验和 | `UNKNOWN` | **CLOSED（否定）** → 空集，镜像内无 OCR 权重 | §4 |
| 镜像实际体积/内存占用 | `UNKNOWN` | **体积 CLOSED** → 5.67 GB 解压 / 1503.6 MiB 压缩；**内存仍 UNKNOWN**（未起服务） | §2.1 |
| `PreprocessImage` 超时/超内存后的确切返回 | `UNKNOWN` | **仍 UNKNOWN**（本轮未触发该 RPC；且 v0.8.3 无 OCR 依赖，行为只会更早失败） | 原文 `config.py:173-179` |
| `docreader/README.md` 的 MINIO 段落是否仍有效 | 已判定失效（A） | **不变** | 原文 |
| 发送期异常在 Python gRPC 下的确切 status 码 | `UNKNOWN` | **仍 UNKNOWN**（需真实运行，属 T11） | 原文 |

---

## 9. 失败记录（完整，供复盘）

| # | 命令 / 动作 | 退出码 | 真实错误 | 处置 |
|---:|---|---:|---|---|
| F-1 | `docker manifest inspect wechatopenai/weknora-docreader:v0.8.3` | **1** | `failed to configure transport: error pinging v2 registry: Get "https://registry-1.docker.io/v2/": net/http: request canceled while waiting for connection (Client.Timeout exceeded while awaiting headers)` | 放弃该命令，改从 mirror 直读 manifest |
| F-2 | 直连 `registry-1.docker.io` | — | 同上（超时），`pypi.org` 同样不通 | 全程走 mirror |
| F-3 | `docker.1ms.run` 拉 blob（实测 `Range: bytes=0-12582911`） | — | HTTP **401**，172 B 错误体 | 该站只放行 manifest/tag，不放 blob；解释第二次 pull 停滞的直接原因 |
| F-4 | `docker.xuanyuan.me` 拉同一 blob | — | HTTP **403**，296 B | 同上 |
| F-5 | `docker.m.daocloud.io` 拉同一 blob | — | HTTP **401**，73 B | 同上 |
| F-6 | 第 1 次 pull（默认 mirror 顺序） | 未完成 | 16/18 层 `Download complete`、12 层 `Pull complete` 后约 20 min 无输出 | kill，重试（层缓存保留） |
| F-7 | 第 2 次 pull（`docker.1ms.run/…`） | 未完成 | 11:41:43 后无输出，WLAN Rx 0.6 MB/20 s ≈ 0.03 MB/s | kill，回退默认 mirror（1panel）成功 |
| F-8 | `python3 -m pip list --format=json`（镜像内） | **1** | `/app/.venv/bin/python3: No module named pip` | 改用 `importlib.metadata` 枚举 72 个 dist |
| F-9 | 清华 `/pypi/<name>/json` 取许可元数据 | 200（不可用） | HTTP 200 但 `name`/`version`/`license` 字段为空 | 放弃外部索引，全部许可判定只从镜像内取 |
| F-10 | `audit_docreader_image.ps1` 初版 | 非 0 | `The property 'VirtualSize' cannot be found on this object`（Docker 29 已移除该字段） | 改 `image.Size` 并记录 `size_bytes_source` |
| F-11 | 同上 | 非 0 | `The property 'variant' cannot be found on this object`（amd64 entry 无 `variant`） | 改用 `PSObject.Properties` 探测 |
| F-12 | 同上（dpkg 解析） | 0 但 `apt.count=0` | Python 侧 `line.split("\\t")` 是字面「反斜杠+t」两字符，匹配不到真实制表符 | 改 `line.split("\t")`，得 971 行 |

> 诊断备忘：`docker pull` 进行中，`docker images` / `docker system df` **都不反映进度**（层在 content store 未提交前不计入 image store），因此「`docker system df` 数字不变」不能用来判断停滞。有效手段是 `Get-NetAdapterStatistics` 的 Rx 增量与 `%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx` 大小增量。

---

## 10. 对 T10/T11 的输入修订（承接原文 §10）

1. **T10 可直接引用（A 级，本文件）**：镜像 digest 与三层校验、18 层与体积、`/app/uv.lock` 与 `/app/pyproject.toml` 的 sha256、pip/apt 组件清单与版本、72 dist 的 `License`/`License-Expression` 原文值。
2. **T10 仍需补齐**：11 个 pip 许可 unknown、227 个 apt 许可 unknown、WebKit 与 `grpc_health_probe` 许可文本、基础镜像 digest。
3. **T11 前置结论变更**：**不要**按 v0.8.3 镜像设计 OCR 用例（镜像内无 rapidocr）。若 OCR 是 T11 验收项，必须先决定换镜像 tag 或自建镜像，否则该项应记 `BLOCKED(镜像能力缺口)` 而不是「待验证」。
4. **仍未闭合（原文有效）**：`PreprocessImage` 超时/超内存返回、发送期异常的真实 gRPC status 码、内存占用。
5. 本文件**不构成**「许可已审计完成」或「DocReader 可生产使用」的结论；限域见 §5。
