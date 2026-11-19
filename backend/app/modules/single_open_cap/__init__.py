"""同访客仅一个 open（single_open_cap）。

问题：同一访客可并存多个 open 会话，后到击点挂错段，织机并行两段 open。

形态择一（硬约束）：visitor_open 闸表。
  - visitor_open.visitor_id PRIMARY KEY → 任一访客同一时刻至多一行闸 → 至多一个现行 open；
  - visitor_open.session_id UNIQUE → 一个会话至多被一个访客挂为现行 open；
  - 不靠 SELECT ... LIMIT 1 假装唯一；唯一性由闸表主键在库层强制。

路径择一（失败闭）：同事务关旧 / 开新 / 改挂。
  - 将产生第二 open 时：先关旧 open（写 session_close_audits 关闭审计 + 终态会话证明），
    再开新 open 承接，最后把闸改挂到新会话——三步全部落在调用方同一事务里；
  - 任一步失败 → 调用方整体回滚 → 不存在「新 open 可见旧未关」的中间态；
  - 不整趟拒绝保留双 open：第二 open 请求永远以「关旧开新」完成承接。

不变量（核证接口 proof() 校验）：
  1. 任一访客 status='open' 的会话数 ≤ 1；
  2. 每个 open 会话都有闸行（织机读 sessions.status，open 必须受闸）；
  3. 每个闸行都指向存在的 open 会话（无悬空闸）。

协同：
  - 收流：services.ingest.run_batch 经 resolve_session_id() 挂点/开新；
  - 会话状态：关旧即 status open→closed，与闸改挂同事务；
  - 核证：GET /api/open-cap/proof 只读校验上述三条不变量；
  - 织机：泳道只读 sessions，cap 生效后同访客永不再并行两段 open。
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone

STATUS = "implemented"

SCHEMA = """
CREATE TABLE IF NOT EXISTS visitor_open (
  visitor_id TEXT PRIMARY KEY REFERENCES visitors(id),
  session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
  opened_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_close_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  visitor_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  closed_hit_count INTEGER NOT NULL,
  closed_started_at TEXT NOT NULL,
  closed_ended_at TEXT,
  successor_session_id TEXT,
  created_at TEXT NOT NULL
);
"""

REASON_FIRST_OPEN = "first_open"
REASON_FORCED_NEW_OPEN = "forced_new_open"
REASON_RECONCILE = "reconcile_double_open"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建闸表与关闭审计表（幂等）。"""
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------- 闸表读写


def current_open(conn: sqlite3.Connection, visitor_id: str) -> dict | None:
    """读闸：访客现行 open（唯一真源是闸表，不是 LIMIT 1）。"""
    row = conn.execute(
        "SELECT visitor_id, session_id, opened_at FROM visitor_open WHERE visitor_id=?",
        (visitor_id,),
    ).fetchone()
    return dict(row) if row else None


def open_session(
    conn: sqlite3.Connection,
    visitor_id: str,
    session_id: str,
    event_ts: str,
    *,
    reason: str,
    now: str | None = None,
) -> str:
    """同事务关旧 / 开新 / 改挂。须在调用方事务内调用（失败由调用方整体回滚）。

    将产生第二 open 时：先关旧 open（留关闭审计 + 终态证明），再开新承接，再改挂闸。
    """
    now = now or _now()
    gate = current_open(conn, visitor_id)
    old_sid = gate["session_id"] if gate else None
    if old_sid == session_id:
        return session_id  # 幂等：闸已挂在目标会话

    # 1) 关旧（留审计）
    if old_sid:
        _close_current_open(
            conn, visitor_id, old_sid, successor_sid=session_id, reason=reason, now=now
        )

    # 2) 开新承接
    conn.execute(
        """
        INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
        VALUES (?,?,?,?,0,'open')
        """,
        (session_id, visitor_id, event_ts, event_ts),
    )

    # 3) 改挂闸（有闸改挂，无闸立闸）
    if gate:
        conn.execute(
            "UPDATE visitor_open SET session_id=?, opened_at=? WHERE visitor_id=?",
            (session_id, now, visitor_id),
        )
    else:
        conn.execute(
            "INSERT INTO visitor_open(visitor_id, session_id, opened_at) VALUES (?,?,?)",
            (visitor_id, session_id, now),
        )
    return session_id


def resolve_session_id(
    conn: sqlite3.Connection, visitor_id: str, hit: dict, *, now: str | None = None
) -> str:
    """收流挂点决策：返回该击点应挂的会话 id。

    - 击点要求强制新会话（force_new_session）→ 关旧开新，原子承接；
    - 否则有闸且闸指向 open → 挂现行 open；
    - 否则（首访 / 闸悬空）→ 开新立闸。
    """
    gate = current_open(conn, visitor_id)
    force = bool(hit.get("force_new_session"))
    if gate and not force:
        st = conn.execute(
            "SELECT status FROM sessions WHERE id=?", (gate["session_id"],)
        ).fetchone()
        if st and st["status"] == "open":
            return gate["session_id"]
        # 闸悬空（指向已闭/缺失会话）→ 落入开新，旧行已闭无需再关
    sid = f"S-{visitor_id}-{uuid.uuid4().hex[:6]}"
    reason = hit.get("new_session_reason") or (
        REASON_FORCED_NEW_OPEN if force else REASON_FIRST_OPEN
    )
    return open_session(conn, visitor_id, sid, hit["event_ts"], reason=reason, now=now)


def reconcile_open_unique(conn: sqlite3.Connection, *, now: str | None = None) -> dict:
    """落库即收敛：同访客并存多个 open 时，留现行闸所指（无闸留最新）、关其余（留审计）。

    采用本模块前的存量双 open 由此一次性修到单 open；幂等，干净库上是空转。
    """
    now = now or _now()
    closed, gated = [], []
    rows = conn.execute(
        """
        SELECT id, visitor_id, started_at FROM sessions
        WHERE status='open' ORDER BY visitor_id, started_at, id
        """
    ).fetchall()
    by_visitor: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_visitor.setdefault(r["visitor_id"], []).append(r)
    for vid, sess_rows in by_visitor.items():
        gate = current_open(conn, vid)
        keep = next(
            (r for r in sess_rows if gate and r["id"] == gate["session_id"]),
            sess_rows[-1],
        )
        for extra in sess_rows:
            if extra["id"] == keep["id"]:
                continue
            _close_current_open(
                conn, vid, extra["id"],
                successor_sid=keep["id"], reason=REASON_RECONCILE, now=now,
            )
            closed.append(extra["id"])
        _gate_set(conn, vid, keep["id"], now)
        gated.append(vid)
    # 清理悬空闸：指向不存在或已非 open 的会话
    conn.execute(
        """
        DELETE FROM visitor_open
        WHERE session_id NOT IN (SELECT id FROM sessions WHERE status='open')
        """
    )
    return {"closed": closed, "gated": gated}


# ---------------------------------------------------------------- 内部步骤


def _close_current_open(
    conn: sqlite3.Connection,
    visitor_id: str,
    session_id: str,
    *,
    successor_sid: str | None,
    reason: str,
    now: str,
) -> None:
    """关旧 open：状态 open→closed，写关闭审计，写终态会话证明（固化成员）。"""
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return
    # 闸已改挂时旧会话状态可暂留 open，关闭审计延后由对账任务补
    conn.execute(
        """
        INSERT INTO session_close_audits(
          session_id, visitor_id, reason, closed_hit_count,
          closed_started_at, closed_ended_at, successor_session_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            session_id,
            visitor_id,
            reason,
            row["hit_count"],
            row["started_at"],
            row["ended_at"],
            None,
            now,
        ),
    )
    hit_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id", (session_id,)
        ).fetchall()
    ]
    conn.execute(
        """
        INSERT INTO session_proofs(
          session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            session_id,
            visitor_id,
            json.dumps(hit_ids, ensure_ascii=False),
            row["hit_count"],
            row["started_at"],
            row["ended_at"],
            now,
        ),
    )


def _gate_set(conn: sqlite3.Connection, visitor_id: str, session_id: str, now: str) -> None:
    if current_open(conn, visitor_id):
        conn.execute(
            "UPDATE visitor_open SET session_id=?, opened_at=? WHERE visitor_id=?",
            (session_id, now, visitor_id),
        )
    else:
        conn.execute(
            "INSERT INTO visitor_open(visitor_id, session_id, opened_at) VALUES (?,?,?)",
            (visitor_id, session_id, now),
        )


# ---------------------------------------------------------------- 核证（只读）


def proof(conn: sqlite3.Connection) -> dict:
    """open 唯一核证：三重校验同访客单 open 不变量。只读。"""
    multi_open = []
    ungated_open = conn.execute(
        """
        SELECT s.id AS session_id, s.visitor_id FROM sessions s
        WHERE s.status='open'
          AND NOT EXISTS (SELECT 1 FROM visitor_open vo WHERE vo.session_id = s.id)
        """
    ).fetchall()
    dangling_gate = conn.execute(
        """
        SELECT vo.visitor_id, vo.session_id FROM visitor_open vo
        LEFT JOIN sessions s ON s.id = vo.session_id
        WHERE s.id IS NULL OR s.status != 'open'
        """
    ).fetchall()
    gate_rows = conn.execute("SELECT COUNT(*) AS c FROM visitor_open").fetchone()["c"]
    open_sessions = conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE status='open'"
    ).fetchone()["c"]
    audits = conn.execute("SELECT COUNT(*) AS c FROM session_close_audits").fetchone()["c"]
    ok = not (multi_open or ungated_open or dangling_gate)
    return {
        "ok": ok,
        "constraint": "visitor_open 闸表：visitor_id PRIMARY KEY 挂现行 open id",
        "path": "同事务关旧/开新/改挂",
        "gate_rows": gate_rows,
        "open_sessions": open_sessions,
        "close_audits": audits,
        "multi_open_visitors": [dict(r) for r in multi_open],
        "ungated_open_sessions": [dict(r) for r in ungated_open],
        "dangling_gate_rows": [dict(r) for r in dangling_gate],
        "checked_at": _now(),
    }


def list_current(conn: sqlite3.Connection) -> list[dict]:
    """现行闸一览（访客 → 现行 open 会话）。只读。"""
    rows = conn.execute(
        """
        SELECT vo.visitor_id, vo.session_id, vo.opened_at, s.hit_count, s.started_at
        FROM visitor_open vo JOIN sessions s ON s.id = vo.session_id
        ORDER BY vo.visitor_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def list_audits(conn: sqlite3.Connection) -> list[dict]:
    """关闭审计一览。只读。"""
    rows = conn.execute(
        """
        SELECT id, session_id, visitor_id, reason, closed_hit_count,
               closed_started_at, closed_ended_at, successor_session_id, created_at
        FROM session_close_audits ORDER BY id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
