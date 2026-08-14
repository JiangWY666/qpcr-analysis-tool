"""跑一遍完整流程并导出 Excel，用来人工核对数值。

优先用根目录那份真实下机数据；它不在（比如刚 clone 下来的仓库）就自动退回
``examples/demo_qPCR_data.xlsx``。两份都找不到时打印一句提示后退出，不抛异常
堆栈——这个脚本是给人看的，不该用 traceback 迎接使用者。

不是单元测试（文件名不匹配 test*.py，不会被 unittest discover 收集）。
从项目根目录运行：
    .\\.venv\\Scripts\\python.exe tests\\run_sample_export.py
"""

from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:  # 兼容 python tests\run_sample_export.py 与 python -m tests.run_sample_export
    from ._fixtures import DEMO_FILE, ROOT, SAMPLE_FILE
except ImportError:
    from _fixtures import DEMO_FILE, ROOT, SAMPLE_FILE

from qpcr_tool.analysis import CQ_SD_WARN, analyze
from qpcr_tool.exporter import build_wide_table, export_excel
from qpcr_tool.grouping import auto_group
from qpcr_tool.reader import ReaderError, read_plate


@dataclass(frozen=True)
class Dataset:
    """一次人工核对要用到的全部参数。

    ``baseline_group`` 只影响汇总表里「相对 XX」那一列的分母，与 ``control``
    （2^-ΔΔCt 的归一化基准）是两回事：真实数据里习惯拿 PBS 组当生物学基准看
    倍数，而对照组是 CT。
    """

    label: str
    path: Path
    reference: list[str]
    control: str
    baseline_group: str
    output: Path
    notes: list[str] = field(default_factory=list)


REAL_DATASET = Dataset(
    label="真实下机数据",
    path=SAMPLE_FILE,
    reference=["GAPDH"],
    control="CT",
    baseline_group="PBS BALF",
    output=ROOT / "样例输出.xlsx",
)

DEMO_DATASET = Dataset(
    label="演示数据",
    path=DEMO_FILE,
    reference=["GAPDH"],
    control="Control",
    baseline_group="Control",
    output=ROOT / "演示输出.xlsx",
    notes=[
        "没找到真实下机数据，已改用仓库自带的演示数据。",
        "演示数据的 Cq 全部虚构，用于验证流程能跑通，不代表任何真实实验结果。",
    ],
)


def pick_dataset() -> Dataset | None:
    """挑一份能用的数据集，真实数据优先。都不在时返回 None。"""
    for dataset in (REAL_DATASET, DEMO_DATASET):
        if dataset.path.is_file():
            return dataset
    return None


def explain_missing() -> None:
    """两份数据都没有时，告诉使用者缺什么、怎么补。"""
    print("找不到可用的数据文件，本次核对无法进行。")
    print(f"  真实下机数据：{REAL_DATASET.path}")
    print(f"  演示数据　　：{DEMO_DATASET.path}")
    print()
    print("真实下机数据出于隐私不随仓库发布；演示数据可以用下面这条命令现生成：")
    print("  .\\.venv\\Scripts\\python.exe examples\\make_demo_data.py")


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def run(dataset: Dataset) -> None:
    """按给定数据集走一遍「读表 → 分组 → 计算 → 宽表 → 导出」并逐段打印。"""
    for note in dataset.notes:
        print(f"[提示] {note}")

    plate = read_plate(str(dataset.path))
    rule("1. 读取")
    print(f"数据集    : {dataset.label}")
    print(f"文件      : {Path(plate.file_path).name}")
    print(f"数据 sheet: {plate.sheet_name}")
    print(f"孔数      : {len(plate.wells)}（有效 {sum(1 for w in plate.wells if w.valid)}，"
          f"无效 {len(plate.invalid_wells)}）")
    print(f"基因      : {plate.targets}")
    print(f"样本      : {plate.samples}")
    for warning in plate.warnings:
        print(f"[读取警告] {warning}")

    groups = auto_group(plate.samples)
    rule("2. 自动分组")
    for index, group in enumerate(groups, start=1):
        print(f"  {index}. {group.name:<18s} <- {group.samples}")

    result = analyze(plate.wells, groups, dataset.reference, dataset.control)
    rule(f"3. 计算（内参 {'、'.join(dataset.reference)}，对照组 {dataset.control}）")
    print(f"分析基因: {result.targets}")
    print(f"列顺序  : {result.groups}")
    print("\n各样本内参基线 Cq（内参复孔均值）:")
    for sample, mean in result.ref_means.items():
        print(f"  {sample:<18s} {mean:.4f}")
    for warning in result.warnings:
        print(f"[分析警告] {warning}")

    table = build_wide_table(result)
    rule("4. 宽表 2^-ΔΔCt（列 = 基因 × 分组，行 = 复孔）")
    print("Excel 里这几个基因块是左右并排的，这里为了终端可读按基因拆开显示。\n")
    for start, end, gene in table.merge_spans():
        groups_in_block = table.group_header[start: end + 1]
        widths = [max(len(g), 9) for g in groups_in_block]
        print(f"【{gene}】")
        print("  " + "复孔".ljust(6) + "".join(
            g.rjust(w + 2) for g, w in zip(groups_in_block, widths)
        ))
        for row_index, row in enumerate(table.rows, start=1):
            cells = row[start: end + 1]
            print("  " + f"#{row_index}".ljust(6) + "".join(
                ("" if v is None else f"{v:.4f}").rjust(w + 2)
                for v, w in zip(cells, widths)
            ))
        print()

    relative = f"相对{dataset.baseline_group}"
    rule("5. 分组汇总（均值 ± 标准差）")
    print(f"  {'基因':<6s}{'分组':<18s}{'n':>3s}{'均值':>11s}{'标准差':>11s}{relative:>15s}")
    for target in result.targets:
        baseline = result.stat(target, dataset.baseline_group)
        baseline_mean = baseline.mean if baseline else None
        for group in result.groups:
            stat = result.stat(target, group)
            if stat is None or stat.mean is None:
                print(f"  {target:<6s}{group:<18s}{'0':>3s}{'-':>11s}{'-':>11s}{'-':>15s}")
                continue
            sd = f"{stat.sd:.4f}" if stat.sd is not None else "-"
            ratio = f"{stat.mean / baseline_mean:.2f}x" if baseline_mean else "-"
            print(f"  {target:<6s}{group:<18s}{stat.n:>3d}{stat.mean:>11.4f}"
                  f"{sd:>11s}{ratio:>15s}")

    rule(f"6. 对照组自洽性检查（{dataset.control} 组理论上应≈1）")
    for target in result.targets:
        values = result.values(target, dataset.control)
        arithmetic = statistics.fmean(values)
        geometric = statistics.geometric_mean(values)
        print(f"  {target:<6s} 算术平均 {arithmetic:.6f}   几何平均 {geometric:.10f}")
    print("  几何平均精确等于 1 是应有结果（对照组 ΔΔCt 均值恒为 0）；")
    print("  算术平均因 2^-x 是凸函数（Jensen 不等式）必定略大于 1。")

    rule(f"7. 复孔一致性（Cq 标准差 > {CQ_SD_WARN} 提示）")
    for item in result.qc:
        flag = "  <== 偏高" if item.flagged else ""
        sd = f"{item.cq_sd:.4f}" if item.cq_sd is not None else "-"
        print(f"  {item.target:<6s}{item.sample:<18s}n={item.n}  "
              f"Cq均值 {item.cq_mean:.4f}  SD {sd}{flag}")
    flagged = [q for q in result.qc if q.flagged]
    print(f"  超阈值的复孔组合：{len(flagged)} / {len(result.qc)}")

    export_excel(result, plate, str(dataset.output))
    rule("8. 导出")
    print(f"已写出：{dataset.output}")


def main() -> int:
    dataset = pick_dataset()
    if dataset is None:
        explain_missing()
        return 1
    try:
        run(dataset)
    except ReaderError as exc:  # 文件在但读不了，同样只给人话不给堆栈
        print(f"读取「{dataset.path}」失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
