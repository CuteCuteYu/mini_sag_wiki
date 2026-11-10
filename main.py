"""mini_sag_wiki 命令行入口。

用法与 ``mini_sag_wiki``（短别名 ``wiki``）完全一致：
    uv run python main.py ask "how long is the rollback window"
    uv run python -m mini_sag_wiki ask "how long is the rollback window"
"""

from mini_sag_wiki.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
