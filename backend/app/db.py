from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

# ============================================================================
# 架构口径（全局唯一，不得混用）：
#   保留策略「挂载」听【片库间】——策略绑定唯一 vault_id。
#   执行只扫该间 status='在册' 且片龄达标的分区；他间任何分区都不受影响。
#   作用域恒为「整间」，命中集合必须能由挂载谓词重放。
#   本系统【不】支持按显式分区键名单挂载（方案乙），接口层以 extra='forbid' 拒绝。
# ============================================================================

SCHEMA = '''
CREATE TABLE IF NOT EXISTS vaults (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  capacity INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS partitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  release_day TEXT NOT NULL,
  vault_id INTEGER NOT NULL,
  tier TEXT NOT NULL,
  object_prefix TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '在册',
  created_at TEXT NOT NULL,
  FOREIGN KEY(vault_id) REFERENCES vaults(id)
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  object_key TEXT NOT NULL UNIQUE,
  bytes INTEGER NOT NULL,
  entered_day TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);
CREATE TABLE IF NOT EXISTS migrate_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  from_tier TEXT NOT NULL,
  to_tier TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);
CREATE TABLE IF NOT EXISTS retention_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  max_age_days INTEGER NOT NULL,
  action TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  -- 挂载点：策略绑定的片库间。全局只认片库间挂载，不设任何键名单列。
  vault_id INTEGER NOT NULL,
  FOREIGN KEY(vault_id) REFERENCES vaults(id)
);
-- 保留扫描批次（一次执行一行），承载可重放的挂载谓词与明确终态。
CREATE TABLE IF NOT EXISTS retention_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  policy_id INTEGER NOT NULL,
  vault_id INTEGER NOT NULL,
  scope TEXT NOT NULL DEFAULT '整间',
  mode TEXT NOT NULL,
  business_day TEXT NOT NULL,
  predicate TEXT NOT NULL,
  status TEXT NOT NULL,
  matched_count INTEGER NOT NULL DEFAULT 0,
  acted_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  -- 扫描启动时分区表的最大 id 水位：扫描持写锁，之后登记的分区 id 必大于此值，
  -- 重放按 id<=水位 还原当时可见集合，不受秒后登记的老片污染。
  snapshot_max_pid INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(policy_id) REFERENCES retention_policies(id),
  FOREIGN KEY(vault_id) REFERENCES vaults(id)
);
CREATE TABLE IF NOT EXISTS destroy_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  policy_id INTEGER NOT NULL,
  batch_id INTEGER,
  mode TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '整间',
  vault_id INTEGER,
  status TEXT NOT NULL,
  age_days INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id),
  FOREIGN KEY(policy_id) REFERENCES retention_policies(id),
  FOREIGN KEY(batch_id) REFERENCES retention_batches(id),
  FOREIGN KEY(vault_id) REFERENCES vaults(id)
);
CREATE TABLE IF NOT EXISTS inventory_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  object_key TEXT NOT NULL UNIQUE,
  bytes INTEGER NOT NULL,
  vault_name TEXT NOT NULL,
  scanned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verify_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  drift_count INTEGER NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
'''


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """对旧库做幂等轻量迁移：补挂载列、批次表、流水作用域列，并回收半截批次。"""
    # retention_policies.vault_id（旧库无此列）。先加可空，回填后由应用保证非空。
    pol_cols = _columns(conn, "retention_policies")
    if "vault_id" not in pol_cols:
        conn.execute("ALTER TABLE retention_policies ADD COLUMN vault_id INTEGER")
        # 按既有种子策略名回填挂载间；其余旧策略默认挂主厅成片间(id=1)。
        name_vault = {
            "常规片龄九十天": "主厅成片间",
            "宣发快取三十天": "宣发快取间",
        }
        for r in conn.execute("SELECT id, name FROM retention_policies").fetchall():
            v = conn.execute(
                "SELECT id FROM vaults WHERE name=?",
                (name_vault.get(r["name"], "主厅成片间"),),
            ).fetchone()
            vid = v["id"] if v else 1
            conn.execute(
                "UPDATE retention_policies SET vault_id=? WHERE id=?",
                (vid, r["id"]),
            )

    # destroy_runs 作用域与批次关联列。
    dr_cols = _columns(conn, "destroy_runs")
    if "batch_id" not in dr_cols:
        conn.execute("ALTER TABLE destroy_runs ADD COLUMN batch_id INTEGER")
    if "scope" not in dr_cols:
        conn.execute("ALTER TABLE destroy_runs ADD COLUMN scope TEXT NOT NULL DEFAULT '整间'")
    if "vault_id" not in dr_cols:
        conn.execute("ALTER TABLE destroy_runs ADD COLUMN vault_id INTEGER")
    if "age_days" not in dr_cols:
        conn.execute("ALTER TABLE destroy_runs ADD COLUMN age_days INTEGER NOT NULL DEFAULT 0")
    # 旧流水回填所属间，便于按间重放与审计。
    conn.execute(
        '''UPDATE destroy_runs
           SET vault_id=(SELECT vault_id FROM partitions WHERE partitions.id=destroy_runs.partition_id)
           WHERE vault_id IS NULL'''
    )

    # 崩溃在「扫描中」的批次没有明确终态：启动时一律回收为「已中断」，不留不可解释状态。
    if _columns(conn, "retention_batches"):  # 表已由 SCHEMA 建立
        conn.execute(
            "UPDATE retention_batches SET status='已中断', note=COALESCE(NULLIF(note,''),'进程中断，批次回收') || '（启动回收）' WHERE status='扫描中'"
        )
        # 旧库批次表补登记水位列（重放还原文见「snapshot_max_pid」）。
        if "snapshot_max_pid" not in _columns(conn, "retention_batches"):
            conn.execute(
                "ALTER TABLE retention_batches ADD COLUMN snapshot_max_pid INTEGER NOT NULL DEFAULT 0"
            )

    # 清除历史「假批次表面」残留键：批次成功只认库内可重放证据，不允许任何旁路面板。
    conn.execute(
        "DELETE FROM settings WHERE key IN ('scope_surface_last', 'scope_surface_ok')"
    )


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 写事务串行化：并发保留/迁移在取写锁处排队，杜绝交叉提交。
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx(on_rollback=None):
    """立即取写锁的串行化事务（BEGIN IMMEDIATE），用于保留扫描与迁移占坑。

    on_rollback: 异常回滚后的回调，接收同一连接（已回到自动提交态），
                 用于写入「已中断」等失败终态，保证不留不可解释的中间状态。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        if on_rollback is not None:
            try:
                on_rollback(conn)
                conn.commit()  # 回调写入的失败终态必须落库，否则连接关闭会被回滚。
            except Exception:
                pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate_legacy(conn)
