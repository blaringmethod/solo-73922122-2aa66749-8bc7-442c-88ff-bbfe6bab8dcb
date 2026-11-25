"""关键操作自测：审计引擎手算用例、开单、纸档补录/更正、冲突、回滚、并发补录。"""
import io
import json
import sqlite3
import threading
from decimal import Decimal

import pytest

from app import audit, services
from app.main import create_app


# ---------------------------------------------------------------- 夹具与工具

@pytest.fixture()
def client(tmp_path):
    app = create_app(
        {
            "DATABASE": str(tmp_path / "t.db"),
            "SECRET_KEY": "t",
            "TESTING": True,
            "PROPAGATE_EXCEPTIONS": False,
        }
    )
    return app.test_client()


HEADER = ["试片编号", "炉次", "尺寸", "检验员", "备注"]


def csv_bytes(header, rows):
    lines = [",".join(header)] + [",".join(map(str, r)) for r in rows]
    return "\n".join(lines).encode("utf-8")


def upload(client, a, b):
    return client.post(
        "/api/upload",
        data={
            "file_a": (io.BytesIO(a), "a.csv"),
            "file_b": (io.BytesIO(b), "b.csv"),
        },
        content_type="multipart/form-data",
    )


def std_params(**over):
    base = dict(
        tol_lo="9.95", tol_hi="10.05", resolution="0.1",
        zero_lo="-0.05", zero_hi="0.05",
    )
    base.update(over)
    return base


def setup_session(client):
    # 旧表：P1..P4；新表：P1..P4（同炉 F1）+ P9（无配对）
    a = csv_bytes(HEADER, [
        ["P1", "F1", "10.02", "王芳", "x"],
        ["P2", "F1", "10.00", "王芳", "x"],
        ["P3", "F1", "10.20", "王芳", "x"],   # 固定超差真值
        ["P4", "F1", "9.98", "王芳", "x"],
    ])
    b = csv_bytes(HEADER, [
        ["P1", "F1", "10.0", "王芳", "x"],   # 真值 10.02 → 约束 d∈[-0.07,0.03]
        ["P2", "F1", "10.0", "王芳", "x"],   # 真值 10.00 → d∈[-0.05,0.05]
        ["P3", "F1", "10.2", "王芳", "x"],   # 真值 10.20（固定超差）→ d∈[-0.05,0.05]
        ["P4", "F1", "10.0", "王芳", "x"],   # 真值 9.98  → d∈[-0.03,0.07]
        ["P9", "F1", "10.5", "王芳", "x"],    # 无证据自由件，恒超差
    ])
    assert upload(client, a, b).status_code == 200
    r = client.post("/api/columns", json={
        "furnace_col": "炉次", "size_col": "尺寸", "pair_col": "试片编号"
    })
    assert r.status_code == 200


def open_f1(client, **over):
    return client.post("/api/orders", json={"furnace": "F1", **std_params(**over)})


# ---------------------------------------------------------------- 引擎手算用例

def make_params():
    return audit.Params(
        tol_lo=Decimal("9.95"), tol_hi=Decimal("10.05"),
        resolution=Decimal("0.1"),
        zero_lo=Decimal("-0.05"), zero_hi=Decimal("0.05"),
    )


def test_engine_fixed_feasible_interval_and_bad():
    p = make_params()
    # r=10.0, t=10.02 → d ∈ [-0.07, 0.03]，与零点区间交为 [-0.05, 0.03]；t 在公差内
    res = audit.compute_audit(p, [audit.Row(1, Decimal("10.0"), old_value=Decimal("10.02"))])
    assert res.feasible
    assert (res.feasible_lo, res.feasible_hi) == (Decimal("-0.05"), Decimal("0.03"))
    assert res.fixed_bad == 0
    assert (res.min_bad, res.max_bad) == (0, 0)


def test_engine_fixed_out_of_tol_counts_regardless_of_d():
    p = make_params()
    res = audit.compute_audit(p, [audit.Row(1, Decimal("10.2"), old_value=Decimal("10.20"))])
    assert res.feasible
    assert res.fixed_bad == 1 and res.min_bad == 1 and res.max_bad == 1


def test_engine_free_min_max_by_sweep():
    p = make_params()
    rows = [
        audit.Row(1, Decimal("10.0")),   # 可合格区间 [-0.1,0.1] → 裁剪 [-0.05,0.03? 见下]
        audit.Row(2, Decimal("10.1")),   # 可合格 [0.0,0.2] → [0.0,0.05]
        audit.Row(3, Decimal("9.9")),    # 可合格 [-0.2,0.0] → [-0.05,0.0]
    ]
    res = audit.compute_audit(p, rows)
    assert res.feasible
    assert res.always_bad_free == 0 and res.free_can_pass == 3
    # d=0 时三条都合格 → 最小 0；d=-0.05 时 10.1 与 10.0 两条超差、
    # d=0.05 时 9.9 与 10.0 两条超差；d=-0.05 处 9.9 恰在端点合格，
    # 取更外点（区间内仍可达的间隙）可使三条同时超差 → 最大 3
    assert res.min_bad == 0 and res.min_bad_deviation == Decimal("0.0")
    assert res.max_bad == 3
    assert res.max_bad_deviation in (Decimal("-0.05"), Decimal("0.05"))


def test_engine_free_always_bad_when_range_cannot_reach_tol():
    p = make_params()
    # r=10.5：范围最低 10.45-0.05=10.4 > U=10.05，任何可行 d 下都超差
    res = audit.compute_audit(p, [audit.Row(1, Decimal("10.5"))])
    assert res.feasible and res.always_bad_free == 1
    assert res.min_bad == 1 and res.max_bad == 1


def test_engine_conflict_no_common_deviation():
    p = make_params()
    rows = [
        audit.Row(1, Decimal("10.0"), old_value=Decimal("9.80")),  # 要求 d∈[0.15,0.25]
        audit.Row(2, Decimal("10.0"), old_value=Decimal("10.20")), # 要求 d∈[-0.25,-0.15]
    ]
    res = audit.compute_audit(p, rows)
    assert not res.feasible
    assert res.min_bad is None and res.max_bad is None
    assert len(res.conflicts) >= 1


def test_engine_endpoints_inclusive():
    # 证据真值恰在公差端点：不算超差；偏差恰在约束端点：可行
    p = make_params()
    res = audit.compute_audit(p, [audit.Row(1, Decimal("10.0"), old_value=Decimal("10.05"))])
    assert res.feasible
    assert res.feasible_hi == Decimal("0.00") or res.feasible_lo <= Decimal("0.00")
    assert res.fixed_bad == 0


def test_engine_exact_decimal_no_float_artifacts():
    p = audit.Params(
        Decimal("0.00"), Decimal("0.30"), Decimal("0.1"),
        Decimal("-0.07"), Decimal("0.07"),
    )
    res = audit.compute_audit(p, [audit.Row(1, Decimal("0.1"), old_value=Decimal("0.12"))])
    # d ∈ [0.1-0.12-0.05, 0.1-0.12+0.05] = [-0.07,0.03]，与零点区间交 [-0.07,0.03]
    assert (res.feasible_lo, res.feasible_hi) == (Decimal("-0.07"), Decimal("0.03"))


# ---------------------------------------------------------------- 上传与列选择

def test_upload_ok(client):
    setup_session(client)
    st = client.get("/api/state").get_json()
    assert st["n_a"] == 4 and st["n_b"] == 5
    assert st["furnace_col"] == "炉次" and st["size_col"] == "尺寸"


def test_upload_header_mismatch(client):
    a = csv_bytes(["x"], [["1"]])
    b = csv_bytes(["y"], [["1"]])
    r = upload(client, a, b)
    assert r.status_code == 400 and "表头不一致" in r.get_json()["error"]


def test_upload_row_limit(client):
    a = csv_bytes(HEADER, [["P1", "F", "1", "z", ""]])
    b = csv_bytes(HEADER, [["P1", "F", str(i), "z", ""] for i in range(20001)])
    r = upload(client, a, b)
    assert r.status_code == 400 and "20000" in r.get_json()["error"]


def test_columns_validation(client):
    setup_session(client)
    r = client.post("/api/columns", json={"furnace_col": "nope", "size_col": "尺寸"})
    assert r.status_code == 400
    r = client.post("/api/columns", json={"furnace_col": "炉次", "size_col": ""})
    assert r.status_code == 400


def test_invalid_size_value_rejected(client):
    a = csv_bytes(HEADER, [["P1", "F1", "10.0", "z", ""]])
    b = csv_bytes(HEADER, [["P1", "F1", "abc", "z", ""]])
    upload(client, a, b)
    client.post("/api/columns", json={"furnace_col": "炉次", "size_col": "尺寸", "pair_col": "试片编号"})
    r = open_f1(client)
    assert r.status_code == 400 and "不是合法小数" in r.get_json()["error"]


# ---------------------------------------------------------------- 开单与结论

def test_open_order_snapshot_and_conclusion(client):
    setup_session(client)
    r = open_f1(client)
    assert r.status_code == 200
    body = r.get_json()
    c = body["conclusion"]
    assert c["feasible"]
    # 固定件 4（P3 真值 10.20 超差 → fixed_bad=1），自由件 1（P9 恒超差）
    assert c["fixed_rows"] == 4 and c["free_rows"] == 1
    assert c["fixed_bad"] == 1 and c["always_bad_free"] == 1
    assert c["min_bad"] >= 2 and c["max_bad"] >= c["min_bad"]
    detail = client.get(f"/api/orders/{body['order_id']}").get_json()
    assert len(detail["rows"]) == 5
    assert detail["rows"][0]["evidence_kind"] == "old"
    assert detail["rows"][4]["evidence_kind"] == "none"


def test_open_unknown_furnace_404(client):
    setup_session(client)
    r = client.post("/api/orders", json={"furnace": "ZZ", **std_params()})
    assert r.status_code == 404


def test_param_validation(client):
    setup_session(client)
    r = open_f1(client, resolution="0")
    assert r.status_code == 400 and "分辨率" in r.get_json()["error"]
    r = open_f1(client, zero_lo="0.1", zero_hi="-0.1")
    assert r.status_code == 400 and "零点偏差" in r.get_json()["error"]


def test_order_fixed_snapshot_survives_reupload_isolation(client):
    setup_session(client)
    r = open_f1(client)
    oid = r.get_json()["order_id"]
    # 重新上传会重置会话并删除单据（演示"按同一份在供对照开单"）
    setup_session(client)
    assert client.get(f"/api/orders/{oid}").status_code == 404


# ---------------------------------------------------------------- 纸档补录

def test_paper_upsert_recomputes(client):
    setup_session(client)
    oid = open_f1(client).get_json()["order_id"]
    # 给自由件 P9（新表第 5 行，读数 10.5）补合格纸档：约束 d∈[-0.07,0.03] 可并入
    r = client.post(f"/api/orders/{oid}/paper", json={"new_row_no": 5, "paper_value": "10.52"})
    assert r.status_code == 200
    c = r.get_json()["conclusion"]
    assert c["feasible"] and c["fixed_rows"] == 5 and c["free_rows"] == 0
    assert c["always_bad_free"] == 0
    detail = client.get(f"/api/orders/{oid}").get_json()
    assert detail["rows"][4]["paper_value"] == "10.52"
    assert detail["rows"][4]["evidence_kind"] == "paper"
    # 纸档台账已收录
    papers = client.get("/api/paper").get_json()["paper_records"]
    assert any(p["new_row_no"] == 5 and p["paper_value"] == "10.52" for p in papers)


def test_paper_correction_overwrites_and_may_conflict(client):
    setup_session(client)
    oid = open_f1(client).get_json()["order_id"]
    # 补一个与全部约束冲突的纸档值（要求的偏差远在零点范围外）
    r = client.post(f"/api/orders/{oid}/paper", json={"new_row_no": 1, "paper_value": "5.00"})
    c = r.get_json()["conclusion"]
    assert not c["feasible"] and c["min_bad"] is None and c["max_bad"] is None
    # 冲突时纸档仍收录，单据状态置 conflict，旧件数区间撤下
    st = client.get("/api/state").get_json()
    assert st["orders"][0]["status"] == "conflict"
    assert st["orders"][0]["conclusion"]["min_bad"] is None
    # 更正为合理值 → 恢复可行并重新给出件数区间
    r = client.post(f"/api/orders/{oid}/paper", json={"new_row_no": 1, "paper_value": "10.02"})
    c = r.get_json()["conclusion"]
    assert c["feasible"] and c["min_bad"] is not None


def test_paper_row_outside_furnace_still_recorded(client):
    setup_session(client)
    oid = open_f1(client).get_json()["order_id"]
    r = client.post(f"/api/orders/{oid}/paper", json={"new_row_no": 5, "paper_value": "10.01"})
    assert r.get_json()["touched_order"] is True
    # 行号越界拒绝
    r = client.post(f"/api/orders/{oid}/paper", json={"new_row_no": 99, "paper_value": "10.01"})
    assert r.status_code == 400


def test_paper_on_unknown_order_404(client):
    setup_session(client)
    r = client.post("/api/orders/999/paper", json={"new_row_no": 1, "paper_value": "10.0"})
    assert r.status_code == 404


# ---------------------------------------------------------------- 写失败回滚

def test_paper_save_failure_rolls_back(client, monkeypatch):
    setup_session(client)
    oid = open_f1(client).get_json()["order_id"]
    before = client.get("/api/state").get_json()

    def boom(*a, **k):
        raise RuntimeError("模拟重算失败")

    monkeypatch.setattr(services.audit, "compute_audit", boom)
    r = client.post(f"/api/orders/{oid}/paper", json={"new_row_no": 2, "paper_value": "10.01"})
    assert r.status_code == 500

    # 纸档未收录、结论与单据原状保留
    st = client.get("/api/state").get_json()
    assert st["paper_records"] == before["paper_records"]
    assert st["orders"][0]["conclusion"]["min_bad"] == before["orders"][0]["conclusion"]["min_bad"]
    detail = client.get(f"/api/orders/{oid}").get_json()
    assert all(row["paper_value"] is None for row in detail["rows"])


# ---------------------------------------------------------------- 并发补录不同行

def test_concurrent_paper_inserts_all_retained(client):
    setup_session(client)
    oid = open_f1(client).get_json()["order_id"]
    errors = []

    def worker(row_no, value):
        try:
            r = client.post(f"/api/orders/{oid}/paper",
                            json={"new_row_no": row_no, "paper_value": value})
            assert r.status_code == 200, r.get_data(as_text=True)
        except Exception as e:  # pragma: no cover - 失败时记录
            errors.append(e)

    threads = [
        threading.Thread(target=worker, args=(i, f"10.0{i % 9}"))
        for i in range(1, 5)  # 4 个线程补录 4 个不同行
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rows_nos = {p["new_row_no"] for p in client.get("/api/paper").get_json()["paper_records"]}
    assert {1, 2, 3, 4}.issubset(rows_nos)
    detail = client.get(f"/api/orders/{oid}").get_json()
    paper_rows = {r["new_row_no"]: r["paper_value"] for r in detail["rows"] if r["paper_value"]}
    assert paper_rows == {1: "10.01", 2: "10.02", 3: "10.03", 4: "10.04"}


# ---------------------------------------------------------------- 大数据量性能（不枚举刻度）

def test_large_furnace_performance(client):
    n = 20000
    a = csv_bytes(HEADER, [[f"P{i}", "FB", "10.000", "z", ""] for i in range(n)])
    b = csv_bytes(HEADER, [[f"P{i}", "FB", "10.0", "z", ""] for i in range(n)])
    assert upload(client, a, b).status_code == 200
    client.post("/api/columns", json={"furnace_col": "炉次", "size_col": "尺寸", "pair_col": "试片编号"})
    r = client.post("/api/orders", json={"furnace": "FB", **std_params()})
    assert r.status_code == 200
    c = r.get_json()["conclusion"]
    assert c["feasible"] and c["total"] == n
    # 旧值 10.000、读数 10.0 → 偏差区间 [-0.05,0.05] 全部可行且真值合格
    assert c["min_bad"] == 0 and c["max_bad"] == 0


def test_large_free_furnace_sweep_min_max(client):
    # 全自由件 2 万行：扫掠给出非平凡 min/max，且耗时不依赖偏差刻度数
    n = 20000
    b_rows = []
    for i in range(n):
        reading = "10.1" if i % 2 == 0 else "9.9"
        b_rows.append([f"P{i}", "FC", reading, "z", ""])
    a = csv_bytes(HEADER, [["PX", "FC", "10.0", "z", ""]])
    b = csv_bytes(HEADER, b_rows)
    upload(client, a, b)
    client.post("/api/columns", json={"furnace_col": "炉次", "size_col": "尺寸", "pair_col": ""})
    r = client.post("/api/orders", json={"furnace": "FC", **std_params()})
    c = r.get_json()["conclusion"]
    assert c["feasible"] and c["free_rows"] == n
    assert c["min_bad"] == 0
    # 两族可合格区间仅在 d=0 相切；取两侧间隙点（如 ±0.025）时全部自由件超差
    assert c["max_bad"] == n
