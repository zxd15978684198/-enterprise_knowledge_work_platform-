# DocReader proto 副本与生成物版权说明

- 来源：`references/WeKnora/docreader/proto/docreader.proto`
- 上游：Tencent/WeKnora，submodule commit `662abaf9f944e37689d04c42160034b2cdec6829`
- 许可：WeKnora 自身代码为 MIT（`references/WeKnora/LICENSE:1-12`）；
  第三方组件许可见同仓 `THIRD_PARTY_NOTICES.md`，未在此重复声明。
- 校验：4689 字节，sha256 `33d4135cfcd2ec95f8d160d6cefdd54be669167e8b8fb69ef81095d218a738d0`
  （与 P0-T09 `docs/poc/evidence/docreader_manifest.md` §1.1 锚点一致）。
- `proto/docreader.proto`：**逐字节副本**，无任何修改，仅用于生成 gRPC stub 与 wire 证据。
- `generated/*.py`：由 `gen_proto.py` 从该副本生成，属 proto 的派生物；
  protobuf / gRPC 运行时许可由 PyKnora 依赖清单另行记录（本任务未做许可评估）。
- `server.py` **不复制** WeKnora 的 Python 服务实现：它是按 `docreader/main.py` 与
  `docreader/auth.py` 的 wire 语义重新实现的 shim，逐条标注了行号来源，
  用于验证 PyKnora 侧契约，不冒充原服务。
