# DocReader 静态预检清单（P0-T09）

> 任务：P0-T09（DocReader 静态预检）｜依据：`docs/poc/03_Phase0_Task_Backlog.md:150-166`、`docs/poc/07_Contract_Closure.md:192`、`docs/tdd/06_Knowledge_Ingestion.md:76-90`、`docs/tdd/01_System_Architecture.md:181`
> 范围：**只做静态预检，不运行任何解析**。本文所有结论来自真实文件，未执行 docker / uv / 服务启动。
> 状态：`PARTIAL` —— 协议、入口、错误、许可主干已按 A 级证据闭合；镜像 digest 与部分第三方许可为 `UNKNOWN`（逐项列于 §9）。
> 证据等级：**A** = 直接读到代码/配置/文档原文；**B** = 从配置或文档推断，未逐行验证；**C** = 未能确认（写 `UNKNOWN` + 已查路径）。
> 路径约定：未加前缀的 `文件:行号` 均相对 `references/WeKnora/`（submodule commit `662abaf9f944e37689d04c42160034b2cdec6829`）。

## 1. 基线与可追溯性

| 项 | 值 | 证据 | 等级 |
|---|---|---|---|
| 本仓库 HEAD | `59d53ac9897f8b0e5d3eeb096332737f7ed6e1ce` | `git rev-parse HEAD` | A |
| 本仓库分支 | `feature/w1-t02-t09-t14` | 任务给定，未切换 | B |
| 参考源码 submodule SHA | `662abaf9f944e37689d04c42160034b2cdec6829` | `git ls-tree HEAD references/WeKnora` → `160000 commit 662abaf… references/WeKnora` | A |
| 参考源码文件数 | 5676 | `(Get-ChildItem -Recurse -File references\WeKnora).Count` | A |
| docreader 服务版本 | `0.1.0` | `docreader/pyproject.toml:3`（`version = "0.1.0"`） | A |
| WeKnora 仓库版本 | `0.8.3` | `VERSION:1` | A |
| Go module | `github.com/Tencent/WeKnora`，`go 1.26.0` | `go.mod:1`、`go.mod:3` | A |

### 1.1 关键文件 SHA-256（本次实测，`Get-FileHash -Algorithm SHA256`）

| 文件 | 字节 | SHA-256 | 等级 |
|---|---|---|---|
| `docreader/proto/docreader.proto` | 4689 | `33d4135cfcd2ec95f8d160d6cefdd54be669167e8b8fb69ef81095d218a738d0` | A |
| `docreader/proto/docreader_pb2.py` | 7414 | `b6d7c8e28db3750bf363a56cec0f945daa19f534aafd6c0eff487fde74de0b1c` | A |
| `docreader/proto/docreader_pb2.pyi` | 8705 | `370a7786a01cfb9acfc28106ef03cc05e5e9995b6f97fb8382acc2ddcd8e71e8` | A |
| `docreader/proto/docreader_pb2_grpc.py` | 9547 | `684c903675852a15ffb9d708c5e117df065fd048e82e83f60e5ef8f6d1bcb052` | A |
| `docreader/proto/docreader.pb.go` | 34032 | `e0336eb94d0579704a1b076010d37209bd013b620f3e641fdb1350c9d7f8e06d` | A |
| `docreader/proto/docreader_grpc.pb.go` | 11141 | `6cd16e93b27c4e156ae9c3865b92d6a1510a703bcedfad8b5e6d5b5a31f0b903` | A |
| `docreader/uv.lock` | 481350 | `92637de3ce37a010cb36414b30de9621f77337b27bc1ad37378d9cdb0e7d104a` | A |
| `docreader/pyproject.toml` | 1096 | `8c999c64907bdc4705928e69a9791d873bb87379e794c06ac9eaa3de3313a6cc` | A |
| `docreader/main.py` | 15952 | `7cfa27d83d64012e913211ab65c1528dd93d55a95e5c484d327428aea08f9d04` | A |
| `docreader/config.py` | 9308 | `cf382038ac6f87338a91593fe5737687e6f87f5e2d8d0b1be60f1e292cae13fa` | A |
| `docreader/auth.py` | 7235 | `86667961c9aeb78581ab385d6496e51d794df9489caaabccb3066e8ee68b22b0` | A |
| `docreader/container_entrypoint.py` | 1814 | `e518c5a2286943f957c7a00707e683a1475788ae48a753d33cfb01c0e01acf60` | A |
| `docker/Dockerfile.docreader` | 6191 | `1c578e3fa8846744ef8afc9a9f64a1261f64336b3537cc74d8fc5b190b5b356b` | A |
| `.github/workflows/docreader.yml` | 3189 | `026b7e5ae1a2f20237382930c043936d1a4732dccbbee1ef3732969c7c4a0c6d` | A |
| `LICENSE` | 15574 | `99f1251d7662dda0b55ba9309a754a73223b5dd56f6417cf0eebcc2fd9e480f9` | A |
| `THIRD_PARTY_NOTICES.md` | 8080 | `5875364c2bd781abf5fb3b60950eab417c4f4db894c584cdd048ec152375f312` | A |
| `docreader/client/client.go` | 2564 | `04e9df2a58cb377b1610f1f35bf09848a52dc836bf0fac273da0edb0ab789547` | A |
| `docreader/client/auth.go` | 4250 | `d7c75e49475fb555c3317e97eab9383a3d4c06de98c657e07cfe69e57619b459` | A |
| `internal/infrastructure/docparser/grpc_parser.go` | 7983 | `29a7e2c3cb0bd8a4d4efa2e397ecc7db3eeb4fd09719a7eb2d0e8a73dd578830` | A |
| `internal/errors/parse_error_codes.go` | 3034 | `985d4d83c926f7744e1199b0e9c43c3da9a0e9eb3550fc84e8b2a5116e59dd76` | A |
| `internal/application/service/docreader_failure.go` | 1142 | `abe99f33d9dfa22c8fac7c7a70c0f8259293a679b6ed5561d93cfd642f77d5c3` | A |

> proto hash 是 P0-T09 的硬锚点：PyKnora 侧锁定 `docreader.proto` 时应复核上表第一行；生成物（`docreader_pb2*.py`、`*.pb.go`）必须与 proto 同批更新，否则视为版本漂移。

## 2. 服务入口、启动与监听

### 2.1 入口与进程模型

| 项 | 事实 | 证据 | 等级 |
|---|---|---|---|
| 服务入口 | `python -m docreader.main`（`main()`） | `docreader/main.py:383` | A |
| gRPC servicer | `DocReaderServicer`：`Read`(225-261)、`ReadStream`(263-365)、`ListEngines`(367-380)；`PreprocessImage` **不是类方法**，而是在 `__init__` 内定义闭包后动态挂载：`self.PreprocessImage = preprocess_image` + `preprocess_image.experimental_thread_pool = self._preprocess_executor` | `docreader/main.py:225,263,367,178-182` | A |
| 预处理器独立线程池 | `max_workers = preprocess.limits.max_workers + 1`（多留 1 个准入线程用于拒绝 busy 请求） | `docreader/main.py:169-176` | A |
| 线程模型 | `ThreadPoolExecutor(max_workers=CONFIG.grpc_max_workers)`，默认 4 | `docreader/main.py:388-395`；`docreader/config.py:99` | A |
| 消息上限 | `grpc.max_send_message_length` / `max_receive_message_length` = `grpc_max_file_size_mb`（**变量名含 mb，实为字节**：`_get_int(...) * 1024 * 1024`，默认 50 MiB） | `docreader/config.py:100-104`；`docreader/main.py:391-392` | A |
| 健康检查 | 注册 `grpc_health.v1.Health` 的 `HealthServicer`，并作为**免认证方法**放行 | `docreader/main.py:400-401`；`docreader/auth.py:102-107` | A |
| 优雅退出 | `SIGTERM` → `server.stop(0)` | `docreader/main.py:423` | A |
| 进程包装 | 容器内先经 `container_entrypoint`（仅 ARM SIGILL 时置 `OPENSSL_armcap=0`，再 `execvpe` 转交 CMD） | `docreader/container_entrypoint.py`（`os.execvpe(sys.argv[1], ...)`） | A |

### 2.2 监听端口与传输方式

| 项 | 事实 | 证据 | 等级 |
|---|---|---|---|
| 传输协议 | **gRPC（HTTP/2）**，非 REST/HTTP | `docreader.proto:7-25`（`service DocReader`）；`main.py:388` | A |
| 端口 | `DOCREADER_GRPC_PORT`（兼容 `PORT`），默认 **50051** | `docreader/config.py:105` | A |
| 监听地址 | `[::]:{port}`（IPv6 双栈） | `docreader/main.py:410,414` | A |
| TLS 分支 | 有证书 → `add_secure_port`；无证书 → `add_insecure_port` + 启动 warning `WITHOUT TLS (insecure mode)` | `docreader/main.py:403-416` | A |
| 证书缺失即拒绝启动 | `GRPC_TLS_ENABLED=true` 但缺 `GRPC_TLS_CERT`/`GRPC_TLS_KEY` → `TLSConfigError` → `logger.error("Refusing to start: …")` + `sys.exit(1)` | `docreader/auth.py:54-58`；`docreader/main.py:403-407` | A |
| 默认部署 | compose 中 docreader **不 publish 端口**，仅 `expose: "50051"`，注释明示 "ships without auth/TLS and is only consumed by the app container" | `docker-compose.yml:430-437` | A |
| 客户端地址 | `DOCREADER_ADDR`（默认 `docreader:50051`）、`DOCREADER_TRANSPORT`（默认 `grpc`） | `docker-compose.yml:199-200` | A |

> 易错点：`docker-compose.yml:756` 的 `"50052:50051"` 属于 **Weaviate gRPC**（`docker-compose.yml:745-756`），与 DocReader 无关，勿混用。

### 2.3 启动命令（三个真实来源 + 一个已失效来源）

| 来源 | 命令 | 证据 | 等级 |
|---|---|---|---|
| 容器（权威） | `ENTRYPOINT ["/app/.venv/bin/python","-m","docreader.container_entrypoint"]`，`CMD ["/app/.venv/bin/python","-m","docreader.main"]` | `docker/Dockerfile.docreader:168-170` | A |
| 容器端口声明 | `EXPOSE 50051` | `docker/Dockerfile.docreader:164` | A |
| CI | `uv run --project docreader --python 3.10.18 python -m docreader.main &` 后轮询 `nc -z 127.0.0.1 50051` | `.github/workflows/docreader.yml:83-95` | A |
| 依赖安装（CI/镜像） | `uv sync --project docreader --locked --no-dev` | `.github/workflows/docreader.yml:56`；`docker/Dockerfile.docreader:68-69` | A |
| ~~Makefile~~ | **已失效**：`run:` 指向 `python src/server/server.py`、`build:` 指向 `./src/client`，而 `docreader/src` 不存在（`Test-Path` = `False`） | `docreader/Makefile:9-16` | A |

> `docreader/README.md:250-253` 记端口 50051/容器名 `WeKnora-docreader`/网络 `WeKnora-network`，但 README 整体与代码存在漂移（见 §9），**只能作 B 级**。

## 3. 协议清单

### 3.1 proto 与生成物

- 真实路径：`docreader/proto/docreader.proto`（128 行，sha256 见 §1.1），`syntax = "proto3"`，`package docreader`（`docreader.proto:1-3`）。
- `option go_package = "github.com/Tencent/WeKnora/internal/docreader/proto"`（`docreader.proto:5`）；而 Go 客户端实际 import `github.com/Tencent/WeKnora/docreader/proto`（`docreader/client/client.go:10`）→ **go_package 与 import 路径不一致**（生成物落在 `docreader/proto`，靠 module 路径解析生效）。移植时不要照抄 go_package。
- 生成物同仓存在四份：`docreader_pb2.py`、`docreader_pb2.pyi`、`docreader_pb2_grpc.py`、`docreader.pb.go`、`docreader_grpc.pb.go`（sha256 见 §1.1）。**PyKnora 若用 Python 客户端，应只用 `docreader_pb2*.py` 与其配套 proto，不要混用 `.pb.go` 版本的语义**（同一 proto，但生成器版本不同）。

### 3.2 四个 RPC（100% 覆盖：请求 / 响应 / 错误 / 限制）

| RPC | 类型 | 请求 | 响应 | 错误形态（真实） | 限制 | 等级 |
|---|---|---|---|---|---|---|
| `Read` | unary | `ReadRequest` | `ReadResponse` | **解析失败不抛 gRPC 错误**：返回 `ReadResponse.error="Failed to parse: <source>"`（`main.py:233-236`）或 `error=str(e)`（`main.py:257-261`），gRPC status 仍 `OK` | 整包受 50 MiB 消息上限；`workers=4`；无服务端 deadline | A |
| `ReadStream` | server streaming | `ReadRequest` | `stream ReadStreamResponse`（1 帧 `meta` + N 帧 `image`） | 解析异常 → 首帧 `meta.error=str(e)` 后 `return`（`main.py:308-315`）；空结果 → `meta.error="Failed to parse: …"`（`main.py:321-326`）；**流中异常 `raise` 打断**，客户端只能靠 gRPC status + 已收帧数判不完整（`main.py:355-357`） | 每帧仍受 50 MiB 上限；`meta` 只发一次（`docreader.proto:95-96`）；无 backpressure；客户端提前关闭触发 `finally: parsed_stream.close()`（`main.py:358-359`） | A |
| `ListEngines` | unary | `ListEnginesRequest{config_overrides}` | `ListEnginesResponse{repeated ParserEngineInfo}` | 未读到显式错误分支；注册表 `check_available` 抛异常时被吞成 `available=false` + `unavailable_reason`（`registry.py:128-135`） | 无分页、无鉴权粒度；Go 侧只用于**管理端引擎列表**（`system.go:552-565`、`temporary_document.go:181`），不参与解析路由 | A |
| `PreprocessImage` | unary | `PreprocessImageRequest{image}` | `PreprocessImageResponse{image,fallback_needed,theta,residual,flip,n_boxes,n_kept,status}` | 「不可纠正」**不算错误**：`fallback_needed=true` + 空 `image`（`docreader.proto:18-23`） | 独立子进程池：默认 `max_workers=1`、`timeout=30s`、`threads=2`、`memory=2048MiB`、`max_pixels=16_000_000`（`config.py:173-179`） | A（默认值 + status 取值） |

### 3.3 消息字段（原文摘录，`docreader.proto`）

| 消息 | 字段（编号） | 行号 |
|---|---|---|
| `ReadRequest` | `file_content=1`(bytes)、`file_name=2`、`file_type=3`、`url=4`、`title=5`、`config=6`(ReadConfig)、`request_id=7` | 57-65 |
| `ReadConfig` | `parser_engine=1`、`parser_engine_overrides=2`(map)、`reserved 3`（原 `image_storage`，注释：图片持久化已归 Go App） | 48-54 |
| `ImageRef` | `filename=1`、`original_ref=2`、`mime_type=3`、`storage_key=4`(共享存储下载 URL)、`image_data=5`(内联字节兜底) | 67-73 |
| `SourceBlock` | `start=1`、`end=2`、`locator_json=3` | 77-84 |
| `ReadResponse` | `markdown_content=1`、`image_refs=2`、`image_dir_path=3`、`metadata=4`(map)、`error=5`、`source_blocks=6` | 86-93 |
| `ReadStreamMeta` | `markdown_content=1`、`image_dir_path=2`、`metadata=3`、`error=4`、`image_count=5`(best-effort，0=未知)、`source_blocks=6` | 96-103 |
| `ReadStreamResponse` | `oneof payload { ReadStreamMeta meta=1; ImageRef image=2 }` | 107-112 |
| `ListEnginesRequest` / `ParserEngineInfo` / `ListEnginesResponse` | `config_overrides=1` / `name,description,file_types,available,unavailable_reason=1..5` / `engines=1` | 114-128 |
| `PreprocessImageRequest` / `Response` | `image=1` / `image=1,fallback_needed=2,theta=3,residual=4,flip=5,n_boxes=6,n_kept=7,status=8`（`status ∈ {ok,no_text,low_quality}`） | 28-46 |

**关键否定事实**：`ReadRequest` 中**没有** `workspace_id` / `kb_id` / `tenant_id` / `principal` 任何业务授权字段（`docreader.proto:57-65` 全字段已核）→ 业务授权完全不在协议层，DocReader 是「无业务身份的纯解析服务」。这正是 `07_Contract_Closure.md:192` 要求的「隔离认证」边界。

### 3.4 `Read` vs `ReadStream` 的真实差别（从实现读出，不是从注释）

1. **首帧时机**：`ReadStream` 的 `meta`（含**完整 markdown**）在**解析全部完成后**才发出（`main.py:297-342`），不是增量吐正文。因此流式**不提供解析进度**，只解决「图片总量超 unary 上限」。
2. **增量粒度**：只有图片是逐帧的——先 `_iter_image_refs(images)` 逐条 `yield` 并 pop 释放内存（`main.py:344-346`），再消费 `parsed_stream.images` 惰性迭代器（`main.py:348-354`）。
3. **真增量解析仅 PDF**：`parse_file_stream` 在基类统一分发 `parser.parse_stream`（`parser.py:87-117`），实际实现只有 `pdf_parser.py:1754` 与 `pdf_parser.py:1876` 两个 `parse_stream`；其它 parser 走 `parser.parse(content)` 一次性物化（`parser.py:110-117`）。
4. **URL 模式强制退化**：`if request.url or not callable(parse_file_stream)` → 一次性 `_parse_request` 再包成流（`main.py:271-278`）。
5. **`image_dir_path` 恒空**：`_resolve_images` 的 docstring 明写 `Returns ("", list[ImageRef]). image_dir_path is always empty.`（`main.py:74`），实现也确实恒返回 `""`（`main.py:79,111`）；`Read` 传 `image_dir_path=image_dir`（`main.py:239,244`），`ReadStream` 直接写字面量 `image_dir_path=""`（`main.py:335`）。图片只走 `ImageRef.image_data` 内联；注释明确「Go App 独占图片持久化」（`main.py:70-72`、`docreader.proto:51-53`）。
6. **失败语义差异**：`Read` 把异常折进 `ReadResponse.error` 且 status=OK（`main.py:233-236,257-261`）；`ReadStream` 把**解析期**异常折进 `meta.error`（`main.py:308-315,321-326`），但**发送期**异常直接 `raise`（`main.py:355-357`）→ 客户端必须同时判 `meta.error`、EOF、已收图片数。

### 3.5 `SourceBlock` / locator：语义与真实覆盖

- 语义（`docreader.proto:75-84` + `docreader/parser/source_locator.py:1-9` 双证）：`start`/`end` 是 `markdown_content` 的 **Unicode code point 偏移，end 开区间**；`locator_json` 例 `{"type":"pdf","page":3,"bbox":[x0,y0,x1,y1]}`，序号 **1-based**，bbox 为**页内比例**、原点**左上**。
- 序列化容错：畸形块被**静默跳过**（`end<=start`、`start<0`、缺 `type` 都 `continue`），注释写明「位置是可选附加，绝不能让解析失败」（`docreader/source_wire.py:12-33`）。
- **真实覆盖（重要缺口）**：全仓只有两个 parser 产出 `source_blocks` —— `pdf_parser.py:1721-1788,2049-2122` 与 `excel_parser.py:105-168`（locator 含 `sheet`/`row_start`，见 `tests/test_excel_parser.py:642`）。**DOCX / HTML / MD / TXT 一律没有 locator**（grep `source_blocks` 于 `docreader/parser/*.py` 仅命中上述两个文件）。
- 对 TDD06 的直接冲击：`docs/tdd/06_Knowledge_Ingestion.md:76-90` 期望「DOCX=DocReader 标题/段落/表格 locator」——**参考实现当前不提供 DOCX locator**，属 PyKnora 需自建或降级的能力（详见 §7 D-05）。

### 3.6 `PreprocessImageResponse.status` 取值：proto 注释与实现不一致（A）

- proto 注释只声明三种：`status is "ok" | "no_text" | "low_quality"`（`docreader.proto:44`）。
- 实现实际返回**至少 10 种**：
  - `preprocess_worker.py:49` `image_too_large`、`:51,55` `undecodable`、`:58` `image_too_large`、`:82` `low_quality`、`:105` `encode_failed`、`:106` `ok`、`:108` `memory_limit`、`:111` `failed`；
  - `preprocess.py:251-255` 子进程异常 → `fallback_needed=True, status=str(exc)` / `status="worker_failed"`；
  - `docreader/README.md:113` 另列 `busy`、`timeout`、`cancelled`（README 为 B 级，但与 `preprocess.py` 的并发准入设计一致，见 `main.py:170-176` 注释）。
- 影响：PyKnora 若按 proto 注释只处理 3 种状态，会把 `busy`/`timeout`/`memory_limit` 当成未知值。**必须按「`fallback_needed` 为唯一判据，`status` 仅作日志/指标」实现**。
- 等级：A（代码）/B（`busy|timeout|cancelled` 仅见 README）。



### 4.1 版本来源（并列，勿只取一处）

| 来源 | 值 | 证据 | 等级 |
|---|---|---|---|
| Python 包版本 | `docreader 0.1.0` | `docreader/pyproject.toml:3` | A |
| 仓库版本 | `0.8.3` | `VERSION:1` | A |
| 镜像 tag | `wechatopenai/weknora-docreader:${WEKNORA_VERSION:-latest}` | `docker-compose.yml:421` | A |
| 镜像 digest | `UNKNOWN` —— compose 未按 digest 固定，缺省 tag 为 `latest` | 已查 `docker-compose.yml:420-439`、`docker/Dockerfile.docreader`；无网络/无 docker，无法 `pull` 取 digest | C |

### 4.2 运行时版本要求

| 项 | 值 | 证据 | 等级 |
|---|---|---|---|
| Python | `requires-python = ">=3.10.18"` | `docreader/pyproject.toml:6` | A |
| 镜像基础 | `python:3.10.18-bookworm`（builder 与 runner 同一 tag，**均未固定 digest**） | `docker/Dockerfile.docreader:4,82` | A |
| CI Python | `3.10.18` | `.github/workflows/docreader.yml:32-35` | A |
| Go（宿主侧） | `go 1.26.0`；CI `GO_VERSION: "1.26"`；镜像 `golang:1.26-bookworm` | `go.mod:3`；`.github/workflows/docreader.yml:35`；`docker/Dockerfile.app:27` | A |
| protoc（构建期） | `3.19.4`（在线/离线包） | `docker/Dockerfile.docreader:51-64` | A |

### 4.3 Python 依赖（26 条，`docreader/pyproject.toml:8-36`）与许可状态

许可列**只写仓内可查到的记录**；仓内无记录的一律 `UNKNOWN`（不猜、不按「都是 MIT」推断）。

| 依赖 | 约束 | 许可（仓内记录） | 记录位置 |
|---|---|---|---|
| rapidocr | `>=3.0` | **Apache-2.0**（锁定版 `3.10.0`） | `THIRD_PARTY_NOTICES.md:89-105` |
| onnxruntime | `>=1.17` | **MIT**（锁定版 `1.23.2`） | `THIRD_PARTY_NOTICES.md:107-116` |
| opencv-python-headless | `>=4.8` | **Apache-2.0**（锁定版 `5.0.0.93`），含 bundled third-party 清单 | `THIRD_PARTY_NOTICES.md:118-132` |
| numpy | `>=1.26` | **BSD-3-Clause**（锁定版 `2.3.4`） | `THIRD_PARTY_NOTICES.md:134-143` |
| grpcio / grpcio-health-checking / grpcio-tools | `>=1.78.0` | `UNKNOWN` | 已查 `LICENSE`(283 行)、`THIRD_PARTY_NOTICES.md`(143 行)、`docreader/pyproject.toml`（无 license 字段）、`docreader/uv.lock`（无 license 元数据） |
| protobuf | `>=6.33.5` | `UNKNOWN` | 同上 |
| lxml / beautifulsoup4 / markdownify / trafilatura | `>=6.1.0` / `>=4.14.2` / `>=0.13.1` / `>=2.0.0` | `UNKNOWN` | 同上 |
| markitdown[docx,pdf,xls,xlsx,pptx] | `>=0.1.3` | `UNKNOWN` | 同上 |
| opendataloader-pdf | `>=2.4.7` | `UNKNOWN`（注意其运行需 Java 11+，见 `registry.py:198-206`） | 同上 |
| playwright | `>=1.55.0` | `UNKNOWN` | 同上 |
| pypdf / pypdfium2 / python-docx / openpyxl / xlrd / pandas / pillow / pydantic / requests | 见 pyproject | `UNKNOWN` | 同上 |
| ebooklib | `>=0.18` | `UNKNOWN` | 同上 |
| textract | `==1.5.0`（**精确锁定**） | `UNKNOWN` | 同上 |

**明确否定「MIT 覆盖所有」**：`LICENSE:7` 只声明「WeKnora 自身代码 MIT，除下列第三方组件外」；`LICENSE:39-43` 列 Apache-2.0 的 `longbridgeapp/opencc`，`LICENSE:252-260` 列 MIT 的 `anydoc`（Tencent 修改版）与 `wails/v2`；`THIRD_PARTY_NOTICES.md:3-4` 明写「WeKnora 的 MIT 不替代第三方许可」。**docreader 的 22 个非 OCR 依赖在仓内无任何许可记录**，必须由 PyKnora 自建 SBOM 补齐（对齐 `docs/tdd/01_System_Architecture.md:181`）。

### 4.4 原生 / 系统依赖（镜像层）

`docker/Dockerfile.docreader:93-112` 安装：`libjpeg62-turbo`、`wget`、`gnupg`、**`openjdk-17-jre-headless`**、`libgl1`、`libglib2.0-0`、**`antiword`**、`tar`、`dpkg`、`libxinerama1`、`libfontconfig1`、`libdbus-glib-1-2`、`libcairo2`、`libcups2`、`libglu1-mesa`、`libsm6`、**`libreoffice`**、`curl`；另装 `grpc_health_probe v0.4.24`（`Dockerfile.docreader:114-136`）、`playwright install webkit` + `install-deps webkit`（`Dockerfile.docreader:148-155`）。

- 许可：以上 apt 包在 `LICENSE` / `THIRD_PARTY_NOTICES.md` / `licenses/` 中**均无记录** → `UNKNOWN`（已查 `licenses/` 全 10 个文件：OpenCC、Wails、go-m1cpu、go-sql-driver、numpy、onnxruntime、opencv×2、rapidocr×2）。
- 体积/资源影响：`libreoffice` + `openjdk-17` + webkit 是镜像体积与启动依赖的主要来源（B 级：由包语义推断，未实测镜像大小）。

### 4.5 OCR 权重与模型

| 项 | 事实 | 证据 | 等级 |
|---|---|---|---|
| 权重归属 | RapidOCR 分发的 OCR 权重版权归 **Baidu / PaddleOCR 权利人**，以 Apache-2.0 分发 | `THIRD_PARTY_NOTICES.md:97-101` | A |
| 权重清单文件 | `licenses/rapidocr-MODEL_LICENSES.md`（仓内存在） | `THIRD_PARTY_NOTICES.md:100-101` + `licenses/` 列表 | A |
| 权重是否入库 | **否**。`THIRD_PARTY_NOTICES.md:3-7` 说明源码归档由 `scripts/copy-licenses.sh` 在打包期生成，不入 Git；容器内 bundle 在 `/app` | A |
| 权重具体文件/大小/校验和 | `UNKNOWN` —— 需 `pip download`/镜像内查看，本轮禁联网、禁装包 | 已查 `licenses/rapidocr-MODEL_LICENSES.md`（存在但未逐行读）、`docreader/pyproject.toml` | C |

### 4.6 可复现性缺口（直接对应 TDD01 §181 的 digest 锁定要求）

| 缺口 | 现状 | 建议 |
|---|---|---|
| docreader 镜像未按 digest 固定 | `docker-compose.yml:421` 用 `${WEKNORA_VERSION:-latest}` | T10 前改为 `@sha256:` 固定，或在 manifest 记录「未固定」并禁止生产使用 latest |
| 基础镜像未固定 | `Dockerfile.docreader:4,82` 无 digest（对比 `Dockerfile.app:2` 的 node 镜像**有** `@sha256:`） | 同上 |
| apt 包未固定版本 | `Dockerfile.docreader:93-112` 无版本号 | 记录为已知漂移源 |
| protoc 版本与 proto 生成物 | protoc 3.19.4 + protobuf>=6.33.5 并存，生成物由何版本产出未标注 | 以 §1.1 的 proto sha256 为准做一致性校验 |

## 5. 错误码与异常行为（真实类型 + 返回码）

### 5.1 服务端：异常 → 字段（不改变 gRPC status）

| 场景 | 触发位置 | 客户端可见 | 证据 | 等级 |
|---|---|---|---|---|
| 文件/URL 解析失败（结果空） | `Read` | `ReadResponse.error = "Failed to parse: <source_desc>"`，status=OK | `main.py:233-236` | A |
| 解析抛异常 | `Read` | `ReadResponse.error = str(e)`（**异常原文直传**，可能含内部路径） | `main.py:257-261` | A |
| 不支持的 file_type | 注册表 | `ValueError("Unsupported file type: txt")` → 折进 `error` 字段 | `registry.py:105-114` + `main.py:257-261` | A |
| 解析抛异常（流式，解析期） | `ReadStream` | 首帧 `meta.error = str(e)` 后结束 | `main.py:308-315` | A |
| 结果空（流式） | `ReadStream` | 首帧 `meta.error = "Failed to parse: <source>"` | `main.py:321-326` | A |
| 发送期异常 | `ReadStream` | `raise` 中断流 → gRPC 返回 `UNKNOWN`（Python 服务端未捕获异常时的默认码，B 级推断）+ 已收帧数不足 | `main.py:355-357` | A（代码）/B（status 码） |
| 非法 UTF-8 | `to_valid_utf8_text` | 代理项 `[\ud800-\udfff]` 替换为 **U+FFFD**，正文**静默变形** | `main.py:39-43` | A |
| 认证失败 | `AuthInterceptor` | `context.abort(StatusCode.UNAUTHENTICATED, "Invalid or missing authentication token")` | `auth.py:117-121` | A |
| TLS 配置错误 | 启动期 | `TLSConfigError` → 进程 `exit(1)`，不进入服务状态 | `auth.py:54-58`、`main.py:403-407` | A |
| 消息超限 | gRPC 层 | `RESOURCE_EXHAUSTED`（proto 注释明示该风险与规避动机） | `docreader.proto:9-15` | B（注释）/A（上限值 `config.py:100-104`） |
| 无 token 部署 | `AuthInterceptor` | **认证整体关闭**，仅启动 warning `Token authentication disabled`；token < 16 字节也只 warning | `auth.py:171-181` | A |

### 5.2 Go 宿主侧：错误分类与业务码

| 场景 | 判定条件 | 业务码 | 证据 | 等级 |
|---|---|---|---|---|
| 超时 | `context.DeadlineExceeded` / `codes.DeadlineExceeded` / 含 `docreader call timeout` | `DOCREADER_TIMEOUT` | `docreader_failure.go:16-21`；`parse_error_codes.go:16` | A |
| 不可达 | `codes.Unavailable` / `*net.OpError` | `DOCREADER_UNAVAILABLE` | `docreader_failure.go:22-27`；`parse_error_codes.go:20` | A |
| 其它 | 兜底 | `DOCREADER_PARSE_FAILED` | `docreader_failure.go:28-29`；`parse_error_codes.go:24` | A |
| 未连接（客户端本地） | `client == nil` | `status.Error(codes.Unavailable, "docreader service not connected")` | `grpc_parser.go:105` | A |
| `ReadStream` 未实现 → 降级 unary | `status.Code(err) == codes.Unimplemented` | 记 warn 后走 `Read` | `grpc_parser.go:137-140` | A |
| 无 meta 帧 | 流正常结束但 `!gotMeta` | `fmt.Errorf("gRPC ReadStream returned no metadata frame")` | `grpc_parser.go:192-194` | A |
| 调用超时上限 | 默认 **30m**，可被 `WEKNORA_DOCREADER_CALL_TIMEOUT` 覆盖 | 包装 `docreader call timeout after <d>` | `config.go:820-827`；`knowledge_process.go:4462-4488` | A |
| 解析器资源上限 | `errors.Is(err, anydoc.ErrResourceLimit)` | 视为**永久拒收**，转成 `ReadResult.Error` | `knowledge_process.go:4413-4416` | A |

> 语义要点：**「解析失败」在 DocReader 里不是 gRPC 错误**。PyKnora 若只判 gRPC status 会把「解析失败」当成「调用成功且内容为空」。必须同时判 `ReadResponse.error` / `ReadStreamMeta.error` 与内容非空。

## 6. 五格式路线（参考实现真实路由 vs PyKnora 计划）

路由判定链（A 级）：`knowledge_process.go:4412` → `resolveDocReader`（`knowledge_process.go:4512-4524`）→ `docparser.NewReader`（`engine_registry.go:69-79`）：本地引擎名命中则用之；`engine=="" && !isURL && IsSimpleFormat(fileType)` → Go `SimpleFormatReader`（`builtin_converter.go:44-46`）；否则 → DocReader 远程。

类型级默认覆盖：`types.SetPreferParserEngine(preferAnydocWhenAvailable)`（`engines.go:33`），对**非 simple 且非 pdf** 的类型，若 anydoc 已链接且支持该类型 → 用 `anydoc`（`engines.go:44-64`）。而官方镜像 **默认 `WITH_ANYDOC=1`**（`docker/Dockerfile.app:74-101`），故**默认部署下 DOCX 走 anydoc（进程内），不是 DocReader**。

| 格式 | 参考实现真实路由 | 关键证据 | DocReader 是否支持 | locator 能力 | PyKnora 计划（TDD06） | 差距 |
|---|---|---|---|---|---|---|
| **TXT** | Go `SimpleFormatReader` 直接 `string(file_content)` | `builtin_converter.go:18-23,62-63`；`engines.go:113-127` | **不支持**（注册表无 `txt`，`registry.py:159-179`）→ 若强送 DocReader 会得 `ValueError: Unsupported file type: txt`（`registry.py:105-114`） | 无 | Python 标准库 + 严格 UTF-8（`06:76-90`） | 与参考实现一致（都不走 DocReader），但 PyKnora 必须自己实现编码校验；参考实现**无 UTF-8 校验**（`builtin_converter.go:62-63` 直接 decode） |
| **MD** | Go `SimpleFormatReader` 原样透传 | `builtin_converter.go:60-61` | 支持（`MarkdownParser`，`registry.py:165-166`）但默认路径不用 | 无 | 简单适配，保留原文 | 参考实现零处理 → 标题层级/相对图片链接需 PyKnora 自建 |
| **PDF** | DocReader `builtin`（`PDFParser`）；anydoc 被**显式排除** | `engines.go:44-64`（`ft == "pdf"` → 返回 ""）；`registry.py:164` | 支持，且是唯一有 `parse_stream` 增量的格式 | **有**：`{"type":"pdf","page":n}` + 可选 `bbox`（`source_locator.py:1-9`；`pdf_parser.py:1721-1788,2049-2122`） | 固定 DocReader gRPC engine profile，页码/bbox/扫描 OCR | 主干可复用；OCR 依赖 `PreprocessImage` + RapidOCR 权重（见 §4.5） |
| **DOCX** | **默认 anydoc（进程内）**；显式选 `builtin` 才走 DocReader `Docx2Parser` | `Dockerfile.app:78`（`WITH_ANYDOC=1`）+ `engines.go:55-63` + `registry.py:162` | 支持（`Docx2Parser`） | **无**（`docx2_parser.py` 不产 `source_blocks`，见 §3.5 grep 结论） | DocReader 标题/段落/表格 locator（`06:76-90`） | **双缺口**：①路由默认不走 DocReader；②即便走 DocReader 也无 DOCX locator → TDD06 该行需修订或由 PyKnora 自建 |
| **HTML** | DocReader `builtin`（`HTMLParser`）；anydoc 不支持 `html`（`anydoc.go:133-150` 无 html 键） | `anydoc.go:133-150`；`registry.py:172-173` | 支持 | 无 | 策略代理抓 bytes 后走 DocReader 文件模式，或成熟正文库择优 | 一致；正文抽取由 `DOCREADER_HTML_MAIN_CONTENT`（默认 `false`，`config.py:124`）控制，开启后走 trafilatura（`html_parser.py:7,20-39`） |

补充（参考实现支持但 P0 不纳入）：`csv/json/图片/音频` 由 Go simple 路径处理（`builtin_converter.go:18-41`）；`epub/mhtml/xmind/ppt/xls` 在 DocReader 注册表内（`registry.py:159-179`）。

## 7. 与 PyKnora 目标契约的差异清单

| ID | 契约项 | PyKnora 要求（出处） | 参考实现现状（证据） | 差距 | 处置建议 |
|---|---|---|---|---|---|
| D-01 | 协议锁定 | proto hash + 参考 SHA + 镜像 digest 锁定（`tdd/01:181`） | proto 可锁（§1.1）；**镜像无 digest、tag 缺省 latest**（`docker-compose.yml:421`）；基础镜像无 digest（`Dockerfile.docreader:4,82`） | 镜像层不可复现 | T10 前补 digest；在此之前 manifest 只承诺 proto/源码可复现 |
| D-02 | 输入形态 | bytes 输入、隔离认证（`poc/07:192`） | bytes 已支持（`file_content`）；认证为**可选共享 token**，未设即全开（`auth.py:171-181`）；mTLS 默认取 `bool(CA)`（`auth.py:69-72`） | 默认部署无认证 | PyKnora 侧强制注入 token + mTLS，且**不允许** DocReader 被外部网络直达（对齐 `docker-compose.yml:430-437` 的隔离注释） |
| D-03 | 业务授权 | 所有资源带 `workspace_id`，后端校验（`AGENTS.md`） | proto 无任何业务身份字段（§3.3 否定事实） | 授权必须在连接器/调用方完成 | 连接器按 workspace 注入 token 与引擎白名单；**禁止**把 DocReader 暴露给客户端 |
| D-04 | 流不完整不发布 | 首帧后断流 → `PARSE_INCOMPLETE`（`tdd/06:76-90`） | 发送期异常直接 `raise`（`main.py:355-357`）；`image_count` 仅 best-effort（`docreader.proto:101`） | 客户端必须自行判定完整性 | PyKnora 客户端：`meta` 必须恰一份 + 收到 EOF + 图片数 ≥ `image_count`（>0 时）三重校验 |
| D-05 | locator 不虚构 | 只记录真实可用的定位（`poc/07:192`） | 仅 PDF/Excel 产 `source_blocks`（§3.5）；DOCX/HTML/MD/TXT 无 | TDD06 的 DOCX locator 行不可实现 | 修订 `tdd/06` DOCX 行为「P0 无 locator，T11 验证后决定是否自建」，或明确降级为「文档级引用」 |
| D-06 | 引擎选择 | 禁任意 URL/engine override（`tdd/06:76-90`） | `parser_engine_overrides` 是**任意 map**（`docreader.proto:50`），可传 `mineru_endpoint`/`mineru_api_key` 等（`registry.py:120-121`）；URL 模式存在且会抓外网（`web_parser.py` + `utils/ssrf.py:106` 白名单） | 攻击面 | 连接器侧白名单引擎名 + 拒绝 `url` 字段 + 不转发 overrides |
| D-07 | 错误语义 | 检查 gRPC status、`meta.error`、EOF（`tdd/06:76-90`） | 解析失败 = status OK + error 字段（`main.py:233-236,257-261`） | 易被误判为成功 | 客户端封装：`error != ""` 一律视为失败，不落库 |
| D-08 | 编码安全 | 严格 UTF-8（`tdd/06:76-90`） | `to_valid_utf8_text` 静默替换为 U+FFFD（`main.py:39-43`） | 正文可能被无痕篡改 | PyKnora 对 TXT/MD 自己做严格校验；DocReader 返回内容记录 U+FFFD 计数作为质量指标 |
| D-09 | 许可 SBOM | 依赖与 OCR 权重单独 SBOM，不由 MIT 覆盖（`tdd/01:181`） | 仅 4 个 OCR 包有记录（§4.3）；22 个 Python 依赖 + apt 包 + 权重文件清单均无 | 许可不可审计 | T10 前产出 `docreader_sbom.md`（可离线用 `uv.lock` + 包元数据生成，但**需联网或本地缓存**，本轮 BLOCKED） |
| D-10 | 图片持久化 | 由 PyKnora 管对象存储（`AGENTS.md` MinIO/S3） | 已归 Go App（`docreader.proto:51-53`、`main.py:70-74`），DocReader 只回内联字节 | 无 | 直接沿用：连接器收 `ImageRef.image_data` 后入 MinIO，`storage_key` 不使用 |
| D-11 | 引擎清单 | 需要稳定能力清单 | `ListEngines` 由 Python 注册表静态返回（`registry.py:116-145`），Go 侧另有本地注册表并做合并（`engine_registry.go:90-145`）；`registry.py:208-211` 注释称「Go 不再调用」，但 `system.go:559`、`temporary_document.go:181` 仍在调 → **注释与代码不一致** | 能力清单双源 | PyKnora 自建单一引擎清单，不依赖 `ListEngines` 的运行时值 |
| D-12 | 传输安全默认值 | 生产必须 TLS/mTLS | 默认 `GRPC_TLS_ENABLED=false`（`docker-compose.yml:451`）；无 token 即无认证（`auth.py:180-181`） | 默认不安全 | 部署模板默认开启 TLS+token，缺失即 fail-fast（参考 `auth.py:54-58` 的拒绝启动模式） |

## 8. 纸面异常路径分析（不冒充运行）

> 以下均为**读代码推演**，未执行。T11 必须真机复现并留证。

| 路径 | 推演结论 | 依据 | 等级 |
|---|---|---|---|
| 缺 `meta` 帧 | 服务端实现必发首帧 meta（成功/失败都发），故「无 meta」只可能来自客户端解析错误或中间代理截断；Go 客户端显式报错 `gRPC ReadStream returned no metadata frame` | `main.py:314,325,332`；`grpc_parser.go:192-194` | A |
| 缺图（`image_count>0` 但实收少） | `image_count` 是「已物化 + 惰性」两路总数（`stream.py:21-23`），发送期异常会 `raise` 断流 → 客户端会看到 status≠OK 或帧数不足；**不能**用 `image_count==0` 反推「无图」 | `main.py:339,344-357`；`docreader.proto:101` | A |
| unknown engine | 注册表：请求引擎不支持该类型 → 先按类型默认引擎 → 再 builtin → 仍无则 `ValueError` 列出可用引擎（`registry.py:105-114`）；**Go 侧未知引擎名会被路由到 DocReader**（`engine_registry.go:63-79`），即「未知引擎」不会在 Go 层报错，而是进 Python 报 `Unsupported file type` | 双证据 | A |
| 默认无 token | `GRPC_AUTH_TOKEN` 为空 → 拦截器直接放行（`auth.py:180-181`），仅启动 warning（`auth.py:177`）；`docker-compose.yml:458` 默认空 | 部署默认即无认证 | A |
| 无 TLS | `GRPC_TLS_ENABLED` 非 `true` → `load_tls_credentials()` 返回 None（`auth.py:47-49`）→ `add_insecure_port` + warning（`main.py:413-416`） | A |
| 客户端带 token 但服务端无 token | 服务端拦截器未启用认证 → 不校验、不拒绝；token 被忽略 | `auth.py:180-181` | A |
| 服务端有 token 但客户端无 | `UNAUTHENTICATED`（`auth.py:117-121`）；Go 侧分类为 `DOCREADER_PARSE_FAILED`（未命中 Timeout/Unavailable 分支，`docreader_failure.go:16-29`）→ **认证失败会被报成解析失败** | 语义错配 | A |

## 9. 未确认项（C 级）与已查路径

| 项 | 结论 | 已查路径 |
|---|---|---|
| docreader 镜像 digest | `UNKNOWN` | `docker-compose.yml:420-439`；`docker/Dockerfile.docreader`（全文 170 行）；无 docker/网络 |
| 22 个非 OCR Python 依赖的许可 | `UNKNOWN` | `LICENSE`(283 行)、`THIRD_PARTY_NOTICES.md`(143 行)、`licenses/`(10 文件)、`docreader/pyproject.toml`、`docreader/uv.lock`（无 license 字段） |
| apt 包（libreoffice/openjdk-17/antiword 等）许可 | `UNKNOWN` | 同上 + `docker/Dockerfile.docreader:93-136` |
| OCR 权重文件名/大小/校验和 | `UNKNOWN` | `licenses/rapidocr-MODEL_LICENSES.md`（存在未逐行读）、`THIRD_PARTY_NOTICES.md:97-101` |
| 镜像实际体积/内存占用 | `UNKNOWN` | 未构建、未运行 |
| `PreprocessImage` 超时/超内存后的确切返回 | `UNKNOWN`（只读到默认阈值） | `docreader/config.py:173-179`；未读 `preprocess` 实现全文 |
| `docreader/README.md` 的 MINIO/STORAGE_TYPE 段落是否仍有效 | **已判定失效（A）** | `README.md:9-17,117-147` vs `config.py`（不读这些变量）+ `main.py:70-74` + `docreader.proto:51-53` |
| 发送期异常在 Python gRPC 下的确切 status 码 | `UNKNOWN`（推断 UNKNOWN，未实测） | `main.py:355-357`；无运行 |

## 10. 对 T10/T11 的输入

1. **可静态确认（T10 直接引用本文）**：proto/服务名/4 RPC 字段、端口 50051、传输 gRPC、启动命令、错误语义、许可缺口、五格式路由。
2. **必须真机（T11）**：真实 `Read`/`ReadStream` 帧序与帧数、`meta.error` 与 status 组合、PDF `source_blocks` 的 code point 偏移与 bbox、U+FFFD 替换行为、token/TLS 四种组合的拒绝形态、`image_count` 与实际帧数关系。
3. **必须先补的输入**：镜像 digest、Python 依赖 SBOM、OCR 权重清单（见 §9）。
4. **本文不构成**「DocReader 可生产使用」的结论：只证明协议与实现可定位、可锁定、可验证。
