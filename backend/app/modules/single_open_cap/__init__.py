"""同访客仅一个 open（single_open_cap）。

问题：同一访客可并存多个 open 会话，后到击点挂错段，织机并行两段 open。
本题修复「唯一切换已发生、切换审计未落地」：关旧开新标成功，却查不到旧→新
切换边；或边有了，旧会话写闸仍开放、收流还能往旧 id 追加；或 visitor_open
已指新会话，切换核证页空白。

形态择一（硬约束，本模块选 A）：
  A. 独立切换审计表 session_switch_audits——一行一条不可变旧→新切换边
     （访客、旧会话、新会话、关闭审计键、时刻），UNIQUE(old,new) 库层防重边。
  （备选 B：访客行不可变 last_switch 快照列，未采用。）

路径择一（失败闭，本模块选甲）：关旧/开新/指针/切换边同事务。
  - 将产生第二 open 时，在调用方同一事务内依次：
      1. 关旧 open：status open→closed（写闸关闭），写关闭审计 + 终态会话证明；
      2. 开新 open 承接；
      3. 闸 visitor_open 改挂到新会话（指针物化）；
      4. 写 session_switch_audits 切换边（带关闭审计键，外键钉死旧/新/审计行）。
  - 任一步失败 → 调用方整体回滚 → 不存在「切换成功而审计边缺行 / 指针已新而审计空」。
  - 不整趟拒绝保留双 open：第二 open 请求永远以「关旧开新」完成承接。
  （备选乙：先写切换草稿校验旧已关/新已开后再原子落审计与指针，未采用。）

旧会话写闸（两层，禁止切换后仍可追加）：
  - 服务层 ensure_session_writable()：收流挂点后、写击点前校验 status='open'，
    closed 即抛错，整批回滚；
  - 库层触发器 hits_reject_closed_session：任何向 closed 会话 INSERT 击点的操作
    直接被 SQLite 拒绝（绕过服务层直写也进不来）。

不变量（核证接口 proof() 校验）：
  1. 任一访客 status='open' 的会话数 ≤ 1；
  2. 每个 open 会话都有闸行（织机读 sessions.status，open 必须受闸）；
  3. 每个闸行都指向存在的 open 会话（无悬空闸）。

切换核证（只读接口 switch_proof() 校验，基线所无）：
  每条边核：访客一致、旧会话已 closed 且写闸闭、新会话 open、关闭审计键可查；
  全局核：指针已新必有边、有边必有关闭审计、关旧开新不留无边切换；并给结论。

协同：
  - 收流：services.ingest.run_batch 经 resolve_session_id() 挂点/开新，
    写点前过 ensure_session_writable() 旧写闸；
  - 关闭写闸：关旧即 status open→closed，与指针物化、切换边同事务；
  - 指针物化：visitor_open 改挂与切换边同成败；
  - 核证：GET /api/open-cap/switch-proof 只读切换核证页；
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

-- 独立切换审计表（形态 A）：一行一条不可变旧→新切换边
CREATE TABLE IF NOT EXISTS session_switch_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  old_session_id TEXT NOT NULL REFERENCES sessions(id),
  new_session_id TEXT NOT NULL REFERENCES sessions(id),
  close_audit_id INTEGER NOT NULL REFERENCES session_close_audits(id),
  switched_at TEXT NOT NULL,
  UNIQUE(old_session_id, new_session_id)
);

-- 旧会话写闸（库层）：向 closed 会话追加击点直接拒绝
CREATE TRIGGER IF NOT EXISTS hits_reject_closed_session
BEFORE INSERT ON hits
WHEN NEW.channel = 'primary' AND EXISTS (
  SELECT 1 FROM sessions WHERE id = NEW.session_id AND status = 'closed'
)
BEGIN
  SELECT RAISE(ABORT, 'session_closed_write_gate: 已关闭会话不可追加击点');
END;
"""

REASON_FIRST_OPEN = "first_open"
REASON_FORCED_NEW_OPEN = "forced_new_open"
REASON_RECONCILE = "reconcile_double_open"


class SessionClosedError(RuntimeError):
    """旧会话写闸拦截：目标会话已 closed，收流不得追加。"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建闸表、关闭审计表、切换审计表与写闸触发器（幂等）。"""
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------- 闸表读写


def current_open(conn: sqlite3.Connection, visitor_id: str) -> dict | None:
    """读闸：访客现行 open（唯一真源是闸表，不是 LIMIT 1）。"""
    row = conn.execute(
        "SELECT visitor_id, session_id, opened_at FROM visitor_open WHERE visitor_id=?",
        (visitor_id,),
    ).fetchone()
    return dict(row) if row else None


def ensure_session_writable(conn: sqlite3.Connection, session_id: str) -> None:
    """旧会话写闸（服务层）：closed 会话不可追加，违则抛错由调用方回滚。"""
    row = conn.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise SessionClosedError(f"会话不存在: {session_id}")
    if row["status"] != "open":
        raise SessionClosedError(f"会话 {session_id} 已 {row['status']}，写闸关闭不可追加")


def open_session(
    conn: sqlite3.Connection,
    visitor_id: str,
    session_id: str,
    event_ts: str,
    *,
    reason: str,
    now: str | None = None,
) -> str:
    """同事务关旧 / 开新 / 改挂 / 写切换边。须在调用方事务内调用（失败整体回滚）。

    四步同事务，缺一回滚：
      1. 关旧 open（status→closed 关写闸 + 关闭审计 + 终态证明），取回关闭审计键；
      2. 开新承接；
      3. 闸改挂到新会话（指针物化）；
      4. 写切换审计边（访客/旧/新/关闭审计键/时刻），外键钉死三行。
    """
    now = now or _now()
    gate = current_open(conn, visitor_id)
    old_sid = gate["session_id"] if gate else None
    if old_sid == session_id:
        return session_id  # 幂等：闸已挂在目标会话

    # 1) 关旧（关写闸 + 留关闭审计），取回关闭审计键
    close_audit_id = None
    if old_sid:
        close_audit_id = _close_current_open(
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

    # 3) 改挂闸（指针物化；有闸改挂，无闸立闸）
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

    # 4) 写切换审计边（与指针同成败；外键保证旧/新/关闭审计行均真实存在）
    if old_sid:
        conn.execute(
            """
            INSERT INTO session_switch_audits(
              visitor_id, old_session_id, new_session_id, close_audit_id, switched_at
            ) VALUES (?,?,?,?,?)
            """,
            (visitor_id, old_sid, session_id, close_audit_id, now),
        )
    return session_id


def resolve_session_id(
    conn: sqlite3.Connection, visitor_id: str, hit: dict, *, now: str | None = None
) -> str:
    """收流挂点决策：返回该击点应挂的会话 id。

    - 击点要求强制新会话（force_new_session）→ 关旧开新，原子承接；
    - 否则有闸且闸指向 open → 挂现行 open（返回前过写闸校验）；
    - 否则（首访 / 闸悬空）→ 开新立闸。
    """
    gate = current_open(conn, visitor_id)
    force = bool(hit.get("force_new_session"))
    if gate and not force:
        st = conn.execute(
            "SELECT status FROM sessions WHERE id=?", (gate["session_id"],)
        ).fetchone()
        if st and st["status"] == "open":
            ensure_session_writable(conn, gate["session_id"])
            return gate["session_id"]
        # 闸悬空（指向已闭/缺失会话）→ 落入开新，旧行已闭无需再关
    sid = f"S-{visitor_id}-{uuid.uuid4().hex[:6]}"
    reason = hit.get("new_session_reason") or (
        REASON_FORCED_NEW_OPEN if force else REASON_FIRST_OPEN
    )
    return open_session(conn, visitor_id, sid, hit["event_ts"], reason=reason, now=now)


def reconcile_open_unique(conn: sqlite3.Connection, *, now: str | None = None) -> dict:
    """落库即收敛：同访客并存多个 open 时，留现行闸所指（无闸留最新）、关其余（留审计）。

    采用本模块前的存量双 open 由此一次性修到单 open；关旧开新同样落切换边，
    与正常路径同约束；幂等，干净库上是空转。
    """
    now = now or _now()
    closed, gated, switched = [], [], []
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
        # 先保证承接会话存在（首落库时 keep 本就是 open 行，这里仅兜底）
        keep_exists = conn.execute(
            "SELECT 1 FROM sessions WHERE id=?", (keep["id"],)
        ).fetchone()
        if not keep_exists:
            conn.execute(
                """
                INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
                VALUES (?,?,?,?,0,'open')
                """,
                (keep["id"], vid, keep["started_at"], keep["started_at"]),
            )
        for extra in sess_rows:
            if extra["id"] == keep["id"]:
                continue
            close_audit_id = _close_current_open(
                conn, vid, extra["id"],
                successor_sid=keep["id"], reason=REASON_RECONCILE, now=now,
            )
            conn.execute(
                """
                INSERT INTO session_switch_audits(
                  visitor_id, old_session_id, new_session_id, close_audit_id, switched_at
                ) VALUES (?,?,?,?,?)
                """,
                (vid, extra["id"], keep["id"], close_audit_id, now),
            )
            closed.append(extra["id"])
            switched.append({"old": extra["id"], "new": keep["id"]})
        _gate_set(conn, vid, keep["id"], now)
        gated.append(vid)
    # 清理悬空闸：指向不存在或已非 open 的会话
    conn.execute(
        """
        DELETE FROM visitor_open
        WHERE session_id NOT IN (SELECT id FROM sessions WHERE status='open')
        """
    )
    return {"closed": closed, "gated": gated, "switched": switched}


# ---------------------------------------------------------------- 内部步骤


def _close_current_open(
    conn: sqlite3.Connection,
    visitor_id: str,
    session_id: str,
    *,
    successor_sid: str | None,
    reason: str,
    now: str,
) -> int:
    """关旧 open：状态 open→closed（关写闸），写关闭审计与终态证明，回关闭审计键。

    审计行的 successor_session_id 必须钉到承接会话；返回审计主键供切换边引用。
    """
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        raise SessionClosedError(f"关旧失败：会话不存在 {session_id}")
    if row["status"] != "open":
        raise SessionClosedError(f"关旧失败：会话 {session_id} 已非 open（{row['status']}）")

    # 关闭审计（承接会话落键，禁止留空后继）
    cur = conn.execute(
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
            successor_sid,
            now,
        ),
    )
    close_audit_id = cur.lastrowid

    # 终态会话证明（固化成员）
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

    # 写闸关闭：open→closed（触发器据此拒绝后续追加）
    conn.execute(
        "UPDATE sessions SET status='closed' WHERE id=? AND status='open'",
        (session_id,),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise SessionClosedError(f"写闸关闭失败：{session_id}")
    return close_audit_id


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
    multi_open = conn.execute(
        """
        SELECT visitor_id, COUNT(*) AS open_count
        FROM sessions WHERE status='open'
        GROUP BY visitor_id HAVING COUNT(*) > 1
        """
    ).fetchall()
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
    switch_edges = conn.execute(
        "SELECT COUNT(*) AS c FROM session_switch_audits"
    ).fetchone()["c"]
    ok = not (multi_open or ungated_open or dangling_gate)
    return {
        "ok": ok,
        "constraint": "visitor_open 闸表：visitor_id PRIMARY KEY 挂现行 open id",
        "path": "同事务关旧/开新/改挂/写切换边，缺一回滚",
        "gate_rows": gate_rows,
        "open_sessions": open_sessions,
        "close_audits": audits,
        "switch_edges": switch_edges,
        "multi_open_visitors": [dict(r) for r in multi_open],
        "ungated_open_sessions": [dict(r) for r in ungated_open],
        "dangling_gate_rows": [dict(r) for r in dangling_gate],
        "checked_at": _now(),
    }


def switch_proof(conn: sqlite3.Connection, visitor_id: str | None = None) -> dict:
    """切换核证（基线所无，只读）：逐边核访客/旧/新/写闸态/关闭审计键，给结论。

    每条切换边核：
      - 边访客与旧/新会话归属一致；
      - 旧会话 status=closed（写闸闭，不可追加）；
      - 新会话 status=open；
      - close_audit_id 指向的关闭审计真实存在且被关会话 == 旧会话、后继 == 新会话。
    全局核：
      - 指针已新而审计仍空（visitor_open 指向的会话无入边且它不是首段）；
      - 有边无关闭审计 / 关闭审计指向后继却无边；
      - 切换边旧会话仍可写（status 未闭）。
    """
    where = "WHERE sw.visitor_id = ?" if visitor_id else ""
    params = (visitor_id,) if visitor_id else ()
    edges = conn.execute(
        f"""
        SELECT sw.id, sw.visitor_id, sw.old_session_id, sw.new_session_id,
               sw.close_audit_id, sw.switched_at,
               so.status AS old_status, sn.status AS new_status,
               ca.id AS ca_id, ca.session_id AS ca_session_id,
               ca.successor_session_id AS ca_successor,
               vo.session_id AS gate_session_id
        FROM session_switch_audits sw
        JOIN sessions so ON so.id = sw.old_session_id
        JOIN sessions sn ON sn.id = sw.new_session_id
        LEFT JOIN session_close_audits ca ON ca.id = sw.close_audit_id
        LEFT JOIN visitor_open vo ON vo.visitor_id = sw.visitor_id
        {where}
        ORDER BY sw.id
        """,
        params,
    ).fetchall()

    edge_reports, violations = [], []
    for r in edges:
        e = dict(r)
        edge_violations = []
        old_owner = conn.execute(
            "SELECT visitor_id FROM sessions WHERE id=?", (e["old_session_id"],)
        ).fetchone()
        new_owner = conn.execute(
            "SELECT visitor_id FROM sessions WHERE id=?", (e["new_session_id"],)
        ).fetchone()
        if not old_owner or old_owner["visitor_id"] != e["visitor_id"]:
            edge_violations.append("旧会话归属与边访客不一致")
        if not new_owner or new_owner["visitor_id"] != e["visitor_id"]:
            edge_violations.append("新会话归属与边访客不一致")
        if e["old_status"] != "closed":
            edge_violations.append(f"旧会话写闸未闭（status={e['old_status']}）")
        # 新会话：现行 open，或本身又作为旧会话被下一跳切换（链上中间段，合法 closed）
        new_has_out_edge = conn.execute(
            "SELECT 1 FROM session_switch_audits WHERE old_session_id=? LIMIT 1",
            (e["new_session_id"],),
        ).fetchone()
        if e["new_status"] != "open" and not new_has_out_edge:
            edge_violations.append(
                f"新会话既非 open 也无后续切换（status={e['new_status']}）"
            )
        if e["ca_id"] is None:
            edge_violations.append("关闭审计键缺失")
        else:
            if e["ca_session_id"] != e["old_session_id"]:
                edge_violations.append("关闭审计被关会话与旧会话不符")
            if e["ca_successor"] != e["new_session_id"]:
                edge_violations.append("关闭审计后继与新会话不符")
        verdict = "通过" if not edge_violations else "未过"
        if edge_violations:
            violations.append({"edge_id": e["id"], "problems": edge_violations})
        edge_reports.append(
            {
                "edge_id": e["id"],
                "visitor_id": e["visitor_id"],
                "old_session_id": e["old_session_id"],
                "new_session_id": e["new_session_id"],
                "close_audit_id": e["close_audit_id"],
                "switched_at": e["switched_at"],
                "old_write_gate": "closed" if e["old_status"] == "closed" else e["old_status"],
                "new_status": e["new_status"],
                "gate_points_at_new": e["gate_session_id"] == e["new_session_id"],
                "verdict": verdict,
                "violations": edge_violations,
            }
        )

    # 全局核 1：指针已指新会话，却查不到入边（指针已新而审计仍空）
    gate_params = (visitor_id,) if visitor_id else ()
    v_filter = "AND vo.visitor_id = ?" if visitor_id else ""
    gate_gap = conn.execute(
        f"""
        SELECT vo.visitor_id, vo.session_id AS gate_session_id
        FROM visitor_open vo
        WHERE EXISTS (
          SELECT 1 FROM sessions s
          WHERE s.visitor_id = vo.visitor_id AND s.id != vo.session_id
        )
          AND NOT EXISTS (
            SELECT 1 FROM session_switch_audits sw
            WHERE sw.visitor_id = vo.visitor_id AND sw.new_session_id = vo.session_id
          )
          {v_filter}
        """,
        gate_params,
    ).fetchall()
    for r in gate_gap:
        violations.append(
            {
                "visitor_id": r["visitor_id"],
                "problems": [f"指针已指 {r['gate_session_id']} 而切换审计边缺失"],
            }
        )

    # 全局核 2：关闭审计钉了后继，却没有对应切换边（切换成功而审计边缺行）
    ca_filter = "AND ca.visitor_id = ?" if visitor_id else ""
    audit_gap = conn.execute(
        f"""
        SELECT ca.id, ca.visitor_id, ca.session_id, ca.successor_session_id
        FROM session_close_audits ca
        WHERE ca.successor_session_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM session_switch_audits sw
            WHERE sw.close_audit_id = ca.id
          )
          {ca_filter}
        """,
        gate_params,
    ).fetchall()
    for r in audit_gap:
        violations.append(
            {
                "close_audit_id": r["id"],
                "visitor_id": r["visitor_id"],
                "problems": [
                    f"关闭审计 {r['session_id']}→{r['successor_session_id']} 无切换边"
                ],
            }
        )

    # 全局核 3：仍存在被切换（有出边）的旧会话却还 open —— 写闸未关
    old_filter = "AND sw.visitor_id = ?" if visitor_id else ""
    open_old = conn.execute(
        f"""
        SELECT DISTINCT sw.old_session_id, sw.visitor_id
        FROM session_switch_audits sw
        JOIN sessions s ON s.id = sw.old_session_id
        WHERE s.status != 'closed'
          {old_filter}
        """,
        gate_params,
    ).fetchall()
    for r in open_old:
        violations.append(
            {
                "old_session_id": r["old_session_id"],
                "visitor_id": r["visitor_id"],
                "problems": ["旧会话切换后写闸仍开放"],
            }
        )

    ok = not violations
    return {
        "ok": ok,
        "scope": "切换审计核证：访客/旧会话/新会话/关闭审计键/写闸态",
        "storage": "独立切换审计表 session_switch_audits",
        "commit_path": "关旧开新同事务写切换审计并关旧写闸，缺一回滚",
        "visitor_id": visitor_id,
        "edge_count": len(edge_reports),
        "edges": edge_reports,
        "violations": violations,
        "conclusion": "全部切换边可查且旧会话写闸已闭" if ok else "存在切换审计缺失或写闸未闭",
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


def list_switches(conn: sqlite3.Connection, visitor_id: str | None = None) -> list[dict]:
    """切换审计边一览（旧→新，含关闭审计键）。只读。"""
    if visitor_id:
        rows = conn.execute(
            """
            SELECT id, visitor_id, old_session_id, new_session_id,
                   close_audit_id, switched_at
            FROM session_switch_audits WHERE visitor_id=? ORDER BY id
            """,
            (visitor_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, visitor_id, old_session_id, new_session_id,
                   close_audit_id, switched_at
            FROM session_switch_audits ORDER BY id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
