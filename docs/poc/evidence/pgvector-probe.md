# pgvector 可用性探针（P0-T25 前置阻断解除验证）

- 任务性质：**前置阻断验证**（不是 T02/T12 的交付物变更）
- 触发原因：`docs/poc/evidence/wave-w5-record.md` 记录「本机 PG 无 `vector` 扩展，T12 用 `float8[]`，
  T25 的 ANN/召回因此 `NOT_RUN`」
- 执行者：`w2-t04-controlsite`（本轮临时探针任务）；复核：Wave Lead
- 时间：2026-10-27 22:35–22:56 +08:00
- 写权：本任务**只写本文件**；未改 `compose.yaml`、未动 core 容器、未改任何迁移
- 结论：**PASS** —— `pgvector 0.8.7` 可用；`hnsw` 与 `ivfflat` 两种索引均可用并真实生效；
  3072 维需要 `halfvec` 表达式索引；`double precision[] → vector` 的迁移路径已实测可走通

## 1. 结论摘要

| 项 | 结果 |
| --- | --- |
| 镜像 | `pgvector/pgvector:pg17`（Docker Hub 直连不可达，经镜像源 `docker.1ms.run` 拉取成功） |
| 镜像 digest（64 位 sha256） | `sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d` |
| 镜像内 PG | `PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) on x86_64-pc-linux-gnu` |
| pgvector 版本 | `0.8.7`（`vector.control: default_version = '0.8.7'`，relocatable = true） |
| 扩展可用性 | `CREATE EXTENSION vector` 成功；`pg_available_extensions` 中 `vector` 默认版本 0.8.7 |
| 索引类型 | `hnsw` 与 `ivfflat` 均可用；`pg_am` 中两者都存在，`EXPLAIN` 确认真实走 `Index Scan` |
| 距离算子 | `<->`（L2）、`<=>`（cosine）、`<#>`（负内积）均可用 |
| 维度上限（实测错误信息） | 列类型 `vector` ≤ 16000 维；`hnsw`/`ivfflat` 索引 ≤ **2000** 维（`vector`）、≤ **4000** 维（`halfvec`） |
| 许可 | pgvector：**PostgreSQL License**（容器内 `/usr/share/doc/pgvector/LICENSE` 实读，见 §5） |
| 清理 | 容器已删、匿名 volume 已删、5434 无监听、core 容器未受影响 |

## 2. 镜像获取：Docker Hub 直连失败 → 镜像源回退

### 2.1 直连（失败，记 BLOCKED 事实）

```powershell
docker pull pgvector/pgvector:pg17
# 运行 >10 分钟后仍在等待层，后台作业终止（未得到镜像）
```

```powershell
# 对照探测（8s 超时）
Invoke-WebRequest -Uri https://registry-1.docker.io/v2/ -Method Head -TimeoutSec 8
# ERR: The request was canceled due to the configured HttpClient.Timeout of 8 seconds elapsing.
Invoke-WebRequest -Uri https://auth.docker.io/token -Method Head -TimeoutSec 8
# ERR: The SSL connection could not be established
```

`docker info` 已配置镜像源：`https://docker.1panel.live/`、`https://docker.m.daocloud.io/`、
`https://docker.1ms.run/`、`https://docker.xuanyuan.me/`。

### 2.2 镜像源探测（`docker manifest inspect`，60s 上限）

| 源 | 结果 |
| --- | --- |
| `pgvector/pgvector:pg17`（直连） | TIMEOUT（25s） |
| `pgvector/pgvector:pg16` / `pg15` / `ankane/pgvector:latest`（直连） | TIMEOUT（25s） |
| `docker.m.daocloud.io/pgvector/pgvector:pg17` | exit 0（manifest 可取） |
| `docker.1ms.run/pgvector/pgvector:pg17` | exit 0 |
| `docker.1panel.live/pgvector/pgvector:pg17` | exit 0 |
| `docker.xuanyuan.me/pgvector/pgvector:pg17` | exit 1：`net/http: request canceled (Client.Timeout exceeded)` |

### 2.3 实际拉取（成功）

```powershell
# 先试 daocloud：8 分钟无层进度，放弃
docker pull docker.m.daocloud.io/pgvector/pgvector:pg17
# 改 1ms：45 秒内完成
docker pull docker.1ms.run/pgvector/pgvector:pg17
```

```text
ee67d2d59980: Pulling fs layer
29edd5ac10d9: Pulling fs layer
7aa71304d2ca: Pulling fs layer
Digest: sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d
Status: Downloaded newer image for docker.1ms.run/pgvector/pgvector:pg17
```

本地重打成规范名（仅本地 tag，内容不变）：

```powershell
docker tag docker.1ms.run/pgvector/pgvector:pg17 pgvector/pgvector:pg17
docker image inspect --format '{{json .RepoDigests}}' pgvector/pgvector:pg17
# ["docker.1ms.run/pgvector/pgvector@sha256:ac08538c…a75d",
#  "pgvector/pgvector@sha256:ac08538c…a75d"]
```

镜像元数据：

| 字段 | 值 |
| --- | --- |
| `Config.Env` | `PG_MAJOR=17`、`PG_VERSION=17.11-1.pgdg12+2`、`GOSU_VERSION=1.19`、`LANG=en_US.utf8` |
| `Size`（镜像元数据） | 160 561 945 B（约 153 MiB，registry blob 口径） |
| `docker images` 体积（本地解压后） | 637 MB |
| `Created` | 2026-10-01T17:35:53Z |

> 直连失败属于**本机网络环境**限制，不是镜像问题；用镜像源拉到的 manifest digest 与上游一致
> （`pgvector/pgvector@sha256:ac08…`）。生产/CI 若要可复现，仍应在能访问 Docker Hub 的环境复核该 digest。

## 3. 一次性容器与验证命令

```powershell
# 临时口令仅存在于当前会话（不写入证据）
docker run -d --name p0-pgvector-probe `
  -e POSTGRES_PASSWORD=<temp> -e POSTGRES_USER=probe -e POSTGRES_DB=probe `
  -p 127.0.0.1:5434:5432 pgvector/pgvector:pg17

docker exec -i p0-pgvector-probe psql -U probe -d probe -v ON_ERROR_STOP=1 -c "…"
```

| 步骤 | 实测 | 耗时（wall，含 docker exec 启动开销） |
| --- | --- | --- |
| `docker run -d` | exit 0 | 4.1 s |
| `pg_isready` 就绪 | ready | 13.3 s（含 run 的 4.1 s） |
| `SELECT version()` | PostgreSQL 17.11 (Debian …) | 795 ms |
| `pg_available_extensions` | `vector / 0.8.7 / (未装)` | 341 ms |
| `CREATE EXTENSION vector` | `CREATE EXTENSION` | 518 ms |
| `SELECT extversion …` | `0.8.7` | 297 ms |
| `'[1,2,3]'::vector` | `[1,2,3]`，`vector_dims=3` | 262 ms |

> wall 时间含 `docker exec` 冷启动约 150–300 ms；真正的服务端耗时见 §4 的 `\timing` 数值。

## 4. 功能验证（真实输出）

### 4.1 扩展与类型

```text
SELECT version();
 PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 12.2.0-14+deb12u1) 12.2.0, 64-bit

SELECT name, default_version, installed_version FROM pg_available_extensions WHERE name='vector';
  name  | default_version | installed_version
 vector | 0.8.7           |

CREATE EXTENSION vector;         → CREATE EXTENSION
SELECT extversion FROM pg_extension WHERE extname='vector';  → 0.8.7
SELECT '[1,2,3]'::vector;        → [1,2,3]（vector_dims=3）
```

### 4.2 表 + 两种索引 + 最近邻查询

```sql
CREATE TABLE p0_probe.items (id bigserial PRIMARY KEY, label text, embedding vector(3) NOT NULL);
INSERT INTO p0_probe.items (label, embedding) VALUES ('a','[1,2,3]'),('b','[1,2,4]'),('c','[9,9,9]');
CREATE INDEX items_hnsw_idx ON p0_probe.items USING hnsw (embedding vector_l2_ops);
```

```text
SELECT id, label, embedding, embedding <-> '[1,2,4]'::vector AS l2
FROM p0_probe.items ORDER BY embedding <-> '[1,2,4]'::vector LIMIT 1;
 id | label | embedding | l2
  2 | b     | [1,2,4]   |  0
```

`EXPLAIN (COSTS OFF)`（`enable_seqscan=off`）：

```text
 Limit
   ->  Index Scan using items_hnsw_idx on items
         Order By: (embedding <-> '[1,2,4]'::vector)

 Limit
   ->  Index Scan using items_ivf_idx on items_ivf
         Order By: (embedding <-> '[1,2,4]'::vector)
```

两种索引都能被优化器用于 `<->` 排序；`ivfflat` 用 `WITH (lists=1)` 在小表上也可建。

### 4.3 距离算子

```text
'[1,2,3]' <-> '[1,2,4]' = 1                      （L2）
'[1,2,3]' <=> '[1,2,4]' = 0.00853986601633272    （cosine distance）
'[1,2,3]' <#> '[1,2,4]' = -17                    （负内积）
'[1,2,3]' <-> '[1,2,3,4]' → ERROR: different vector dimensions 3 and 4
```

### 4.4 5000 × 128 维：构建与查询耗时（`\timing`）

```sql
INSERT INTO p0_probe.big (embedding)
SELECT array_agg(random() ORDER BY g)::vector
FROM generate_series(1,5000) s, generate_series(1,128) g GROUP BY s;   -- 5000 行、5000 个互异向量
```

```text
CREATE INDEX big_hnsw_idx ON p0_probe.big USING hnsw (embedding vector_l2_ops);
Time: 1289.672 ms (00:01.290)        -- 索引 4080 kB

CREATE INDEX big_ivf_idx ON p0_probe.big USING ivfflat (embedding vector_l2_ops) WITH (lists=50);
Time: 115.791 ms                     -- 索引 2920 kB

SET hnsw.ef_search=40;
SELECT … ORDER BY embedding <-> (…) LIMIT 5;      Time: 1.484 ms / 0.938 ms（两次）
SET ivfflat.probes=10;
SELECT … ORDER BY embedding <-> (…) LIMIT 5;      Time: 1.890 ms
```

召回抽检（同一查询，HNSW vs 精确顺序扫描，top-5 id）：

```text
HNSW  top5: 1,52,1662,3512,4496
精确  top5: 1,52,1662,3512,4496      → 5/5 一致（5000×128 随机数据，仅供参考，非业务召回基线）
```

> 说明：首次用 `(SELECT array_agg(random()) …)` 生成时 PostgreSQL 把不相关子查询缓存成了
> **5000 行同一向量**（`count(DISTINCT embedding)=1`），导致 HNSW 与精确 top-5 不一致；
> 改用 `array_agg(random() ORDER BY g) … GROUP BY s` 后 5000 个向量互异，结果才可采信。
> 这条弯路也说明「先验证数据生成本身」是必要的。

### 4.5 维度上限（实测错误信息）

| 场景 | 结果 |
| --- | --- |
| `vector(16000)` 列 | 成功（列类型上限 16000 维） |
| `vector(2000)` + `hnsw` 索引 | 成功 |
| `vector(2001)` + `hnsw` 索引 | `ERROR: column cannot have more than 2000 dimensions for hnsw index` |
| `vector(2001)` + `ivfflat` 索引 | `ERROR: column cannot have more than 2000 dimensions for ivfflat index` |
| `halfvec(4000)` + `hnsw` 索引 | 成功 |
| `halfvec(4001)` + `hnsw` 索引 | `ERROR: column cannot have more than 4000 dimensions for hnsw index` |
| `vector(3072)` 列 + 表达式索引 `((embedding::halfvec(3072)) halfvec_l2_ops)` | 成功 |

**对 T25 的含义**：常见 1536 维可直接建索引；**3072 维（如 OpenAI text-embedding-3-large）
不能直接建 `vector` 索引**，必须用 `halfvec` 表达式索引或降维，且要评估 halfvec 精度对召回的影响。

### 4.6 单列多维度 + 按 profile 偏索引（迁移设计关键）

迁移 004 现在同时有 `embedding_profile` / `embedding_dim`，未来可能并存多种维度。
实测「无维度约束的 `vector` 列 + 每 profile 一个部分表达式索引」可行：

```sql
CREATE TABLE p0_probe.multi (id bigserial PRIMARY KEY, profile text NOT NULL, dim int NOT NULL, embedding vector NOT NULL);
-- 200 行 768 维 + 200 行 1024 维（同一列混存）
CREATE INDEX multi_p768_hnsw  ON p0_probe.multi USING hnsw ((embedding::vector(768))  vector_l2_ops) WHERE profile='p768';
CREATE INDEX multi_p1024_hnsw ON p0_probe.multi USING hnsw ((embedding::vector(1024)) vector_l2_ops) WHERE profile='p1024';
```

```text
EXPLAIN（WHERE profile='p768'）：
 Limit
   ->  Index Scan using multi_p768_hnsw on multi
         Order By: ((embedding)::vector(768) <-> (InitPlan 1).col1)
```

`vector_dims(embedding)` 按行返回 768/1024，均正确。

### 4.7 `double precision[]` → `vector` 迁移路径（针对迁移 004）

迁移 004 现状（只读引用）：`embedding double precision[]` + `embedding_dim` + 约束
`phase0_chunks_embedding_len_check CHECK (COALESCE(array_length(embedding,1),0) = embedding_dim)`。

**直接 `ALTER COLUMN … TYPE vector(3)` 会失败**（实测）：

```text
ERROR:  function array_length(vector, integer) does not exist
        HINT: No function matches the given name and argument types.
```

因为旧的 `array_length()` 约束会在改类型时被重新检查。正确顺序（实测通过）：

```sql
-- 1) 先删数组长度约束（否则 ALTER TYPE 校验旧约束失败）
ALTER TABLE p0_probe.conv DROP CONSTRAINT conv_embedding_len_check;
-- 2) 改类型；同维度数据可直接 USING 转换
ALTER TABLE p0_probe.conv ALTER COLUMN embedding TYPE vector(3) USING embedding::vector;
-- 3) 用 vector_dims() 重建维度约束
ALTER TABLE p0_probe.conv ADD CONSTRAINT conv_embedding_dims_check
  CHECK (vector_dims(embedding) = embedding_dim);
-- 4) 建 ANN 索引
CREATE INDEX conv_hnsw ON p0_probe.conv USING hnsw (embedding vector_l2_ops);
```

```text
STEP2_CONVERT_AND_INDEX: ALTER TABLE ×3 + CREATE INDEX，ID 1/2 的 embedding=[1,2,3]/[1,2,4]，dims=3
STEP3: SELECT count(*) WHERE vector_dims(embedding) <> embedding_dim → 0
```

反向证据：混合维度的表直接转 `vector(3)` 会失败，提示明确：

```text
ALTER TABLE p0_probe.conv2 ALTER COLUMN embedding TYPE vector(3) USING embedding::vector;
ERROR:  expected 3 dimensions, not 4
```

→ 若允许一个表里多种维度，**不要**用定长 `vector(N)` 列，改用无维度 `vector` + 每 profile 部分表达式索引（§4.6 已验证）。

### 4.8 权限与 schema 解析（部署/迁移门禁）

```text
-- 非超级用户（即使是他自己的库 owner）不能装扩展
ERROR:  permission denied to create extension "vector"
HINT:  Must be superuser to create this extension.

-- 扩展默认装进 public；若 search_path 不含 public，vector 类型不可见
SET search_path = p0_probe;
SELECT '[1,2,3]'::vector;
ERROR:  type "vector" does not exist
```

含义：迁移里 `CREATE EXTENSION vector` 必须由**超级用户/受控 DBA 角色**执行一次（或由镜像初始化脚本预装），
业务 schema 的 `search_path` 要包含扩展所在 schema（或 `vector.control` 的 `relocatable=true` 时把扩展装到
专用 schema 并用 `vector` 类型所在的 schema 限定，例如 `extensions.vector`）。

## 5. 许可

| 组件 | 许可 | 证据 |
| --- | --- | --- |
| pgvector（扩展本身） | PostgreSQL License | 容器内 `/usr/share/doc/pgvector/LICENSE` 实读：`Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group` + “Permission to use, copy, modify, and distribute … without fee …” |
| 镜像内 PostgreSQL 17.11 | PostgreSQL License（PGDG Debian 构建） | 同上目录 `/usr/share/doc/postgresql-17/`；Debian 基础层许可见镜像 manifest |
| 镜像 `pgvector/pgvector` | 上游项目为 PostgreSQL License；镜像构建脚本随上游仓库发布 | `vector.control` 位于 `/usr/share/postgresql/17/extension/`，`comment = 'vector data type and ivfflat and hnsw access methods'` |

许可结论：**PostgreSQL License（类 BSD/MIT 的宽松许可），可商用/可分发**，需保留版权声明；
与仓库现有「优先成熟开源」口径一致。**此条不构成法务结论**，正式分发前仍应由 Lead 按 AGENTS.md 记录到许可清单。

## 6. 结论与建议

### 6.1 推荐用于 core profile 的镜像钉法

```yaml
image: pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d
```

- 与现有 core 的 `postgres:17-alpine@sha256:266d3c25…` **同为 PG 17.11**，数据目录 major/minor 兼容路径最省事；
  但由 Alpine(musl) 换成 Debian(glibc) 属**镜像基底变更**，切换前必须按 T02 口径演练：
  备份 → 新容器指向**拷贝**的数据目录 → 启动 → `CREATE EXTENSION` → 冒烟；不能直接拿生产 volume 试。
- 该 digest 是在本机经镜像源取得的 manifest digest；建议 Lead 在能直连 Docker Hub 的环境复核同一 sha256 后再落 `compose.yaml`。
- 镜像体积从 424 MB（postgres:17-alpine）增至 637 MB（本地解压口径），需在环境/CI 文档记录。

### 6.2 迁移 004 若切 `vector` 类型需要的改动（建议清单，不在本任务实施）

1. **扩展安装**：在目标库 `CREATE EXTENSION IF NOT EXISTS vector;`（**需超级用户**，应由受控 DBA/初始化脚本执行，
   不能放进普通 app 迁移的自动执行路径）；固定版本预期 0.8.7。
2. **列类型**：`embedding double precision[]` → `vector(embedding_dim)` 只适用于**单一维度**；
   若允许并存多 profile，改用无维度 `vector` + 每 profile 部分表达式索引（§4.6）。
3. **约束替换**：先 `DROP CONSTRAINT … array_length(embedding,1) …`，转换后改用
   `CHECK (vector_dims(embedding) = embedding_dim)`；`embedding_dim BETWEEN 1 AND 4096` 保留。
4. **数据转换**：`ALTER COLUMN embedding TYPE vector(N) USING embedding::vector`；混合维度必须先分流（§4.7 反向证据）。
5. **索引**：按 profile 建 `hnsw (embedding vector_l2_ops)` 或 `vector_cosine_ops`（二选一，取决于检索口径）；
   维度 >2000 时用 `((embedding::halfvec(N)) halfvec_l2_ops)`；`ivfflat` 需提供 `lists` 且**空表建索引需 ANALYZE**。
6. **search_path / schema**：扩展装在哪就要求业务 schema 能解析该类型；跨 schema 用 `extensions.vector`
   显式限定，并在迁移/连接初始化里固定 `search_path`。
7. **回滚**：`vector → double precision[]` 可用 `USING embedding::real[]`（需补测）；回滚前必须删 ANN 索引。
8. **测试隔离**：`tests/phase0` 的 schema 槽（`p0_t12` 等）需在新的 pgvector 实例上重建；`CREATE EXTENSION`
   是库级对象，波次并行时**不要多个 schema 各装一份**（同一库只装一次，装到固定 schema）。
9. **迁移版本**：pgvector 升级用 `ALTER EXTENSION vector UPDATE;`；`0.8.7` 已是当前镜像默认版本，
   若未来钉新版本需按 `pg_available_extension_versions` 确认升级路径后再改镜像。

### 6.3 T25 依赖该扩展的门禁条件（建议 Lead 直接采用）

T25 的 ANN/召回从 `NOT_RUN` 转 `PASS` 至少需要：

1. `compose.yaml` core profile 的 postgres 换成 §6.1 的 `tag@sha256`，且 `docker compose config --images` 能复现该 digest；
2. 目标库存在 `vector` 扩展且 `extversion` 有记录（例如要求 `>= 0.8`），并记录到环境证据；
3. `embedding_profile` 的每个维度都有实际可用的索引：≤2000 维用 `vector`，>2000 维必须是 `halfvec` 表达式索引，并在证据里写清 `EXPLAIN` 显示 `Index Scan`；
4. 召回评测必须在**真实业务 embedding** 上跑（本文件的 5000×128 随机数据只证明「索引可用」，**不是**召回基线）；
5. ANN 参数（`hnsw.ef_search`、`ivfflat.probes`、`lists`）与索引类型、维度一起写进证据，否则结果不可复现；
6. pgvector 版本与许可进入依赖清单（PostgreSQL License），镜像 digest 进入 `image_digests`。

## 7. NOT_RUN / 未覆盖

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 真实业务 embedding 上的 recall@10 / MRR | NOT_RUN | 探针只验证能力；需要 T25 的评测数据 |
| 10 万级 chunk 的索引构建时间/查询 P95 | NOT_RUN | 本次仅 5000×128 冒烟；容量测试属 T25 性能项 |
| `vector → double precision[]` 回滚 | NOT_RUN | 未实测，建议 T12 在迁移评审时补 |
| pgvector 版本升级（`ALTER EXTENSION UPDATE`） | NOT_RUN | 镜像默认即 0.8.7，无更旧/更新版本可测 |
| Alpine(musl) 版 pgvector 镜像 | NOT_RUN | 上游无 alpine 变体；基底切换风险见 §6.1 |
| `halfvec` 精度对召回的量化影响 | NOT_RUN | 需业务 embedding 与金标准 |
| 并发查询/写入下的索引行为、锁与 WAL 体积 | NOT_RUN | 探针未做并发压测 |
| core volume 直接切换镜像 | NOT_RUN（明确不建议直接做） | 属 T02/运维写权，需备份演练 |

## 8. 清理验证

```text
docker rm -f p0-pgvector-probe            → p0-pgvector-probe（RM_EXIT=0）
docker volume rm d6cabfc1…a474            → 匿名 volume（VOL_RM_EXIT=0）
Get-NetTCPConnection -LocalPort 5434 -State Listen | Measure-Object → Count=0
docker volume ls                         → 回到基线 7 个卷（无 hash 名匿名卷）
docker ps -a                             → 只剩既有容器；pyknora-phase0-postgres-1 / redis-1 均 Up (healthy)
```

- 探针容器的数据目录是镜像声明的匿名 volume（`/var/lib/postgresql/data`），已单独删除；
  **未使用** core 的 `pyknora-phase0_phase0-pgdata`，core 容器全程未重启、未改配置。
- 镜像保留在本地：`pgvector/pgvector:pg17`（`ac08538c6f8b`，637 MB 解压口径）。若需回收：
  `docker rmi pgvector/pgvector:pg17`（本任务保留以便 T25 复跑，是否回收请 Lead 定）。

## 9. 复现步骤

```powershell
# 0) 本机 Docker Hub 直连不可用；走可用镜像源（按 §2.2 结果）
docker pull docker.1ms.run/pgvector/pgvector:pg17
docker tag  docker.1ms.run/pgvector/pgvector:pg17 pgvector/pgvector:pg17
docker image inspect --format '{{json .RepoDigests}}' pgvector/pgvector:pg17

# 1) 一次性容器（独立名字/端口，勿复用 core 容器）
docker run -d --name p0-pgvector-probe -e POSTGRES_PASSWORD=<temp> `
  -e POSTGRES_USER=probe -e POSTGRES_DB=probe -p 127.0.0.1:5434:5432 pgvector/pgvector:pg17
docker exec -i p0-pgvector-probe pg_isready -U probe -d probe

# 2) 能力验证
docker exec -i p0-pgvector-probe psql -U probe -d probe -v ON_ERROR_STOP=1 `
  -c "CREATE EXTENSION vector;" `
  -c "SELECT extversion FROM pg_extension WHERE extname='vector';" `
  -c "SELECT '[1,2,3]'::vector;" `
  -c "CREATE TABLE p0_probe.items (id bigserial PRIMARY KEY, embedding vector(3));" `
  -c "INSERT INTO p0_probe.items (embedding) VALUES ('[1,2,3]'),('[1,2,4]');" `
  -c "CREATE INDEX ON p0_probe.items USING hnsw (embedding vector_l2_ops);" `
  -c "SELECT id FROM p0_probe.items ORDER BY embedding <-> '[1,2,4]'::vector LIMIT 1;"

# 3) 清理（容器 + 匿名 volume）
docker rm -f p0-pgvector-probe
docker volume ls    # 找到该容器的匿名卷后删除；确认 5434 无监听
```

## 10. 残余风险

1. **网络依赖**：本机只能经镜像源取镜像；镜像源可能缓存陈旧或被限流。落 `compose.yaml` 前应在能直连
   Docker Hub 的环境复核 `sha256:ac08538c…`（digest 一致才说明是同一 manifest）。
2. **基底切换**（musl→glibc）：同 PG 17.11 但二进制不同，`pgdata` 复用需按 §6.1 备份演练；不能假设「同版本就能换镜像」。
3. **镜像体积**+213 MB（424→637 MB 解压口径），CI 拉取时间与磁盘需重估。
4. **2000 维索引上限**是硬约束：若产品锁定 3072 维模型，T25 必须走 `halfvec`（精度风险）或降维（效果风险），
   需在 TDD/ADR 里显式决策，不能默认「pgvector 就能 ANN」。
5. **`CREATE EXTENSION` 的超级用户要求**与「业务迁移自动执行」冲突，需 DBA/初始化脚本方案；否则生产迁移会失败。
6. **许可结论**基于容器内 LICENSE 文本，属工程判断，不是法务结论。

## 11. Owner 与复核

- 执行 Owner：`w2-t04-controlsite`（本轮临时探针）
- 复核 Owner：Wave Lead（决定是否据此改 `compose.yaml` / 迁移 004 / T25 门禁）
- 复核要点：①digest 是否在直连环境复核通过；②基底切换演练是否排入 T02/运维；③T25 是否接受 §6.3 的门禁条件。
