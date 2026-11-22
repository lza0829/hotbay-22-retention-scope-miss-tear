from __future__ import annotations
import json
from datetime import date
from fastapi import HTTPException
from app.db import db, write_tx
from app.services.core import (
    business_day,
    has_open_migration,
    now,
    partition_age,
)
from app.services import scope_surface

# 全局唯一作用域口径：策略只按片库间整间挂载，禁止键名单。
SCOPE_VAULT = "整间"
VALID_ACTIONS = ("销毁", "只标过期")
VALID_MODES = ("连目录销毁", "只删元数据")


# ---------------------------------------------------------------------------
# 策略挂载
# ---------------------------------------------------------------------------
def _require_vault(conn, vault_id: int):
    v = conn.execute("SELECT * FROM vaults WHERE id=?", (vault_id,)).fetchone()
    if not v or not v["active"]:
        raise HTTPException(400, detail="挂载片库间不可用")
    return v


def list_policies():
    with db() as conn:
        rows = conn.execute(
            '''SELECT p.*, v.name AS vault_name
               FROM retention_policies p JOIN vaults v ON v.id=p.vault_id
               ORDER BY p.id'''
        ).fetchall()
        return [dict(r) for r in rows]


def create_policy(name: str, max_age_days: int, action: str, vault_id: int):
    if not name or not name.strip():
        raise HTTPException(400, detail="策略名不能为空")
    if not isinstance(max_age_days, int) or max_age_days < 0:
        raise HTTPException(400, detail="片龄阈值必须为非负整数")
    if action not in VALID_ACTIONS:
        raise HTTPException(400, detail=f"动作只能是{'/'.join(VALID_ACTIONS)}")
    with write_tx() as conn:
        _require_vault(conn, vault_id)
        if conn.execute("SELECT 1 FROM retention_policies WHERE name=?", (name,)).fetchone():
            raise HTTPException(400, detail="策略名已存在")
        cur = conn.execute(
            "INSERT INTO retention_policies(name, max_age_days, action, active, vault_id) VALUES (?,?,?,1,?)",
            (name.strip(), max_age_days, action, vault_id),
        )
        pid = cur.lastrowid
    return {"id": pid, "name": name, "vault_id": vault_id}


# ---------------------------------------------------------------------------
# 挂载谓词：命中集合的唯一定义，扫描/预览/重放共用，保证可重放。
# 谓词 = 挂间 vault_id + status='在册' + 片龄(营业日-发行日) >= max_age_days。
# ---------------------------------------------------------------------------
def _build_predicate(policy, vault_name: str, today: date) -> str:
    return (
        f"vault_id={policy['vault_id']}（{vault_name}） AND status='在册' AND "
        f"片龄(营业日{today.isoformat()} - 发行日) >= {policy['max_age_days']}天"
    )


def _load_active_policy(conn, policy_id: int):
    pol = conn.execute(
        "SELECT * FROM retention_policies WHERE id=? AND active=1", (policy_id,)
    ).fetchone()
    if not pol:
        raise HTTPException(404, detail="保留策略不存在或已停用")
    return pol


def _candidates(conn, policy, today: date):
    """只取挂间内在册分区；他间分区在 SQL 层即被排除，不可能扫穿。"""
    rows = conn.execute(
        "SELECT * FROM partitions WHERE vault_id=? AND status='在册' ORDER BY id",
        (policy["vault_id"],),
    ).fetchall()
    out = []
    for p in rows:
        age = partition_age(p["release_day"], today)
        if age >= policy["max_age_days"]:
            out.append((p, age))
    return out


def _apply_mark_expired(conn, p, age, policy_id, batch_id, vault_id, mode):
    conn.execute("UPDATE partitions SET status='已过期' WHERE id=?", (p["id"],))
    cur = conn.execute(
        '''INSERT INTO destroy_runs(partition_id, policy_id, batch_id, mode, scope, vault_id,
                                    status, age_days, created_at, note)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (p["id"], policy_id, batch_id, mode, SCOPE_VAULT, vault_id,
         "已标记过期", age, now(), f"片龄{age}天，仅标记不销毁"),
    )
    return {"partition_id": p["id"], "part_key": p["part_key"], "run_id": cur.lastrowid,
            "status": "已标记过期", "age_days": age}


def _apply_destroy(conn, p, age, policy_id, batch_id, vault_id, mode):
    clips = conn.execute("SELECT object_key FROM clips WHERE partition_id=?", (p["id"],)).fetchall()
    if mode == "连目录销毁":
        for c in clips:
            conn.execute("DELETE FROM inventory_objects WHERE object_key=?", (c["object_key"],))
    # 只删元数据模式保留目录对象，仅清除素材段与分区终态。
    conn.execute("DELETE FROM clips WHERE partition_id=?", (p["id"],))
    conn.execute("UPDATE partitions SET status='已销毁' WHERE id=?", (p["id"],))
    cur = conn.execute(
        '''INSERT INTO destroy_runs(partition_id, policy_id, batch_id, mode, scope, vault_id,
                                    status, age_days, created_at, note)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (p["id"], policy_id, batch_id, mode, SCOPE_VAULT, vault_id,
         "已销毁", age, now(), f"片龄{age}天，{mode}"),
    )
    return {"partition_id": p["id"], "part_key": p["part_key"], "run_id": cur.lastrowid,
            "status": "已销毁", "age_days": age}


# ---------------------------------------------------------------------------
# 干跑预览：不落任何写操作，按挂载谓词给出将命中/将跳过集合，供策略页核对。
# ---------------------------------------------------------------------------
def preview_retention(policy_id: int):
    with db() as conn:
        pol = _load_active_policy(conn, policy_id)
        vault = _require_vault(conn, pol["vault_id"])
        today = business_day(conn)
        matched, skipped = [], []
        for p, age in _candidates(conn, pol, today):
            item = {"partition_id": p["id"], "part_key": p["part_key"],
                    "vault_id": p["vault_id"], "age_days": age, "tier": p["tier"]}
            if has_open_migration(conn, p["id"]):
                item["reason"] = "迁移占坑，扫描跳过"
                skipped.append(item)
            else:
                matched.append(item)
        return {
            "policy": pol["name"], "scope": SCOPE_VAULT,
            "vault_id": vault["id"], "vault_name": vault["name"],
            "business_day": today.isoformat(),
            "predicate": _build_predicate(pol, vault["name"], today),
            "matched": matched, "skipped": skipped,
        }


# ---------------------------------------------------------------------------
# 正式扫描：串行写事务 + 批次回执，保证终态可解释、命中可重放。
# ---------------------------------------------------------------------------
def run_retention(policy_id: int, mode: str = "连目录销毁"):
    if mode not in VALID_MODES:
        raise HTTPException(400, detail="销毁模式无效")
    # 批次 id 需在闭包外可见，供回滚回调落「已中断」终态。
    state: dict = {}

    def _mark_interrupted(conn):
        bid = state.get("batch_id")
        if bid is not None:
            conn.execute(
                "UPDATE retention_batches SET status='已中断', finished_at=?, note='扫描异常回滚' WHERE id=?",
                (now(), bid),
            )

    # BEGIN IMMEDIATE 立即取写锁：并发保留/迁移在此排队，杜绝交叉提交。
    with write_tx(on_rollback=_mark_interrupted) as conn:
        pol = _load_active_policy(conn, policy_id)
        vault = _require_vault(conn, pol["vault_id"])
        # 同库不允许并发扫描批次，避免重复销毁。
        if conn.execute("SELECT 1 FROM retention_batches WHERE status='扫描中'").fetchone():
            raise HTTPException(409, detail="已有扫描中的保留批次，请待其结束")
        today = business_day(conn)
        predicate = _build_predicate(pol, vault["name"], today)
        batch = conn.execute(
            '''INSERT INTO retention_batches(policy_id, vault_id, scope, mode, business_day,
                                             predicate, status, created_at)
               VALUES (?,?,?,?,?,?,'扫描中',?)''',
            (pol["id"], pol["vault_id"], SCOPE_VAULT, mode, today.isoformat(), predicate, now()),
        )
        batch_id = batch.lastrowid
        state["batch_id"] = batch_id
        acted, skipped = [], []
        candidates = _candidates(conn, pol, today)
        for p, age in candidates:
            if has_open_migration(conn, p["id"]):
                skipped.append({"partition_id": p["id"], "part_key": p["part_key"],
                                "age_days": age, "reason": "迁移占坑，扫描跳过"})
                continue
            if pol["action"] == "只标过期":
                acted.append(_apply_mark_expired(
                    conn, p, age, pol["id"], batch_id, pol["vault_id"], mode))
            else:
                acted.append(_apply_destroy(
                    conn, p, age, pol["id"], batch_id, pol["vault_id"], mode))
        detail = json.dumps({"acted": acted, "skipped": skipped}, ensure_ascii=False)
        conn.execute(
            '''UPDATE retention_batches SET status='已完成', finished_at=?,
               matched_count=?, acted_count=?, skipped_count=?, detail=? WHERE id=?''',
            (now(), len(candidates), len(acted), len(skipped), detail, batch_id),
        )
        return {
            "batch_id": batch_id, "policy": pol["name"], "scope": SCOPE_VAULT,
            "vault_id": vault["id"], "vault_name": vault["name"],
            "business_day": today.isoformat(), "predicate": predicate,
            "matched_count": len(candidates), "affected": acted, "skipped": skipped,
        }


# ---------------------------------------------------------------------------
# 批次与流水
# ---------------------------------------------------------------------------
def list_batches():
    with db() as conn:
        rows = conn.execute(
            '''SELECT b.*, p.name AS policy_name, v.name AS vault_name
               FROM retention_batches b
               JOIN retention_policies p ON p.id=b.policy_id
               JOIN vaults v ON v.id=b.vault_id
               ORDER BY b.id DESC'''
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail_obj"] = json.loads(d.get("detail") or "[]")
            except json.JSONDecodeError:
                d["detail_obj"] = []
            out.append(d)
        return out


def list_destroys():
    with db() as conn:
        rows = conn.execute(
            '''SELECT d.*, p.part_key, p.title, pol.name AS policy_name, v.name AS vault_name
               FROM destroy_runs d
               JOIN partitions p ON p.id=d.partition_id
               JOIN retention_policies pol ON pol.id=d.policy_id
               LEFT JOIN vaults v ON v.id=d.vault_id
               ORDER BY d.id DESC'''
        ).fetchall()
        return [dict(r) for r in rows]


def replay_batch(batch_id: int):
    """由批次记录的挂载谓词重放命中集合，并与批次实际动作对账。"""
    with db() as conn:
        b = conn.execute("SELECT * FROM retention_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(404, detail="扫描批次不存在")
        pol = conn.execute("SELECT * FROM retention_policies WHERE id=?", (b["policy_id"],)).fetchone()
        today = date.fromisoformat(b["business_day"])
        # 用批次当时的营业日与挂载谓词重新求命中集合（含已被本批次销毁、当前非在册的分区）。
        rows = conn.execute(
            "SELECT * FROM partitions WHERE vault_id=? ORDER BY id", (b["vault_id"],)
        ).fetchall()
        replay_hits = []
        for p in rows:
            age = partition_age(p["release_day"], today)
            if age >= pol["max_age_days"]:
                replay_hits.append({
                    "partition_id": p["id"], "part_key": p["part_key"],
                    "age_days": age, "current_status": p["status"],
                })
        actual = [dict(r) for r in conn.execute(
            "SELECT dr.partition_id, p.part_key, dr.status, dr.age_days FROM destroy_runs dr "
            "JOIN partitions p ON p.id=dr.partition_id WHERE dr.batch_id=? ORDER BY dr.id",
            (batch_id,),
        ).fetchall()]
        return {
            "batch_id": batch_id, "scope": b["scope"], "vault_id": b["vault_id"],
            "business_day": b["business_day"], "predicate": b["predicate"],
            "replay_hit_count": len(replay_hits), "replay_hits": replay_hits,
            "batch_status": b["status"], "actual_actions": actual,
        }

_run_retention_impl = run_retention


def run_retention(policy_id: int, mode: str = "连目录销毁"):
    out = _run_retention_impl(policy_id, mode=mode)
    return scope_surface.after_retention(out)

