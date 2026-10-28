# DocReader 镜像 SBOM（旁挂增补，不改原文）

| 项 | 值 |
|---|---|
| 任务 | T09R（P0-T09 的 SBOM 增补项） |
| 状态 | **PASS（限域）** —— 镜像可固定、apt/pip 组件可枚举、许可可分级；11 个 pip 组件与 227 个 apt 包许可未闭合，**不得**声称许可已审计完毕 |
| 取代关系 | 本文件取代 `docs/poc/evidence/docreader_manifest.md` 的 §4.3 / §4.4 许可行 / §4.5 / §9 相关行；取代声明见 `docreader_manifest_amendment-01.md`。原文不改 |
| 数据来源 | 镜像自身（`docker run --rm --entrypoint sh` 只读盘点），**不是** PyPI JSON、不是网页检索、不是 `uv.lock` 推断 |
| 原始数据 | `docs/poc/evidence/docreader_image_inventory.json`（1,754,577 B，sha256 `3c0d993a8fb4284ddb15f9826c58e35afc37695162bc27ccc38d67178772bb01`） |
| 生成脚本 | `experiments/phase0/audit_docreader_image.ps1`（sha256 `574c790998a88434f72beaef7f5180123713cdad6a4841c9c972a49f4d83c9d6`；JSON 内 `host.script_sha256` 自记录一致） |
| 生成时间 | `2026-10-28T04:03:41.576Z`（+08:00 为 12:03:41）；容器内盘点耗时 7.542 s |

> **一句话**：镜像 `wechatopenai/weknora-docreader:v0.8.3` 已按 digest 固定；内含 **apt 971 包 / pip 72 个 distribution / onnx 权重 4 个（全部非 OCR）**；pip 许可 61 个可判定、**11 个仍 unknown**；apt 中 **744/971** 有 Debian machine-readable `License:` 字段，**227 个没有**；**镜像内不存在 RapidOCR 与任何 OCR 权重**。

---

## 1. 组件来源与身份（provenance）

| 项 | 值 | 证据 |
|---|---|---|
| 镜像引用 | `wechatopenai/weknora-docreader:v0.8.3` | `docker pull` 输出 |
| index digest | `sha256:9af668ac0ac857b9dfe8430d0fe77c0e5ad2636dc2b5285cd5500a13d5a35266` | `RepoDigests` + 对 registry manifest 原始字节本地重算 |
| amd64 manifest | `sha256:ee308c6d69f278619794bd5f42af1d78624efefb57dce345e5d00d12798b19fa` | 本地重算 = `Docker-Content-Digest` |
| config blob | `sha256:2e53ece9f2a1caa9acef4b54e70cc9f079ef0c2f3103abca9f372e44a6f67df0` | 同上 |
| 镜像 Config ID | 同 index digest | `docker image inspect .Id` |
| 层数 / 压缩后大小 | 18 层 / 1,576,643,756 B（≈1503.6 MiB）；`docker images` 显示 5.67 GB（解压后） | `docker image inspect .Size` |
| created | `2026-10-14T07:57:12.189503576Z` | config |
| 架构 | `linux/amd64` | config |
| `org.opencontainers.image.revision` | `3e22822c1d2397be730c458c7539d0eb8c113a24`（= `git rev-list -n1 v0.8.3`） | config `Labels` |
| `org.opencontainers.image.version` | `v0.8.3` | 同上 |
| `org.opencontainers.image.licenses` | **`NOASSERTION`** | 同上 —— 镜像**没有**声明汇总许可 |
| 基础镜像 | `python:3.10.18-bookworm`（**未固定 digest**） | history: `# debian.sh --arch 'amd64' out/ 'bookworm'` |
| 现场 Python | `3.10.18`（`/app/.venv/bin/python3`） | 容器内 `sys.version` |

**可复现性硬证据（本文件最重要的两个 hash）**

| 镜像内文件 | sha256 | 与源码基线的关系 |
|---|---|---|
| `/app/uv.lock`（401,710 B） | `57861b67cfcbbaeee7e8ae49b8f22985b6eae4d83ffd3c0b59f761181af1bf85` | **与 `git show v0.8.3:docreader/uv.lock` 逐字节一致** |
| `/app/pyproject.toml`（730 B） | `757a432a66e826fbbc45f590bc41dd349cbafa2ecd1517596255a93b76d0259f` | **与 `git show v0.8.3:docreader/pyproject.toml` 逐字节一致** |

→ 镜像的 Python 依赖集合由 `v0.8.3` 那份 lock 决定，**不是**由审计基线 `662abaf9f` 的 lock 决定。二者相差 202 个提交，见 amendment §5。

---

## 2. 计数总览

| 类别 | 数量 | 许可已判定 | 许可 unknown | 说明 |
|---|---:|---:|---:|---|
| apt 二进制包（dpkg） | **971** | 744（有 `License:` 字段） | **227**（无该字段） | 650 个不同许可短名；`with_file=971` 表示 copyright 文件全部存在 |
| 其中手工安装（`apt-mark showmanual`） | 131 | 102 | **29** | 见 §4.2；29 个含 `antiword`、`libcairo2`、`libxinerama1` 等 Dockerfile 明列的运行依赖 |
| pip distribution | **72** | 61 | **11** | 由 `importlib.metadata` 取，非 `pip list` |
| Python lock 条目（`uv.lock`） | 80 条 / 78 名 | — | — | 与镜像 72 个 dist：64 共享、14 只在 lock、8 只在镜像（命名归一） |
| onnx 权重/模型文件 | **4** | 4（来源包已知） | 0 | **0 个 OCR 权重**，见 §5 |
| 非 apt 非 pip 自带二进制 | 3 类 | 0 | 3 | webkit 浏览器包、grpc_health_probe、JRE/LibreOffice 见 §6 |

**`pip list` 不可用的事实**：`/app/.venv/bin/python3 -m pip list` → exit 1，`No module named pip`（uv 创建的 venv 不含 pip）。因此 pip 清单改由 `importlib.metadata.distributions()` 枚举，**不是**从 lock 抄的版本。这一点使「镜像实际安装集」独立于「lock 声明集」，交叉核对才有意义（§3.3）。

---

## 3. pip 组件（72）

### 3.1 判定规则（可复核，不靠印象）

| 规则 | 输入 | 采用条件 | tier |
|---|---|---|---|
| **R1** | `Metadata-Version ≥ 2.4` 的 `License-Expression`（PEP 639） | 直接采用 | `A` |
| **R2** | `License:` 字段 | 字段**首个短语**在受控同义词表内且能唯一对应一个 SPDX id（`MIT`、`Apache-2.0`、`Expat`、`BSD-3-Clause`、`3-Clause BSD License`、`BSD 3-Clause License`、`The BSD 2-Clause License`、`MPL-2.0`、`PSF-2.0`/`Python Software Foundation License`、`ISC` …） | `C1` |
| **R2'** | `License:` 字段 | 字段本身是含 `AND`/`OR` 的 SPDX 形状表达式 | `C2` |
| **R3** | `Classifier: License :: OSI Approved :: …` | 分类器**唯一**指向单一 id（如 `MIT License`、`Apache Software License`、`GNU Affero General Public License v3 or later`） | `B` |
| **R4** | 以上都不满足 | 记 `unknown(<具体原因>)`，**不猜变体、不猜版本、不按包名推断** | `B0`/`D`/`E` |

**禁止事项**（本轮硬约束）：不用 PyPI JSON（实测清华镜像返回 200 但 `name/version/license` 字段为空）、不用网页检索结果冒充许可证据、不把 classifier 的泛化值（`BSD License`）自动展开成 `BSD-3-Clause`。

### 3.2 全量清单

| # | component | version | SPDX / 判定 | tier | 判定依据 | 许可文件 sha256(前12) |
|---:|---|---|---|---|---|---|
| 1 | `annotated-types` | 0.7.0 | `MIT` | B | classifier MIT | `fe1049884b1a` |
| 2 | `argcomplete` | 3.6.3 | `Apache-2.0` | B | classifier Apache | `0d542e0c8804` / NOTICE `2c889c721ec8` |
| 3 | `babel` | 2.17.0 | `BSD-3-Clause` | C1 | License 字段精确值 | `7801a84fe811` |
| 4 | `beautifulsoup4` | 4.14.2 | `MIT` | C1 | License 字段精确值 | `55b4d8d4b1e5` |
| 5 | `certifi` | 2025.10.5 | `MPL-2.0` | C1 | License 字段精确值 | `e93716da6b9c` |
| 6 | `cffi` | 2.0.0 | `MIT` | A | License-Expression | `5ba24ddc5706` |
| 7 | `chardet` | 5.2.0 | **`unknown(LGPL 版本未定)`** | B0 | 字段仅 `LGPL`；classifier 为「v2 or later」，2.0/2.1 不可区分 | `dc626520dcd5` (26,530 B) |
| 8 | `charset-normalizer` | 3.4.4 | `MIT` | C1 | License 字段精确值 | `6d0d41bfe170` |
| 9 | `click` | 8.3.0 | `BSD-3-Clause` | A | License-Expression | `9a8ad106a394` |
| 10 | `cobble` | 0.1.4 | **`unknown(BSD 变体未定且无许可文件)`** | B0 | 字段缺失、classifier 仅 `BSD License`、dist 内**无任何** LICENSE | — |
| 11 | `coloredlogs` | 15.0.1 | `MIT` | C1 | License 字段精确值 | `0bef4ffb1855` |
| 12 | `courlan` | 1.3.2 | `Apache-2.0` | C1 | License 字段精确值 | `a6cba85bc92e` |
| 13 | `cryptography` | 49.0.0 | `Apache-2.0 OR BSD-3-Clause` | A | License-Expression | `aac73b3148f6` / `3e0c7c091a94` |
| 14 | `dateparser` | 1.2.2 | **`unknown(BSD 变体未定)`** | B0 | 字段 `BSD`、classifier 泛化；`LICENSE` 正文未逐条裁定 | `b75db655bb7a` (1,468 B) |
| 15 | `defusedxml` | 0.7.1 | `PSF-2.0` | B | 字段 `PSFL` + classifier PSF | `b80ce9da8c42` |
| 16 | `docx2txt` | 0.9 | **`unknown(无许可元数据)`** | D | 无 License 字段、无 classifier，仅有 `LICENSE.txt` | `c89b2a2423d1` (1,078 B) |
| 17 | `EbookLib` | 0.20 | `AGPL-3.0-or-later` | B | classifier 明确「v3 or later」；字段为 AGPL 全名 | `57c8ff33c9c0` (34,520 B) |
| 18 | `et_xmlfile` | 2.0.0 | `MIT` | C1 | License 字段精确值 | `4ccdaaebc0f4` / `0c84bb42f5d3` |
| 19 | `flatbuffers` | 25.9.23 | `Apache-2.0` | C1 | License 字段精确值 | `cfc7749b96f6` |
| 20 | `greenlet` | 3.2.4 | `MIT AND Python-2.0` | C2 | 字段为 SPDX 形状 | `e5ff3c23c110` / `769831d6e5df` |
| 21 | `grpcio` | 1.80.0 | `Apache-2.0` | A | License-Expression | `58c86a3f2b5c` (30,191 B) |
| 22 | `grpcio-health-checking` | 1.80.0 | `Apache-2.0` | A | License-Expression | `58c86a3f2b5c` |
| 23 | `grpcio-tools` | 1.80.0 | `Apache-2.0` | A | License-Expression | 无许可文件（同源仓库） |
| 24 | `htmldate` | 1.9.4 | `Apache-2.0` | C1 | License 字段精确值 | `a6cba85bc92e` |
| 25 | `humanfriendly` | 10.0 | `MIT` | C1 | License 字段精确值 | `4ac48f411780` |
| 26 | `idna` | 3.18 | `BSD-3-Clause` | A | License-Expression | `1a9a4f0e3d47` |
| 27 | `jusText` | 3.0.2 | `BSD-2-Clause` | C1 | 字段原文 `The BSD 2-Clause License`（含条款数） | `d71c0b2234fd` |
| 28 | `lxml` | 6.1.0 | `BSD-3-Clause` | C1 | License 字段精确值 | `8fc2b5681335` / `41d49dd406aa` |
| 29 | `lxml_html_clean` | 0.4.4 | `BSD-3-Clause` | C1 | License 字段精确值 | `69edb445c133` |
| 30 | `magika` | 0.6.3 | `Apache-2.0` | C1 | License 字段精确值 | 无许可文件；携带 `model.onnx`（见 §5） |
| 31 | `mammoth` | 1.11.0 | `BSD-2-Clause` | C1 | License 字段精确值 | `6663bbd04920` |
| 32 | `markdownify` | 1.2.0 | `MIT` | B | classifier MIT | `fae8b42db142` |
| 33 | `markitdown` | 0.1.3 | `MIT` | A | License-Expression | 无许可文件 |
| 34 | `mpmath` | 1.3.0 | **`unknown(BSD 变体未定)`** | B0 | 字段 `BSD`、classifier 泛化 | `c26cae81da45` (1,537 B) |
| 35 | `numpy` | 2.2.6 | **`unknown(字段为版权声明，无许可 id)`** | B0 | 字段是 `Copyright (c) 2005-2024, NumPy Developers…` 全文；`LICENSE.txt` 无 SPDX 标识 | `c002bd26de7d` (47,755 B) |
| 36 | `onnxruntime` | 1.20.1 | `MIT` | C1 | License 字段精确值 `MIT` | 无许可文件 |
| 37 | `opendataloader-pdf` | 2.4.7 | `Apache-2.0` | A | License-Expression | 无许可文件 |
| 38 | `openpyxl` | 3.1.5 | `MIT` | C1 | License 字段精确值 | `0c84bb42f5d3` |
| 39 | `packaging` | 25.0 | `Apache-2.0` | B | classifier Apache | `0d542e0c8804` / `cad1ef5bd340` |
| 40 | `pandas` | 2.3.3 | `BSD-3-Clause` | C1 | 字段以 `BSD 3-Clause License` 起（含条款数）；classifier 泛化不采用 | `1dea2e80ed1c` (62,399 B) |
| 41 | `pdfminer.six` | 20250506 | `MIT` | A | License-Expression | `f01e5d699a33` |
| 42 | `pillow` | 12.3.0 | `MIT-CMU` | A | License-Expression | `dda12a98c197` (70,307 B) |
| 43 | `playwright` | 1.55.0 | `Apache-2.0` | A | License-Expression | `7fab1461b419` |
| 44 | `protobuf` | 6.33.6 | `BSD-3-Clause` | C1 | 字段原文 `3-Clause BSD License`（含条款数） | `6e5e117324af` |
| 45 | `pycparser` | 2.23 | `BSD-3-Clause` | C1 | License 字段精确值 | `0c846399369e` |
| 46 | `pydantic` | 2.13.4 | `MIT` | A | License-Expression | `a9e186f3ca16` |
| 47 | `pydantic_core` | 2.46.4 | `MIT` | A | License-Expression | `2afdd30d54b4` |
| 48 | `pyee` | 13.0.0 | `MIT` | C1 | License 字段精确值 | `1ff556ca206b` |
| 49 | `pypdf` | 6.14.2 | `BSD-3-Clause` | A | License-Expression | `a97ac230e5f3` |
| 50 | `pypdfium2` | 5.8.0 | **`unknown(复合声明未枚举)`** | E | 字段 `BSD-3-Clause, Apache-2.0, dependency licenses`；随附 `licenses/LICENSES/Apache-2.0.txt`（11,358 B），但项目自述「依赖另有许可」未列 | — |
| 51 | `python-dateutil` | 2.9.0.post0 | **`unknown(双许可未枚举)`** | B0 | 字段 `Dual License`；`LICENSE` 内文区分「2017-12-01 后贡献 Apache-2.0 / dateutil 本体 BSD 三条款」，选择影响义务 | `ba00f51a0d92` (2,889 B) |
| 52 | `python-docx` | 1.2.0 | `MIT` | C1 | License 字段精确值 | `7652f271e46d` |
| 53 | `python-dotenv` | 1.2.2 | `BSD-3-Clause` | C1 | License 字段精确值 | `80619b7049f0` |
| 54 | `python-pptx` | 1.0.2 | `MIT` | C1 | License 字段精确值 | `7652f271e46d` |
| 55 | `pytz` | 2025.2 | `MIT` | C1 | License 字段精确值 | `be8b1a37ebe2` |
| 56 | `regex` | 2025.11.3 | `Apache-2.0 AND CNRI-Python` | A | License-Expression | `bff55ef4cdcc` |
| 57 | `requests` | 2.34.2 | `Apache-2.0` | C1 | License 字段精确值 | `09e8a9bcec80` / NOTICE `f5110972deda` (38 B) |
| 58 | `setuptools` | 80.9.0 | `MIT` | A | License-Expression | `86da0f01aeae` |
| 59 | `six` | 1.17.0 | `MIT` | C1 | License 字段精确值 | `4375ba20e2b9` |
| 60 | `soupsieve` | 2.8.4 | `MIT` | A | License-Expression | `76a0acdb98c3` |
| 61 | `SpeechRecognition` | 3.14.3 | **`unknown(BSD 变体未定)`** | B0 | 字段 `BSD`；另带 `LICENSE-FLAC.txt`（18,092 B，`8177f9751321`）属第三方数据许可 | `4aa04a4e6f8d` (1,515 B) |
| 62 | `sympy` | 1.14.0 | **`unknown(BSD 变体未定)`** | B0 | 字段 `BSD`、classifier 泛化 | `07a5e9819f72` (7,885 B) |
| 63 | `textract` | 1.5.0 | `MIT` | C1 | License 字段精确值 | `b46ef691e1a9` |
| 64 | `tld` | 0.13.1 | `MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later` | C2 | 字段为 SPDX 形状 | `96131738ed6d` (26,434 B) |
| 65 | `trafilatura` | 2.0.0 | `Apache-2.0` | C1 | License 字段精确值 | `a6cba85bc92e` |
| 66 | `typing-inspection` | 0.4.2 | `MIT` | A | License-Expression | `804b59b25f2c` |
| 67 | `typing_extensions` | 4.15.0 | `PSF-2.0` | A | License-Expression | `3b2f81fe21d1` |
| 68 | `tzdata` | 2025.2 | `Apache-2.0` | C1 | License 字段精确值 | `33e8e5002d35` / `c71d239df917` |
| 69 | `tzlocal` | 5.3.1 | `MIT` | C1 | License 字段精确值 | `d99ab209aeb1` |
| 70 | `urllib3` | 2.7.0 | `MIT` | A | License-Expression | `130e3a64d5fd` |
| 71 | `xlrd` | 2.0.2 | **`unknown(双许可未枚举)`** | B0 | 字段 `BSD`，`LICENSE` 首句「There are two licenses associated with xlrd」 | `b5a5dbce6026` (3,771 B) |
| 72 | `xlsxwriter` | 3.2.9 | `BSD-2-Clause` | C1 | License 字段精确值 | `cf08b60a4ded` |

tier 分布：`A 21 / C1 32 / C2 2 / B 6 / B0 9 / D 1 / E 1`（合计 72）；据此 unknown = **11**（`B0 9 + D 1 + E 1`）。若把 R2/R3 允许的同义映射也收紧为 unknown（**最保守口径**，即只认 `License-Expression` 与 classifier 的精确单值），unknown 为 **15**（多出 `jusText`、`pandas`、`protobuf`、`EbookLib` —— 这 4 个字段/分类器已明确写出条款数或版本，本文件按 R2/R3 采纳）。

### 3.3 `uv.lock`(v0.8.3) ↔ 镜像实际安装集 交叉核对

| 指标 | 值 |
|---|---|
| lock 条目 / 去重名 | 80 / 78 |
| 镜像 distribution | 72 |
| 交集 | 64 |
| **版本不匹配** | **0**（`version_mismatch = []`） |

- `only_in_lock`（14）：`audioop-lts 0.2.2`、`colorama 0.4.6`、`docreader 0.1.0`(virtual)、`ebooklib 0.20`、`et-xmlfile 2.0.0`、`justext 3.0.2`、`lxml-html-clean 0.4.4`、`pdfminer-six 20250506`、`pydantic-core 2.46.4`、`pyreadline3 3.5.4`、`speechrecognition 3.14.3`、`standard-aifc 3.13.0`、`standard-chunk 3.13.0`、`typing-extensions 4.15.0`。
- 其中 **8 个只是名写不同**（`EbookLib`/`et_xmlfile`/`jusText`/`lxml_html_clean`/`pdfminer.six`/`pydantic_core`/`SpeechRecognition`/`typing_extensions`），与 `only_in_image` 一一配对。
- 其余 **6 个是平台 marker 排除**：`colorama`/`pyreadline3` 为 Windows-only；`audioop-lts`/`standard-aifc`/`standard-chunk` 是 Python ≥3.13 的回填包（镜像 Python 3.10 不装）；`docreader 0.1.0` 是项目自身（`source = virtual`，`uv sync` 不安装本体，靠 `WorkingDir=/app` 的源码目录导入）。
- **多平台多版本分支（关键，且原文写错过）**

| 包 | lock 声明的两套版本 | 镜像实际 | 解析分支 |
|---|---|---|---|
| `numpy` | `2.2.6` / `2.3.4` | **2.2.6** | `python < 3.11 && linux/amd64` |
| `onnxruntime` | `1.20.1` / `1.23.2` | **1.20.1** | 同上；且它是 `magika 0.6.3`（`markitdown` 的传递依赖）引入，**不是** docreader 的直接依赖 |

→ `THIRD_PARTY_NOTICES.md:107-143` 写的 `onnxruntime 1.23.2` / `numpy 2.3.4` 是 **darwin/aarch64 与 py≥3.11 分支**的值，**对 amd64 镜像不成立**。见 amendment `D-15`/`D-16`。

---

## 4. apt 组件（971）

### 4.1 方法

`dpkg-query -W -f='${Package}\t${Version}\t${Status}\t${Architecture}\n'` → 971 行（全部 `install ok installed`）；逐包读 `/usr/share/doc/<pkg>/copyright`（**971/971 存在**），取其中的 machine-readable `License:` 段短名（Debian copyright-format 1.0）。

- **744** 个包含 `License:` 字段 → 短名可直接引用。
- **227** 个包**无** `License:` 字段（多数是 `debian/copyright` 用自然语言描述、或只给 `Copyright:`/`Files:` 段）→ 记 `unknown`，**不**用包名/常识补全。
- 650 个不同许可短名；Debian 短名**不是** SPDX id，引用时保留原文。

### 4.2 手工安装的 131 个包：许可粗览（节选关键依赖）

| package | Debian `License:` 短名（原文节选） |
|---|---|
| `libreoffice` | `MPL-2.0, Apache-2.0, CC0-1.0, GPL-2+, CC-BY-SA-3.0, GPL-3+, LGPL-3+, Expat, MIT, GPL-2, BSD-3-clause, …` |
| `openjdk-17-jre-headless` | `GPL2 with Classpath exception, BSD-3-Clause, MIT, LGPL-2.1` |
| `imagemagick` | `ImageMagick` 系列 17 个自定义短名 + `GPL-2+`、`LGPL-3+`、`GNU-All-Permissive-License` |
| `git` | `GPL-2, BSD-3-clause, Zlib, LGPL-2.1+, EDL-1.0, GPL-2+, Expat, ISC, Boost, Apache-2.0, …` |
| `gnupg` | `GPL-3+, permissive, LGPL-2.1+, Expat, LGPL-3+, CC0-1.0, BSD-3-clause, …` |
| `curl`（及 `libcurl4-openssl-dev`） | `curl, OLDAP-2.8, ISC, BSD-3-clause, BSD-4-Clause-UC, FSFULLR, X11, …` |
| `fonts-noto-color-emoji` | `Apache-2.0, SIL-1.1` |
| `fonts-freefont-ttf` / `fonts-ipafont-gothic` / `fonts-tlwg-loma-otf` / `fonts-unifont` / `fonts-wqy-zenhei` | `GPL-3+ with Special Font Exception` / `IPA-1, GPL-3+` / `GPL-2+ with Font exception, LPPL` / `GPL-2+, GFDL-NIV` / `GPL-2 with Font embedding exception, M+ FONTS License` |
| `gstreamer1.0-plugins-bad` | `LGPL-2+, UNKNOWN, GPL-2+, MPL-1.1, BSD (2 clause), BSD (3 clause), …` —— **自身含一个 `UNKNOWN` 短名** |
| `libgl1` / `libgles2` / `libglx0` / `libopengl0` / `libegl1` | `MIT, Apache-2.0, public-domain, GPL-3+, BSD-1-clause, GPL` |
| `libxinerama1` / `libsm6` / `libx11-6` / `libcairo2` / `libfontconfig1` / `libatomic1` / `libc6-dev` / `libdrm2` / `libnotify4` / `libproxy1v5` | **无 `License:` 字段** → `unknown` |
| `antiword` | **无 `License:` 字段** → `unknown`（原文 §4.4 明列其为主要运行依赖） |
| `wget` / `patch` / `unzip` / `xvfb` / `xfonts-scalable` / `tk-dev` / `libwebp7` / `libxslt1.1` | **无 `License:` 字段** → `unknown` |

**许可分布 Top 30（按包数）**：`GPL-2+ 315`、`BSD-3-clause 202`、`LGPL-2.1+ 191`、`Expat 178`、`GPL-3+ 160`、`Apache-2.0 134`、`GPL-2 121`、`BSD-2-clause 118`、`LGPL-2+ 117`、`MIT 112`、`public-domain 104`、`LGPL-3+ 102`、`CC0-1.0 82`、`other 66`、`ISC 64`、`MPL-2.0 62`、`Artistic 56`、`GPL-1+ 52`、`LGPL-2 51`、`MPL-1.1 50`、`CC-BY-SA-3.0 50`、`Artistic or GPL-1+ 47`、`X11 40`、`LGPL-3 38`、`BSD-3-Clause 37`、`MPL-1.1 or GPL-2 or LGPL-2 36`、`GPL-3 33`、`Zlib 30`、`FSFULLR 28`、`LGPL-2.1 28`

> 说明：一个包可含多个短名，故合计远大于 971；这里统计的是「短名出现在多少个包中」。`GPL-2+` 高居首位来自 Debian 工具链与基础库（`binutils`、`gcc`、`libc6` 等），**只是镜像内分发/运行，不构成对外发布**；但一旦 PyKnora 对外分发该镜像或其衍生层，GPL 系义务需要单独评审。本文件只记录事实，不作法律结论。

### 4.3 227 个无 `License:` 字段的包

`antiword, autotools-dev, base-files, binutils, binutils-common, binutils-x86-64-linux-gnu, ca-certificates-java, cpp, cpp-12, debian-archive-keyring, emacsen-common, fontconfig, fontconfig-config, fonts-liberation, g++, g++-12, gcc, gcc-12, gcc-12-base, hunspell-en-us, krb5-multidev, libapr1, libaprutil1, libasan8, libatomic1, libavahi-client3, libavahi-common-data, libavahi-common3, libbinutils, libbsh-java, libc-bin, libc-dev-bin, libc6, libc6-dev, libcairo-gobject2, libcairo-script-interpreter2, libcairo2, libcairo2-dev, libcc1-0, libcrypt-dev, libcrypt1, libctf-nobfd0, libctf0, libdc1394-25, libdjvulibre-dev, libdjvulibre-text, libdjvulibre21, libdrm-amdgpu1, libdrm-common, libdrm-intel1, libdrm-nouveau2, libdrm-radeon1, libdrm2, libexttextcat-2.0-0, libexttextcat-data, libflite1, libflute-java, libfontconfig-dev, libfontconfig1, libfontenc1, libformula-java, libgcc-12-dev, libgcc-s1, libgcrypt20, libgfortran5, libgomp1, libgprofng0, libgssapi-krb5-2, libgssrpc4, libgupnp-igd-1.0-4, libhsqldb1.8.0-java, libhunspell-1.7-0, libice-dev, libice6, libiec61883-0, libitext-java, libitm1, libjaxp1.3-java, libk5crypto3, libkadm5clnt-mit12, libkadm5srv-mit12, libkdb5-10, libkrb5-3, libkrb5-dev, libkrb5support0, liblayout-java, liblsan0, libmhash2, libmjpegutils-2.1-0, libmpc3, libmpcdec6, libmpeg2-4, libmpeg2encpp-2.1-0, libmpfr6, libmplex2-2.1-0, libmythes-1.2-0, libnice10, libnotify4, libnuma1, libopencore-amrnb0, libopencore-amrwb0, liborc-0.4-0, libpaper-utils, libpaper1, libpciaccess0, libpentaho-reporting-flow-engine-java, libpixman-1-0, libpixman-1-dev, libproxy1v5, libpthread-stubs0-dev, libpython3-stdlib, libquadmath0, libraw1394-11, librdf0, librtmp1, libselinux1, libselinux1-dev, libsemanage-common, libsemanage2, libsensors-config, libsensors5, libserializer-java, libsidplay1v5, libsm-dev, libsm6, libspandsp2, libstdc++-12-dev, libstdc++6, libtasn1-6, libtcl8.6, libtk8.6, libtsan2, libubsan1, libusb-1.0-0, libwebp-dev, libwebp7, libwebpdemux2, libwebpmux3, libwpg-0.3-3, libx11-6, libx11-data, libx11-dev, libx11-xcb1, libxau-dev, libxau6, libxaw7, libxcb-dri2-0, libxcb-dri3-0, libxcb-glx0, libxcb-present0, libxcb-randr0, libxcb-render0, libxcb-render0-dev, libxcb-shape0, libxcb-shm0, libxcb-shm0-dev, libxcb-sync1, libxcb-xfixes0, libxcb-xkb1, libxcb1, libxcb1-dev, libxcomposite1, libxcursor1, libxdamage1, libxdmcp-dev, libxdmcp6, libxext-dev, libxext6, libxfixes3, libxfont2, libxft-dev, libxft2, libxi6, libxinerama1, libxkbcommon-x11-0, libxkbcommon0, libxkbfile1, libxmlsec1, libxmlsec1-nss, libxmu6, libxmuu1, libxpm4, libxrandr2, libxrender-dev, libxrender1, libxshmfence1, libxslt1-dev, libxslt1.1, libxss-dev, libxss1, libxt-dev, libxt6, libxtst6, libxv1, libxxf86dga1, libxxf86vm1, libzbar0, lp-solve, m4, patch, python3, python3-distutils, python3-lib2to3, python3-minimal, shared-mime-info, tcl, tcl-dev, tcl8.6, tcl8.6-dev, tk, tk-dev, tk8.6, tk8.6-dev, unzip, wget, x11-common, x11-utils, x11-xkb-utils, x11-xserver-utils, xfonts-encodings, xfonts-scalable, xfonts-utils, xkb-data, xorg-sgml-doctools, xserver-common, xtrans-dev, xvfb`（**227 条全部列出**；与 `docreader_image_inventory.json` 的 `container.copyrights.entries` 中 `license_short_names = []` 的条目一一对应）

**这些包的 copyright 文件都在**（`with_file = 971`），只是没有机器可读的 `License:` 段。替代路径：读取同目录 `copyright` 正文人工判定，或对上游 Debian source package 取 `debian/copyright` 的 SPDX 映射工具结果。**不采用**「按包名猜」。

---

## 5. 模型权重与 onnx 文件（4，全部非 OCR）

`find / -xdev \( -name '*.onnx' -o -iname '*rapidocr*' \) -type f` 命中 4 个文件，**没有任何路径含 `rapidocr`**（`any_rapidocr_path = []`）：

| 文件 | 大小 | sha256 | 归属 | 许可 |
|---|---:|---|---|---|
| `/app/.venv/lib/python3.10/site-packages/onnxruntime/datasets/logreg_iris.onnx` | 670 B | `8224784c98d73412d9fd99abcd57a38568bd590980d0fbe5916464531c52e8fc` | onnxruntime 自带测试样例 | 随 `onnxruntime 1.20.1`（MIT，见 §3.2 #36） |
| `…/onnxruntime/datasets/mul_1.onnx` | 130 B | `71f431c4e9321ec6fbeb158d02ed240459a7dcc98673fa79a4f439ce42efaf10` | 同上 | 同上 |
| `…/onnxruntime/datasets/sigmoid.onnx` | 103 B | `5340aba67a7e3475162ad794378af55f1718f55f9a5d74b4af60ecc7f7a624b6` | 同上 | 同上 |
| `…/magika/models/standard_v3_3/model.onnx` | 3,163,737 B | `fe2d2eb49c5f88a9e0a6c048e15d6ffdf86235519c2afc535044de433169ec8c` | `magika 0.6.3` 内容类型识别模型 | 随 `magika`（Apache-2.0，见 §3.2 #30）；**模型权重自身的训练数据许可未在 dist 内说明** → 见 §7 U-5 |

### 5.1 决定性否定事实：**v0.8.3 镜像不含 RapidOCR，也不含任何 OCR 权重**

三条独立证据：

1. `importlib.metadata.version('rapidocr')` → `ModuleNotFoundError: No module named 'rapidocr'`（`probes.rapidocr_version`）。
2. 容器内 `/app/uv.lock` 与 `v0.8.3` 的 `docreader/uv.lock` **逐字节一致**，而该 lock 的 80 条里 **没有 `rapidocr`**（rapidocr 是 `v0.8.3` 之后 202 个提交内才加入的依赖）。
3. 全盘 `*.onnx` 只有 4 个（上表），最大的是 magika 的 3.16 MB 内容类型模型 —— OCR 检测/识别模型是数十 MB 量级，不存在。

**对原文的影响（必须在 amendment 里显式声明）**：原文 §4.5 与 §6「PDF 行」的「OCR 依赖 `PreprocessImage` + RapidOCR 权重」**对 v0.8.3 镜像不成立**；`licenses/rapidocr-MODEL_LICENSES.md` 里那 3 个期望 SHA-256（`PP-OCRv6_det_small.onnx` 等）在镜像内**无法校验，因为没有这些文件**。详见 amendment §3 与 `D-14`。

---

## 6. 非 apt / 非 pip 的自带二进制（3 类，全部无许可文本）

| 组件 | 事实 | 许可证据 |
|---|---|---|
| Playwright WebKit 浏览器包 | `/root/.cache/ms-playwright/webkit-2203/{minibrowser-gtk,minibrowser-wpe}`，**273 MB**；`.links` 目录存在 | **缓存内 0 个 `LICENSE`/`COPYING`/`NOTICE` 文件**（`find` 返回空）→ `unknown` |
| `grpc_health_probe` | `/usr/bin/grpc_health_probe`，11,457,876 B，`v0.4.24`，commit `645566fc4b06a8d51552166a34717ce7090fddf7` | 静态二进制，镜像内无许可文本 → `unknown` |
| `openjdk 17.0.20.1` / `libreoffice` | `/usr/bin/java`（`17.0.20.1+1-1-deb12u1-Debian`）、`/usr/bin/soffice` | 均来自 apt，许可见 §4.2；全盘仅 `/usr/lib/libreoffice/NOTICE` 一处 NOTICE |

WebKit 是镜像里**体积最大的单一非 pip/非 apt 组件**（273 MB，约占总解压体积 5.67 GB 的 4.8%），许可不可从镜像内获得，必须回 Playwright 上游取。

---

## 7. 未知项登记（阻断 + 替代）

| ID | 未知项 | 原因 | 阻断影响 | 替代 / 闭合路径 |
|---|---|---|---|---|
| U-1 | 11 个 pip 组件的 SPDX | `chardet`(LGPL 2.0/2.1)、`cobble`(无文件)、`dateparser`/`mpmath`/`SpeechRecognition`/`sympy`/`numpy`(仅 `BSD`/版权文本)、`docx2txt`(无元数据)、`pypdfium2`(复合)、`python-dateutil`/`xlrd`(双许可) | 不能对外声明「依赖许可已审」；若将来分发镜像，这些包的选择会改变义务 | 读 `<dist>-info/licenses/*` 正文人工裁定（本 SBOM 已给出每个文件 sha256，可直接定位）；`cobble` 需回上游仓库取 LICENSE |
| U-2 | 227 个 apt 包许可 | Debian copyright 无 machine-readable `License:` 段 | 同上；其中含 `antiword`、`libc6`、`libcairo2`、`libx11-6` 等运行必需件 | 读 `copyright` 正文；或按 Debian source package 的 `debian/copyright` 用 `licensecheck`/`reuse` 工具跑一遍 |
| U-3 | WebKit 浏览器包许可 | 缓存内无任何许可文件 | 对外分发镜像时属未审第三方二进制（273 MB） | 回 playwright 上游 `browser_patches`/`chrome-linux` 的 LICENSE；或改为 `install-deps` 后按需下载 |
| U-4 | `grpc_health_probe` 许可 | 静态二进制、镜像内无文本 | 影响可分发性声明 | 按 `v0.4.24` tag 回 `grpc-ecosystem/grpc-health-probe` 取 LICENSE |
| U-5 | magika 权重训练数据许可 | dist 内只声明代码 Apache-2.0，未声明模型卡 | 若将模型作为产物再分发需确认 | 回 magika 仓库 model card |
| U-6 | 基础镜像 `python:3.10.18-bookworm` 的 digest | Dockerfile 未按 digest 固定 | 基础层不可字节复现（应用层已可固定） | 记录该 tag 对应 digest 并写进变更记录 |
| U-7 | `licenses/` 目录不在镜像内 | `/app/docreader/licenses` 不存在；`v0.8.3` 仓内 `licenses/` 只有 5 个文件且**不含任何 pip 组件** | 镜像不自带合规材料，SBOM 必须外置（即本文件） | 由 PyKnora 侧维护本 SBOM + 后续从 `662abaf9f` 基线补齐 `licenses/` |

---

## 8. 复现命令与产物校验

```powershell
# 前提：镜像已落地（digest 见 §1）
pwsh -NoProfile -File experiments/phase0/audit_docreader_image.ps1 `
  -NoPull -OutFile docs/poc/evidence/docreader_image_inventory.json
```

| 步骤 | 命令（要点） | 退出码 | 真实输出摘要 |
|---|---|---|---|
| 盘点运行 | 上述 ps1 | **0** | `container-audit 7.542 s / 295,129 B`，写出 1,754,219 字符 |
| 产物 sha256 | `Get-FileHash docs/poc/evidence/docreader_image_inventory.json -Algorithm SHA256` | 0 | `3C0D993A8FB4284DDB15F9826C58E35AFC37695162BC27CCC38D67178772BB01` |
| dpkg 查询 | `dpkg-query -W -f='${Package}\t${Version}\t${Status}\t${Architecture}\n'` | 0 | 971 行 |
| pip 元数据 | `importlib.metadata.distributions()` | 0 | 72 个 dist |
| `pip list`（对照，失败） | `python3 -m pip list --format=json` | **1** | `No module named pip` |
| 权重扫描 | `find / -xdev \( -name '*.onnx' -o -iname '*rapidocr*' \) -type f` | 0 | 4 个文件，0 个 rapidocr 路径 |

---

## 9. 本文件**不**证明什么

1. 不证明「许可合规」——11 个 pip 组件与 227 个 apt 包仍未闭合，WebKit/grpc_health_probe 无许可文本。
2. 不证明「镜像 = 审计基线」——镜像构建自 `v0.8.3`（`3e22822c1`），与 `662abaf9f` 相差 **202 个提交**；本 SBOM 的所有结论只对 **amd64 的 `v0.8.3` 镜像**成立。
3. 不证明「OCR 可用」——正相反：**v0.8.3 镜像没有 OCR 依赖与权重**。
4. 不证明「已实测运行」——除盘点类命令外，服务未起、gRPC 未连；`T11` 仍需真机。
5. 不替代法律意见——GPL/LGPL/AGPL 系义务只记录事实，未作结论。
