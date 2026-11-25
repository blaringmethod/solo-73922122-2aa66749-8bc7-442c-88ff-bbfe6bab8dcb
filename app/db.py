"""SQLite 连接与事务工具。

表结构：
- meta：会话级元数据（表头、炉次列/尺寸列选择、行数）。
- rows_a / rows_b：旧表、新表原始行（JSON），按数据行号（从 1 计）。
- orders：按炉次开的单；参数与证据快照后"固定该炉全部新表读数及配对旧值"。
- order_rows：单据内的炉内行（新表行号、新读数、配对旧值、纸档值）。
- paper_records：纸档补录/更正（按新表行号），开单时固化进 order_rows；
  对已开单据则触发整单按合并后证据重算。
- conclusions：每次核算的超差件数区间结论（与纸档在同一事务内一同生效）。
"""
import os
import sqlite3
from contextlib import contextmanager

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
CREATE TABLE IF NOT EXISTS rows_a (
  row_no INTEGER PRIMARY KEY,
  data   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rows_b (
  row_no INTEGER PRIMARY KEY,
  data   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id          INTEGER PRIMARY KEY,
  furnace     TEXT NOT NULL,             -- 炉次值
  tol_lo      TEXT NOT NULL,             -- 公差下限（Decimal 字符串）
  tol_hi      TEXT NOT NULL,
  resolution  TEXT NOT NULL,             -- 新表分辨率
  zero_lo     TEXT NOT NULL,             -- 零点偏差范围
  zero_hi     TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'ok', -- ok=有共同可行偏差 / conflict=冲突
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_rows (
  id          INTEGER PRIMARY KEY,
  order_id    INTEGER NOT NULL,
  new_row_no  INTEGER NOT NULL,
  reading     TEXT NOT NULL,             -- 新表读数（取整后）
  old_value   TEXT,                      -- 配对旧值（无配对为 NULL）
  paper_value TEXT,                      -- 纸档真值（无纸档为 NULL）
  UNIQUE (order_id, new_row_no),
  FOREIGN KEY (order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS paper_records (
  id          INTEGER PRIMARY KEY,
  new_row_no  INTEGER NOT NULL UNIQUE,   -- 按新表行号补录/更正，一行一值
  paper_value TEXT NOT NULL,
  source_order_id INTEGER,               -- 最近一次由哪张单据的补录写入
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conclusions (
  id          INTEGER PRIMARY KEY,
  order_id    INTEGER NOT NULL UNIQUE,   -- 每单保留最新一次结论
  feasible    INTEGER NOT NULL,
  feasible_lo TEXT,
  feasible_hi TEXT,
  min_bad     INTEGER,
  max_bad     INTEGER,
  min_bad_deviation TEXT,
  max_bad_deviation TEXT,
  fixed_bad   INTEGER NOT NULL,
  always_bad_free INTEGER NOT NULL,
  fixed_rows  INTEGER NOT NULL,
  free_rows   INTEGER NOT NULL,
  conflicts   TEXT NOT NULL,             -- JSON
  computed_at TEXT NOT NULL,
  FOREIGN KEY (order_id) REFERENCES orders(id)
);
CREATE INDEX IF NOT EXISTS idx_order_rows_order ON order_rows(order_id);
CREATE INDEX IF NOT EXISTS idx_paper_row ON paper_records(new_row_no);
"""


def connect(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path):
    conn = connect(path)
    try:
        with conn:
            conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(conn):
    """写事务：BEGIN IMMEDIATE 串行化写入；任一步失败整体回滚，原状保留。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
