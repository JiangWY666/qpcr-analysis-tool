"""回归：Bio-Rad CFX 偶发导出的小写 ZIP 条目名也能打开。"""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook

try:
    from ._fixtures import ROOT
except ImportError:
    from _fixtures import ROOT

from qpcr_tool.reader import (
    _needs_ooxml_case_fix,
    _normalize_ooxml_casing,
    read_plate,
)


def _build_minimal_cfx_xlsx(path: Path) -> None:
    """造一份结构像 CFX 的正常大小写 xlsx。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "0"
    # A 列空着，表头从 B 开始，贴近 CFX 导出
    headers = ["", "Well", "Fluor", "Target", "Content", "Sample", "Cq"]
    ws.append(headers)
    ws.append(["", "A01", "SYBR", "GAPDH", "Unkn", "Ctrl", 18.1])
    ws.append(["", "A02", "SYBR", "GAPDH", "Unkn", "Ctrl", 18.2])
    ws.append(["", "A03", "SYBR", "IL6", "Unkn", "Ctrl", 24.0])
    ws.append(["", "A04", "SYBR", "IL6", "Unkn", "Ctrl", 24.1])
    info = wb.create_sheet("Run Information")
    info.append(["File Name", "case_fix_demo.pcrd"])
    info.append(["Created By User", "demo"])
    wb.save(path)


def _rewrite_zip_lowercase(src: Path, dst: Path) -> None:
    """把 Content_Types / sharedStrings 改成全小写，复现 CFX 的坑。"""
    mapping = {
        "[Content_Types].xml": "[content_types].xml",
        "xl/sharedStrings.xml": "xl/sharedstrings.xml",
    }
    with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(
        dst, "w", compression=zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            name = mapping.get(info.filename, info.filename)
            zout.writestr(name, zin.read(info.filename))


class TestOoxmlCaseFix(unittest.TestCase):
    def test_lowercase_entries_can_be_opened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            normal = Path(tmp) / "normal.xlsx"
            broken = Path(tmp) / "broken_case.xlsx"
            _build_minimal_cfx_xlsx(normal)
            _rewrite_zip_lowercase(normal, broken)

            self.assertFalse(_needs_ooxml_case_fix(str(normal)))
            self.assertTrue(_needs_ooxml_case_fix(str(broken)))

            # 直接用 openpyxl 会炸；我们的 read_plate 必须能过
            plate = read_plate(str(broken))
            self.assertEqual(plate.targets, ["GAPDH", "IL6"])
            self.assertEqual(plate.samples, ["Ctrl"])
            self.assertEqual(len(plate.wells), 4)
            self.assertEqual(len(plate.invalid_wells), 0)

    def test_normalize_rewrites_canonical_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            normal = Path(tmp) / "normal.xlsx"
            broken = Path(tmp) / "broken_case.xlsx"
            _build_minimal_cfx_xlsx(normal)
            _rewrite_zip_lowercase(normal, broken)
            stream = _normalize_ooxml_casing(str(broken))
            with zipfile.ZipFile(stream) as zf:
                names = set(zf.namelist())
            self.assertIn("[Content_Types].xml", names)
            self.assertNotIn("[content_types].xml", names)

    def test_july_cfx_file_if_present(self) -> None:
        """本机若有那份会踩坑的真实文件，顺带验一遍。不进版本库。"""
        matches = sorted(ROOT.glob("admin_2026-07-28*Quantification*.xlsx"))
        if not matches:
            self.skipTest("本机没有 2026-07-28 那份 CFX 文件")
        path = matches[0]
        self.assertTrue(_needs_ooxml_case_fix(str(path)))
        plate = read_plate(str(path))
        self.assertGreaterEqual(len(plate.wells), 1)
        self.assertIn("GAPDH", plate.targets)


if __name__ == "__main__":
    unittest.main()
