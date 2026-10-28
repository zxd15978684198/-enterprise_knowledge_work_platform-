"""P0-T30 备份/还原/迁移/版本回滚演练工具。

设计边界（与任务卡一致）：

* **只演练、不新增编号迁移**：本包不产出 `db/migrations/*.sql`，迁移文件全部只读。
* **不碰 core 卷**：所有演练都在独立临时 database（`p0_t30_*`，由 `template0` 新建）
  与 ``%TEMP%`` 目录内进行，``cleanup_all()`` 负责删库删文件；``pyknora`` 库与
  ``pgdata``/``redisdata`` 卷不在本包的任何写入路径上。
* **单实例语义**：T05 判定为 PARTIAL（原生 ``PostgresSaver`` 无法 fence，结论是
  「单活 runner」），因此本包只验证单实例的备份/还原/迁移/回滚；多实例切换、
  leader election、RPO/RTO 校准一律不做，证据里记 ``NOT_RUN``。

模块划分：

``pgctl``       数据库生命周期与 ``pg_dump``/``pg_restore`` 封装（含故障注入原语）
``migrations``  001–008 迁移清单、apply 与重复 apply 的实测结论
``seed``        代表性业务事实与 T05 checkpoint 的确定性种入
``inventory``   逐表行数 + 关键指针快照与差异比对
``drill``       全流程编排（备份→还原→校验→回滚→故障注入）与 CLI
"""

from __future__ import annotations

from .drill import DrillReport, run_full_drill
from .inventory import Snapshot, diff_snapshots, snapshot
from .migrations import MigrationFile, discover_migrations
from .pgctl import (
    BackupInterrupted,
    DatabaseStateError,
    RestoreConflict,
    RestoreInterrupted,
    RestoreProbeError,
    redact_dsn,
)

__all__ = [
    "BackupInterrupted",
    "DatabaseStateError",
    "DrillReport",
    "MigrationFile",
    "RestoreConflict",
    "RestoreInterrupted",
    "RestoreProbeError",
    "Snapshot",
    "diff_snapshots",
    "discover_migrations",
    "redact_dsn",
    "run_full_drill",
    "snapshot",
]
