from __future__ import annotations
from fastapi import APIRouter
from app.db import db

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    with db() as conn:
        def cnt(sql, *a):
            return conn.execute(sql, a).fetchone()["c"]

        vaults = [
            dict(x) for x in conn.execute(
                "SELECT id, name, capacity, active FROM vaults ORDER BY id"
            ).fetchall()
        ]
        for v in vaults:
            # 待销毁徽章按挂载间联动：每间在册/已过期与扫描占坑各自可核。
            v["在册分区数"] = cnt(
                "SELECT COUNT(*) AS c FROM partitions WHERE vault_id=? AND status='在册'", v["id"]
            )
            v["待销毁候选"] = cnt(
                "SELECT COUNT(*) AS c FROM partitions WHERE vault_id=? AND status='已过期'", v["id"]
            )
        return {
            "热档": cnt("SELECT COUNT(*) AS c FROM partitions WHERE tier='热档' AND status='在册'"),
            "温档": cnt("SELECT COUNT(*) AS c FROM partitions WHERE tier='温档' AND status='在册'"),
            "冷档": cnt("SELECT COUNT(*) AS c FROM partitions WHERE tier='冷档' AND status='在册'"),
            "迁移执行中": cnt("SELECT COUNT(*) AS c FROM migrate_jobs WHERE status='执行中'"),
            "待销毁候选": cnt("SELECT COUNT(*) AS c FROM partitions WHERE status='已过期'"),
            "已销毁": cnt("SELECT COUNT(*) AS c FROM partitions WHERE status='已销毁'"),
            "扫描中批次": cnt("SELECT COUNT(*) AS c FROM retention_batches WHERE status='扫描中'"),
            "中断批次": cnt("SELECT COUNT(*) AS c FROM retention_batches WHERE status='已中断'"),
            "目录对象": cnt("SELECT COUNT(*) AS c FROM inventory_objects"),
            "最近互证": (
                lambda r: dict(r) if r else None
            )(conn.execute("SELECT status, drift_count, created_at FROM verify_runs ORDER BY id DESC LIMIT 1").fetchone()),
            "片库间": vaults,
        }

