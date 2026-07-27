"""std_agent_sdk 测试路径装配：指向 sdk/（本包的项目根），未装包时也能 import。"""

from __future__ import annotations

import sys
from pathlib import Path

_SDK_ROOT = Path(__file__).resolve().parents[2]
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))
