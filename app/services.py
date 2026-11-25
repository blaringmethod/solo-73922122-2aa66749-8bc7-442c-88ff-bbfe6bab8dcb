"""业务逻辑：上传台账 → 选炉次/尺寸列 → 按炉次开单 → 纸档补录 → 超差区间核算。

关键约定：
- 全部数值用 Decimal 精确小数运算，区间均含端点。
- 开单即"固定该炉全部新表读数及配对旧值"：行数据快照进 order_rows，
  之后重新上传或重开不影响已开单据；纸档按新表行号补录，开单时固化。
- 补录/更正与核查结论在同一事务内写库（先落纸档，再重算结论），
  任一步失败整体回滚，原状保留。
- 无共同可行偏差（冲突）时仍收录纸档，撤下旧的件数区间并报冲突。
- 核算走 audit.compute_audit：O(n log n) 区间扫掠，不枚举偏差刻度，
  也不对每个边界重扫全炉。
"""
import csv
import io
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from . import audit
from .db import transaction

MAX_ROWS = 20000


class ServiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- 基础工具

def get_meta(conn, key, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )


def parse_decimal(text, label):
    """严格解析十进制小数（拒绝科学计数法以外的非法输入；空值报错）。"""
    if text is None or str(text).strip() == "":
        raise ServiceError(f"{label}不能为空")
    try:
        return Decimal(str(text).strip())
    except InvalidOperation:
        raise ServiceError(f"{label}不是合法小数：{text!r}")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_rows(conn, table):
    return [
        json.loads(r["data"])
        for r in conn.execute(f"SELECT data FROM {table} ORDER BY row_no")
    ]


# ---------------------------------------------------------------- 上传

def _parse_csv(raw, label):
    if not raw:
        raise ServiceError(f"{label}文件为空")
    text = None
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ServiceError(f"{label}编码无法识别（仅支持 UTF-8 / GB18030）")

    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if not rows:
        raise ServiceError(f"{label}没有内容")
    header = [c.strip() for c in rows[0]]
    if not header or any(not h for h in header):
        raise ServiceError(f"{label}表头存在空列名")
    if len(set(header)) != len(header):
        raise ServiceError(f"{label}表头存在重复列名")

    data = []
    for i, r in enumerate(rows[1:], start=1):
        if len(r) != len(header):
            raise ServiceError(f"{label}第 {i} 行列数（{len(r)}）与表头（{len(header)}）不一致")
        if len(data) >= MAX_ROWS:
            raise ServiceError(f"{label}超过 {MAX_ROWS} 行上限")
        data.append(dict(zip(header, (c.strip() for c in r))))
    if not data:
        raise ServiceError(f"{label}没有数据行")
    return header, data


def load_session(conn, file_a, file_b):
    """上传两份同表头 CSV（旧表 file_a、新表 file_b），重置会话与全部单据。"""
    header_a, rows_a = _parse_csv(file_a.read(), "旧表")
    header_b, rows_b = _parse_csv(file_b.read(), "新表")
    if header_a != header_b:
        raise ServiceError("两份 CSV 表头不一致，无法建对照")

    with transaction(conn):
        # 先子表后父表，满足外键约束
        for t in ("conclusions", "order_rows", "orders", "paper_records", "rows_a", "rows_b"):
            conn.execute(f"DELETE FROM {t}")
        conn.executemany(
            "INSERT INTO rows_a(row_no, data) VALUES(?, ?)",
            [(i, json.dumps(r, ensure_ascii=False)) for i, r in enumerate(rows_a, 1)],
        )
        conn.executemany(
            "INSERT INTO rows_b(row_no, data) VALUES(?, ?)",
            [(i, json.dumps(r, ensure_ascii=False)) for i, r in enumerate(rows_b, 1)],
        )
        for k in ("furnace_col", "size_col", "pair_col"):
            conn.execute("DELETE FROM meta WHERE k=?", (k,))
        set_meta(conn, "header", json.dumps(header_a, ensure_ascii=False))
        set_meta(conn, "n_a", str(len(rows_a)))
        set_meta(conn, "n_b", str(len(rows_b)))
    return header_a, len(rows_a), len(rows_b)


# ---------------------------------------------------------------- 列选择

def set_columns(conn, furnace_col, size_col, pair_col=None):
    header = json.loads(get_meta(conn, "header", "null"))
    if not header:
        raise ServiceError("请先上传两份 CSV", 409)
    for name, col in (("炉次列", furnace_col), ("尺寸列", size_col)):
        if not col:
            raise ServiceError(f"请选择{name}")
        if col not in header:
            raise ServiceError(f"{name}不在表头中：{col}")
    if pair_col:
        if pair_col not in header:
            raise ServiceError(f"配对列不在表头中：{pair_col}")
    with transaction(conn):
        set_meta(conn, "furnace_col", furnace_col)
        set_meta(conn, "size_col", size_col)
        set_meta(conn, "pair_col", pair_col or "")


# ---------------------------------------------------------------- 配对与炉次

def _pair_old_by(rows_a, pair_col):
    """按配对列建 旧值 映射：同值取首次出现行；空值不参与配对。"""
    mapping = {}
    if not pair_col:
        return mapping
    for r in rows_a:
        key = r.get(pair_col, "")
        if key and key not in mapping:
            mapping[key] = r
    return mapping


def list_furnaces(conn):
    """新表中的全部炉次值及行数（按炉次值排序）。"""
    furnace_col = get_meta(conn, "furnace_col")
    if not furnace_col:
        raise ServiceError("请先选择炉次列与尺寸列", 409)
    rows_b = _load_rows(conn, "rows_b")
    counts = {}
    for r in rows_b:
        v = r.get(furnace_col, "")
        counts[v] = counts.get(v, 0) + 1
    return [{"furnace": f, "rows": counts[f]} for f in sorted(counts)]


# ---------------------------------------------------------------- 开单

def _build_order_rows(conn, furnace, size_col, furnace_col, pair_col):
    rows_a = _load_rows(conn, "rows_a")
    rows_b = _load_rows(conn, "rows_b")
    old_map = _pair_old_by(rows_a, pair_col)
    paper_map = {
        r["new_row_no"]: r["paper_value"]
        for r in conn.execute("SELECT new_row_no, paper_value FROM paper_records")
    }

    out = []
    for idx, r in enumerate(rows_b, 1):
        if r.get(furnace_col, "") != furnace:
            continue
        reading = parse_decimal(r.get(size_col, ""), f"新表第 {idx} 行尺寸值")
        old_value = None
        if pair_col:
            old_row = old_map.get(r.get(pair_col, ""))
            if old_row is not None:
                old_value = parse_decimal(
                    old_row.get(size_col, ""), f"配对旧表行尺寸值（{pair_col}={r.get(pair_col)}）"
                )
        paper = paper_map.get(idx)
        out.append(
            {
                "new_row_no": idx,
                "reading": reading,
                "old_value": old_value,
                "paper_value": Decimal(paper) if paper is not None else None,
            }
        )
    if not out:
        raise ServiceError(f"新表中不存在炉次 {furnace!r} 的记录", 404)
    return out


def _persist_conclusion(conn, order_id, res: audit.AuditResult):
    conn.execute("DELETE FROM conclusions WHERE order_id=?", (order_id,))
    conn.execute(
        "INSERT INTO conclusions(order_id, feasible, feasible_lo, feasible_hi, "
        "min_bad, max_bad, min_bad_deviation, max_bad_deviation, fixed_bad, "
        "always_bad_free, fixed_rows, free_rows, conflicts, computed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            order_id,
            1 if res.feasible else 0,
            str(res.feasible_lo) if res.feasible_lo is not None else None,
            str(res.feasible_hi) if res.feasible_hi is not None else None,
            res.min_bad,
            res.max_bad,
            str(res.min_bad_deviation) if res.min_bad_deviation is not None else None,
            str(res.max_bad_deviation) if res.max_bad_deviation is not None else None,
            res.fixed_bad,
            res.always_bad_free,
            res.fixed_rows,
            res.free_rows,
            json.dumps(res.conflicts, ensure_ascii=False),
            _now(),
        ),
    )


def _audit_from_order(conn, order_id):
    o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if o is None:
        raise ServiceError("单据不存在", 404)
    params = audit.Params(
        tol_lo=Decimal(o["tol_lo"]),
        tol_hi=Decimal(o["tol_hi"]),
        resolution=Decimal(o["resolution"]),
        zero_lo=Decimal(o["zero_lo"]),
        zero_hi=Decimal(o["zero_hi"]),
    )
    rows = [
        audit.Row(
            new_row_no=r["new_row_no"],
            new_reading=Decimal(r["reading"]),
            old_value=Decimal(r["old_value"]) if r["old_value"] is not None else None,
            paper_value=Decimal(r["paper_value"]) if r["paper_value"] is not None else None,
        )
        for r in conn.execute(
            "SELECT * FROM order_rows WHERE order_id=? ORDER BY new_row_no", (order_id,)
        )
    ]
    return o, params, audit.compute_audit(params, rows)


def open_order(conn, furnace, tol_lo, tol_hi, resolution, zero_lo, zero_hi):
    """按同一份在供对照对指定炉次开单：快照读数/旧值/纸档并立即核算。"""
    furnace_col = get_meta(conn, "furnace_col")
    size_col = get_meta(conn, "size_col")
    pair_col = get_meta(conn, "pair_col", "")
    if not furnace_col or not size_col:
        raise ServiceError("请先选择炉次列与尺寸列", 409)

    params = audit.Params(
        tol_lo=parse_decimal(tol_lo, "公差下限"),
        tol_hi=parse_decimal(tol_hi, "公差上限"),
        resolution=parse_decimal(resolution, "新表分辨率"),
        zero_lo=parse_decimal(zero_lo, "零点偏差下限"),
        zero_hi=parse_decimal(zero_hi, "零点偏差上限"),
    )
    if params.tol_lo > params.tol_hi:
        raise ServiceError("公差下限不得大于上限")
    if params.resolution <= 0:
        raise ServiceError("新表分辨率须为正数")
    if params.zero_lo > params.zero_hi:
        raise ServiceError("零点偏差下限不得大于上限")

    built = _build_order_rows(conn, furnace, size_col, furnace_col, pair_col)
    rows = [
        audit.Row(
            new_row_no=b["new_row_no"],
            new_reading=b["reading"],
            old_value=b["old_value"],
            paper_value=b["paper_value"],
        )
        for b in built
    ]
    res = audit.compute_audit(params, rows)

    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO orders(furnace, tol_lo, tol_hi, resolution, zero_lo, zero_hi, "
            "status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                furnace,
                str(params.tol_lo), str(params.tol_hi), str(params.resolution),
                str(params.zero_lo), str(params.zero_hi),
                "ok" if res.feasible else "conflict",
                _now(),
            ),
        )
        order_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO order_rows(order_id, new_row_no, reading, old_value, paper_value) "
            "VALUES (?,?,?,?,?)",
            [
                (order_id, b["new_row_no"], str(b["reading"]),
                 str(b["old_value"]) if b["old_value"] is not None else None,
                 str(b["paper_value"]) if b["paper_value"] is not None else None)
                for b in built
            ],
        )
        _persist_conclusion(conn, order_id, res)
    return order_id, res


# ---------------------------------------------------------------- 纸档补录

def _get_order(conn, order_id):
    o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if o is None:
        raise ServiceError("单据不存在", 404)
    return o


def upsert_paper(conn, new_row_no, paper_value, order_id):
    """按新表行号补录/更正纸档：纸档与重算结论同一事务一同生效。

    并发补录不同行：BEGIN IMMEDIATE 串行化写入，各自的行均保留；
    补录同一行即更正（覆盖旧值）。冲突单据也照常收录纸档并重算。
    """
    n_b = int(get_meta(conn, "n_b", "0"))
    if not (1 <= new_row_no <= n_b):
        raise ServiceError(f"新表行号须在 1~{n_b} 之间")
    value = parse_decimal(paper_value, "纸档值")
    _get_order(conn, order_id)

    with transaction(conn):
        now = _now()
        conn.execute(
            "INSERT INTO paper_records(new_row_no, paper_value, source_order_id, "
            "created_at, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(new_row_no) DO UPDATE SET paper_value=excluded.paper_value, "
            "source_order_id=excluded.source_order_id, updated_at=excluded.updated_at",
            (new_row_no, str(value), order_id, now, now),
        )
        # 固化进该单据对应行（若该行属于本炉）
        cur = conn.execute(
            "UPDATE order_rows SET paper_value=? WHERE order_id=? AND new_row_no=?",
            (str(value), order_id, new_row_no),
        )
        touched = cur.rowcount > 0
        # 按合并后的证据整单重算（一次 O(n log n)，不按边界重扫）
        o, _params, res = _audit_from_order(conn, order_id)
        conn.execute(
            "UPDATE orders SET status=? WHERE id=?",
            ("ok" if res.feasible else "conflict", order_id),
        )
        _persist_conclusion(conn, order_id, res)
    return touched, res


# ---------------------------------------------------------------- 查询

def _conclusion_dict(conn, order_id):
    c = conn.execute(
        "SELECT * FROM conclusions WHERE order_id=?", (order_id,)
    ).fetchone()
    if c is None:
        return None
    return {
        "feasible": bool(c["feasible"]),
        "feasible_lo": c["feasible_lo"],
        "feasible_hi": c["feasible_hi"],
        "min_bad": c["min_bad"],
        "max_bad": c["max_bad"],
        "min_bad_deviation": c["min_bad_deviation"],
        "max_bad_deviation": c["max_bad_deviation"],
        "fixed_bad": c["fixed_bad"],
        "always_bad_free": c["always_bad_free"],
        "fixed_rows": c["fixed_rows"],
        "free_rows": c["free_rows"],
        "conflicts": json.loads(c["conflicts"]),
        "computed_at": c["computed_at"],
    }


def get_order_detail(conn, order_id):
    o = _get_order(conn, order_id)
    rows = [
        {
            "new_row_no": r["new_row_no"],
            "reading": r["reading"],
            "old_value": r["old_value"],
            "paper_value": r["paper_value"],
            "evidence": r["paper_value"] if r["paper_value"] is not None else r["old_value"],
            "evidence_kind": (
                "paper" if r["paper_value"] is not None
                else ("old" if r["old_value"] is not None else "none")
            ),
        }
        for r in conn.execute(
            "SELECT * FROM order_rows WHERE order_id=? ORDER BY new_row_no", (order_id,)
        )
    ]
    return {
        "id": o["id"],
        "furnace": o["furnace"],
        "params": {
            "tol_lo": o["tol_lo"], "tol_hi": o["tol_hi"],
            "resolution": o["resolution"],
            "zero_lo": o["zero_lo"], "zero_hi": o["zero_hi"],
        },
        "status": o["status"],
        "created_at": o["created_at"],
        "rows": rows,
        "conclusion": _conclusion_dict(conn, o["id"]),
    }


def list_orders(conn):
    out = []
    for o in conn.execute("SELECT * FROM orders ORDER BY id"):
        c = _conclusion_dict(conn, o["id"])
        out.append(
            {
                "id": o["id"],
                "furnace": o["furnace"],
                "status": o["status"],
                "row_count": conn.execute(
                    "SELECT COUNT(*) AS n FROM order_rows WHERE order_id=?", (o["id"],)
                ).fetchone()["n"],
                "conclusion": c,
                "created_at": o["created_at"],
            }
        )
    return out


def list_paper(conn):
    return [
        {
            "new_row_no": r["new_row_no"],
            "paper_value": r["paper_value"],
            "source_order_id": r["source_order_id"],
            "updated_at": r["updated_at"],
        }
        for r in conn.execute(
            "SELECT * FROM paper_records ORDER BY new_row_no"
        )
    ]


def get_state(conn):
    header = json.loads(get_meta(conn, "header", "null"))
    furnace_col = get_meta(conn, "furnace_col", "")
    size_col = get_meta(conn, "size_col", "")
    pair_col = get_meta(conn, "pair_col", "")
    return {
        "header": header or [],
        "furnace_col": furnace_col,
        "size_col": size_col,
        "pair_col": pair_col,
        "n_a": int(get_meta(conn, "n_a", "0")),
        "n_b": int(get_meta(conn, "n_b", "0")),
        "orders": list_orders(conn),
        "paper_records": list_paper(conn),
    }
