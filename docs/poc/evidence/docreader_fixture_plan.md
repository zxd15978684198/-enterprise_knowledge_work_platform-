# DocReader Fixture 计划（P0-T09 → T10/T11）

> 任务：P0-T09 交付物之二。配套文档：`docs/poc/evidence/docreader_manifest.md`（协议/入口/错误/许可事实来源）。
> 范围：**只做计划，不创建 fixture、不运行解析**。本文回答两件事：T10/T11 真跑需要哪些样本与环境输入；哪些必须真机、哪些可静态确认。
> 状态：`PARTIAL` —— fixture 清单与断言已可执行；**T11 真跑当前 BLOCKED**（镜像 digest 未固定、本机无 DocReader 运行时），阻断项见 §7。
> 证据等级：**A** = 直接读到代码/文件；**B** = 由配置或文档推断；**C** = 未能确认。
> 路径约定：`references/WeKnora/...` 为只读参考源码（commit `662abaf9f944e37689d04c42160034b2cdec6829`）；未加前缀的相对路径指 PyKnora 本仓。

## 0. 结论摘要

| 结论 | 依据 | 等级 |
|---|---|---|
| T10 可**完全静态**完成，无需任何服务或样本 | 协议/字段/错误/许可均已在 manifest 闭合 | A |
| T11 **必须真机**：`Read`/`ReadStream` 帧序、`meta.error` 与 status 组合、PDF code point 偏移、U+FFFD 替换、token/TLS 四组合 | 均为运行时行为，静态无法判定 | A |
| 仓内**可直接复用**的真实样本共 21 个数据文件（anydoc upstream，MIT，含 PDF/DOCX/XLSX/PPTX/EPUB/RTF/ODT/ODS/ODP/CSV；§2 按用途列 18 条，其余 `handmade-math.*` 变体见 §2 表下注） | `references/WeKnora/internal/infrastructure/docparser/anydoc/testdata/upstream/`（含 `LICENSE:1-3`、`README.md:1`） | A |
| 仓内 docreader 自身**只有 1 个**二进制 fixture | `references/WeKnora/docreader/tests/fixtures/issue_2634_vertical_merge.docx`（37371 B） | A |
| **TXT / MD / HTML 仓内无任何真实样本** → 必须新建 | 全仓 glob 结论；`anydoc` 支持列表无 html/md/txt（`anydoc.go:133-150`） | A |
| 5 格式中只有 PDF 有 locator 可断言；DOCX/HTML/MD/TXT 无 locator | `pdf_parser.py:1721-1788,2049-2122`；`excel_parser.py:105-168`；其余 parser 无 `source_blocks` | A |
| 大文件上限 50 MiB 是**唯一**明确的消息上限，且变量名含 `_MB` 实为字节 | `docreader/config.py:100-104`；`docreader/main.py:391-392` | A |

## 1. T11 运行前置（服务端 + 客户端）

### 1.1 启动 DocReader（三选一，均为真实命令）

| 方式 | 命令 | 出处 | 等级 |
|---|---|---|---|
| 容器（权威） | `ENTRYPOINT /app/.venv/bin/python -m docreader.container_entrypoint` + `CMD /app/.venv/bin/python -m docreader.main` | `references/WeKnora/docker/Dockerfile.docreader:168-170` | A |
| CI / 本机 venv | `uv run --project docreader --python 3.10.18 python -m docreader.main` | `references/WeKnora/.github/workflows/docreader.yml:83-95` | A |
| 依赖同步 | `uv sync --project docreader --locked --no-dev` | `references/WeKnora/.github/workflows/docreader.yml:56` | A |

健康检查（两种等价）：

- 容器：`grpc_health_probe -addr=localhost:50051`（`docker-compose.yml:544`、`docker/Dockerfile.docreader:114-136`）
- 本机：`nc -z 127.0.0.1 50051`（`.github/workflows/docreader.yml:83-95`）
- 免认证方法：`/grpc.health.v1.Health/Check`、`/grpc.health.v1.Health/Watch`（`docreader/auth.py:102-107`）

> **端口可达性坑**：compose 的 docreader 只 `expose: "50051"`、不 publish（`docker-compose.yml:430-437`）。若要在宿主机直连（PyKnora 的 T11 客户端不在 compose 网络内），**必须**临时加 `127.0.0.1:50051:50051` 端口映射，或让客户端进同一 compose 网络。不要误配 `docker-compose.yml:756` 的 `50052:50051`（那是 Weaviate）。

### 1.2 环境变量矩阵（服务端）

| 变量 | 默认 | 用途 | 出处 | 等级 |
|---|---|---|---|---|
| `DOCREADER_GRPC_PORT` / `PORT` | `50051` | 监听端口 | `docreader/config.py:105` | A |
| `DOCREADER_GRPC_MAX_WORKERS` | `4` | 线程池并发 | `docreader/config.py:99` | A |
| `DOCREADER_GRPC_MAX_FILE_SIZE_MB` / `MAX_FILE_SIZE_MB` | `50` | **消息字节上限**（值 ×1024×1024） | `docreader/config.py:100-104` | A |
| `DOCREADER_IMAGE_OUTPUT_DIR` | `/tmp/docreader` | 图片输出目录（当前 `image_dir_path` 恒空，实际不落盘） | `docreader/config.py:151-153`；`docreader/main.py:248,335` | A |
| `GRPC_AUTH_TOKEN` | 空（**= 认证关闭**） | 共享 token | `docreader/auth.py:171-181` | A |
| `GRPC_TLS_ENABLED` | `false` | 是否启用 TLS | `docreader/auth.py:47-49` | A |
| `GRPC_TLS_CERT` / `GRPC_TLS_KEY` | 空 | true 时缺失 → 拒绝启动 | `docreader/auth.py:54-58` | A |
| `GRPC_TLS_CA` / `GRPC_MTLS_REQUIRE_CLIENT_CERT` | 空 / `bool(CA)` | mTLS | `docreader/auth.py:69-72` | A |
| `DOCREADER_HTML_MAIN_CONTENT` | `false` | HTML 正文抽取开关（trafilatura） | `docreader/config.py:124` | A |
| `DOCREADER_PDF_FORCE_SCANNED` | `false` | 强制扫描路线（**不在 config.py**，import 时读） | `docreader/parser/pdf_parser.py:139` | A |
| `DOCREADER_PDF_SANITIZE_TEXT` | `true` | PDF 文本清洗（同上，不在 config.py） | `docreader/parser/pdf_parser.py:125` | A |
| `SSRF_WHITELIST` / `SSRF_WHITELIST_EXTRA` / `SSRF_DNS_WHITELIST_ONLY` | 空 | URL 模式白名单 | `docreader/utils/ssrf.py:106`；`docker-compose.yml:446-449` | A |

> **配置二源警告**：`DOCREADER_PDF_*`、`DOCREADER_XLSX_*` 等由模块 import 期直接读 `os.environ`（`pdf_parser.py:125,139`、`xlsx_merge.py:25`），**不出现在 `print_config()`**（`docreader/config.py:223-227`），且改值必须**重启进程**。T11 记录环境时必须显式列出这些变量，不能只抄启动日志。

### 1.3 环境变量矩阵（客户端）

| 变量 | 默认 | 用途 | 出处 | 等级 |
|---|---|---|---|---|
| `DOCREADER_ADDR` | `docreader:50051` | 目标地址 | `docker-compose.yml:199` | A |
| `DOCREADER_TRANSPORT` | `grpc` | 传输选择 | `docker-compose.yml:200` | A |
| `GRPC_TLS_ENABLED` / `GRPC_TLS_CA` / `GRPC_TLS_SERVER_NAME` / `GRPC_TLS_CERT` / `GRPC_TLS_KEY` / `GRPC_AUTH_TOKEN` | 空/false | 客户端 TLS/token | `docreader/client/auth.go:35-44` | A |
| `MAX_FILE_SIZE_MB` | `50` | 客户端收发上限（`size*1024*1024`） | `docreader/client/client.go:14-21` | A |
| `WEKNORA_DOCREADER_CALL_TIMEOUT` | `30m` | 调用超时（Go 宿主） | `references/WeKnora/internal/config/config.go:820-827` | A |

## 2. Fixture 清单 A：仓内已有（只读复用，不复制、不改动）

来源：`references/WeKnora/internal/infrastructure/docparser/anydoc/testdata/upstream/`，来自 `firecrawl/anydoc v0.2.4` @ `42bf1c5ecdde9eb0d96d6bd75a9e6698cf93b14c`，**MIT**（`upstream/README.md:1`；`upstream/LICENSE:1-3` Copyright © 2026 Sideguide Technologies Inc.）。

| ID | 格式 | 路径（相对 `.../testdata/upstream/`） | 字节 | 建议用途 | 等级 |
|---|---|---|---|---|---|
| A-PDF-01 | PDF（文本层） | `pdf/text.pdf` | 90060 | 文本 PDF 主样本；页码 locator 断言 | A |
| A-PDF-02 | PDF（扫描） | `pdf/handmade-scanned.pdf` | 1046 | 扫描/无文本层 → OCR 与 `image_count` 路径 | A |
| A-PDF-03 | PDF（混合） | `pdf/handmade-mixed.pdf` | 1067 | 文本+图混排，验证 locator 与图片并存 | A |
| A-DOCX-01 | DOCX | `docx/text.docx` | 10159 | 段落/标题基准 | A |
| A-DOCX-02 | DOCX（公式） | `docx/handmade-math.docx` | 1569 | OMML/公式，验证不崩且不虚构文本 | A |
| A-DOCX-03 | DOCX（合并单元格） | `references/WeKnora/docreader/tests/fixtures/issue_2634_vertical_merge.docx` | 37371 | **docreader 自身**回归样本（非 anydoc 目录） | A |
| A-DOC-01 | DOC | `doc/text.doc` | 20480 | legacy .doc（走 `DocParser`/antiword） | A |
| A-XLSX-01 | XLSX | `xlsx/sheet.xlsx` | 7106 | Excel `source_blocks`（sheet/row_start）断言 | A |
| A-XLS-01 | XLS | `xls/sheet.xls` | 7168 | legacy Excel | A |
| A-XLSB-01 | XLSB | `xlsb/handmade-sheet.xlsb` | 1602 | 边界格式（P0 不纳入） | A |
| A-PPTX-01 | PPTX | `pptx/pres.pptx` | 15734 | P0 不纳入，仅防回归 | A |
| A-PPT-01 | PPT | `ppt/pres.ppt` | 464384 | 同上 | A |
| A-EPUB-01 | EPUB | `epub/book.epub` | 6167 | 同上 | A |
| A-RTF-01 | RTF | `rtf/text.rtf` | 20312 | 同上 | A |
| A-ODT-01 | ODT | `odt/text.odt` | 25489 | 同上 | A |
| A-ODS-01 | ODS | `ods/sheet.ods` | 16142 | 同上 | A |
| A-ODP-01 | ODP | `odp/pres.odp` | 19065 | 同上 | A |
| A-CSV-01 | CSV | `csv/sheet.csv` | 218 | P0 由 Go simple 路径处理，非 DocReader | A |

> 补充（未单列的 4 个 `handmade-math.*` 变体，合计后 upstream 数据文件为 21 个）：`epub/handmade-math.epub`（1350 B）、`odt/handmade-math.odt`（1494 B）、`pptx/handmade-math.pptx`（2681 B）、`rtf/handmade-math.rtf`（959 B）。这些是「最小公式样本」，用于验证解析器不因公式/嵌入对象崩溃，P0 不纳入验收。
>
> 使用约束：这些文件位于只读 submodule，**只能读取**。若 T11 需要「文件在本地磁盘」的形态，从 PyKnora 侧以只读方式引用绝对路径，或复制到 `experiments/phase0/fixtures/`（见 §6 归属约定）。复制必须同时带 `LICENSE` 与 `README.md` 以保留 MIT 归属。

## 3. Fixture 清单 B：需新建（T10 期间生成，不联网）

生成原则：**只用 Python 标准库 + 纯文本/zip 结构**，不引入新依赖（当前 PyKnora 根项目依赖为空）。PDF 可用仓内已验证的手工构造法（`references/WeKnora/docreader/tests/test_pdf_embedded_images.py:29` 的 `_pdf_from_objects(objects: list) -> bytes`）复刻；DOCX 用 `zipfile` 手工组装最小 OOXML。

| ID | 文件名 | 格式 | 目标大小 | 生成方式 | 验证目标 | 需真实服务 | 等级 |
|---|---|---|---|---|---|---|---|
| B-TXT-01 | `utf8-basic.txt` | TXT | ≤ 2 KiB | 纯文本，含 CJK、emoji、astral（U+1F600）、换行混用 | PyKnora 严格 UTF-8 通过；**DocReader 拒收**（`registry.py:105-114`） | 否（PyKnora 本地） | A |
| B-TXT-02 | `invalid-utf8.txt` | TXT | ≤ 1 KiB | 写入 GBK 字节序列（如 `0xD6 0xD0`） | PyKnora 必须**拒绝**而非替换；对比 DocReader 的 U+FFFD 行为（`main.py:39-43`） | 否 | A |
| B-TXT-03 | `lone-surrogate.txt` | TXT | ≤ 256 B | 含非法代理项字节 `0xED 0xA0 0x80` | 记录「参考实现静默替换」事实 | 否 | A |
| B-MD-01 | `basic.md` | MD | ≤ 4 KiB | 标题 1–6 级、列表、代码围栏、表格 | 原文保真（参考实现直接透传，`builtin_converter.go:60-61`） | 否 | A |
| B-MD-02 | `with-relative-image.md` | MD | ≤ 4 KiB | 引用 `./img/one.png` | 相对图片链接在无 DocReader 时**不会**被解析成 `ImageRef`（缺口证据） | 否 | A |
| B-HTML-01 | `article-with-noise.html` | HTML | ≤ 8 KiB | 手写：`<nav>`/`<footer>`/广告 div + 正文 `<article>` | `DOCREADER_HTML_MAIN_CONTENT=false`（默认）vs `true` 的正文差异（`docreader/config.py:124`；`html_parser.py` trafilatura 分支） | **是** | A |
| B-HTML-02 | `malformed.html` | HTML | ≤ 2 KiB | 未闭合标签、重复 `<html>` | lxml 容错不崩 | **是** | A |
| B-PDF-01 | `unicode-cjk-1p.pdf` | PDF | ≤ 20 KiB | 手工构造单页，正文含 CJK + emoji + 组合字符 | **code point 偏移**断言：`SourceBlock.start/end` 与 Python 字符串切片一致（`docreader.proto:78-79`） | **是** | A |
| B-PDF-02 | `scanned-1p.pdf` | PDF | ≤ 200 KiB | 单页仅内嵌 JPEG、无文本层 | 扫描路线 → OCR → `image_count` 与 `ImageRef` 帧 | **是** | A |
| B-PDF-03 | `many-images-8p.pdf` | PDF | ≤ 1 MiB | 8 页，每页一张图 | `ReadStream` 多帧（1 meta + ≥8 image）；**帧数 = 图片数** 断言（`main.py:344-354`） | **是** | A |
| B-PDF-04 | `two-col-1p.pdf` | PDF | ≤ 20 KiB | 双栏排版 | 阅读顺序（`DOCREADER_PDF_LAYOUT_ORDERING` 相关，`docker-compose.yml:527`） | **是** | A |
| B-DOCX-01 | `headings-table.docx` | DOCX | ≤ 20 KiB | `zipfile` 组最小 OOXML：H1/H2、段落、2×3 表格 | DOCX 走 DocReader `Docx2Parser` 时**无 locator**（缺口证据） | **是** | A |
| B-BIN-01 | `empty.pdf` | PDF | 0 B | 空文件 | 空输入错误形态（`main.py:233-236,321-326`） | **是** | A |
| B-BIN-02 | `corrupted.pdf` | PDF | ≤ 1 KiB | 正确 `%PDF-1.7` 头 + 随机字节 | 解析异常 → `error=str(e)`（`main.py:257-261`） | **是** | A |
| B-BIN-03 | `oversize-51mb.pdf` | PDF | **51 MiB** | 脚本生成（填充合法 PDF 注释流），**不入库** | 触发 `RESOURCE_EXHAUSTED`（上限 50 MiB，`config.py:100-104`） | **是** | A |

> `B-BIN-03` 必须由脚本按需生成并加入忽略规则，禁止提交 51 MiB 二进制（对齐 `AGENTS.md` 的「不提交大体积产物」精神）。

## 4. T11 用例 → fixture → 断言映射

| 用例 | RPC | fixture | 断言（必须逐条留证） | 需真实服务 | 等级 |
|---|---|---|---|---|---|
| C-01 基础 unary | `Read` | A-PDF-01 | status=OK；`error==""`；`markdown_content` 非空；`image_dir_path==""` | 是 | A |
| C-02 流式帧序 | `ReadStream` | A-PDF-01 | 第 1 帧必为 `meta`（`main.py:332`）；之后每帧 `image`；末帧后 EOF | 是 | A |
| C-03 多图流 | `ReadStream` | B-PDF-03 | 收到图片帧数 ≥ `meta.image_count`（>0 时）；不得缺帧 | 是 | A |
| C-04 扫描 OCR | `ReadStream` | A-PDF-02 / B-PDF-02 | `meta.image_count > 0`；markdown 含 OCR 文本或明确为空 | 是 | A |
| C-05 解析失败语义 | `Read` | B-BIN-02 / B-BIN-01 | **status=OK 且 `error!=""`**（关键：不是 gRPC 错误） | 是 | A |
| C-06 流式失败语义 | `ReadStream` | B-BIN-02 | 首帧即 `meta.error!=""` 后流结束（`main.py:308-315`） | 是 | A |
| C-07 Unicode 偏移 | `Read` | B-PDF-01 | `markdown_content[start:end]` 与 `locator` 语义一致（**code point，非 UTF-16**） | 是 | A |
| C-08 locator 缺失 | `Read` | B-DOCX-01 / B-HTML-01 | `source_blocks` 为空 → 证明 DOCX/HTML 无 locator | 是 | A |
| C-09 未知引擎 | `Read` | A-PDF-01 + `parser_engine="nope"` | 返回 `error` 含 `Unsupported file type` 或回退到 builtin（**必须实测，静态只知两条分支**） | 是 | A（分支）/C（实际走向） |
| C-10 不支持类型 | `Read` | B-TXT-01 + `file_type="txt"` | `error` 含 `Unsupported file type: txt`（`registry.py:114`） | 是 | A |
| C-11 无 token | `Read` | A-PDF-01，`GRPC_AUTH_TOKEN` 未设 | 调用成功（认证整体关闭，`auth.py:180-181`） | 是 | A |
| C-12 有 token 正确 | `Read` | A-PDF-01，服务端+客户端同 token | 成功；metadata `authorization: Bearer <token>` | 是 | A |
| C-13 有 token 缺失/错误 | `Read` | A-PDF-01，仅服务端设 token | `UNAUTHENTICATED`（`auth.py:117-121`）；并记录 Go 侧误分类为 `DOCREADER_PARSE_FAILED`（`docreader_failure.go:16-29`） | 是 | A |
| C-14 TLS 启用 | `Read` | 自签证书 + A-PDF-01 | `add_secure_port`（`main.py:410`）；明文客户端被拒 | 是 | A |
| C-15 TLS 配置缺失 | 启动 | — | 进程 `exit(1)` + `Refusing to start`（`main.py:403-407`） | 是 | A |
| C-16 超限消息 | `Read` | B-BIN-03 | `RESOURCE_EXHAUSTED` | 是 | A |
| C-17 健康检查免认证 | `grpc.health.v1.Health/Check` | — | 无 token 也返回 SERVING | 是 | A |
| C-18 ListEngines | `ListEngines` | — | 返回引擎名集合，至少含 `builtin`/`markitdown`/`opendataloader`（`registry.py:159-206`） | 是 | A |
| C-19 PreprocessImage | `PreprocessImage` | B-PDF-02 内嵌 JPEG | 不可纠正时 `fallback_needed=true` 且 `image` 为空；`status` 实测取值超出 proto 注释的 3 种（`preprocess_worker.py:49-111`、`preprocess.py:251-255`），**断言只依赖 `fallback_needed`，不依赖 `status` 白名单** | 是 | A |
| C-20 不完整流 | `ReadStream` | B-PDF-03 + 客户端首帧后主动断开 | 服务端 `finally: parsed_stream.close()` 生效，无泄漏（`main.py:358-359`） | 是 | B（需资源观测） |

## 5. 真机 vs 静态确认

| 项 | 静态可确认 | 必须真机 | 依据 |
|---|---|---|---|
| proto 字段/编号/服务名/4 RPC | ✅ | — | `docreader.proto`（sha256 见 manifest §1.1） |
| 端口/传输/启动命令/镜像内容 | ✅ | — | `config.py:105`、`Dockerfile.docreader:164-170` |
| 错误码映射（Go 侧业务码） | ✅ | — | `docreader_failure.go:16-29` |
| 许可与 SBOM 缺口 | ✅（除 21 依赖/apt/权重） | 权重文件清单需运行时/联网 | manifest §4.3/§9 |
| 五格式路由 | ✅ | — | `engine_registry.go:69-79`、`registry.py:159-179`、`anydoc.go:133-150` |
| 帧序与帧数 | — | ✅ | `main.py:332-357` 只给结构，实测才能验证 |
| `meta.error` 与 gRPC status 的真实组合 | — | ✅ | 发送期异常码为推断（manifest §5.1） |
| code point 偏移正确性 | — | ✅ | `docreader.proto:78-79` 只声明语义 |
| U+FFFD 静默替换 | 半（代码可读） | ✅（需真实非法输入） | `main.py:39-43` |
| 图片数量与 `image_count` 一致性 | — | ✅ | best-effort 注释（`docreader.proto:101`） |
| 内存/并发上限 | — | ✅ | 未实测 |

## 6. Fixture 落盘归属与治理

| 项 | 约定 | 说明 |
|---|---|---|
| 建议目录 | `experiments/phase0/fixtures/docreader/` | **该目录当前归 `w1-t02-env`（P0-T02）所有**；P0-T09 不创建任何文件，T10 开工前须与 P0-T02 协调归属，避免写冲突 |
| 只读来源 | `references/WeKnora/**` | 只读 submodule，禁止复制后修改、禁止在其中生成 |
| 第三方归属 | 复制 anydoc 样本时必须同带 `LICENSE` + `README.md` | MIT 要求保留版权与许可声明（`upstream/LICENSE:10-13`） |
| 大文件 | `oversize-51mb.pdf` 由脚本生成 + 忽略规则，**不入库** | 避免仓库膨胀 |
| 敏感数据 | 全部 fixture 为**合成/公开**样本，不含真实用户文档、不含 PII | 对齐 `AGENTS.md` 的日志与数据要求 |
| 可复现性 | 每个 fixture 记录生成脚本 + sha256；生成脚本本身入库 | 保证 T11 结果可重放 |

## 7. 阻断项与替代

| ID | 阻断 | 影响 | 替代方案 | 等级 |
|---|---|---|---|---|
| BLK-01 | docreader 镜像**无 digest**（`docker-compose.yml:421` 缺省 `latest`；基础镜像 `Dockerfile.docreader:4,82` 无 digest） | T11 结果无法声明「对应某确定镜像」 | 记录 `WEKNORA_VERSION` + 构建 commit；用 `docker image inspect` 自算 digest 后回填 | A |
| BLK-02 | 本机**无 Docker / 无 uv 环境**，且任务禁跑 docker/uv | 无法启动服务，T11 全部用例 NOT_RUN | 由 Lead 在获授权后起环境；本文提供完整命令与断言 | A |
| BLK-03 | 22 个 Python 依赖 + apt 包 + OCR 权重**无许可记录** | SBOM 不完整，无法签署许可结论 | 离线可用 `uv.lock` 版本号 + 联网查 PyPI 元数据补齐；权重清单需镜像内查看 | A |
| BLK-04 | `DOCREADER_PDF_*` 等配置**二源**，不在 `print_config()` | 环境记录易失真 | T11 显式列出全部 env（§1.2），不依赖启动日志 | A |
| BLK-05 | DOCX/HTML/MD/TXT **无 locator** | TDD06 的 DOCX locator 期望不可满足 | 修订 TDD06 或由 PyKnora 自建（见 manifest D-05） | A |
| BLK-06 | `PreprocessImage` 超时/超内存后的确切返回未读全 | C-19 断言不完整 | T11 实测补；或读 `preprocess` 实现全文 | C |

## 8. T10 开工清单（最小可执行序列）

1. 复核 `docreader/proto/docreader.proto` sha256 = `33d4135c…38d0`（manifest §1.1），不一致即停止。
2. 按 §3 生成 B-* fixture（纯标准库脚本），逐个记录 sha256。
3. 按 §2 建立 A-* 只读引用清单（不复制，或在 §6 约定后复制并带 LICENSE）。
4. 写「断言期望文件」（每个 fixture 一份 expected：页数/图片数/是否有 locator/关键文本片段）。
5. 环境就绪后按 §4 逐用例执行，**失败即记 BLOCKED，不得用 mock 冒充通过**（对齐 `AGENTS.md`）。
