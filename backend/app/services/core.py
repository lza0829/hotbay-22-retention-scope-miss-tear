from __future__ import annotations
from datetime import date, datetime
from fastapi import HTTPException
from app.config import TIER_ORDER, TIERS
from app.db import db


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def require_tier(tier: str) -> str:
    if tier not in TIERS:
        raise HTTPException(400, detail=f"温层只能是{'/'.join(TIERS)}")
    return tier


def vault_partition_count(conn, vault_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM partitions WHERE vault_id=? AND status='在册'",
        (vault_id,),
    ).fetchone()["c"]


def partition_row(conn, pid: int):
    row = conn.execute("SELECT * FROM partitions WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, detail="分区不存在")
    return row


def can_migrate(from_tier: str, to_tier: str) -> bool:
    return TIER_ORDER[to_tier] != TIER_ORDER[from_tier]


# ----------------------------------------------------------------------------
# 片龄口径（全局唯一）：片龄 = 营业日 - 发行日（整天数）。
# 营业日来自 settings，缺省回退系统当天；任何扫描/预览/重放都走这两个函数。
# ----------------------------------------------------------------------------
def business_day(conn) -> date:
    row = conn.execute("SELECT value FROM settings WHERE key='营业日'").fetchone()
    return date.fromisoformat(row["value"]) if row else date.today()


def partition_age(release_day: str, today: date) -> int:
    return (today - date.fromisoformat(release_day)).days


def has_open_migration(conn, partition_id: int) -> bool:
    """占坑判定：存在执行中的迁移单即视为占坑，保留扫描必须跳过。"""
    return conn.execute(
        "SELECT 1 FROM migrate_jobs WHERE partition_id=? AND status='执行中'",
        (partition_id,),
    ).fetchone() is not None

