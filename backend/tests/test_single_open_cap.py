"""single_open_cap 验收：夹具制造双 open 风险，验收只剩一个 open。"""

import json
import sqlite3

import pytest

from app.db import connect
from app.modules import single_open_cap as cap
from app.services import ingest


def _opens(conn, vid):
    return conn.execute(
        "SELECT id, status, hit_count FROM sessions WHERE visitor_id=? AND status='open'",
        (vid,),
    ).fetchall()


def _hit_sessions(conn, vid):
    return {
        r["id"]: r["session_id"]
        for r in conn.execute(
            "SELECT id, session_id FROM hits WHERE visitor_id=?", (vid,)
        ).fetchall()
    }


# 1. 硬约束：闸表主键在库层拒绝同访客第二行闸（不靠 LIMIT 1 假装唯一）
def test_gate_table_hard_constraint(fresh_db):
    conn = connect()
    try:
        gate = cap.current_open(conn, "V-1001")
        assert gate, "种子收敛后 V-1001 必须有闸"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO visitor_open(visitor_id, session_id, opened_at) VALUES (?,?,?)",
                ("V-1001", "S-whatever", "2026-11-18T00:00:00Z"),
            )
        conn.rollback()
        # session_id UNIQUE：一个会话至多被一个访客挂为现行 open
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO visitor_open(visitor_id, session_id, opened_at) VALUES (?,?,?)",
                ("V-9999", gate["session_id"], "2026-11-18T00:00:00Z"),
            )
        conn.rollback()
    finally:
        conn.close()


# 2. 落库即立闸：种子 open 会话全部受闸，核证通过
def test_seed_registers_gate_and_proof_ok(fresh_db):
    conn = connect()
    try:
        for vid in ("V-1001", "V-1002"):
            gate = cap.current_open(conn, vid)
            assert gate and gate["session_id"] == f"S-{vid}-1"
        p = cap.proof(conn)
        assert p["ok"], p
        assert p["open_sessions"] == p["gate_rows"] == 2
    finally:
        conn.close()


# 3. 双 open 风险夹具：关旧开新原子承接，验收只剩一个 open
def test_double_open_risk_resolves_to_single_open(fresh_db):
    result = ingest.run_batch("single_open_cap_batch.json")
    assert result["ok"], result
    assert result["report"]["written"] == 6

    conn = connect()
    try:
        # 只剩一个 open：闸挂最新会话，旧两段已 closed
        opens = _opens(conn, "V-1001")
        assert len(opens) == 1
        current = opens[0]["id"]
        gate = cap.current_open(conn, "V-1001")
        assert gate["session_id"] == current
        assert current != "S-V-1001-1"

        # 旧两段皆闭：V-1001 共三段会话，除现行 open 外全部 closed
        closed = conn.execute(
            "SELECT id, status FROM sessions WHERE visitor_id='V-1001' AND status='closed'"
        ).fetchall()
        all_ids = {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM sessions WHERE visitor_id='V-1001'"
            ).fetchall()
        }
        assert len(all_ids) == 3
        assert {r["id"] for r in closed} == all_ids - {current}

        # 关闭审计链：两次关旧开新，后继指针成链
        audits = [
            dict(r) for r in conn.execute(
                "SELECT * FROM session_close_audits WHERE visitor_id='V-1001' ORDER BY id"
            ).fetchall()
        ]
        assert len(audits) == 2
        assert audits[0]["session_id"] == "S-V-1001-1"
        assert audits[0]["reason"] == "sdk_reconnect"
        assert audits[0]["successor_session_id"] == audits[1]["session_id"]
        assert audits[1]["reason"] == "relanding"
        assert audits[1]["successor_session_id"] == current

        # 击点挂对段：强制新会话后的点进新段，普通点挂现行 open
        hs = _hit_sessions(conn, "V-1001")
        assert hs["H-cap-1"] == hs["H-cap-2"] == audits[1]["session_id"]
        assert hs["H-cap-3"] == hs["H-cap-4"] == current

        # 普通访客不受影响：V-1002 续挂原 open；新访客 V-1004 首开立闸
        assert _hit_sessions(conn, "V-1002")["H-cap-5"] == "S-V-1002-1"
        assert len(_opens(conn, "V-1002")) == 1
        assert cap.current_open(conn, "V-1004")["session_id"] == _opens(conn, "V-1004")[0]["id"]

        # 关旧留终态证明（固化成员）
        proof_rows = conn.execute(
            "SELECT hit_ids_json FROM session_proofs WHERE session_id='S-V-1001-1'"
        ).fetchall()
        assert json.loads(proof_rows[-1]["hit_ids_json"]) == ["H1", "H2", "H3"]

        # 核证通过
        assert cap.proof(conn)["ok"]
    finally:
        conn.close()


# 4. 失败闭：批中途失败整体回滚——不存在「新 open 可见旧未关」
def test_failure_rolls_back_whole_batch(fresh_db, tmp_path, monkeypatch):
    bad = tmp_path / "bad_batch.json"
    bad.write_text(
        json.dumps(
            {
                "batch_id": "B-bad",
                "hits": [
                    {
                        "id": "H-bad-1",
                        "visitor_id": "V-1001",
                        "event_ts": "2026-11-17T11:00:00Z",
                        "page_path": "/promo",
                        "event_name": "view",
                        "force_new_session": True,
                    },
                    # 缺 event_ts：第二击点必炸，检验第一击点的关旧开新是否同滚
                    {"id": "H-bad-2", "visitor_id": "V-1001", "page_path": "/x", "event_name": "view"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ingest, "FIXTURES_DIR", tmp_path)
    result = ingest.run_batch("bad_batch.json")
    assert not result["ok"]

    conn = connect()
    try:
        # 旧 open 仍在且唯一，闸未动，无新会话、无审计、无击点
        opens = _opens(conn, "V-1001")
        assert [r["id"] for r in opens] == ["S-V-1001-1"]
        assert cap.current_open(conn, "V-1001")["session_id"] == "S-V-1001-1"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE visitor_id='V-1001'"
        ).fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM session_close_audits").fetchone()["c"] == 0
        assert not conn.execute(
            "SELECT 1 FROM hits WHERE id LIKE 'H-bad-%'"
        ).fetchone()
        assert cap.proof(conn)["ok"]
    finally:
        conn.close()


# 5. 核证只读接口能抓到绕过模块直写的双 open，且收敛后复归单 open
def test_proof_detects_tampered_double_open_then_reconcile(fresh_db):
    conn = connect()
    try:
        # 绕过模块直写第二段 open（模拟历史脏态）
        conn.execute(
            "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)"
            " VALUES ('S-V-1001-rogue', 'V-1001', '2026-11-17T09:00:00Z',"
            " '2026-11-17T09:00:00Z', 0, 'open')"
        )
        conn.commit()
        p = cap.proof(conn)
        assert not p["ok"]
        assert p["multi_open_visitors"][0]["visitor_id"] == "V-1001"
        assert any(
            r["session_id"] == "S-V-1001-rogue" for r in p["ungated_open_sessions"]
        )

        # 收敛：留现行闸所指、关其余（留审计），复归单 open
        cap.reconcile_open_unique(conn)
        conn.commit()
        p2 = cap.proof(conn)
        assert p2["ok"], p2
        opens = _opens(conn, "V-1001")
        assert [r["id"] for r in opens] == ["S-V-1001-1"]
        assert cap.current_open(conn, "V-1001")["session_id"] == "S-V-1001-1"
        audit = conn.execute(
            "SELECT * FROM session_close_audits WHERE session_id='S-V-1001-rogue'"
        ).fetchone()
        assert audit and audit["reason"] == cap.REASON_RECONCILE
    finally:
        conn.close()


# 6. 普通击点续挂现行 open：不开新、不留审计
def test_plain_hit_attaches_to_current_open(fresh_db, tmp_path, monkeypatch):
    fixture = tmp_path / "plain.json"
    fixture.write_text(
        json.dumps(
            {
                "batch_id": "B-plain",
                "hits": [
                    {
                        "id": "H-plain-1",
                        "visitor_id": "V-1001",
                        "event_ts": "2026-11-17T08:20:00Z",
                        "page_path": "/about",
                        "event_name": "view",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ingest, "FIXTURES_DIR", tmp_path)
    assert ingest.run_batch("plain.json")["ok"]

    conn = connect()
    try:
        assert _hit_sessions(conn, "V-1001")["H-plain-1"] == "S-V-1001-1"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE visitor_id='V-1001'"
        ).fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM session_close_audits").fetchone()["c"] == 0
        assert cap.proof(conn)["ok"]
    finally:
        conn.close()


# 7. 接口面：核证/现行闸/审计三只读端点 + 收流跑风险夹具
def test_api_surface(fresh_db):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/ingest/run", json={"fixture": "single_open_cap_batch.json"})
        assert r.status_code == 200 and r.json()["ok"]

        proof = client.get("/api/open-cap/proof").json()
        assert proof["ok"] and proof["open_sessions"] == proof["gate_rows"]

        current = client.get("/api/open-cap/current").json()
        by_visitor = {row["visitor_id"]: row["session_id"] for row in current}
        assert set(by_visitor) == {"V-1001", "V-1002", "V-1004"}

        audits = client.get("/api/open-cap/audits").json()
        assert len(audits) == 2
        assert audits[0]["successor_session_id"] == audits[1]["session_id"]

        # 织机视角：任一访客泳道至多一段 open
        sessions = client.get("/api/sessions").json()
        per_visitor = {}
        for s in sessions:
            if s["status"] == "open":
                per_visitor.setdefault(s["visitor_id"], []).append(s["id"])
        assert all(len(v) == 1 for v in per_visitor.values())
