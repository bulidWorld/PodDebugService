#!/usr/bin/env python3
"""仓库数据库结构同步器的兼容入口。"""
from pathlib import Path
import runpy

if __name__ == "__main__":
    # 保留旧脚本入口，将执行委托给仓库根目录的正式同步脚本。
    repository_root = Path(__file__).resolve().parents[3]
    runpy.run_path(str(repository_root / "scripts" / "sync_schema.py"), run_name="__main__")
