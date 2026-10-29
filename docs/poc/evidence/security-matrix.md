# P0-T29 安全门禁矩阵（P0 全链授权与内容安全回归）

- 任务：P0-T29（PoC PoC-01/02/03/04/06/09）
- 生成时间：`2026-10-29T05:40:41.691719+00:00`
- 探针执行：**549** 条；拒绝 536；越权/漏放 **0**
- 证据 JSON：`docs/poc/evidence/security-probes.json`
- 基线：branch `feature/w9-t28-t29` head `f7b96760cea986ee1d39f84e9a5b5c28dfc2d14c` dirty=True（dirty_sha256 `0aa24bf32f6a1bf6e8c38bcfcf741c5d0f8ac141fd422fbede25c125176e573b`）

## 1. 矩阵总表

| 类别 | 安全性质 | 探测数 | 拒绝 | 放行 | 越权/漏放 |
| --- | --- | ---: | ---: | ---: | ---: |
| `business_state` | 业务事实状态（终态事件、恢复、引用）写入必须后端重新授权 | 15 | 15 | 0 | 0 |
| `canary` | secret/PII 不进入 trace/state/DTO/异常/日志等任何 sink | 14 | 13 | 1 | 0 |
| `citations` | 伪造/越权/跨空间引用不得被接受，且必须留下拒绝记录 | 101 | 100 | 1 | 0 |
| `cross_workspace` | 跨空间读/写/检索/引用/恢复/工具调用一律拒绝；越权成功 = 0 | 361 | 360 | 1 | 0 |
| `payload` | body/payload/审批参数/引用篡改不得扩大授权 | 20 | 17 | 3 | 0 |
| `revocation` | 撤权后旧 session/token/游标/审批/Job 立即失效，恢复后须重新授权 | 14 | 10 | 4 | 0 |
| `ssrf` | 出站每跳校验，禁止到达非白名单地址（含重定向与内网） | 24 | 21 | 3 | 0 |
| **合计** | 门禁条件：越权/漏放 = 0 | 549 | 536 | 13 | 0 |

- 预算下限：每类 {'cross_workspace': 100, 'revocation': 12, 'payload': 20, 'ssrf': 24, 'canary': 12, 'business_state': 12, 'citations': 100}；总量 100（实测 549）。

## 2. 探测向量与实测

| 类别 | 探测 ID | 探测向量 | 期望 | 实测 | 拒绝 | 证据位置 |
| --- | --- | --- | --- | --- | --- | --- |
| `cross_workspace` | `xw-control-owner-visible` | positive control：A owner 在自己的 Scope 内应看到 A 的 chunk | allow | rows=15 | 否 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_principals-select_one-00` | B 空间 select_one A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-select_list-00` | B 空间 select_list A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-update-00` | B 空间 update A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-delete-00` | B 空间 delete A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_documents-select_one-00` | B 空间 select_one A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-select_list-00` | B 空间 select_list A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-update-00` | B 空间 update A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-delete-00` | B 空间 delete A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_document_versions-select_one-00` | B 空间 select_one A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-select_list-00` | B 空间 select_list A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-update-00` | B 空间 update A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-delete-00` | B 空间 delete A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_chunks-select_one-00` | B 空间 select_one A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-select_list-00` | B 空间 select_list A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-update-00` | B 空间 update A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-delete-00` | B 空间 delete A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_approvals-select_one-00` | B 空间 select_one A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-select_list-00` | B 空间 select_list A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-update-00` | B 空间 update A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-delete-00` | B 空间 delete A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_run_events-select_one-00` | B 空间 select_one A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-select_list-00` | B 空间 select_list A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-update-00` | B 空间 update A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-delete-00` | B 空间 delete A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_jobs-select_one-00` | B 空间 select_one A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-select_list-00` | B 空间 select_list A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-update-00` | B 空间 update A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-delete-00` | B 空间 delete A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_object_refs-select_one-00` | B 空间 select_one A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-select_list-00` | B 空间 select_list A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-update-00` | B 空间 update A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-delete-00` | B 空间 delete A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_citations-select_one-00` | B 空间 select_one A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-select_list-00` | B 空间 select_list A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-update-00` | B 空间 update A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-delete-00` | B 空间 delete A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_principals-select_one-01` | B 空间 select_one A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-select_list-01` | B 空间 select_list A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-update-01` | B 空间 update A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-delete-01` | B 空间 delete A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_documents-select_one-01` | B 空间 select_one A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-select_list-01` | B 空间 select_list A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-update-01` | B 空间 update A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-delete-01` | B 空间 delete A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_document_versions-select_one-01` | B 空间 select_one A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-select_list-01` | B 空间 select_list A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-update-01` | B 空间 update A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-delete-01` | B 空间 delete A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_chunks-select_one-01` | B 空间 select_one A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-select_list-01` | B 空间 select_list A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-update-01` | B 空间 update A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-delete-01` | B 空间 delete A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_approvals-select_one-01` | B 空间 select_one A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-select_list-01` | B 空间 select_list A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-update-01` | B 空间 update A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-delete-01` | B 空间 delete A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_run_events-select_one-01` | B 空间 select_one A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-select_list-01` | B 空间 select_list A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-update-01` | B 空间 update A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-delete-01` | B 空间 delete A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_jobs-select_one-01` | B 空间 select_one A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-select_list-01` | B 空间 select_list A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-update-01` | B 空间 update A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-delete-01` | B 空间 delete A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_object_refs-select_one-01` | B 空间 select_one A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-select_list-01` | B 空间 select_list A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-update-01` | B 空间 update A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-delete-01` | B 空间 delete A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_citations-select_one-01` | B 空间 select_one A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-select_list-01` | B 空间 select_list A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-update-01` | B 空间 update A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-delete-01` | B 空间 delete A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_principals-select_one-02` | B 空间 select_one A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-select_list-02` | B 空间 select_list A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-update-02` | B 空间 update A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-delete-02` | B 空间 delete A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_documents-select_one-02` | B 空间 select_one A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-select_list-02` | B 空间 select_list A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-update-02` | B 空间 update A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-delete-02` | B 空间 delete A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_document_versions-select_one-02` | B 空间 select_one A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-select_list-02` | B 空间 select_list A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-update-02` | B 空间 update A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-delete-02` | B 空间 delete A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_chunks-select_one-02` | B 空间 select_one A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-select_list-02` | B 空间 select_list A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-update-02` | B 空间 update A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-delete-02` | B 空间 delete A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_approvals-select_one-02` | B 空间 select_one A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-select_list-02` | B 空间 select_list A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-update-02` | B 空间 update A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-delete-02` | B 空间 delete A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_run_events-select_one-02` | B 空间 select_one A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-select_list-02` | B 空间 select_list A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-update-02` | B 空间 update A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-delete-02` | B 空间 delete A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_jobs-select_one-02` | B 空间 select_one A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-select_list-02` | B 空间 select_list A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-update-02` | B 空间 update A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-delete-02` | B 空间 delete A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_object_refs-select_one-02` | B 空间 select_one A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-select_list-02` | B 空间 select_list A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-update-02` | B 空间 update A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-delete-02` | B 空间 delete A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_citations-select_one-02` | B 空间 select_one A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-select_list-02` | B 空间 select_list A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-update-02` | B 空间 update A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-delete-02` | B 空间 delete A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_principals-select_one-03` | B 空间 select_one A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-select_list-03` | B 空间 select_list A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-update-03` | B 空间 update A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-delete-03` | B 空间 delete A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_documents-select_one-03` | B 空间 select_one A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-select_list-03` | B 空间 select_list A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-update-03` | B 空间 update A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-delete-03` | B 空间 delete A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_document_versions-select_one-03` | B 空间 select_one A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-select_list-03` | B 空间 select_list A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-update-03` | B 空间 update A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-delete-03` | B 空间 delete A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_chunks-select_one-03` | B 空间 select_one A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-select_list-03` | B 空间 select_list A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-update-03` | B 空间 update A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-delete-03` | B 空间 delete A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_approvals-select_one-03` | B 空间 select_one A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-select_list-03` | B 空间 select_list A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-update-03` | B 空间 update A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-delete-03` | B 空间 delete A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_run_events-select_one-03` | B 空间 select_one A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-select_list-03` | B 空间 select_list A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-update-03` | B 空间 update A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-delete-03` | B 空间 delete A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_jobs-select_one-03` | B 空间 select_one A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-select_list-03` | B 空间 select_list A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-update-03` | B 空间 update A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-delete-03` | B 空间 delete A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_object_refs-select_one-03` | B 空间 select_one A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-select_list-03` | B 空间 select_list A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-update-03` | B 空间 update A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-delete-03` | B 空间 delete A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_citations-select_one-03` | B 空间 select_one A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-select_list-03` | B 空间 select_list A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-update-03` | B 空间 update A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-delete-03` | B 空间 delete A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_principals-select_one-04` | B 空间 select_one A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-select_list-04` | B 空间 select_list A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-update-04` | B 空间 update A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-delete-04` | B 空间 delete A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_documents-select_one-04` | B 空间 select_one A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-select_list-04` | B 空间 select_list A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-update-04` | B 空间 update A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-delete-04` | B 空间 delete A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_document_versions-select_one-04` | B 空间 select_one A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-select_list-04` | B 空间 select_list A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-update-04` | B 空间 update A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-delete-04` | B 空间 delete A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_chunks-select_one-04` | B 空间 select_one A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-select_list-04` | B 空间 select_list A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-update-04` | B 空间 update A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-delete-04` | B 空间 delete A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_approvals-select_one-04` | B 空间 select_one A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-select_list-04` | B 空间 select_list A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-update-04` | B 空间 update A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-delete-04` | B 空间 delete A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_run_events-select_one-04` | B 空间 select_one A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-select_list-04` | B 空间 select_list A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-update-04` | B 空间 update A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-delete-04` | B 空间 delete A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_jobs-select_one-04` | B 空间 select_one A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-select_list-04` | B 空间 select_list A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-update-04` | B 空间 update A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-delete-04` | B 空间 delete A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_object_refs-select_one-04` | B 空间 select_one A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-select_list-04` | B 空间 select_list A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-update-04` | B 空间 update A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-delete-04` | B 空间 delete A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_citations-select_one-04` | B 空间 select_one A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-select_list-04` | B 空间 select_list A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-update-04` | B 空间 update A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-delete-04` | B 空间 delete A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_principals-select_one-05` | B 空间 select_one A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-select_list-05` | B 空间 select_list A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-update-05` | B 空间 update A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-delete-05` | B 空间 delete A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_documents-select_one-05` | B 空间 select_one A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-select_list-05` | B 空间 select_list A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-update-05` | B 空间 update A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-delete-05` | B 空间 delete A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_document_versions-select_one-05` | B 空间 select_one A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-select_list-05` | B 空间 select_list A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-update-05` | B 空间 update A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-delete-05` | B 空间 delete A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_chunks-select_one-05` | B 空间 select_one A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-select_list-05` | B 空间 select_list A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-update-05` | B 空间 update A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-delete-05` | B 空间 delete A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_approvals-select_one-05` | B 空间 select_one A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-select_list-05` | B 空间 select_list A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-update-05` | B 空间 update A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-delete-05` | B 空间 delete A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_run_events-select_one-05` | B 空间 select_one A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-select_list-05` | B 空间 select_list A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-update-05` | B 空间 update A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-delete-05` | B 空间 delete A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_jobs-select_one-05` | B 空间 select_one A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-select_list-05` | B 空间 select_list A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-update-05` | B 空间 update A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-delete-05` | B 空间 delete A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_object_refs-select_one-05` | B 空间 select_one A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-select_list-05` | B 空间 select_list A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-update-05` | B 空间 update A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-delete-05` | B 空间 delete A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_citations-select_one-05` | B 空间 select_one A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-select_list-05` | B 空间 select_list A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-update-05` | B 空间 update A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-delete-05` | B 空间 delete A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_principals-select_one-06` | B 空间 select_one A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-select_list-06` | B 空间 select_list A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-update-06` | B 空间 update A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-delete-06` | B 空间 delete A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_documents-select_one-06` | B 空间 select_one A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-select_list-06` | B 空间 select_list A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-update-06` | B 空间 update A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-delete-06` | B 空间 delete A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_document_versions-select_one-06` | B 空间 select_one A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-select_list-06` | B 空间 select_list A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-update-06` | B 空间 update A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-delete-06` | B 空间 delete A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_chunks-select_one-06` | B 空间 select_one A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-select_list-06` | B 空间 select_list A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-update-06` | B 空间 update A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-delete-06` | B 空间 delete A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_approvals-select_one-06` | B 空间 select_one A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-select_list-06` | B 空间 select_list A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-update-06` | B 空间 update A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-delete-06` | B 空间 delete A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_run_events-select_one-06` | B 空间 select_one A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-select_list-06` | B 空间 select_list A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-update-06` | B 空间 update A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-delete-06` | B 空间 delete A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_jobs-select_one-06` | B 空间 select_one A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-select_list-06` | B 空间 select_list A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-update-06` | B 空间 update A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-delete-06` | B 空间 delete A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_object_refs-select_one-06` | B 空间 select_one A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-select_list-06` | B 空间 select_list A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-update-06` | B 空间 update A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-delete-06` | B 空间 delete A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_citations-select_one-06` | B 空间 select_one A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-select_list-06` | B 空间 select_list A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-update-06` | B 空间 update A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-delete-06` | B 空间 delete A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_principals-select_one-07` | B 空间 select_one A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-select_list-07` | B 空间 select_list A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-update-07` | B 空间 update A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-delete-07` | B 空间 delete A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_documents-select_one-07` | B 空间 select_one A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-select_list-07` | B 空间 select_list A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-update-07` | B 空间 update A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-delete-07` | B 空间 delete A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_document_versions-select_one-07` | B 空间 select_one A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-select_list-07` | B 空间 select_list A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-update-07` | B 空间 update A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-delete-07` | B 空间 delete A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_chunks-select_one-07` | B 空间 select_one A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-select_list-07` | B 空间 select_list A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-update-07` | B 空间 update A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-delete-07` | B 空间 delete A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_approvals-select_one-07` | B 空间 select_one A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-select_list-07` | B 空间 select_list A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-update-07` | B 空间 update A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-delete-07` | B 空间 delete A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_run_events-select_one-07` | B 空间 select_one A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-select_list-07` | B 空间 select_list A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-update-07` | B 空间 update A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-delete-07` | B 空间 delete A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_jobs-select_one-07` | B 空间 select_one A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-select_list-07` | B 空间 select_list A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-update-07` | B 空间 update A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-delete-07` | B 空间 delete A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_object_refs-select_one-07` | B 空间 select_one A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-select_list-07` | B 空间 select_list A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-update-07` | B 空间 update A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-delete-07` | B 空间 delete A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_citations-select_one-07` | B 空间 select_one A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-select_list-07` | B 空间 select_list A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-update-07` | B 空间 update A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-delete-07` | B 空间 delete A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_principals-select_one-08` | B 空间 select_one A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-select_list-08` | B 空间 select_list A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-update-08` | B 空间 update A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-delete-08` | B 空间 delete A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_documents-select_one-08` | B 空间 select_one A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-select_list-08` | B 空间 select_list A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-update-08` | B 空间 update A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-delete-08` | B 空间 delete A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_document_versions-select_one-08` | B 空间 select_one A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-select_list-08` | B 空间 select_list A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-update-08` | B 空间 update A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-delete-08` | B 空间 delete A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_chunks-select_one-08` | B 空间 select_one A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-select_list-08` | B 空间 select_list A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-update-08` | B 空间 update A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-delete-08` | B 空间 delete A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_approvals-select_one-08` | B 空间 select_one A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-select_list-08` | B 空间 select_list A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-update-08` | B 空间 update A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-delete-08` | B 空间 delete A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_run_events-select_one-08` | B 空间 select_one A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-select_list-08` | B 空间 select_list A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-update-08` | B 空间 update A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-delete-08` | B 空间 delete A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_jobs-select_one-08` | B 空间 select_one A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-select_list-08` | B 空间 select_list A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-update-08` | B 空间 update A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-delete-08` | B 空间 delete A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_object_refs-select_one-08` | B 空间 select_one A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-select_list-08` | B 空间 select_list A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-update-08` | B 空间 update A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-delete-08` | B 空间 delete A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_citations-select_one-08` | B 空间 select_one A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-select_list-08` | B 空间 select_list A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-update-08` | B 空间 update A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-delete-08` | B 空间 delete A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_principals-select_one-09` | B 空间 select_one A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-select_list-09` | B 空间 select_list A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-update-09` | B 空间 update A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_principals-delete-09` | B 空间 delete A 的 victim principal | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_principals` |
| `cross_workspace` | `xw-phase0_documents-select_one-09` | B 空间 select_one A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-select_list-09` | B 空间 select_list A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-update-09` | B 空间 update A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_documents-delete-09` | B 空间 delete A 的 victim document | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_documents` |
| `cross_workspace` | `xw-phase0_document_versions-select_one-09` | B 空间 select_one A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-select_list-09` | B 空间 select_list A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-update-09` | B 空间 update A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_document_versions-delete-09` | B 空间 delete A 的 victim retired version | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_document_versions` |
| `cross_workspace` | `xw-phase0_chunks-select_one-09` | B 空间 select_one A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-select_list-09` | B 空间 select_list A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-update-09` | B 空间 update A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_chunks-delete-09` | B 空间 delete A 的 victim chunk | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_chunks` |
| `cross_workspace` | `xw-phase0_approvals-select_one-09` | B 空间 select_one A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-select_list-09` | B 空间 select_list A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-update-09` | B 空间 update A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_approvals-delete-09` | B 空间 delete A 的 victim approval | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_approvals` |
| `cross_workspace` | `xw-phase0_run_events-select_one-09` | B 空间 select_one A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-select_list-09` | B 空间 select_list A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-update-09` | B 空间 update A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_run_events-delete-09` | B 空间 delete A 的 victim run event | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_run_events` |
| `cross_workspace` | `xw-phase0_jobs-select_one-09` | B 空间 select_one A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-select_list-09` | B 空间 select_list A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-update-09` | B 空间 update A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_jobs-delete-09` | B 空间 delete A 的 victim job | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_jobs` |
| `cross_workspace` | `xw-phase0_object_refs-select_one-09` | B 空间 select_one A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-select_list-09` | B 空间 select_list A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-update-09` | B 空间 update A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_object_refs-delete-09` | B 空间 delete A 的 victim object ref | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_object_refs` |
| `cross_workspace` | `xw-phase0_citations-select_one-09` | B 空间 select_one A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-select_list-09` | B 空间 select_list A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-update-09` | B 空间 update A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `cross_workspace` | `xw-phase0_citations-delete-09` | B 空间 delete A 的 victim citation | deny | rows=0 affected=0 | 是 | `p0_t29.phase0_citations` |
| `revocation` | `rev-baseline-live` | 撤权前 session 可读（正向对照） | allow | allowed | 否 | `p0_t29.phase0_principals` |
| `revocation` | `rev-disabled-scope` | 撤权后复用旧 Scope 读 chunk | deny | denied:NO_VISIBLE_ROWS | 是 | `p0_t29.phase0_principals` |
| `revocation` | `rev-disabled-citation` | 撤权后复用旧 citation | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_principals` |
| `revocation` | `rev-disabled-job` | 撤权后 Job 恢复 | deny | denied:PRINCIPAL_DISABLED | 是 | `p0_t29.phase0_principals` |
| `revocation` | `rev-stale-token` | 撤权后旧 session token | deny | denied:PRINCIPAL_DISABLED | 是 | `p0_t29.phase0_principals` |
| `revocation` | `rev-expired-token` | 过期 session token | deny | denied:TOKEN_EXPIRED | 是 | `p0_t29.phase0_principals` |
| `revocation` | `rev-tampered-token` | 自签未登记 session（攻击者本地伪造） | deny | denied:TOKEN_UNKNOWN | 是 | `p0_t29.phase0_principals` |
| `revocation` | `rev-stale-approval` | 撤权后复用旧审批 | deny | denied:APPROVAL_PRINCIPAL_INVALID | 是 | `p0_t29.phase0_principals` |
| `revocation` | `rev-old-cursor` | 撤权后旧游标 | deny | denied:CURSOR_FORGED | 是 | `p0_t29.phase0_principals` |
| `revocation` | `rev-kb-grant` | KB 授权撤销后检索 | deny | denied:sqlstate=00000 | 是 | `p0_t29.phase0_principals` |
| `revocation` | `rev-forged-scope-header` | body 自报 workspace/principal | deny | denied:SCOPE_CLAIM_IGNORED | 是 | `p0_t29.phase0_principals` |
| `revocation` | `rev-restored-scope` | 恢复后新 session 放行 | allow | allowed | 否 | `p0_t29.phase0_principals` |
| `revocation` | `rev-restored-citation` | 恢复后新引用放行 | allow | allowed | 否 | `p0_t29.phase0_principals` |
| `revocation` | `rev-restored-job` | 恢复后 Job 恢复放行 | allow | allowed | 否 | `p0_t29.phase0_principals` |
| `payload` | `pl-approval-control` | 正向对照：完整审批放行 | allow | allowed | 否 | `p0_t29.phase0_approvals` |
| `payload` | `pl-approval-operation` | 工具操作/参数版本漂移 | deny | denied:TOOL_APPROVAL_MISMATCH | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-approval-expired` | 审批已过期 | deny | denied:TOOL_APPROVAL_EXPIRED | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-approval-status` | 审批未批准（pending） | deny | denied:TOOL_APPROVAL_MISMATCH | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-db-control` | DB 审批逐项比对正向对照 | allow | allowed | 否 | `p0_t29.phase0_approvals` |
| `payload` | `pl-db-intent` | 篡改审批 intent_key | deny | denied:APPROVAL_INTENT_MISMATCH | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-db-operation` | 篡改审批 operation | deny | denied:APPROVAL_OPERATION_MISMATCH | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-db-scope` | 篡改审批 scope_hash | deny | denied:APPROVAL_SCOPE_MISMATCH | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-db-idempotency` | 篡改幂等键 | deny | denied:APPROVAL_IDEMPOTENCY_MISMATCH | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-db-run` | 审批绑定到其它 run | deny | denied:sqlstate=23503 | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-db-status` | 审批状态非 approved | deny | denied:APPROVAL_STATUS_INVALID | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-db-expired` | 审批过期 | deny | denied:APPROVAL_EXPIRED | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-db-missing` | 审批记录不存在 | deny | denied:APPROVAL_MISSING | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-db-approver-disabled` | 审批人撤权 | deny | denied:APPROVAL_PRINCIPAL_INVALID | 是 | `p0_t29.phase0_approvals` |
| `payload` | `pl-citation-control` | 正向对照：合法 handle 放行 | allow | allowed | 否 | `p0_t29.phase0_citations` |
| `payload` | `pl-citation-cross-space` | 篡改引用指向 B 空间 chunk | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `payload` | `pl-citation-stale` | 篡改引用指向 retired 旧版本 chunk | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `payload` | `pl-citation-unknown` | 篡改引用指向不存在 chunk | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `payload` | `pl-citation-handle` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `payload` | `pl-citation-duplicate` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `ssrf` | `ssrf-allow-literal` | 出站请求 http://127.0.0.1:5600/ | allow | reached:200 hops=1 | 否 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-allow-localhost` | 出站请求 http://localhost:5600/ | allow | reached:200 hops=1 | 否 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-allow-redirect` | 出站请求 http://127.0.0.1:5600/redirect/2 | allow | reached:200 hops=3 | 否 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-scheme-file` | 出站请求 file:///etc/passwd | deny | blocked:SCHEME | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-scheme-gopher` | 出站请求 gopher://127.0.0.1:6379/_PING | deny | blocked:SCHEME | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-scheme-ftp` | 出站请求 ftp://127.0.0.1/ | deny | blocked:SCHEME | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-userinfo` | 出站请求 http://pyknora:secret@127.0.0.1:5600/ | deny | blocked:USERINFO | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-port` | 出站请求 http://127.0.0.1:22/ | deny | blocked:PORT | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-port-pg` | 出站请求 http://127.0.0.1:5433/ | deny | blocked:PORT | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-metadata` | 出站请求 http://169.254.169.254/latest/meta-data/ | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-private-10` | 出站请求 http://10.0.0.5/ | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-private-172` | 出站请求 http://172.16.3.4/ | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-private-192` | 出站请求 http://192.168.1.10/ | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-loopback-alt` | 出站请求 http://127.0.0.2:5600/ | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-unspecified` | 出站请求 http://0.0.0.0:5600/ | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-ipv6` | 出站请求 http://[::1]:5600/ | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-redirect-metadata` | 出站请求 http://127.0.0.1:5600/redirect-metadata | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-redirect-private` | 出站请求 http://127.0.0.1:5600/redirect-private | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-redirect-loop` | 出站请求 http://127.0.0.1:5600/redirect-loop | deny | blocked:REDIRECT_LIMIT | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-redirect-file` | 出站请求 http://127.0.0.1:5600/redirect-file | deny | blocked:SCHEME | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-external` | 出站请求 http://example.com/ | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-decimal-ip` | 出站请求 http://2130706433:5600/ | deny | blocked:HOST | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-slow` | 出站请求 http://127.0.0.1:5600/slow | deny | blocked:TimeoutError | 是 | `p0_t29.phase0_object_refs` |
| `ssrf` | `ssrf-block-huge` | 出站请求 http://127.0.0.1:5600/huge | deny | blocked:BODY_TOO_LARGE | 是 | `p0_t29.phase0_object_refs` |
| `canary` | `canary-trace-secret` | secret 写入 trace note_code | deny | denied:CANARY_CLEAN | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-trace-auth` | Authorization 值写入 trace | deny | denied:CANARY_CLEAN | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-trace-pii-email` | PII 邮箱写入 trace | deny | denied:CANARY_CLEAN | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-trace-pii-phone` | PII 手机号写入 trace | deny | denied:CANARY_CLEAN | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-trace-dsn` | DSN 写入 trace | deny | denied:CANARY_CLEAN | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-forbidden-field` | 直接写 prompt 字段 | deny | denied:TRACE_FIELD_REJECTED | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-state-fragment` | state fragment 含 secret | deny | denied:CANARY_CLEAN | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-error-message` | 异常消息回显 secret | deny | denied:CANARY_CLEAN | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-dto` | 安全 DTO（trace 序列化 + state fragment）不含 secret/Auth | deny | denied:CANARY_CLEAN | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-dsn-redaction` | DSN 证据脱敏 | deny | denied:DSN_REDACTED | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-hmac-digest` | HMAC 摘要不含明文 | deny | denied:CANARY_CLEAN | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-correlation-id` | 可记录相关摘要前缀 | deny | denied:CANARY_CLEAN | 是 | `safe_trace.SafeTrace` |
| `canary` | `canary-full-sink-scan` | trace 事件 + state fragment 全量扫描 canary | deny | canary_hits=0 | 是 | `safe_trace.SafeTrace.to_state_fragment` |
| `business_state` | `bs-event-no-scope` | 无 Scope 写终态事件 | deny | denied:sqlstate=42501 | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-event-cross-workspace` | B Scope 写 A 的事件 | deny | denied:sqlstate=42501 | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-event-forged-principal` | payload 自报 principal | deny | denied:sqlstate=42501 | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-event-terminal` | 重复终态事件 | deny | denied:sqlstate=23505 | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-citation-revoked` | 撤权 principal 引用 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-citation-cross-insert` | B Scope 插入 A 的引用 | deny | denied:sqlstate=42501 | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-object-cross-workspace` | B 空间取 A 的对象签名 | deny | denied:OBJECT_NOT_IN_SCOPE | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-object-expired` | 过期对象签名 | deny | denied:OBJECT_SIGNATURE_EXPIRED | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-object-method` | 签名未覆盖的方法 | deny | denied:OBJECT_METHOD_MISMATCH | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-object-forged` | 伪造对象签名 | deny | denied:OBJECT_SIGNATURE_MISMATCH | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-object-forged-key` | 路径穿越 object_key | deny | denied:OBJECT_NOT_IN_SCOPE | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-job-cross-workspace` | B 空间恢复 A 的 Job | deny | denied:JOB_NOT_IN_SCOPE | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-job-payload-workspace` | Job payload 篡改 workspace | deny | denied:JOB_PAYLOAD_MISMATCH | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-job-payload-principal` | Job payload 篡改 principal | deny | denied:JOB_PAYLOAD_MISMATCH | 是 | `p0_t29.phase0_run_events` |
| `business_state` | `bs-kb-revoked-retrieval` | KB 授权撤销后检索 | deny | denied:sqlstate=00000 | 是 | `p0_t29.phase0_run_events` |
| `citations` | `ci-control` | 正向对照：合法 handle 被接受 | allow | allowed | 否 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-handle-00` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-chunk-ref-00` | chunk 引用不存在 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-cross-workspace-chunk-00` | chunk 属于 B 空间 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-retired-version-00` | chunk 属于 retired 旧版本 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-revoked-kb-grant-00` | chunk 属于已撤销 KB 授权 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-not-in-evidence-00` | chunk 可见但不在本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-duplicate-handle-00` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-out-of-range-00` | handle 大于 evidence 长度 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-mixed-valid-forged-00` | 合法 handle + 伪造 chunk 混排 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-zero-handle-00` | handle 0（越界） | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-handle-01` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-chunk-ref-01` | chunk 引用不存在 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-cross-workspace-chunk-01` | chunk 属于 B 空间 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-retired-version-01` | chunk 属于 retired 旧版本 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-revoked-kb-grant-01` | chunk 属于已撤销 KB 授权 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-not-in-evidence-01` | chunk 可见但不在本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-duplicate-handle-01` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-out-of-range-01` | handle 大于 evidence 长度 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-mixed-valid-forged-01` | 合法 handle + 伪造 chunk 混排 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-zero-handle-01` | handle 0（越界） | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-handle-02` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-chunk-ref-02` | chunk 引用不存在 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-cross-workspace-chunk-02` | chunk 属于 B 空间 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-retired-version-02` | chunk 属于 retired 旧版本 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-revoked-kb-grant-02` | chunk 属于已撤销 KB 授权 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-not-in-evidence-02` | chunk 可见但不在本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-duplicate-handle-02` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-out-of-range-02` | handle 大于 evidence 长度 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-mixed-valid-forged-02` | 合法 handle + 伪造 chunk 混排 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-zero-handle-02` | handle 0（越界） | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-handle-03` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-chunk-ref-03` | chunk 引用不存在 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-cross-workspace-chunk-03` | chunk 属于 B 空间 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-retired-version-03` | chunk 属于 retired 旧版本 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-revoked-kb-grant-03` | chunk 属于已撤销 KB 授权 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-not-in-evidence-03` | chunk 可见但不在本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-duplicate-handle-03` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-out-of-range-03` | handle 大于 evidence 长度 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-mixed-valid-forged-03` | 合法 handle + 伪造 chunk 混排 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-zero-handle-03` | handle 0（越界） | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-handle-04` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-chunk-ref-04` | chunk 引用不存在 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-cross-workspace-chunk-04` | chunk 属于 B 空间 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-retired-version-04` | chunk 属于 retired 旧版本 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-revoked-kb-grant-04` | chunk 属于已撤销 KB 授权 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-not-in-evidence-04` | chunk 可见但不在本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-duplicate-handle-04` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-out-of-range-04` | handle 大于 evidence 长度 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-mixed-valid-forged-04` | 合法 handle + 伪造 chunk 混排 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-zero-handle-04` | handle 0（越界） | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-handle-05` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-chunk-ref-05` | chunk 引用不存在 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-cross-workspace-chunk-05` | chunk 属于 B 空间 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-retired-version-05` | chunk 属于 retired 旧版本 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-revoked-kb-grant-05` | chunk 属于已撤销 KB 授权 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-not-in-evidence-05` | chunk 可见但不在本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-duplicate-handle-05` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-out-of-range-05` | handle 大于 evidence 长度 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-mixed-valid-forged-05` | 合法 handle + 伪造 chunk 混排 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-zero-handle-05` | handle 0（越界） | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-handle-06` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-chunk-ref-06` | chunk 引用不存在 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-cross-workspace-chunk-06` | chunk 属于 B 空间 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-retired-version-06` | chunk 属于 retired 旧版本 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-revoked-kb-grant-06` | chunk 属于已撤销 KB 授权 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-not-in-evidence-06` | chunk 可见但不在本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-duplicate-handle-06` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-out-of-range-06` | handle 大于 evidence 长度 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-mixed-valid-forged-06` | 合法 handle + 伪造 chunk 混排 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-zero-handle-06` | handle 0（越界） | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-handle-07` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-chunk-ref-07` | chunk 引用不存在 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-cross-workspace-chunk-07` | chunk 属于 B 空间 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-retired-version-07` | chunk 属于 retired 旧版本 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-revoked-kb-grant-07` | chunk 属于已撤销 KB 授权 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-not-in-evidence-07` | chunk 可见但不在本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-duplicate-handle-07` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-out-of-range-07` | handle 大于 evidence 长度 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-mixed-valid-forged-07` | 合法 handle + 伪造 chunk 混排 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-zero-handle-07` | handle 0（越界） | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-handle-08` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-chunk-ref-08` | chunk 引用不存在 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-cross-workspace-chunk-08` | chunk 属于 B 空间 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-retired-version-08` | chunk 属于 retired 旧版本 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-revoked-kb-grant-08` | chunk 属于已撤销 KB 授权 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-not-in-evidence-08` | chunk 可见但不在本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-duplicate-handle-08` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-out-of-range-08` | handle 大于 evidence 长度 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-mixed-valid-forged-08` | 合法 handle + 伪造 chunk 混排 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-zero-handle-08` | handle 0（越界） | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-handle-09` | handle 超出本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-unknown-chunk-ref-09` | chunk 引用不存在 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-cross-workspace-chunk-09` | chunk 属于 B 空间 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-retired-version-09` | chunk 属于 retired 旧版本 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-revoked-kb-grant-09` | chunk 属于已撤销 KB 授权 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-not-in-evidence-09` | chunk 可见但不在本轮 evidence | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-duplicate-handle-09` | 重复 handle | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-out-of-range-09` | handle 大于 evidence 长度 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-mixed-valid-forged-09` | 合法 handle + 伪造 chunk 混排 | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `citations` | `ci-zero-handle-09` | handle 0（越界） | deny | denied:CITATION_REJECTED | 是 | `p0_t29.phase0_citations` |
| `canary` | `redis-atomic-counter` | Redis 原子计数（T29 资源槽 DB15；W9 指定 DB16 超上限） | allow | rounds=10 ok=10 value=10 | 否 | `redis:db15:p0_t29:counter` |

## 3. 拒绝证据

- 越权/漏放记录：**0 条**（全部探测符合期望）。

### 3.2 schema 与角色快照（拒绝来自数据库强制而非口头声明）

- schema `p0_t29`，迁移 `t29_security_inline`，DDL sha256 `f1ae6e4fbc81e65c2acf7d631b5d223fec8337c706356470b4049ac4313dbe33`
- schema 快照：```{"functions": [{"name": "phase0_current_principal_id", "security_definer": false, "volatility": "s"}, {"name": "phase0_current_workspace_id", "security_definer": false, "volatility": "s"}, {"name": "phase0_events_append_only", "security_definer": false, "volatility": "v"}, {"name": "phase0_principal_active", "security_definer": true, "volatility": "s"}, {"name": "phase0_run_event_guard", "security_definer": false, "volatility": "v"}], "ledger": {"filename": "inline:security_probe.py::MIGRATION_SQL", "migration_id": "t29_security_inline", "sha256": "f1ae6e4fbc81e65c2acf7d631b5d223fec8337c706356470b4049ac4313dbe33"}, "policies": [{"cmd": "*", "name": "phase0_approvals_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_bindings_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_chunks_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_citations_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_documents_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_jobs_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_kb_grants_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_object_refs_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_principals_scope", "qual": "(workspace_id = p0_t29.phase0_current_workspace_id())", "with_check": "(workspace_id = p0_t29.phase0_current_workspace_id())"}, {"cmd": "*", "name": "phase0_run_events_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_runs_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_versions_scope", "qual": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())", "with_check": "((workspace_id = p0_t29.phase0_current_workspace_id()) AND p0_t29.phase0_principal_active())"}, {"cmd": "*", "name": "phase0_workspaces_scope", "qual": "(workspace_id = p0_t29.phase0_current_workspace_id())", "with_check": "(workspace_id = p0_t29.phase0_current_workspace_id())"}], "tables": {"phase0_approvals": [true, true], "phase0_chunks": [true, true], "phase0_citations": [true, true], "phase0_document_versions": [true, true], "phase0_documents": [true, true], "phase0_execution_bindings": [true, true], "phase0_jobs": [true, true], "phase0_kb_grants": [true, true], "phase0_object_refs": [true, true], "phase0_principals": [true, true], "phase0_run_events": [true, true], "phase0_runs": [true, true], "phase0_schema_migrations": [false, false], "phase0_workspaces": [true, true]}}```
- 应用角色属性：```{"attributes": {"rolbypassrls": false, "rolcanlogin": true, "rolcreatedb": false, "rolcreaterole": false, "rolsuper": false}, "current_user": "pyknora", "owns_tables": 0, "session_user": "pyknora"}```

### 3.3 种子与 Redis 事实

- seed：```{"chunks": 21, "documents": 6, "jobs": 1, "runs": 2, "stale_chunks": 3, "workspaces": {"A": "f5c43f62-702a-5b70-a5e2-9d6ba398f3fe", "B": "c7dcb9f2-5d2f-5336-b6bc-393ba49a008b", "C": "8c9867ba-1b22-5240-be65-66bff860187f"}}```
- Redis（DB15）：```{"atomic_ok": true, "cleaned": 1, "db": 15, "rounds": 10}```

## 4. 覆盖缺口与 NOT_RUN

- `NOT_RUN` **真实 chat 模型链路**：禁止联网/禁止真实 key；本任务不含模型语义段
- `NOT_RUN` **真实身份提供商登录**：C-07 BLOCKED：身份域产品+安全+架构决策未定
- `NOT_RUN` **真实对象存储签名代理**：无 MinIO/S3 环境；只用合成 HMAC 签名口径
- `NOT_RUN` **真实 DNS 重绑定攻击**：本机无受控 DNS；用 host 白名单 + IP 校验替身
- `NOT_RUN` **pgvector ANN 检索越权**：ANN 属 T25 范围且本任务镜像精确扫描口径
- `RESOURCE_DEVIATION` **Redis 槽位**：W9 波次记录指定 Redis DB16，但服务器 CONFIG GET databases=16 只支持索引 0-15，实测 DB16 不可用（ERR DB index is out of range）；改用空闲 DB15，运行前后验证 DB0 仅保留其它任务的 t02:probe 键、DB1-DB14 无键。
- 结论口径：无真实模型/身份提供商，本报告最高判定为 `PASS（限域）`；不得据此宣称生产安全已验收。
- T26 引用基线只用于输入完整性（`sample_count=100`、结构测量），不作为安全验收依据；伪造/越权引用矩阵由本任务自建种子实测。
- T26 基线元数据：```{"available": true, "cases_embedded": 20, "citation_accuracy": 1.0, "fabricated_citations": 0, "note": "T26 基线是结构测量（ModelPort 替身），不能当作安全验收；T29 用自建种子重放 100 例伪造/越权引用", "sample_count": 100, "unauthorized_citations": 0}```

## 5. 对 T31 的输入

- 复用本报告探测向量与「安全 = 拒绝」判定约定，构建 P0 回归门禁：跨空间矩阵、撤权窗口、载荷/审批篡改、SSRF 每跳校验、canary 全 sink、业务事实状态授权。
- 固化两个判定助手：`_expect_denied`（系统抛拒绝 = 安全）与 `_citation_guard`（全量接受 = 越权），避免双向 raise 造成的假 PASS。
- 生产化前置项：受限 worker 角色（SECURITY DEFINER owner 不得是超级用户）、T12/T13 真表 RLS、真实 IdP/MinIO/模型链路的端到端负例。

