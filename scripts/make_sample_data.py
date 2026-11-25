#!/usr/bin/env python3
"""生成演示台账：sample_data/old_system.csv 与 new_system.csv。

数据设计（配合 README 的演示与 tests 的断言）：
- 表头：试片编号, 炉次, 尺寸, 检验员, 备注
- 尺寸列「尺寸」：旧表为精确小数真值；新表为按 0.1 分辨率取整后的读数，
  真值与读数相差一个全炉共用的零点偏差（F01~F03 分别为 -0.03 / 0.02 / 0.04）。
- 炉次：F01 / F02 / F03 各 60 行，新表共 180 行；另 20 行（F04）无旧配对。
- 配对列「试片编号」：
  * F01：60 行全部配对旧值，其中 3 行真值在公差外（固定超差件）。
  * F02：前 50 行有配对旧值，后 10 行无配对（自由件，演示纸档补录）。
  * F03：60 行全部配对。
  * F04：仅新表 20 行，无任何证据（全部自由件）。
- 全部用 Decimal 量化生成，避免浮点误差；读数保留 1 位小数。
"""
import csv
import os
from decimal import Decimal

HEADER = ["试片编号", "炉次", "尺寸", "检验员", "备注"]
INSPECTORS = ["王芳", "李强", "赵敏"]
RES = Decimal("0.1")
TOL_LO, TOL_HI = Decimal("9.95"), Decimal("10.05")

# 每炉：炉次 -> (新表行数, 零点偏差, 前若干行有配对旧值)
FURNACES = [
    ("F01", 60, Decimal("-0.03"), 60),
    ("F02", 60, Decimal("0.02"), 50),
    ("F03", 60, Decimal("0.04"), 60),
    ("F04", 20, Decimal("0.00"), 0),
]
# F01 中固定超差的试片序号（0 基）及其真值（超出公差）
F01_BAD = {7: Decimal("9.90"), 23: Decimal("10.10"), 41: Decimal("10.08")}


def quantize_reading(true_value: Decimal) -> Decimal:
    """模拟新表：真值加零点偏差后按分辨率四舍五入到 1 位小数。"""
    measured = true_value + Decimal("0")  # 偏差由调用方体现在真值里
    return measured.quantize(RES)


def main():
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data")
    os.makedirs(out_dir, exist_ok=True)

    old_rows = []   # 旧表：只含有配对的试片
    new_rows = []   # 新表：全部 200 行
    seq = 0
    for furnace, n, bias, paired in FURNACES:
        for i in range(n):
            seq += 1
            piece = f"P{seq:04d}"
            # 基准真值在公差内波动；F01 指定行改为超差值
            if furnace == "F01" and i in F01_BAD:
                true_value = F01_BAD[i]
            else:
                wave = Decimal(((i * 37) % 80) - 40) / Decimal("1000")  # -0.040~0.039
                true_value = (Decimal("10.00") + wave).quantize(Decimal("0.001"))
            reading = (true_value + bias).quantize(RES)  # 新表读数 = 真值 + 偏差后取整
            new_rows.append(
                [piece, furnace, f"{reading:.1f}", INSPECTORS[i % 3],
                 "无旧档" if i >= paired else "复测"]
            )
            if i < paired:
                old_rows.append(
                    [piece, furnace, f"{true_value:.3f}", INSPECTORS[i % 3], "复测"]
                )

    for name, rows in (("old_system.csv", old_rows), ("new_system.csv", new_rows)):
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(rows)
        print(f"已生成 {path}（{len(rows)} 行）")


if __name__ == "__main__":
    main()
