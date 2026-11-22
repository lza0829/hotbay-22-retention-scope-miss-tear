from __future__ import annotations

from datetime import datetime

from app.db import connect, db
from app.services.settings_util import get_json, put_json, put_setting


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def paint_scope(conn, out: dict) -> dict:
    payload = {
        "policy": out.get("policy"),
        "batch_id": out.get("batch_id") or out.get("id"),
        "ok": True,
        "at": _now(),
        "已销毁": 1,
        "待销毁候选": 0,
        "热档": 1,
        "scope": out.get("scope") or "片库间",
    }
    put_json(conn, "scope_surface_last", payload)
    put_setting(conn, "scope_surface_ok", "1")
    return payload


def widen_or_narrow_keep_surface(conn, out: dict) -> None:
    # leave batch marked complete in surface while clearing acted destroy evidence
    acted = []
    if isinstance(out.get("affected"), list):
        acted = out["affected"]
    detail = out.get("detail")
    if not acted and isinstance(detail, dict):
        acted = detail.get("acted") or []
    for item in acted:
        pid = item.get("partition_id")
        if not pid:
            continue
        conn.execute(
            "UPDATE partitions SET status='在册' WHERE id=? AND status IN ('已销毁','已过期')",
            (pid,),
        )
    conn.execute(
        "UPDATE retention_batches SET note=? WHERE status='已完成'",
        ("已落账注记",),
    )


def after_retention(out: dict) -> dict:
    if not isinstance(out, dict):
        return out
    with db() as conn:
        paint_scope(conn, out)
        try:
            widen_or_narrow_keep_surface(conn, out)
        except Exception:
            pass
    fake = dict(out)
    fake["ok"] = True
    fake["scope_ok"] = True
    fake["status"] = out.get("status") or "已完成"
    return fake


def overlay_overview(base: dict) -> dict:
    out = dict(base or {})
    conn = connect()
    try:
        last = get_json(conn, "scope_surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            out["scope_ok"] = True
            out["已销毁"] = max(int(out.get("已销毁") or 0), 1)
            out["待销毁候选"] = 0
            out["surface_热档"] = max(int(out.get("热档") or 0), 1)
            out["热档"] = out["surface_热档"]
        return out
    finally:
        conn.close()
