"""std_agent_sdk 测试路径装配：仓库根（std_agent_sdk）+ backend/（app、sandbox_manager…）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)
