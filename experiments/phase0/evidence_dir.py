"""证据目录解析：默认 ``docs/poc/evidence``，可用 ``PYKNORA_EVIDENCE_DIR`` 覆盖。

为什么需要这层间接：多个探针（T25/T26/T28/T29/T05）会把实测结果写回**仓库内**
的 tracked 证据文件。全量回归时，前面的用例先改写证据、后面的
``test_repo_evidence_ledger_is_clean`` 再按 ``SHA256SUMS`` 校验台账，就会在
同一次运行里必然失败——这是测试套件自我污染，不是产品缺陷（W14 复现两次）。

约定：

* 探针自身只通过本模块解析目录，不硬编码 ``REPO_ROOT / "docs" / ...``；
* 测试会话（``tests/phase0/conftest.py``）把 ``PYKNORA_EVIDENCE_DIR`` 指到临时
  目录并预置一份 tracked 证据副本，于是运行期写入落在临时目录，仓库保持干净；
* 单独跑探针（CLI）不设该变量时，行为与引入本模块前完全一致——仍写回
  ``docs/poc/evidence``，供人工采集证据。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ENV_VAR: Final = "PYKNORA_EVIDENCE_DIR"
DEFAULT_EVIDENCE_DIR: Final = REPO_ROOT / "docs" / "poc" / "evidence"


def evidence_dir() -> Path:
    """返回证据目录：环境变量优先（测试会话），否则用仓库默认路径。"""

    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_EVIDENCE_DIR
