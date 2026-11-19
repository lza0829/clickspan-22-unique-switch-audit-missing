"""single_open_cap 只读核证接口：open 唯一证明 / 现行闸 / 关闭审计。"""

from fastapi import APIRouter

from app.db import connect
from app.modules import single_open_cap

router = APIRouter(prefix="/api/open-cap", tags=["single_open_cap"])


@router.get("/proof")
def open_cap_proof():
    """open 唯一核证：同访客单 open 三重不变量校验。只读。"""
    conn = connect()
    try:
        return single_open_cap.proof(conn)
    finally:
        conn.close()


@router.get("/current")
def open_cap_current():
    """现行闸一览：访客 → 现行 open 会话。只读。"""
    conn = connect()
    try:
        return single_open_cap.list_current(conn)
    finally:
        conn.close()


@router.get("/audits")
def open_cap_audits():
    """关闭审计一览：每次关旧开新的留痕。只读。"""
    conn = connect()
    try:
        return single_open_cap.list_audits(conn)
    finally:
        conn.close()
