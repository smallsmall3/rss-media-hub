"""支持 `python -m app <命令>`。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
