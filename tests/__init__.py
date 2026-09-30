"""把 tests/ 目录放进 sys.path，便于 `python -m unittest discover` 直接找到本包。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
