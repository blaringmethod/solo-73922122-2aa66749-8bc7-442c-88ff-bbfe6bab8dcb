"""炉次超差审计引擎（纯函数，Decimal 精确小数，O(n log n) 扫掠）。

语义（区间均含端点）：
- 全炉共用一个零点偏差 d，d 必须落在零点偏差范围 [zlo, zhi] 内。
- 每条新读数 r（取整后的读数），真值范围为 [r-d-h, r-d+h]，
  h = 新表分辨率 / 2。
- 真值取证据：有纸档以纸档为准，否则用配对旧值；两者皆无时为"自由件"，
  其真值可在自身范围内任取。
- 固定件（有证据值 t）可行条件：t ∈ [r-d-h, r-d+h]
  ⇔ d ∈ [r-t-h, r-t+h]。所有固定件区间与零点区间的交集即共同可行偏差集；
  交集为空则冲突（无共同可行偏差）。
- 超差：真值不在公差 [L, U] 内（L、U 含端点，贴边不算超差）。
  固定件在任一可行 d 下结论恒定。
  自由件：存在 d 使其范围内存在合格点即"可合格"；否则恒超差。
- 求可行 d 区间上超差件数的最小/最大值，用区间覆盖扫掠一次完成，
  不枚举偏差刻度，也不对每个边界重扫全炉。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# 事件排序：同一坐标先处理区间进入（+1），再处理区间离开（-1）。
# 区间含端点，离开在该坐标之后才生效。
_START = 1
_END = 0


@dataclass
class Params:
    tol_lo: Decimal          # 公差下限 L（含）
    tol_hi: Decimal          # 公差上限 U（含）
    resolution: Decimal      # 新表分辨率（>0）
    zero_lo: Decimal         # 零点偏差范围下限（含）
    zero_hi: Decimal         # 零点偏差范围上限（含）

    @property
    def half(self) -> Decimal:
        return self.resolution / 2


@dataclass
class Row:
    new_row_no: int
    new_reading: Decimal
    old_value: Decimal | None = None     # 配对旧值
    paper_value: Decimal | None = None   # 纸档值（优先于旧值）

    @property
    def evidence(self) -> Decimal | None:
        return self.paper_value if self.paper_value is not None else self.old_value


@dataclass
class AuditResult:
    feasible: bool
    feasible_lo: Decimal | None = None   # 共同可行偏差区间
    feasible_hi: Decimal | None = None
    total: int = 0
    fixed_bad: int = 0                  # 固定件中超差件数（与 d 无关）
    always_bad_free: int = 0            # 恒超差的自由件数
    free_can_pass: int = 0              # 可合格的自由件数
    min_bad: int | None = None          # 超差件数最小值
    max_bad: int | None = None          # 超差件数最大值
    min_bad_deviation: Decimal | None = None   # 达到最小值的偏差
    max_bad_deviation: Decimal | None = None   # 达到最大值的偏差
    fixed_rows: int = 0
    free_rows: int = 0
    conflicts: list[dict] = field(default_factory=list)  # 使交集落空的约束


def _free_conform_interval(r: Decimal, p: Params):
    """自由件可合格的偏差区间：范围内存在点 ∈ [L, U]。

    范围 [r-d-h, r-d+h] 与 [L, U] 相交（含端点）
    ⇔ r-d-h ≤ U 且 r-d+h ≥ L
    ⇔ d ≥ r-h-U 且 d ≤ r+h-L。
    完全落在公差外（恒超差）时返回 None。
    """
    lo = r - p.half - p.tol_hi
    hi = r + p.half - p.tol_lo
    if lo > hi:  # 理论上不会发生（h≥0），保险起见
        return None
    return lo, hi


def compute_audit(params: Params, rows: list[Row]) -> AuditResult:
    if params.tol_lo > params.tol_hi:
        raise ValueError("公差下限不得大于上限")
    if params.resolution <= 0:
        raise ValueError("新表分辨率须为正数")
    if params.zero_lo > params.zero_hi:
        raise ValueError("零点偏差下限不得大于上限")

    result = AuditResult(feasible=False, total=len(rows))

    # 1) 固定件约束 → 共同可行偏差 = 零点区间 ∩ 各证据区间
    d_lo, d_hi = params.zero_lo, params.zero_hi
    fixed_bad = 0
    for row in rows:
        t = row.evidence
        if t is None:
            continue
        result.fixed_rows += 1
        r = row.new_reading
        c_lo = r - t - params.half
        c_hi = r - t + params.half
        # 超差判定与 d 无关：证据真值是否在公差内（端点合格）
        if t < params.tol_lo or t > params.tol_hi:
            fixed_bad += 1
        # 收缩可行区间，记录破坏交集的约束用于报错说明
        if c_hi < d_lo or c_lo > d_hi:
            result.conflicts.append(
                {
                    "new_row_no": row.new_row_no,
                    "reading": str(r),
                    "evidence": str(t),
                    "evidence_kind": "paper" if row.paper_value is not None else "old",
                    "deviation_interval": [str(c_lo), str(c_hi)],
                }
            )
            # 交集已空：置为显式空区间，后续约束一律记为冲突
            d_lo, d_hi = Decimal(1), Decimal(0)
        else:
            d_lo, d_hi = max(d_lo, c_lo), min(d_hi, c_hi)

    result.fixed_bad = fixed_bad

    # 2) 自由件：可合格区间（用于扫掠）与恒超差计数
    events: list[tuple[Decimal, int, int]] = []  # (坐标, 类型, 增量)
    always_bad = 0
    for row in rows:
        if row.evidence is not None:
            continue
        result.free_rows += 1
        iv = _free_conform_interval(row.new_reading, params)
        lo, hi = iv
        # 可合格区间与当前可行偏差区间无交集 → 在所有可行 d 下恒超差
        if hi < d_lo or lo > d_hi:
            always_bad += 1
            continue
        lo, hi = max(lo, d_lo), min(hi, d_hi)
        result.free_can_pass += 1
        events.append((lo, _START, 1))
        events.append((hi, _END, -1))
    result.always_bad_free = always_bad

    base_bad = fixed_bad + always_bad  # 与 d 无关的超差底数

    # 3) 冲突：无共同可行偏差
    if d_lo > d_hi:
        return result

    result.feasible = True
    result.feasible_lo, result.feasible_hi = d_lo, d_hi

    # 4) 一次扫掠：求可行区间上"可合格自由件"覆盖数的最大/最小值。
    #    超差件数 = base_bad +（可合格自由件数 − 当前覆盖数）。
    # 事件已裁剪到 [d_lo, d_hi]；按坐标分组，闭区间在坐标点上先计入 START，
    # END 在该坐标之后才生效；坐标之间覆盖数恒定（间隙内任取一点即可）。
    if not events:
        result.min_bad = result.max_bad = base_bad
        result.min_bad_deviation = d_lo
        result.max_bad_deviation = d_lo
        return result

    events.sort(key=lambda e: (e[0], e[1]))  # 同坐标 START(1) 先于 END(0)
    coords: list[Decimal] = []
    starts_at: dict[Decimal, int] = {}
    ends_at: dict[Decimal, int] = {}
    for x, typ, delta in events:
        if x not in starts_at:
            coords.append(x)
            starts_at[x] = ends_at[x] = 0
        if typ == _START:
            starts_at[x] += delta
        else:
            ends_at[x] -= delta

    cur = 0
    max_cov = min_cov = 0
    max_at = min_at = d_lo

    def record(value: int, point: Decimal):
        nonlocal max_cov, min_cov, max_at, min_at
        if value > max_cov:
            max_cov, max_at = value, point
        if value < min_cov:
            min_cov, min_at = value, point

    # 首个事件坐标之前（含 d_lo，事件坐标均 ≥ d_lo）覆盖数为 0
    if coords[0] > d_lo:
        record(0, d_lo)

    for idx, x in enumerate(coords):
        cur += starts_at[x]
        record(cur, x)                       # 坐标点本身（闭区间）
        cur -= ends_at[x]
        nxt = coords[idx + 1] if idx + 1 < len(coords) else None
        if nxt is not None:
            record(cur, (x + nxt) / 2)       # 间隙 (x, nxt) 内任一点
        elif x < d_hi:
            record(cur, d_hi)                # 末坐标到可行上限之间

    can_pass_total = result.free_can_pass
    result.min_bad = base_bad + (can_pass_total - max_cov)
    result.max_bad = base_bad + (can_pass_total - min_cov)
    result.min_bad_deviation = max_at
    result.max_bad_deviation = min_at
    return result
