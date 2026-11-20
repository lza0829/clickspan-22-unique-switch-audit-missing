"""切换审计核证（22-unique-switch-audit-missing）验收。

夹具触发关旧开新后核：
  - 切换审计边可查（访客、旧会话、新会话、关闭审计键、时刻五要素齐全）；
  - 旧会话写闸关闭，服务层与库层触发器双重拒绝追加；
  - 指针与审计同成败，禁止「指针已新而审计仍空却回成功」；
  - 切换核证只读接口给结论；唯一闸/关闭写闸/指针物化/织机协同。
"""

import json
import sqlite3

import pytest

from app.db import connect
from app.modules import single_open_cap as cap
from app.services import ingest


def _switch_edges(conn, vid=None):
    if vid:
        rows = conn.execute(
            "SELECT * FROM session_switch_audits WHERE visitor_id=? ORDER BY id", (vid,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM session_switch_audits ORDER BY id").fetchall()
    return [dict(r) for r in rows]


# 1. 夹具触发关旧开新：每条切换都落下可查切换审计边，五要素齐全且外键钉死
def test_switch_edges_landed_with_all_five_elements(fresh_db):
    result = ingest.run_batch("switch_audit_batch.json")
    assert result["ok"], result
    assert result["report"]["written"] == 6

    conn = connect()
    try:
        edges = _switch_edges(conn)
        # V-1001 两跳、V-1002 一跳；首访 V-1005 无切换
        assert len(edges) == 3
        v1001 = [e for e in edges if e["visitor_id"] == "V-1001"]
        v1002 = [e for e in edges if e["visitor_id"] == "V-1002"]
        assert len(v1001) == 2 and len(v1002) == 1

        for e in edges:
            # 五要素：访客、旧会话、新会话、关闭审计键、时刻
            assert e["visitor_id"]
            assert e["old_session_id"]
            assert e["new_session_id"]
            assert isinstance(e["close_audit_id"], int)
            assert e["switched_at"]
            # 关闭审计键可查，且审计行钉的就是这条旧→新
            ca = conn.execute(
                "SELECT * FROM session_close_audits WHERE id=?", (e["close_audit_id"],)
            ).fetchone()
            assert ca is not None
            assert ca["session_id"] == e["old_session_id"]
            assert ca["successor_session_id"] == e["new_session_id"]
            assert ca["visitor_id"] == e["visitor_id"]

        # V-1001 边成链：第一条的新会话 == 第二条的旧会话
        assert v1001[0]["new_session_id"] == v1001[1]["old_session_id"]
        assert v1001[0]["old_session_id"] == "S-V-1001-1"

        # UNIQUE(old,new)：库层拒绝重复边
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO session_switch_audits(
                  visitor_id, old_session_id, new_session_id, close_audit_id, switched_at
                ) VALUES (?,?,?,?,?)
                """,
                (
                    v1002[0]["visitor_id"],
                    v1002[0]["old_session_id"],
                    v1002[0]["new_session_id"],
                    v1002[0]["close_audit_id"],
                    "2026-11-18T12:00:00Z",
                ),
            )
        conn.rollback()
    finally:
        conn.close()


# 2. 旧会话写闸关闭：status=closed，服务层拒追加，库层触发器也拒追加
def test_old_session_write_gate_closed(fresh_db):
    assert ingest.run_batch("switch_audit_batch.json")["ok"]

    conn = connect()
    try:
        edges = _switch_edges(conn, "V-1001")
        oldest = edges[0]["old_session_id"]
        middle = edges[0]["new_session_id"]  # 第二跳的旧会话
        current = cap.current_open(conn, "V-1001")["session_id"]

        # 所有被切换的旧会话都是 closed，仅现行新会话 open
        for old_sid in (oldest, middle):
            st = conn.execute("SELECT status FROM sessions WHERE id=?", (old_sid,)).fetchone()
            assert st["status"] == "closed"
        assert conn.execute(
            "SELECT status FROM sessions WHERE id=?", (current,)
        ).fetchone()["status"] == "open"

        # 服务层写闸
        with pytest.raises(cap.SessionClosedError):
            cap.ensure_session_writable(conn, oldest)
        cap.ensure_session_writable(conn, current)  # 现行 open 可写，不抛

        # 库层触发器：绕过服务层直插 closed 会话也被拒
        with pytest.raises(sqlite3.IntegrityError) as ei:
            conn.execute(
                """
                INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
                VALUES ('H-rogue-1','V-1001',?,'2026-11-18T13:00:00Z','/x','view','primary')
                """,
                (oldest,),
            )
        assert "write_gate" in str(ei.value)
        conn.rollback()

        # 侧车 channel 不受主通道写闸限制（留给 sidecar 类模块），且不计入正式点
        conn.execute(
            """
            INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
            VALUES ('H-side-1','V-1001',?,'2026-11-18T13:01:00Z','/x','late','sidecar')
            """,
            (oldest,),
        )
        conn.rollback()
    finally:
        conn.close()


# 3. 收流路径：切换后普通击点只进新会话，旧 id 永不复用
def test_ingest_never_appends_to_old_session(fresh_db, tmp_path, monkeypatch):
    assert ingest.run_batch("switch_audit_batch.json")["ok"]
    conn = connect()
    try:
        current = cap.current_open(conn, "V-1001")["session_id"]
        old_sessions = {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM sessions WHERE visitor_id='V-1001' AND status='closed'"
            ).fetchall()
        }
    finally:
        conn.close()

    follow = tmp_path / "follow.json"
    follow.write_text(
        json.dumps(
            {
                "batch_id": "B-follow",
                "hits": [
                    {
                        "id": "H-follow-1",
                        "visitor_id": "V-1001",
                        "event_ts": "2026-11-18T14:00:00Z",
                        "page_path": "/after",
                        "event_name": "view",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ingest, "FIXTURES_DIR", tmp_path)
    assert ingest.run_batch("follow.json")["ok"]

    conn = connect()
    try:
        row = conn.execute(
            "SELECT session_id FROM hits WHERE id='H-follow-1'"
        ).fetchone()
        assert row["session_id"] == current
        # 旧会话击点数冻结在切换时刻，之后零增长
        for sid in old_sessions:
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM hits WHERE session_id=? AND channel='primary'",
                (sid,),
            ).fetchone()["c"]
            after = conn.execute(
                "SELECT COUNT(*) AS c FROM hits WHERE session_id=? AND id='H-follow-1'",
                (sid,),
            ).fetchone()["c"]
            assert after == 0
            assert cnt == conn.execute(
                "SELECT hit_count FROM sessions WHERE id=?", (sid,)
            ).fetchone()["hit_count"]
    finally:
        conn.close()


# 4. 指针与审计同成败：边的新会话必是闸指会话；制造「有指针无边」核证必判未过
def test_pointer_and_audit_same_fate_switch_proof_detects_gap(fresh_db):
    assert ingest.run_batch("switch_audit_batch.json")["ok"]
    conn = connect()
    try:
        # 每条边的新会话都与现行/后继闸指一致（最后一跳等于当前闸）
        edges = _switch_edges(conn)
        for e in edges:
            gate = cap.current_open(conn, e["visitor_id"])
            chain_last = [
                x for x in edges if x["visitor_id"] == e["visitor_id"]
            ][-1]
            if e is chain_last:
                assert gate["session_id"] == e["new_session_id"]
        # 正常态核证通过
        sp = cap.switch_proof(conn)
        assert sp["ok"], sp
        assert sp["edge_count"] == 3
        assert all(x["verdict"] == "通过" for x in sp["edges"])
        assert all(x["old_write_gate"] == "closed" for x in sp["edges"])
        assert sp["conclusion"]

        # 模拟脏态 A：删掉某访客的切换边但保留指针 → 指针已新而审计仍空
        victim = _switch_edges(conn, "V-1002")[0]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM session_switch_audits WHERE id=?", (victim["id"],))
        sp2 = cap.switch_proof(conn, "V-1002")
        assert not sp2["ok"]
        assert any("切换审计边缺失" in p for v in sp2["violations"] for p in v["problems"])
        conn.rollback()

        # 模拟脏态 B：旧会话被强行 reopen（边仍在）→ 写闸未闭必须被核证抓到
        intact_old = _switch_edges(conn, "V-1002")[0]["old_session_id"]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE sessions SET status='open' WHERE id=?", (intact_old,)
        )
        sp3 = cap.switch_proof(conn, "V-1002")
        assert not sp3["ok"]
        assert any(
            "写闸" in p for v in sp3["violations"] for p in v["problems"]
        )
        conn.rollback()
    finally:
        conn.close()


# 5. 失败闭：切换边写入失败则关旧/开新/改挂整体回滚，绝不回成功
def test_switch_edge_failure_rolls_back_everything(fresh_db, monkeypatch):
    """sqlite3.Connection 是不可变内置类型，改在 db.connect 上包一层注入失败。"""
    from app import db as db_mod

    real_connect = db_mod.connect
    calls = {"n": 0}

    class FlakyConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if "INSERT INTO session_switch_audits" in sql:
                calls["n"] += 1
                raise sqlite3.OperationalError("injected switch edge write failure")
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def flaky_connect():
        return FlakyConn(real_connect())

    monkeypatch.setattr(db_mod, "connect", flaky_connect)
    monkeypatch.setattr(ingest, "connect", flaky_connect)
    result = ingest.run_batch("switch_audit_batch.json")
    assert not result["ok"]
    assert calls["n"] >= 1

    conn = connect()
    try:
        # 一切回到切换前：种子两会话仍 open、闸未动、无边、无新会话、无新击点
        for vid in ("V-1001", "V-1002"):
            opens = conn.execute(
                "SELECT id FROM sessions WHERE visitor_id=? AND status='open'", (vid,)
            ).fetchall()
            assert [r["id"] for r in opens] == [f"S-{vid}-1"]
            assert cap.current_open(conn, vid)["session_id"] == f"S-{vid}-1"
        assert conn.execute("SELECT COUNT(*) AS c FROM session_switch_audits").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE id LIKE 'S-V-1005-%'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM hits WHERE id LIKE 'H-sw-%'"
        ).fetchone()["c"] == 0
        assert cap.proof(conn)["ok"]
        assert cap.switch_proof(conn)["ok"]  # 无边即无违规
    finally:
        conn.close()


# 6. 关旧开新同事务的模块级直验：open_session 四步齐备，缺一步即抛
def test_open_session_atomic_four_steps(fresh_db):
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        old = cap.current_open(conn, "V-1001")["session_id"]
        new_sid = cap.open_session(
            conn, "V-1001", "S-V-1001-direct",
            "2026-11-18T15:00:00Z", reason="forced_new_open",
        )
        conn.commit()

        assert new_sid == "S-V-1001-direct"
        # 闸已改挂
        assert cap.current_open(conn, "V-1001")["session_id"] == new_sid
        # 旧会话 closed
        assert conn.execute(
            "SELECT status FROM sessions WHERE id=?", (old,)
        ).fetchone()["status"] == "closed"
        # 恰好一条边，键齐
        edges = _switch_edges(conn, "V-1001")
        assert len(edges) == 1
        e = edges[0]
        assert e["old_session_id"] == old and e["new_session_id"] == new_sid
        ca = conn.execute(
            "SELECT successor_session_id FROM session_close_audits WHERE id=?",
            (e["close_audit_id"],),
        ).fetchone()
        assert ca["successor_session_id"] == new_sid

        # 幂等：闸已挂目标会话时空转，不产生第二条边
        conn.execute("BEGIN IMMEDIATE")
        cap.open_session(
            conn, "V-1001", new_sid,
            "2026-11-18T15:01:00Z", reason="forced_new_open",
        )
        conn.commit()
        assert len(_switch_edges(conn, "V-1001")) == 1
    finally:
        conn.close()


# 7. 接口面：切换核证页 / 切换边只读端点，且织机泳道每访客至多一段 open
def test_switch_proof_api_and_loom(fresh_db):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/ingest/run", json={"fixture": "switch_audit_batch.json"})
        assert r.status_code == 200 and r.json()["ok"]

        sp = client.get("/api/open-cap/switch-proof").json()
        assert sp["ok"] and sp["edge_count"] == 3
        assert sp["storage"].startswith("独立切换审计表")
        last_by_visitor = {}
        for edge in sp["edges"]:
            assert edge["verdict"] == "通过"
            assert edge["old_write_gate"] == "closed"
            last_by_visitor[edge["visitor_id"]] = edge
        # 闸只挂每访客最后一跳的新会话；中间段的新会话已被下一跳替换
        for edge in last_by_visitor.values():
            assert edge["gate_points_at_new"] is True

        sp_one = client.get("/api/open-cap/switch-proof?visitor_id=V-1001").json()
        assert sp_one["ok"] and sp_one["edge_count"] == 2

        switches = client.get("/api/open-cap/switches").json()
        assert len(switches) == 3
        assert all({"visitor_id", "old_session_id", "new_session_id",
                    "close_audit_id", "switched_at"} <= set(s) for s in switches)

        # 织机协同：任一访客泳道至多一段 open，旧段全部 closed
        sessions = client.get("/api/sessions").json()
        per_visitor = {}
        for s in sessions:
            per_visitor.setdefault(s["visitor_id"], []).append(s)
        for vid, group in per_visitor.items():
            assert sum(1 for s in group if s["status"] == "open") <= 1

        # 核证主页仍通过
        assert client.get("/api/open-cap/proof").json()["ok"]
