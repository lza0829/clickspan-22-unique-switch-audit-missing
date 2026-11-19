import os
import sys
import tempfile
from pathlib import Path

# 必须在任何 app.* 导入之前定界：测试用临时 SQLite 库 + 仓库本地夹具
_BACKEND = Path(__file__).resolve().parents[1]
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="clickspan-test-")
os.environ["FIXTURES_DIR"] = str(_BACKEND.parent / "fixtures")
sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

from app import seed  # noqa: E402
from app.db import DB_PATH  # noqa: E402


@pytest.fixture()
def fresh_db():
    """每用例一座新库：删库文件 → init_db（建闸/种子/收敛）。"""
    if DB_PATH.exists():
        DB_PATH.unlink()
    seed.init_db()
    yield DB_PATH
    if DB_PATH.exists():
        DB_PATH.unlink()
