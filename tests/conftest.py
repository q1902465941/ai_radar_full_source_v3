from __future__ import annotations

import os
import tempfile
from pathlib import Path


_TEST_DB_ROOT = Path(tempfile.gettempdir()) / "ai_radar_pytest" / str(os.getpid())
_TEST_DB_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["DB_PATH"] = str(_TEST_DB_ROOT / "ai_radar_pytest.sqlite")
