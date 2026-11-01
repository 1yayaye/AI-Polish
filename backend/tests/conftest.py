"""测试引导：门禁命令从仓库根目录运行 `python -m pytest backend/tests`，
pytest 只会把 backend/tests 加进 sys.path；这里补上 backend/ 使 `import app` 可用。
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
