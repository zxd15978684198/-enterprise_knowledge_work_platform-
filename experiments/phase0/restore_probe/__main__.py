"""``python -m restore_probe`` — runbook entry point for the backup/restore drill."""

from __future__ import annotations

import sys

from .drill import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
