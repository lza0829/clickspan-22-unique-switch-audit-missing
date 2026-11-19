import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.db import FIXTURES_DIR, connect
from app.modules import single_open_cap


def run_batch(fixture_name: str | None = None) -> dict:
    """基座装载：按夹具追加击点；超时切会话 / 脏点隔离留给功能题空桩。

    同访客单 open 由 single_open_cap 闸表保证：挂点/关旧/开新/改挂同事务。
    """
    name = fixture_name or "ingest_batch.json"
    if Path(name).name != name or not name.endswith(".json"):
        return {"ok": False, "error": "非法夹具名"}
    path = FIXTURES_DIR / name
    if not path.exists():
        return {"ok": False, "error": f"缺少 {name}"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    hits = payload.get("hits", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        written = 0
        for hit in hits:
            vid = hit["visitor_id"]
            exists = conn.execute("SELECT 1 FROM visitors WHERE id=?", (vid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
                    (vid, hit.get("visitor_label") or vid, _now()),
                )
            # 单 open 闸：有闸挂现行 open；强制新会话则同事务关旧/开新/改挂
            sid = single_open_cap.resolve_session_id(conn, vid, hit)
            conn.execute(
                """
                INSERT OR IGNORE INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hit["id"],
                    vid,
                    sid,
                    hit["event_ts"],
                    hit["page_path"],
                    hit["event_name"],
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                written += 1
                conn.execute(
                    """
                    UPDATE sessions
                    SET hit_count = hit_count + 1,
                        ended_at = CASE
                          WHEN ended_at IS NULL OR ended_at < ? THEN ?
                          ELSE ended_at END
                    WHERE id=?
                    """,
                    (hit["event_ts"], hit["event_ts"], sid),
                )
                ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
                        (sid,),
                    ).fetchall()
                ]
                sess_row = conn.execute(
                    "SELECT * FROM sessions WHERE id=?", (sid,)
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO session_proofs(
                      session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        sid,
                        vid,
                        json.dumps(ids, ensure_ascii=False),
                        sess_row["hit_count"],
                        sess_row["started_at"],
                        sess_row["ended_at"],
                        _now(),
                    ),
                )
        report = {"batch_id": batch_id, "written": written, "input": len(hits), "fixture": name}
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _view_compat_payload(payload: dict) -> dict:
    """读路径字段别名兼容，可与写路径短暂分叉。"""
    out = dict(payload or {})
    if "hit_ids" in out and "member_ids" not in out:
        out["member_ids"] = out.get("hit_ids")
    if "content_hash" in out and "digest" not in out:
        out["digest"] = out.get("content_hash")
    if "gap_sec" in out and "gap" not in out:
        out["gap"] = out.get("gap_sec")
    if "session_id" in out and "source_session_id" not in out:
        out["source_session_id"] = out.get("session_id")
    if "verified" not in out:
        out["verified"] = True
    return out


def _strand_read_fallback(rows: list) -> list:
    """珠串读路径保留全量点，由页面自行分段。"""
    return list(rows or [])


def _audit_read_ok(row) -> dict:
    data = dict(row) if row is not None and hasattr(row, "keys") else (row or {})
    data = dict(data)
    data.setdefault("aligned", True)
    data.setdefault("writable", True)
    return data

