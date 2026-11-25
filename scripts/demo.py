#!/usr/bin/env python3
"""端到端演示：上传 → 选列 → 按炉次开单 → 纸档补录/更正（含冲突恢复）→ 并发补录。

用法：python3 scripts/demo.py [base_url]   默认 http://127.0.0.1:8000
仅依赖标准库。任一步断言失败即以非零码退出。
"""
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "sample_data")

STEP = 0
PARAMS = {
    "tol_lo": "9.95", "tol_hi": "10.05",
    "resolution": "0.1", "zero_lo": "-0.05", "zero_hi": "0.05",
}


def ok(msg):
    print(f"  ✓ {msg}")


def request(method, path, body=None, headers=None, expect=200):
    global STEP
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            status, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    if status != expect:
        raise SystemExit(f"✗ {method} {path} 期望 {expect} 实得 {status}: {payload[:300]!r}")
    return payload


def api(method, path, obj=None, expect=200):
    body = json.dumps(obj).encode() if obj is not None else None
    headers = {"Content-Type": "application/json"} if obj is not None else {}
    return json.loads(request(method, path, body, headers, expect))


def upload(path_a, path_b):
    boundary = "----qc-demo-boundary"
    buf = io.BytesIO()
    for field, path in (("file_a", path_a), ("file_b", path_b)):
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{os.path.basename(path)}"\r\nContent-Type: text/csv\r\n\r\n'.encode()
        )
        buf.write(open(path, "rb").read())
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return json.loads(request(
        "POST", "/api/upload", buf.getvalue(),
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
    ))


def step(title):
    global STEP
    STEP += 1
    print(f"\n[{STEP}] {title}")


def open_order(furnace):
    return api("POST", "/api/orders", {"furnace": furnace, **PARAMS})


def main():
    step("上传旧表（170 行）与新表（200 行，4 个炉次）")
    r = upload(os.path.join(SAMPLE, "old_system.csv"), os.path.join(SAMPLE, "new_system.csv"))
    assert r["rows_a"] == 170 and r["rows_b"] == 200, r
    ok(f"表头 {len(r['columns'])} 列：{'、'.join(r['columns'])}")

    step("选择炉次列=炉次、尺寸列=尺寸、配对列=试片编号")
    api("POST", "/api/columns", {
        "furnace_col": "炉次", "size_col": "尺寸", "pair_col": "试片编号"
    })
    furnaces = api("GET", "/api/furnaces")["furnaces"]
    assert {f["furnace"] for f in furnaces} == {"F01", "F02", "F03", "F04"}
    ok("炉次：" + "；".join(f"{f['furnace']}({f['rows']}行)" for f in furnaces))

    step("F01 开单：60 条全配对固定件，3 条真值超差 → 件数恒为 3")
    c = open_order("F01")["conclusion"]
    assert c["feasible"] and c["fixed_rows"] == 60 and c["free_rows"] == 0
    assert c["min_bad"] == 3 and c["max_bad"] == 3, c
    ok(f"超差件数 {c['min_bad']}~{c['max_bad']}；共同可行偏差 {c['feasible_lo']}（唯一点）")

    step("F02 开单：50 固定 + 10 自由 → 自由件使件数出现 min/max 区间")
    c = open_order("F02")["conclusion"]
    assert c["feasible"] and c["fixed_rows"] == 50 and c["free_rows"] == 10
    assert c["min_bad"] == 0 and c["max_bad"] == 10, c
    ok(f"超差件数 {c['min_bad']}（偏差 {c['min_bad_deviation']}）"
       f" ~ {c['max_bad']}（偏差 {c['max_bad_deviation']}）")

    step("F03 开单：全部合格固定件 → 0 件")
    c = open_order("F03")["conclusion"]
    assert c["min_bad"] == 0 and c["max_bad"] == 0
    ok("超差件数 0~0")

    step("F04 开单：20 条无任何证据的自由件 → 0~20 件")
    r = open_order("F04")
    oid4 = r["order_id"]
    c = r["conclusion"]
    assert c["free_rows"] == 20 and c["min_bad"] == 0 and c["max_bad"] == 20
    ok(f"超差件数 {c['min_bad']}~{c['max_bad']}（真值可在范围内任取）")

    step("按新表行号给 F04 补录纸档（第 181 行，读数 10.0 → 纸档 10.02）")
    r = api("POST", f"/api/orders/{oid4}/paper",
            {"new_row_no": 181, "paper_value": "10.02"})
    c = r["conclusion"]
    assert c["feasible"] and c["fixed_rows"] == 1 and c["free_rows"] == 19
    ok(f"已并入本炉并同事务重算；自由件剩 {c['free_rows']} 条")

    step("补录冲突纸档（第 182 行给值 5.00）→ 收录但报冲突、撤下件数区间")
    r = api("POST", f"/api/orders/{oid4}/paper",
            {"new_row_no": 182, "paper_value": "5.00"})
    c = r["conclusion"]
    assert not c["feasible"] and c["min_bad"] is None and c["max_bad"] is None
    assert len(c["conflicts"]) >= 1
    # 纸档仍收录
    papers = {p["new_row_no"]: p["paper_value"]
              for p in api("GET", "/api/paper")["paper_records"]}
    assert papers.get(182) == "5.00"
    st = api("GET", "/api/state")
    order4 = next(o for o in st["orders"] if o["id"] == oid4)
    assert order4["status"] == "conflict" and order4["conclusion"]["min_bad"] is None
    ok("单据置 conflict，件数区间已撤下；纸档台账保留 5.00")

    step("更正该纸档为 10.03 → 冲突解除，重新给出件数区间")
    r = api("POST", f"/api/orders/{oid4}/paper",
            {"new_row_no": 182, "paper_value": "10.03"})
    c = r["conclusion"]
    assert c["feasible"] and c["min_bad"] is not None and c["max_bad"] is not None
    ok(f"恢复可行：超差件数 {c['min_bad']}~{c['max_bad']}")

    step("并发补录 3 个不同行 → 全部保留，按合并证据核算")
    errors = []

    def worker(row_no, value):
        try:
            rr = api("POST", f"/api/orders/{oid4}/paper",
                     {"new_row_no": row_no, "paper_value": value})
            assert rr["conclusion"]["feasible"]
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [
        threading.Thread(target=worker, args=(183, "10.01")),
        threading.Thread(target=worker, args=(184, "9.99")),
        threading.Thread(target=worker, args=(185, "10.04")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    detail = api("GET", f"/api/orders/{oid4}")
    paper_rows = {x["new_row_no"]: x["paper_value"] for x in detail["rows"] if x["paper_value"]}
    assert paper_rows.get(183) == "10.01" and paper_rows.get(184) == "9.99" and paper_rows.get(185) == "10.04"
    assert detail["conclusion"]["fixed_rows"] == 5  # 181/182/183/184/185
    ok(f"五行纸档均在；固定件 {detail['conclusion']['fixed_rows']}、自由件 {detail['conclusion']['free_rows']}")

    step("非法行号与写失败保护（行号越界返回 400）")
    request("POST", f"/api/orders/{oid4}/paper",
            json.dumps({"new_row_no": 99999, "paper_value": "10.00"}).encode(),
            {"Content-Type": "application/json"}, expect=400)
    ok("越界补录被拒，库内原状不变")

    print("\n全部演示步骤通过 ✅")


if __name__ == "__main__":
    main()
