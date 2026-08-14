"""生物学重复拆分层的测试：拆分、配对、分组归并、降级与还原。

读真实下机数据的用例挂了 ``@requires_sample_file``，那份文件不进版本库，缺失时
整体跳过；用 ``build_plate`` 现造板子的用例不依赖任何外部文件，任何环境下都跑。

用标准库 unittest，从项目根目录运行：
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_replicates -v
"""

from __future__ import annotations

import unittest

try:  # 兼容 discover -s tests、python -m unittest tests.xxx、直接运行本文件三种方式
    from ._fixtures import SAMPLE_FILE, requires_sample_file
except ImportError:
    from _fixtures import SAMPLE_FILE, requires_sample_file

from qpcr_tool.analysis import analyze, compute_replicate_qc
from qpcr_tool.grouping import auto_group
from qpcr_tool.reader import PlateData, WellRecord, read_plate
from qpcr_tool.replicates import (
    PairingPreview,
    SplitReport,
    build_pairing_preview,
    detect_block_orientation,
    detect_replicate_layout,
    group_by_original_sample,
    parse_well,
    restore_original_samples,
    split_biological_replicates,
)

TARGETS = ["GAPDH", "IL1b", "IL6", "TNFa"]
ORIGINAL_SAMPLES = ["CT", "PBS BALF", "LPS BALF", "LPS BALF CIT013"]
VIRTUAL_SAMPLES = [f"{s}-{i}" for s in ORIGINAL_SAMPLES for i in (1, 2, 3)]
REFERENCE = ["GAPDH"]
CONTROL = "CT"

# 板子布局：每个原始样本的三个孔是三个生物学重复，同一列的孔属于同一号生物学重复。
#   CT               A01 A02 A03(GAPDH) A04 A05 A06(IL1b) E01 E02 E03(IL6) E04 E05 E06(TNFa)
#   LPS BALF         C01 C02 C03        C04 C05 C06       G01 G02 G03      G04 G05 G06
EXPECTED_PAIRING = {
    "CT-1": {"GAPDH": "A01", "IL1b": "A04", "IL6": "E01", "TNFa": "E04"},
    "CT-3": {"GAPDH": "A03", "IL1b": "A06", "IL6": "E03", "TNFa": "E06"},
    "LPS BALF-2": {"GAPDH": "C02", "IL1b": "C05", "IL6": "G02", "TNFa": "G05"},
}


def load_plate() -> PlateData:
    """每次都重新读盘，避免某个用例改了孔状态影响到别的用例。"""
    return read_plate(str(SAMPLE_FILE))


def split_plate() -> PlateData:
    plate = load_plate()
    split_biological_replicates(plate)
    return plate


def make_well(
    row_index: int, well: str, target: str, sample: str, cq: float | None
) -> WellRecord:
    """构造一个测试用孔，original_sample 与 read_plate 一样预先填好。"""
    return WellRecord(
        row_index=row_index,
        well=well,
        target=target,
        sample=sample,
        content="Unkn",
        cq=cq,
        cq_text="" if cq is None else str(cq),
        included=cq is not None,
        original_sample=sample,
    )


def build_plate(rows: list[tuple[str, str, str, float | None]]) -> PlateData:
    """按 (孔位, 基因, 样本, Cq) 列表造一份 PlateData，row_index 即列表顺序。"""
    plate = PlateData(file_path="<memory>", sheet_name="测试")
    for index, (well, target, sample, cq) in enumerate(rows, start=2):
        plate.wells.append(make_well(index, well, target, sample, cq))
    return plate


def rq_by_well(result) -> dict[tuple[str, str], float]:
    return {(item.target, item.well): item.rq for item in result.well_results}


def numbering(plate: PlateData) -> dict[str, tuple[str, int]]:
    """孔位 -> (虚拟样本名, 重复编号)，用来比对两种排序方式的拆分结果。"""
    return {w.well: (w.sample, w.replicate_index) for w in plate.wells}


def numbering_by_row_index(plate: PlateData) -> dict[str, tuple[str, int]]:
    """独立实现：完全按源文件行序给每个 (样本, 基因) 区块编号，作为对照基准。

    不调用 replicates.py 的任何排序逻辑，用来证明方向识别没有改变一维板子的结果。
    """
    buckets: dict[tuple[str, str], list[WellRecord]] = {}
    for well in plate.wells:
        buckets.setdefault((well.original_sample or well.sample, well.target), []).append(well)
    expected: dict[str, tuple[str, int]] = {}
    for (sample, _target), wells in buckets.items():
        ordered = sorted(wells, key=lambda w: w.row_index)
        for index, well in enumerate(ordered, start=1):
            name = f"{sample}-{index}" if len(ordered) > 1 else sample
            expected[well.well] = (name, index)
    return expected


class TestDetectLayout(unittest.TestCase):
    """探测：每个样本几个生物学重复，且探测本身不改动数据。"""

    @requires_sample_file
    def test_真实板子每个样本三个重复(self):
        plate = load_plate()
        self.assertEqual(
            detect_replicate_layout(plate), {name: 3 for name in ORIGINAL_SAMPLES}
        )

    @requires_sample_file
    def test_探测是只读的(self):
        plate = load_plate()
        before = [(w.well, w.sample, w.original_sample, w.replicate_index) for w in plate.wells]
        detect_replicate_layout(plate)
        after = [(w.well, w.sample, w.original_sample, w.replicate_index) for w in plate.wells]
        self.assertEqual(before, after)
        self.assertEqual(plate.samples, ORIGINAL_SAMPLES)

    def test_孔数不一致的样本不出现在结果里(self):
        plate = build_plate([
            ("A01", "GAPDH", "S1", 20.0), ("A02", "GAPDH", "S1", 20.1),
            ("A03", "IL1b", "S1", 25.0), ("A04", "IL1b", "S1", 25.1),
            ("B01", "GAPDH", "S2", 21.0), ("B02", "GAPDH", "S2", 21.1),
            ("B03", "IL1b", "S2", 26.0),
        ])
        self.assertEqual(detect_replicate_layout(plate), {"S1": 2})


@requires_sample_file
class TestSplitRealPlate(unittest.TestCase):
    """任务核心：真实文件拆成 12 个虚拟样本，且孔位配对完全正确。"""

    @classmethod
    def setUpClass(cls):
        cls.plate = load_plate()
        cls.report = split_biological_replicates(cls.plate)

    def test_报告内容(self):
        self.assertTrue(self.report.enabled)
        self.assertEqual(self.report.split_samples, ORIGINAL_SAMPLES)
        self.assertEqual(self.report.replicate_counts, {n: 3 for n in ORIGINAL_SAMPLES})
        self.assertEqual(self.report.skipped, [])
        self.assertEqual(self.report.warnings, [])

    def test_拆成十二个虚拟样本(self):
        self.assertEqual(self.plate.samples, VIRTUAL_SAMPLES)
        self.assertEqual(len(self.plate.wells), 48)

    def test_每个虚拟样本四个基因各一个孔(self):
        for name in VIRTUAL_SAMPLES:
            wells = [w for w in self.plate.wells if w.sample == name]
            self.assertEqual(len(wells), 4, f"{name} 的孔数不是 4")
            self.assertEqual(sorted(w.target for w in wells), TARGETS, f"{name} 的基因不全")

    def test_孔位配对关系(self):
        """写死具体孔位：1 号重复必须是 A01+A04+E01+E04，不能被平均混掉。"""
        for name, expected in EXPECTED_PAIRING.items():
            actual = {w.target: w.well for w in self.plate.wells if w.sample == name}
            self.assertEqual(actual, expected, f"{name} 的配对不对")

    def test_原名与重复序号都填好了(self):
        for well in self.plate.wells:
            self.assertIn(well.original_sample, ORIGINAL_SAMPLES)
            self.assertIn(well.replicate_index, (1, 2, 3))
            self.assertEqual(well.sample, f"{well.original_sample}-{well.replicate_index}")

    def test_每个原始样本下三个重复各占四个孔(self):
        for name in ORIGINAL_SAMPLES:
            for index in (1, 2, 3):
                wells = [
                    w for w in self.plate.wells
                    if w.original_sample == name and w.replicate_index == index
                ]
                self.assertEqual(len(wells), 4, f"{name} 第 {index} 号重复")


@requires_sample_file
class TestPairingSurvivesAnalysis(unittest.TestCase):
    """最关键的一条：下游数学一行没改，内参却从「取平均」变成了「1:1 配对」。"""

    @classmethod
    def setUpClass(cls):
        cls.plate = split_plate()
        cls.groups = group_by_original_sample(cls.plate)
        cls.result = analyze(cls.plate.wells, cls.groups, REFERENCE, CONTROL)
        cls.cq = {w.well: w.cq for w in cls.plate.wells}
        cls.results_by_well = {r.well: r for r in cls.result.well_results}

    def test_内参没被平均而是配对到同一号重复(self):
        # (目标基因孔, 该孔应当配到的内参孔)
        for target_well, reference_well in [
            ("A04", "A01"),  # IL1b / CT-1  -> GAPDH / CT-1
            ("E01", "A01"),  # IL6  / CT-1  -> 同一号重复共用一个内参孔
            ("E06", "A03"),  # TNFa / CT-3
            ("C05", "C02"),  # IL1b / LPS BALF-2
            ("G05", "C02"),  # TNFa / LPS BALF-2
        ]:
            self.assertAlmostEqual(
                self.results_by_well[target_well].ref_mean,
                self.cq[reference_well],
                places=12,
                msg=f"{target_well} 的内参基线应当就是 {reference_well} 的 Cq 原值",
            )

    def test_内参基线不等于三孔均值(self):
        """反证：若内参被平均，A04 的 ref_mean 会是 A01-A03 的均值。"""
        three_well_mean = sum(self.cq[w] for w in ("A01", "A02", "A03")) / 3
        self.assertNotAlmostEqual(
            self.results_by_well["A04"].ref_mean, three_well_mean, places=6
        )

    def test_每个虚拟样本的内参基线就是它自己的GAPDH孔(self):
        for well in self.plate.wells:
            if well.target != "GAPDH":
                continue
            self.assertAlmostEqual(
                self.result.ref_means[well.sample], well.cq, places=12, msg=well.sample
            )

    def test_逐孔ΔCt仍是Cq减内参(self):
        for item in self.result.well_results:
            self.assertAlmostEqual(item.dct, item.cq - item.ref_mean, places=12)
            self.assertAlmostEqual(item.rq, 2.0 ** (-item.ddct), places=12)


@requires_sample_file
class TestGroupByOriginalSample(unittest.TestCase):
    """分组归并：CT-1/CT-2/CT-3 必须回到同一个 CT 组，组顺序按文件出现顺序。"""

    def test_未拆分时每组一个样本(self):
        plate = load_plate()
        groups = group_by_original_sample(plate)
        self.assertEqual([g.name for g in groups], ORIGINAL_SAMPLES)
        self.assertEqual([g.samples for g in groups], [[n] for n in ORIGINAL_SAMPLES])
        self.assertEqual(
            [(g.name, g.samples) for g in groups],
            [(g.name, g.samples) for g in auto_group(plate.samples)],
        )

    def test_拆分后四个组各含三个虚拟样本(self):
        groups = group_by_original_sample(split_plate())
        self.assertEqual([g.name for g in groups], ORIGINAL_SAMPLES)
        for group in groups:
            self.assertEqual(group.samples, [f"{group.name}-{i}" for i in (1, 2, 3)])

    def test_分析结果每个基因每组仍是三个RQ(self):
        plate = split_plate()
        result = analyze(plate.wells, group_by_original_sample(plate), REFERENCE, CONTROL)
        self.assertEqual(result.targets, ["IL1b", "IL6", "TNFa"])
        self.assertEqual(result.groups, ORIGINAL_SAMPLES)
        self.assertEqual(len(result.well_results), 3 * 4 * 3)
        for target in result.targets:
            for group in result.groups:
                self.assertEqual(
                    len(result.values(target, group)), 3, f"{target} / {group}"
                )


class TestGroupByOriginalSampleMerging(unittest.TestCase):
    """拆分与「合并末尾编号」是正交的：板上常常同时有同名多孔和 PBS1/PBS2 这种命名。"""

    @staticmethod
    def make_plate() -> PlateData:
        """PBS1/PBS2、G43-L1/G43-L2、BLM1/BLM2 各带两个技术孔。"""
        rows = []
        names = ["PBS1", "G43-L1", "PBS2", "G43-L2", "BLM1", "BLM2"]
        for gene_index, gene in enumerate(["GAPDH", "IL6"]):
            for offset in (0, 1):
                row = chr(ord("A") + gene_index * 2 + offset)
                for column, sample in enumerate(names, start=1):
                    rows.append((f"{row}{column:02d}", gene, sample, 20.0 + column))
        return build_plate(rows)

    def test_不合并时每个样本名各自成组(self):
        plate = self.make_plate()
        split_biological_replicates(plate)
        groups = group_by_original_sample(plate)
        self.assertEqual(
            [g.name for g in groups],
            ["PBS1", "G43-L1", "PBS2", "G43-L2", "BLM1", "BLM2"],
        )

    def test_合并后按去掉末尾编号的原始样本名成组(self):
        plate = self.make_plate()
        split_biological_replicates(plate)
        groups = group_by_original_sample(plate, merge_trailing_numbers=True)
        self.assertEqual([g.name for g in groups], ["PBS", "G43-L", "BLM"])
        # 组内保留拆分后的虚拟样本名，顺序仍按原始样本在文件里的出现顺序
        self.assertEqual(
            groups[0].samples, ["PBS1-1", "PBS1-2", "PBS2-1", "PBS2-2"]
        )

    def test_剥不出同伴的名字不会被截断(self):
        """「LPS BALF CIT013」这类自带数字后缀的名字必须原样保留。"""
        plate = build_plate([
            ("A01", "GAPDH", "PBS1", 20.0), ("A02", "GAPDH", "PBS2", 20.1),
            ("A03", "GAPDH", "LPS BALF CIT013", 20.2),
            ("B01", "IL6", "PBS1", 25.0), ("B02", "IL6", "PBS2", 25.1),
            ("B03", "IL6", "LPS BALF CIT013", 25.2),
        ])
        groups = group_by_original_sample(plate, merge_trailing_numbers=True)
        self.assertEqual([g.name for g in groups], ["PBS", "LPS BALF CIT013"])


@requires_sample_file
class TestSplitChangesResults(unittest.TestCase):
    """拆分必须真的改变数值，否则这个功能就是摆设。"""

    @classmethod
    def setUpClass(cls):
        plain = load_plate()
        cls.plain_result = analyze(
            plain.wells, auto_group(plain.samples), REFERENCE, CONTROL
        )
        split = split_plate()
        cls.split_result = analyze(
            split.wells, group_by_original_sample(split), REFERENCE, CONTROL
        )
        cls.plain_rq = rq_by_well(cls.plain_result)
        cls.split_rq = rq_by_well(cls.split_result)

    def test_两种模式覆盖同样的孔(self):
        self.assertEqual(set(self.plain_rq), set(self.split_rq))
        self.assertEqual(len(self.plain_rq), 36)

    def test_至少一个孔的RQ不同(self):
        changed = [k for k in self.plain_rq if self.plain_rq[k] != self.split_rq[k]]
        self.assertTrue(changed, "拆分前后 RQ 完全一致，说明配对没有生效")
        biggest = max(
            abs(self.split_rq[k] - self.plain_rq[k]) / self.plain_rq[k]
            for k in self.plain_rq
        )
        self.assertGreater(biggest, 0.05, "差异小到只有浮点噪声的量级")

    def test_两种模式对照组RQ均值都接近一(self):
        for name, result in [("不拆分", self.plain_result), ("拆分", self.split_result)]:
            for target in result.targets:
                values = result.values(target, CONTROL)
                self.assertAlmostEqual(
                    sum(values) / len(values), 1.0, delta=0.05,
                    msg=f"{name} 模式下 {target} 在对照组的 RQ 均值",
                )


class TestMismatchedWellCounts(unittest.TestCase):
    """降级路径：某个样本各基因孔数对不上时，不拆它，并说清楚原因。"""

    @classmethod
    def setUpClass(cls):
        cls.plate = build_plate([
            ("A01", "GAPDH", "S1", 20.0), ("A02", "GAPDH", "S1", 20.1),
            ("A03", "GAPDH", "S1", 20.2),
            ("A04", "IL1b", "S1", 25.0), ("A05", "IL1b", "S1", 25.1),
            ("A06", "IL1b", "S1", 25.2),
            ("B01", "GAPDH", "S2", 21.0), ("B02", "GAPDH", "S2", 21.1),
            ("B03", "GAPDH", "S2", 21.2),
            ("B04", "IL1b", "S2", 26.0), ("B05", "IL1b", "S2", 26.1),
        ])
        cls.report = split_biological_replicates(cls.plate)

    def test_进了skipped且没被计入拆分(self):
        self.assertEqual(self.report.skipped, ["S2"])
        self.assertEqual(self.report.split_samples, ["S1"])
        self.assertEqual(self.report.replicate_counts, {"S1": 3})
        self.assertTrue(self.report.enabled)

    def test_没有被改名也没有编号(self):
        s2 = [w for w in self.plate.wells if w.original_sample == "S2"]
        self.assertEqual(len(s2), 5)
        for well in s2:
            self.assertEqual(well.sample, "S2")
            self.assertEqual(well.replicate_index, 0)
        self.assertEqual(self.plate.samples, ["S1-1", "S1-2", "S1-3", "S2"])

    def test_警告说清了样本各基因孔数和降级口径(self):
        self.assertEqual(len(self.report.warnings), 1)
        text = self.report.warnings[0]
        for fragment in ("S2", "GAPDH 3 孔", "IL1b 2 孔", "退回按内参均值归一化"):
            self.assertIn(fragment, text)

    def test_正常样本不受牵连(self):
        s1 = {w.well: (w.sample, w.replicate_index) for w in self.plate.wells
              if w.original_sample == "S1"}
        self.assertEqual(s1["A01"], ("S1-1", 1))
        self.assertEqual(s1["A04"], ("S1-1", 1))
        self.assertEqual(s1["A06"], ("S1-3", 3))


class TestInvalidWellsKeepPositions(unittest.TestCase):
    """Cq 无效的孔在板子上照样占位，跳过它会让后面的重复编号整体错位。"""

    @requires_sample_file
    def test_中间孔无效时第三个孔仍是三号重复(self):
        plate = load_plate()
        for well in plate.wells:
            if well.well == "A05":  # IL1b / CT 的第 2 个孔，人为置为未检出
                well.cq = None
                well.cq_text = "N/A"
                well.included = False
        split_biological_replicates(plate)

        by_well = {w.well: w for w in plate.wells}
        self.assertEqual(by_well["A05"].replicate_index, 2)
        self.assertEqual(by_well["A05"].sample, "CT-2")
        self.assertEqual(by_well["A06"].replicate_index, 3)
        self.assertEqual(by_well["A06"].sample, "CT-3")
        self.assertEqual(by_well["A06"].original_sample, "CT")

    @requires_sample_file
    def test_无效孔不影响其余孔的配对(self):
        plate = load_plate()
        for well in plate.wells:
            if well.well == "A05":
                well.cq = None
                well.included = False
        split_biological_replicates(plate)
        result = analyze(
            plate.wells, group_by_original_sample(plate), REFERENCE, CONTROL
        )
        cq = {w.well: w.cq for w in plate.wells}
        by_well = {r.well: r for r in result.well_results}

        self.assertNotIn("A05", by_well)  # 无效孔本来就不参与计算
        self.assertAlmostEqual(by_well["A06"].ref_mean, cq["A03"], places=12)
        self.assertAlmostEqual(by_well["E02"].ref_mean, cq["A02"], places=12)

    def test_构造数据上同样成立(self):
        plate = build_plate([
            ("A01", "GAPDH", "S1", 20.0), ("A02", "GAPDH", "S1", 20.1),
            ("A03", "GAPDH", "S1", 20.2),
            ("A04", "IL1b", "S1", 25.0), ("A05", "IL1b", "S1", None),
            ("A06", "IL1b", "S1", 25.2),
        ])
        split_biological_replicates(plate)
        indices = {w.well: w.replicate_index for w in plate.wells}
        self.assertEqual(
            indices,
            {"A01": 1, "A02": 2, "A03": 3, "A04": 1, "A05": 2, "A06": 3},
        )


@requires_sample_file
class TestIdempotencyAndRestore(unittest.TestCase):
    """反复拆不能拆出 CT-1-1，还原要能回到原始样本名。"""

    def test_连续两次拆分结果一致(self):
        plate = load_plate()
        first = split_biological_replicates(plate)
        snapshot = [(w.well, w.sample, w.original_sample, w.replicate_index)
                    for w in plate.wells]
        second = split_biological_replicates(plate)

        self.assertEqual(first, second)
        self.assertIsInstance(second, SplitReport)
        self.assertEqual(
            [(w.well, w.sample, w.original_sample, w.replicate_index) for w in plate.wells],
            snapshot,
        )
        self.assertEqual(plate.samples, VIRTUAL_SAMPLES)
        self.assertFalse([s for s in plate.samples if s.endswith("-1-1")])

    def test_还原后回到四个原始样本名(self):
        plate = split_plate()
        restore_original_samples(plate)
        self.assertEqual(plate.samples, ORIGINAL_SAMPLES)
        self.assertEqual({w.replicate_index for w in plate.wells}, {0})
        for well in plate.wells:
            self.assertEqual(well.sample, well.original_sample)

    def test_还原后重新拆分仍得到同样结果(self):
        plate = load_plate()
        expected = split_biological_replicates(plate)
        restore_original_samples(plate)
        self.assertEqual(split_biological_replicates(plate), expected)
        self.assertEqual(plate.samples, VIRTUAL_SAMPLES)

    def test_还原后的分析结果与从未拆过的一致(self):
        plate = split_plate()
        restore_original_samples(plate)
        restored = analyze(plate.wells, auto_group(plate.samples), REFERENCE, CONTROL)
        plain = load_plate()
        expected = analyze(plain.wells, auto_group(plain.samples), REFERENCE, CONTROL)
        self.assertEqual(rq_by_well(restored), rq_by_well(expected))


class TestParseWell(unittest.TestCase):
    """孔位字符串解析：常见板型都要认得，认不出必须明确返回 None。"""

    def test_合法孔位(self):
        for text, expected in [
            ("A1", (0, 0)), ("A01", (0, 0)), ("A12", (0, 11)),
            ("B01", (1, 0)), ("H12", (7, 11)), ("P24", (15, 23)),
            ("AA01", (26, 0)), ("AF48", (31, 47)),
            ("a01", (0, 0)), (" C3 ", (2, 2)), ("A 01", (0, 0)),
        ]:
            self.assertEqual(parse_well(text), expected, f"输入 {text!r}")

    def test_非法输入返回None(self):
        for text in ["", "   ", "01", "1A", "AAA01", "A", "A0", "A-1", "孔位", "A1B", "R"]:
            self.assertIsNone(parse_well(text), f"输入 {text!r} 不该被解析出来")


class TestDetectBlockOrientation(unittest.TestCase):
    """五种排列方向各来一例。"""

    @staticmethod
    def wells(*names: str) -> list[WellRecord]:
        return [make_well(i, n, "G", "S", 20.0) for i, n in enumerate(names, start=2)]

    def test_single(self):
        self.assertEqual(detect_block_orientation(self.wells("A01")), "single")

    def test_horizontal(self):
        self.assertEqual(
            detect_block_orientation(self.wells("A01", "A02", "A03")), "horizontal"
        )

    def test_vertical(self):
        self.assertEqual(
            detect_block_orientation(self.wells("A01", "B01", "C01")), "vertical"
        )

    def test_grid(self):
        self.assertEqual(
            detect_block_orientation(self.wells("A01", "A02", "B01", "B02")), "grid"
        )

    def test_unknown(self):
        self.assertEqual(
            detect_block_orientation(self.wells("A01", "A02", "第三孔")), "unknown"
        )


@requires_sample_file
class TestRealPlateOrientation(unittest.TestCase):
    """真实板子是横着上样的，改造后的编号必须和按文件行序排完全一致。"""

    def test_全部识别为横向(self):
        plate = load_plate()
        report = split_biological_replicates(plate)
        self.assertEqual(set(report.orientations.values()), {"horizontal"})
        self.assertEqual(len(report.orientations), 16)  # 4 样本 × 4 基因
        self.assertEqual(report.orientations[("CT", "GAPDH")], "horizontal")
        self.assertEqual(report.ambiguous, [])
        self.assertEqual(report.warnings, [])
        self.assertEqual(report.fill_direction, "auto")

    def test_与按行序排的结果逐孔一致(self):
        expected = numbering_by_row_index(load_plate())
        for direction in ("auto", "row", "column"):
            plate = load_plate()
            split_biological_replicates(plate, direction)
            self.assertEqual(
                numbering(plate), expected, f"fill_direction={direction} 时结果变了"
            )

    def test_非法的填板方向直接报错(self):
        with self.assertRaises(ValueError):
            split_biological_replicates(load_plate(), "斜着")


class TestVerticalLayout(unittest.TestCase):
    """竖着上样：必须按行号配对，而不是文件行序。"""

    EXPECTED = {"A01": 1, "B01": 2, "C01": 3, "A02": 1, "B02": 2, "C02": 3}

    def test_顺序录入时配对正确(self):
        plate = build_plate([
            ("A01", "GAPDH", "S1", 20.0), ("B01", "GAPDH", "S1", 20.1),
            ("C01", "GAPDH", "S1", 20.2),
            ("A02", "IL1b", "S1", 25.0), ("B02", "IL1b", "S1", 25.1),
            ("C02", "IL1b", "S1", 25.2),
        ])
        report = split_biological_replicates(plate)
        self.assertEqual(report.orientations[("S1", "GAPDH")], "vertical")
        self.assertEqual(report.orientations[("S1", "IL1b")], "vertical")
        self.assertEqual(report.warnings, [])
        self.assertEqual({w.well: w.replicate_index for w in plate.wells}, self.EXPECTED)

    def test_文件行序被打乱时仍按行号配对(self):
        """GAPDH 一列的行序是倒着的：按 row_index 排会把 C01 配给 A02。"""
        plate = build_plate([
            ("C01", "GAPDH", "S1", 20.2), ("B01", "GAPDH", "S1", 20.1),
            ("A01", "GAPDH", "S1", 20.0),
            ("A02", "IL1b", "S1", 25.0), ("B02", "IL1b", "S1", 25.1),
            ("C02", "IL1b", "S1", 25.2),
        ])
        split_biological_replicates(plate)
        self.assertEqual({w.well: w.replicate_index for w in plate.wells}, self.EXPECTED)
        pairs = {
            index: sorted(w.well for w in plate.wells if w.replicate_index == index)
            for index in (1, 2, 3)
        }
        self.assertEqual(pairs, {1: ["A01", "A02"], 2: ["B01", "B02"], 3: ["C01", "C02"]})


class TestGridLayout(unittest.TestCase):
    """方块排列：auto 要报歧义，显式指定方向要给出各自正确的配对。"""

    ROWS = [
        ("A01", "GAPDH", "S1", 20.0), ("A02", "GAPDH", "S1", 20.1),
        ("B01", "GAPDH", "S1", 20.2), ("B02", "GAPDH", "S1", 20.3),
        ("A03", "IL1b", "S1", 25.0), ("A04", "IL1b", "S1", 25.1),
        ("B03", "IL1b", "S1", 25.2), ("B04", "IL1b", "S1", 25.3),
    ]

    def pairs(self, fill_direction: str) -> dict[int, list[str]]:
        plate = build_plate(self.ROWS)
        self.report = split_biological_replicates(plate, fill_direction)
        return {
            index: sorted(w.well for w in plate.wells if w.replicate_index == index)
            for index in (1, 2, 3, 4)
        }

    def test_auto下给出歧义警告并退回先横后竖(self):
        pairs = self.pairs("auto")
        self.assertEqual(self.report.ambiguous, ["S1"])
        self.assertEqual(self.report.orientations[("S1", "GAPDH")], "grid")
        self.assertEqual(len(self.report.warnings), 1)
        text = self.report.warnings[0]
        for fragment in ("S1", "GAPDH", "IL1b", "跨行又跨列", "先横后竖", "纵向优先"):
            self.assertIn(fragment, text)
        self.assertEqual(
            pairs,
            {1: ["A01", "A03"], 2: ["A02", "A04"], 3: ["B01", "B03"], 4: ["B02", "B04"]},
        )

    def test_横向优先(self):
        pairs = self.pairs("row")
        self.assertEqual(self.report.warnings, [])  # 用户已明确指定，不再啰嗦
        self.assertEqual(self.report.ambiguous, ["S1"])
        self.assertEqual(
            pairs,
            {1: ["A01", "A03"], 2: ["A02", "A04"], 3: ["B01", "B03"], 4: ["B02", "B04"]},
        )

    def test_纵向优先(self):
        pairs = self.pairs("column")
        self.assertEqual(self.report.warnings, [])
        self.assertEqual(
            pairs,
            {1: ["A01", "A03"], 2: ["B01", "B03"], 3: ["A02", "A04"], 4: ["B02", "B04"]},
        )

    def test_两种方向给出不同的配对(self):
        self.assertNotEqual(self.pairs("row"), self.pairs("column"))


class TestInconsistentOrientation(unittest.TestCase):
    """同一样本下 GAPDH 横排、IL1b 竖排：配对极可能出错，必须醒目警告。"""

    @classmethod
    def setUpClass(cls):
        cls.plate = build_plate([
            ("A01", "GAPDH", "S1", 20.0), ("A02", "GAPDH", "S1", 20.1),
            ("A03", "GAPDH", "S1", 20.2),
            ("B01", "IL1b", "S1", 25.0), ("C01", "IL1b", "S1", 25.1),
            ("D01", "IL1b", "S1", 25.2),
        ])
        cls.report = split_biological_replicates(cls.plate)

    def test_方向被分别识别出来(self):
        self.assertEqual(self.report.orientations[("S1", "GAPDH")], "horizontal")
        self.assertEqual(self.report.orientations[("S1", "IL1b")], "vertical")

    def test_警告写清了是哪个基因什么方向(self):
        self.assertEqual(len(self.report.warnings), 1)
        text = self.report.warnings[0]
        for fragment in ("S1", "GAPDH 横向排列", "IL1b 纵向排列", "配对预览"):
            self.assertIn(fragment, text)

    def test_仍然按各自方向完成了拆分(self):
        self.assertEqual(
            {w.well: w.replicate_index for w in self.plate.wells},
            {"A01": 1, "A02": 2, "A03": 3, "B01": 1, "C01": 2, "D01": 3},
        )

    def test_配对预览里也点出了不一致(self):
        preview = build_pairing_preview(self.plate)[0]
        self.assertIn("不一致", preview.orientation_note)


class TestUnparsedWells(unittest.TestCase):
    """孔位认不出来时退回文件行序，并说明这件事。"""

    def test_退回行序编号并给出提示(self):
        plate = build_plate([
            ("样品一", "GAPDH", "S1", 20.0), ("样品二", "GAPDH", "S1", 20.1),
            ("A01", "IL1b", "S1", 25.0), ("A02", "IL1b", "S1", 25.1),
        ])
        report = split_biological_replicates(plate)
        self.assertEqual(report.orientations[("S1", "GAPDH")], "unknown")
        self.assertEqual(
            {w.well: w.replicate_index for w in plate.wells},
            {"样品一": 1, "样品二": 2, "A01": 1, "A02": 2},
        )
        self.assertEqual(len(report.warnings), 1)
        for fragment in ("S1", "GAPDH", "无法解析", "文件行序"):
            self.assertIn(fragment, report.warnings[0])


class TestPairingPreview(unittest.TestCase):
    """配对预览是用户核对配对的唯一手段，内容必须和实际拆分一致。"""

    @requires_sample_file
    def test_真实数据的CT那三行(self):
        plate = split_plate()
        previews = build_pairing_preview(plate)
        self.assertEqual([p.sample for p in previews], ORIGINAL_SAMPLES)

        ct = previews[0]
        self.assertIsInstance(ct, PairingPreview)
        self.assertEqual(ct.targets, TARGETS)
        self.assertEqual(ct.orientation_note, "横向排列")
        self.assertEqual(
            ct.rows,
            [
                (1, {"GAPDH": "A01", "IL1b": "A04", "IL6": "E01", "TNFa": "E04"}),
                (2, {"GAPDH": "A02", "IL1b": "A05", "IL6": "E02", "TNFa": "E05"}),
                (3, {"GAPDH": "A03", "IL1b": "A06", "IL6": "E03", "TNFa": "E06"}),
            ],
        )

    @requires_sample_file
    def test_预览与拆分后的样本名一一对应(self):
        plate = split_plate()
        actual = {
            (w.original_sample, w.replicate_index, w.target): w.well for w in plate.wells
        }
        for preview in build_pairing_preview(plate):
            for index, cells in preview.rows:
                for target, well in cells.items():
                    self.assertEqual(actual[(preview.sample, index, target)], well)

    @requires_sample_file
    def test_未拆分时把同一基因的孔并成一行(self):
        plate = load_plate()
        ct = build_pairing_preview(plate)[0]
        self.assertEqual(ct.rows, [(0, {
            "GAPDH": "A01、A02、A03", "IL1b": "A04、A05、A06",
            "IL6": "E01、E02、E03", "TNFa": "E04、E05、E06",
        })])
        self.assertIn("未拆分", ct.orientation_note)
        self.assertIn("横向排列", ct.orientation_note)

    def test_孔数不一致的样本也能预览(self):
        plate = build_plate([
            ("A01", "GAPDH", "S1", 20.0), ("A02", "GAPDH", "S1", 20.1),
            ("A03", "IL1b", "S1", 25.0),
        ])
        split_biological_replicates(plate)
        preview = build_pairing_preview(plate)[0]
        self.assertEqual(preview.rows, [(0, {"GAPDH": "A01、A02", "IL1b": "A03"})])
        self.assertIn("孔数不一致", preview.orientation_note)


@requires_sample_file
class TestReplicateQCAfterSplit(unittest.TestCase):
    """回归：拆分后复孔一致性不能退化成 48 行 n=1、SD 全空。"""

    def test_仍是十六行三复孔(self):
        plate = split_plate()
        qc = compute_replicate_qc(plate.wells)
        self.assertEqual(len(qc), 4 * 4)
        self.assertEqual({item.n for item in qc}, {3})
        self.assertFalse([item for item in qc if item.cq_sd is None])
        self.assertEqual({item.sample for item in qc}, set(ORIGINAL_SAMPLES))

    def test_与未拆分时的统计值完全一致(self):
        plain = compute_replicate_qc(load_plate().wells)
        split = compute_replicate_qc(split_plate().wells)
        self.assertEqual(
            [(q.target, q.sample, q.n, q.cq_mean, q.cq_sd) for q in plain],
            [(q.target, q.sample, q.n, q.cq_mean, q.cq_sd) for q in split],
        )

    def test_坏孔在拆分后依然能被标出来(self):
        plate = load_plate()
        for well in plate.wells:
            if well.well == "A06":  # IL1b / CT 的第 3 个孔，人为拉偏
                well.cq = 20.0
        split_biological_replicates(plate)
        flagged = [(q.target, q.sample) for q in compute_replicate_qc(plate.wells) if q.flagged]
        self.assertEqual(flagged, [("IL1b", "CT")])


@requires_sample_file
class TestMissingReferenceMessage(unittest.TestCase):
    """缺内参的警告要能回溯到「哪个样本的第几号重复」，并说明整个生物学重复出局。"""

    @classmethod
    def setUpClass(cls):
        plate = split_plate()
        for well in plate.wells:
            if well.well == "A02":  # CT-2 的唯一一个 GAPDH 孔
                well.included = False
        cls.result = analyze(
            plate.wells, group_by_original_sample(plate), REFERENCE, CONTROL
        )

    def test_警告里写的是原样本名加重复编号(self):
        text = "\n".join(self.result.warnings)
        self.assertIn("CT 的第 2 号生物学重复", text)
        self.assertNotIn("CT-2", text)

    def test_提示了整个生物学重复的所有基因都会退出(self):
        text = "\n".join(self.result.warnings)
        self.assertIn("整个生物学重复的所有基因", text)
        self.assertIn("3 个目标基因孔", text)  # IL1b / IL6 / TNFa 各一个

    def test_确实少了三个孔(self):
        wells = [r.well for r in self.result.well_results if r.sample == "CT-2"]
        self.assertEqual(wells, [])
        self.assertEqual(len(self.result.well_results), 33)


class TestSingleWellSamples(unittest.TestCase):
    """每个基因本来就只有一个孔时无需拆分，也不该改名。"""

    def test_单孔样本只编号不改名(self):
        plate = build_plate([
            ("A01", "GAPDH", "S1", 20.0), ("A02", "IL1b", "S1", 25.0),
            ("B01", "GAPDH", "S2", 21.0), ("B02", "IL1b", "S2", 26.0),
        ])
        report = split_biological_replicates(plate)

        self.assertFalse(report.enabled)
        self.assertEqual(report.split_samples, [])
        self.assertEqual(report.skipped, [])
        self.assertEqual(report.replicate_counts, {"S1": 1, "S2": 1})
        self.assertEqual(plate.samples, ["S1", "S2"])
        self.assertEqual({w.replicate_index for w in plate.wells}, {1})
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("没有发现需要拆分的生物学重复", report.warnings[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
