"""PySide6 图形界面：导入下机 Excel、勾选内参与对照组、计算并导出宽表。

界面动作都拆成了不依赖对话框的公开方法（``load_file`` / ``run_analysis`` /
``copy_to_clipboard`` / ``export_to``），按钮槽函数只负责弹对话框取参数，
这样自动化测试可以直接驱动窗口。
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Callable

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QFont,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .analysis import (
    MISSING_REFERENCE_PREFIX,
    SPLIT_DROPOUT_PREFIX,
    AnalysisError,
    AnalysisResult,
    analyze,
)
from .exporter import ExportError, WideTable, build_wide_table, export_excel
from .grouping import SampleGroup, auto_group, move_group, sample_to_group_map
from .reader import PlateData, ReaderError, WellRecord, read_plate
from .replicates import (
    NO_SPLIT_NEEDED,
    PairingPreview,
    SplitReport,
    build_pairing_preview,
    group_by_original_sample,
    restore_original_samples,
    split_biological_replicates,
)

WINDOW_TITLE = "qPCR 分析工具 v1.0"
EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xls")
EXCEL_FILTER = "Excel 文件 (*.xlsx *.xlsm *.xls)"

# 常见管家基因，加载文件后据此预勾选内参（大小写与符号都会归一化后比较）
HOUSEKEEPING_GENES = (
    "gapdh", "actb", "bactin", "betaactin", "18s", "28s", "tubb", "tuba",
    "hprt", "hprt1", "b2m", "rpl13a", "rps18", "u6", "ubc", "ywhaz",
    "pgk1", "sdha", "tbp",
)

# 猜测对照组用的关键词，越靠前优先级越高
CONTROL_KEYWORDS = (
    "对照", "control", "ctrl", "ct", "nc", "sham", "wt", "pbs",
    "vehicle", "veh", "normal", "untreated", "blank",
)

COLOR_INVALID = QColor("#EDEEF0")   # Cq 无效孔
COLOR_FLAGGED = QColor("#FFF4D2")   # 复孔离散度偏高
COLOR_CONTROL = QColor("#E3EFFE")   # 对照组所在列
COLOR_SECTION = QColor("#EDF2FA")   # 配对预览里的样本小节标题
COLOR_AMBIGUOUS = QColor("#FFE2C2") # 配对预览里方向有歧义的样本

WELL_COLUMNS = ("参与计算", "孔位", "基因", "样本", "重复", "所属分组", "Cq", "备注")
GROUP_COLUMNS = ("对照组", "组名", "包含样本")
SUMMARY_COLUMNS = ("基因", "分组", "n", "均值", "标准差")

# 填板方向下拉框：界面文案 -> split_biological_replicates 的 fill_direction 取值
FILL_DIRECTION_OPTIONS = (
    ("自动识别", "auto"),
    ("横向优先", "row"),
    ("纵向优先", "column"),
)

SPLIT_TOOLTIP = (
    "勾选（推荐）：同一个样本名下每个基因的第 N 个孔视为同一只动物，\n"
    "每个目标基因孔与同一只动物自己的内参孔 1:1 配对，个体间的上样量差异\n"
    "才能被真正校正掉。下机表里 CT 会被临时拆成 CT-1 / CT-2 / CT-3，\n"
    "但分组、导出仍按原始样本名 CT 进行。\n\n"
    "取消勾选：同名复孔的内参先取平均，再作为该样本所有目标基因孔的基线。\n"
    "适用于一管 cDNA 分成多孔上样的技术复孔。"
)

DIRECTION_TOOLTIP = (
    "决定同一个基因的多个孔按什么顺序编号成 1、2、3 号生物学重复。\n"
    "自动识别：同一行的孔按列号排，同一列的孔按行号排；\n"
    "　　　　　排成方块无法判断时退回先横后竖并给出警告。\n"
    "横向优先：先横着走完一行再换下一行（排序键为 行、列）。\n"
    "纵向优先：先竖着走完一列再换下一列（排序键为 列、行）。"
)

MERGE_TOOLTIP = (
    "只有剥掉编号后确实能与别的样本合并时才生效，"
    "「LPS BALF CIT013」这种自带数字后缀的名字会原样保留。"
)

MERGE_DISABLED_TOOLTIP = (
    "已开启生物学重复配对，分组按原始样本名进行"
    "（CT-1 / CT-2 / CT-3 自动回到 CT 组），该选项不适用。"
)

APP_QSS = """
QWidget {
    background-color: #F5F6F8;
    color: #1F2328;
}
QWidget#cellHost, QWidget#leftPanel, QScrollArea#leftScroll {
    background: transparent;
}
QMainWindow, QDialog {
    background-color: #F5F6F8;
}
QGroupBox {
    background-color: #FFFFFF;
    border: 1px solid #E1E4E8;
    border-radius: 6px;
    margin-top: 10px;
    padding: 14px 10px 10px 10px;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 4px;
    color: #3A4149;
}
QLineEdit, QComboBox, QListWidget, QTableWidget, QTextEdit {
    background-color: #FFFFFF;
    border: 1px solid #D8DCE1;
    border-radius: 6px;
    padding: 4px 6px;
    selection-background-color: #2D7FF9;
    selection-color: #FFFFFF;
}
QLineEdit:read-only {
    background-color: #F0F1F3;
    color: #4A5158;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #2D7FF9;
}
QPushButton {
    background-color: #FFFFFF;
    border: 1px solid #D0D5DB;
    border-radius: 6px;
    padding: 6px 14px;
}
QPushButton:hover {
    background-color: #F1F6FF;
    border-color: #2D7FF9;
}
QPushButton:pressed {
    background-color: #E1EBFB;
}
QPushButton:disabled {
    background-color: #F2F3F5;
    color: #A8AEB5;
    border-color: #E5E8EB;
}
QPushButton#primaryButton {
    background-color: #2D7FF9;
    border: 1px solid #2D7FF9;
    color: #FFFFFF;
    font-weight: bold;
    padding: 8px 28px;
}
QPushButton#primaryButton:hover {
    background-color: #1F6FE5;
    border-color: #1F6FE5;
}
QPushButton#primaryButton:pressed {
    background-color: #1A5FC8;
    border-color: #1A5FC8;
}
QPushButton#primaryButton:disabled {
    background-color: #C3D8F8;
    border-color: #C3D8F8;
    color: #F4F7FC;
}
QHeaderView::section {
    background-color: #F0F1F3;
    color: #3A4149;
    font-weight: bold;
    border: none;
    border-right: 1px solid #E4E7EA;
    border-bottom: 1px solid #E4E7EA;
    padding: 6px 8px;
}
QTableWidget {
    gridline-color: #EDEFF2;
    alternate-background-color: #FAFBFC;
}
QTableWidget::item:selected {
    background-color: #D6E6FF;
    color: #1F2328;
}
QTabWidget::pane {
    border: 1px solid #E1E4E8;
    border-radius: 6px;
    background-color: #FFFFFF;
    top: -1px;
}
QTabBar::tab {
    background-color: #ECEEF1;
    color: #4A5158;
    border: 1px solid #E1E4E8;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
    padding: 7px 22px;
}
QTabBar::tab:selected {
    background-color: #FFFFFF;
    color: #2D7FF9;
    font-weight: bold;
}
QCheckBox, QRadioButton {
    spacing: 6px;
    background: transparent;
}
QSplitter::handle {
    background-color: #E4E7EA;
}
QSplitter::handle:horizontal {
    width: 4px;
}
QSplitter::handle:vertical {
    height: 4px;
}
QStatusBar {
    background-color: #ECEEF1;
    color: #4A5158;
}
QLabel#summaryLabel {
    color: #37414A;
}
QLabel#hintLabel {
    color: #7A828A;
}
QLabel#alertBanner {
    background-color: #FFF3D6;
    border: 1px solid #E3B341;
    border-radius: 6px;
    color: #8A5300;
    padding: 8px 10px;
}
QLabel#helpBadge {
    background-color: #E2E8F0;
    border-radius: 9px;
    color: #4A5158;
    font-weight: bold;
    padding: 1px 7px;
}
"""


def _norm(name: str) -> str:
    """基因/分组名归一化：转小写、β 当 b、去掉空白与常见符号。"""
    text = name.strip().lower().replace("β", "b").replace("Β", "b")
    return "".join(ch for ch in text if ch.isalnum())


def _tokens(name: str) -> set[str]:
    """按非字母数字切词，用于短关键词（ct / wt / nc）的整词匹配。"""
    token = ""
    result: set[str] = set()
    for ch in name.strip().lower().replace("β", "b"):
        if ch.isalnum():
            token += ch
        elif token:
            result.add(token)
            token = ""
    if token:
        result.add(token)
    return result


def is_housekeeping(target: str) -> bool:
    """判断一个基因名是否像管家基因。"""
    norm = _norm(target)
    if not norm:
        return False
    for alias in HOUSEKEEPING_GENES:
        if norm == alias or (len(alias) >= 3 and norm.startswith(alias)):
            return True
    return False


def guess_control_index(names: list[str]) -> int:
    """在分组名里挑一个最像对照组的，挑不出来就返回 0。

    匹配强度依次为「整名相同 > 整词命中 > 名字里包含」，同强度时按关键词优先级。
    """
    best: tuple[int, int, int] | None = None
    for index, name in enumerate(names):
        norm = _norm(name)
        tokens = _tokens(name)
        for rank, keyword in enumerate(CONTROL_KEYWORDS):
            if norm == keyword:
                score = (0, rank, index)
            elif keyword in tokens:
                score = (1, rank, index)
            elif len(keyword) > 2 and keyword in norm:
                score = (2, rank, index)
            else:
                continue
            if best is None or score < best:
                best = score
            break
    return best[2] if best else 0


def apply_theme(app: QApplication) -> None:
    """给整个应用套上浅色主题与中文字体。"""
    font = QFont("Microsoft YaHei UI")
    font.setPointSizeF(10.0)
    app.setFont(font)
    app.setStyleSheet(APP_QSS)


def install_exception_hook() -> None:
    """兜底钩子：未捕获异常弹窗提示，而不是让窗口静默消失。"""
    previous = sys.excepthook

    def hook(exc_type: type[BaseException], exc: BaseException, tb) -> None:
        previous(exc_type, exc, tb)
        if QApplication.instance() is None:
            return
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("程序出现未处理的错误")
        box.setText(f"{exc_type.__name__}：{exc}")
        box.setInformativeText("程序仍在运行，建议保存当前结果后重启。")
        box.setDetailedText(detail)
        box.exec()

    sys.excepthook = hook


class RunInfoDialog(QDialog):
    """展示下机文件的 Run Information 键值对。"""

    def __init__(self, info: list[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("运行信息")
        self.resize(600, 480)

        table = QTableWidget(len(info), 2, self)
        table.setHorizontalHeaderLabels(("项目", "内容"))
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, (key, value) in enumerate(info):
            table.setItem(row, 0, QTableWidgetItem(key))
            table.setItem(row, 1, QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        close_btn = QPushButton("关闭", self)
        close_btn.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(table)
        layout.addLayout(bottom)


class MainWindow(QMainWindow):
    """主窗口。左侧配置内参与分组，右侧看孔位数据与分析结果。"""

    WIDE_HEADER_ROWS = 2  # 宽表预览里「基因行 + 分组行」占用的行数

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plate: PlateData | None = None
        self.groups: list[SampleGroup] = []
        self.result: AnalysisResult | None = None
        self.wide: WideTable | None = None
        self.split_report: SplitReport | None = None
        self.previews: list[PairingPreview] = []
        # 算结果时用的那份拆分报告。之后用户改了开关也不影响导出，报告与数值始终对得上
        self._result_report: SplitReport | None = None

        self._control_index = 0
        self._control_buttons: QButtonGroup | None = None
        self._qc_flags: dict[tuple[str, str], float] = {}
        self._updating = False

        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1280, 900)
        self.setMinimumSize(1080, 700)
        self.setAcceptDrops(True)
        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(10.0)
        self.setFont(font)
        self.setStyleSheet(APP_QSS)

        self._build_ui()
        self._update_actions()
        self.statusBar().showMessage("请选择或拖入 qPCR 下机 Excel 文件")

    # ------------------------------------------------------------------ 界面搭建

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self._build_file_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 860])
        layout.addWidget(splitter, 1)
        layout.addWidget(self._build_action_bar())

        self.setCentralWidget(central)

    def _build_file_bar(self) -> QWidget:
        box = QGroupBox("数据文件", self)
        outer = QVBoxLayout(box)
        outer.setSpacing(6)

        row = QHBoxLayout()
        self.path_edit = QLineEdit(box)
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("尚未选择文件，可点击右侧按钮或把 Excel 直接拖进窗口")
        self.browse_btn = QPushButton("选择文件…", box)
        self.browse_btn.clicked.connect(self._on_browse)
        self.run_info_btn = QPushButton("运行信息", box)
        self.run_info_btn.clicked.connect(self._on_show_run_info)
        row.addWidget(self.path_edit, 1)
        row.addWidget(self.browse_btn)
        row.addWidget(self.run_info_btn)
        outer.addLayout(row)

        self.summary_label = QLabel("等待导入数据", box)
        self.summary_label.setObjectName("summaryLabel")
        outer.addWidget(self.summary_label)

        # 拆分歧义、方向不一致、整只动物出局这类问题必须一眼看见，不能只躺在结果页底部
        self.alert_banner = QLabel("", box)
        self.alert_banner.setObjectName("alertBanner")
        self.alert_banner.setWordWrap(True)
        self.alert_banner.setVisible(False)
        outer.addWidget(self.alert_banner)
        return box

    def _build_left_panel(self) -> QWidget:
        """左栏三块内容偏高，套一层滚动区：窗口压矮时出滚动条，而不是互相挤到重叠。"""
        panel = QWidget(self)
        panel.setObjectName("leftPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_reference_box(), 2)
        layout.addWidget(self._build_replicate_box(), 0)
        layout.addWidget(self._build_group_box(), 3)

        area = QScrollArea(self)
        area.setObjectName("leftScroll")
        area.setWidget(panel)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return area

    def _build_reference_box(self) -> QWidget:
        box = QGroupBox("内参基因", self)
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        self.target_list = QListWidget(box)
        self.target_list.itemChanged.connect(self._on_reference_changed)
        # 基因通常只有几个，别让这个列表把左栏的高度都占了
        self.target_list.setMinimumHeight(96)
        self.target_list.setMaximumHeight(150)
        layout.addWidget(self.target_list, 1)

        self.reference_hint = QLabel("勾选一个或多个内参；选多个时按几何平均归一化", box)
        self.reference_hint.setObjectName("hintLabel")
        self.reference_hint.setWordWrap(True)
        layout.addWidget(self.reference_hint)
        return box

    def _build_replicate_box(self) -> QWidget:
        box = QGroupBox("生物学重复", self)
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.split_check = QCheckBox("同名样本的复孔视为生物学重复（推荐）", box)
        self.split_check.setChecked(True)
        self.split_check.setToolTip(SPLIT_TOOLTIP)
        self.split_check.toggled.connect(self._on_split_toggled)
        help_badge = QLabel("?", box)
        help_badge.setObjectName("helpBadge")
        help_badge.setToolTip(SPLIT_TOOLTIP)
        top.addWidget(self.split_check)
        top.addWidget(help_badge)
        top.addStretch(1)
        layout.addLayout(top)

        direction_row = QHBoxLayout()
        direction_row.setSpacing(6)
        direction_label = QLabel("孔位排列方向", box)
        direction_label.setToolTip(DIRECTION_TOOLTIP)
        self.direction_combo = QComboBox(box)
        self.direction_combo.addItems([label for label, _ in FILL_DIRECTION_OPTIONS])
        self.direction_combo.setToolTip(DIRECTION_TOOLTIP)
        self.direction_combo.currentIndexChanged.connect(self._on_direction_changed)
        direction_row.addWidget(direction_label)
        direction_row.addWidget(self.direction_combo, 1)
        layout.addLayout(direction_row)

        self.split_hint = QLabel("导入文件后这里会显示拆分结果", box)
        self.split_hint.setObjectName("hintLabel")
        self.split_hint.setWordWrap(True)
        layout.addWidget(self.split_hint)
        return box

    def _build_group_box(self) -> QWidget:
        box = QGroupBox("实验分组", self)
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        self.merge_check = QCheckBox("合并样本名末尾的编号（如 LPS-1/LPS-2 归为 LPS）", box)
        self.merge_check.setToolTip(MERGE_TOOLTIP)
        self.merge_check.toggled.connect(self._on_merge_toggled)
        layout.addWidget(self.merge_check)

        self.group_table = QTableWidget(0, len(GROUP_COLUMNS), box)
        self.group_table.setHorizontalHeaderLabels(GROUP_COLUMNS)
        self.group_table.verticalHeader().setVisible(False)
        self.group_table.setAlternatingRowColors(True)
        self.group_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.group_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.group_table.itemChanged.connect(self._on_group_item_changed)
        header = self.group_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.group_table.setColumnWidth(0, 58)
        self.group_table.setColumnWidth(1, 120)
        # 左栏多了「生物学重复」一块，给分组表兜个底，别被挤成两行
        self.group_table.setMinimumHeight(130)
        layout.addWidget(self.group_table, 1)

        buttons = QHBoxLayout()
        self.move_up_btn = QPushButton("上移", box)
        self.move_up_btn.clicked.connect(lambda: self._move_selected_group(-1))
        self.move_down_btn = QPushButton("下移", box)
        self.move_down_btn.clicked.connect(lambda: self._move_selected_group(1))
        self.regroup_btn = QPushButton("重新识别", box)
        self.regroup_btn.clicked.connect(self._rebuild_groups)
        buttons.addWidget(self.move_up_btn)
        buttons.addWidget(self.move_down_btn)
        buttons.addWidget(self.regroup_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        hint = QLabel("上下顺序即导出列顺序，组名可双击修改", box)
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return box

    def _build_right_panel(self) -> QWidget:
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_well_tab(), "孔位数据")
        self.tabs.addTab(self._build_pairing_tab(), "配对预览")
        self._result_page = self._build_result_tab()
        self.tabs.addTab(self._result_page, "分析结果")
        return self.tabs

    def _build_pairing_tab(self) -> QWidget:
        """配对预览：用户核对「哪几个孔属于同一只动物」的唯一手段。"""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        top = QLabel(
            "每个原始样本一个小节，行是生物学重复（一只动物），列是各基因，"
            "格子里是这只动物在该基因上用的孔。同一行的孔必须真的来自同一只动物，"
            "否则内参就配错了。",
            page,
        )
        top.setObjectName("hintLabel")
        top.setWordWrap(True)
        layout.addWidget(top)

        self.pairing_table = QTableWidget(0, 1, page)
        self.pairing_table.setHorizontalHeaderLabels(("重复",))
        self.pairing_table.verticalHeader().setVisible(False)
        self.pairing_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pairing_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.pairing_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.pairing_table, 1)

        self.pairing_hint = QLabel("导入文件后这里会显示每只动物的孔位配对", page)
        self.pairing_hint.setObjectName("hintLabel")
        self.pairing_hint.setWordWrap(True)
        layout.addWidget(self.pairing_hint)
        return page

    def _build_well_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        self.check_all_btn = QPushButton("全选", page)
        self.check_all_btn.clicked.connect(lambda: self._set_all_included(True))
        self.check_none_btn = QPushButton("全不选", page)
        self.check_none_btn.clicked.connect(lambda: self._set_all_included(False))
        self.check_valid_btn = QPushButton("只保留有效孔", page)
        self.check_valid_btn.clicked.connect(self._keep_valid_only)
        self.sort_combo = QComboBox(page)
        self.sort_combo.addItems(("按孔位排序", "按基因排序"))
        self.sort_combo.currentIndexChanged.connect(lambda _: self._populate_well_table())
        toolbar.addWidget(self.check_all_btn)
        toolbar.addWidget(self.check_none_btn)
        toolbar.addWidget(self.check_valid_btn)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("排序", page))
        toolbar.addWidget(self.sort_combo)
        layout.addLayout(toolbar)

        self.well_table = QTableWidget(0, len(WELL_COLUMNS), page)
        self.well_table.setHorizontalHeaderLabels(WELL_COLUMNS)
        self.well_table.verticalHeader().setVisible(False)
        self.well_table.setAlternatingRowColors(True)
        self.well_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.well_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.well_table.itemChanged.connect(self._on_well_item_changed)
        header = self.well_table.horizontalHeader()
        for index in range(len(WELL_COLUMNS) - 1):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(WELL_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.well_table, 1)

        hint = QLabel(
            "取消勾选即把该孔剔除出计算；灰色行是 Cq 无效孔，淡黄行是复孔离散度偏高，"
            "手动剔除坏孔后重新点「开始计算」即可。",
            page,
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return page

    def _build_result_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Vertical, page)

        wide_box = QGroupBox("宽表预览（可直接粘贴进 GraphPad Prism）", splitter)
        wide_layout = QVBoxLayout(wide_box)
        self.wide_hint = QLabel(
            "第 1 行是基因、第 2 行是分组，下面每一行是一个复孔的 RQ 值；淡蓝列为对照组。",
            wide_box,
        )
        self.wide_hint.setObjectName("hintLabel")
        self.wide_hint.setWordWrap(True)
        wide_layout.addWidget(self.wide_hint)
        self.wide_view = QTableWidget(0, 0, wide_box)
        self.wide_view.horizontalHeader().setVisible(False)
        self.wide_view.verticalHeader().setVisible(False)
        self.wide_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        wide_layout.addWidget(self.wide_view, 1)
        splitter.addWidget(wide_box)

        summary_box = QGroupBox("分组汇总", splitter)
        summary_layout = QVBoxLayout(summary_box)
        self.summary_table = QTableWidget(0, len(SUMMARY_COLUMNS), summary_box)
        self.summary_table.setHorizontalHeaderLabels(SUMMARY_COLUMNS)
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.setAlternatingRowColors(True)
        self.summary_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        summary_header = self.summary_table.horizontalHeader()
        summary_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        summary_layout.addWidget(self.summary_table)
        splitter.addWidget(summary_box)

        warning_box = QGroupBox("警告与提示", splitter)
        warning_layout = QVBoxLayout(warning_box)
        self.warning_view = QTextEdit(warning_box)
        self.warning_view.setReadOnly(True)
        warning_layout.addWidget(self.warning_view)
        splitter.addWidget(warning_box)

        splitter.setSizes([380, 270, 120])
        layout.addWidget(splitter, 1)
        self._show_warnings()
        return page

    def _build_action_bar(self) -> QWidget:
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.calc_btn = QPushButton("开始计算", bar)
        self.calc_btn.setObjectName("primaryButton")
        self.calc_btn.clicked.connect(self._on_calculate)
        layout.addWidget(self.calc_btn)
        layout.addStretch(1)

        self.copy_all_btn = QPushButton("复制到剪贴板（含表头）", bar)
        self.copy_all_btn.clicked.connect(lambda: self._guard(lambda: self.copy_to_clipboard(True)))
        self.copy_values_btn = QPushButton("仅复制数值", bar)
        self.copy_values_btn.clicked.connect(lambda: self._guard(lambda: self.copy_to_clipboard(False)))
        self.export_btn = QPushButton("导出 Excel…", bar)
        self.export_btn.clicked.connect(self._on_export)
        layout.addWidget(self.copy_all_btn)
        layout.addWidget(self.copy_values_btn)
        layout.addWidget(self.export_btn)
        return bar

    # ------------------------------------------------------------------ 公开动作

    def load_file(self, path: str) -> PlateData:
        """读取下机文件并刷新界面。解析失败时抛 ReaderError。"""
        plate = read_plate(path)
        self.plate = plate
        self.result = None
        self.wide = None
        self._result_report = None
        self._qc_flags = {}

        self.path_edit.setText(os.path.abspath(path))
        self.summary_label.setText(
            f"已识别 {len(plate.targets)} 个基因、{len(plate.samples)} 个样本、"
            f"{len(plate.wells) - len(plate.invalid_wells)} 个有效孔"
            f"（{len(plate.invalid_wells)} 个无效）"
        )
        self._populate_target_list()
        self._apply_replicate_split()
        self._rebuild_groups()
        self._populate_pairing_table()
        self._update_split_hint()
        self._clear_results()
        self._update_alerts()
        self._update_actions()
        self.tabs.setCurrentIndex(0)
        self.statusBar().showMessage(
            f"已加载「{os.path.basename(path)}」，请确认内参与对照组后点「开始计算」", 8000
        )
        return plate

    def run_analysis(self) -> AnalysisResult:
        """按当前界面配置执行 2^-ΔΔCt 计算。参数不足时抛 AnalysisError。"""
        if self.plate is None:
            raise AnalysisError("请先导入 qPCR 下机 Excel 文件。")
        result = analyze(
            self.plate.wells,
            self.groups,
            self.reference_targets(),
            self.control_group_name(),
        )
        self.result = result
        self.wide = build_wide_table(result)
        self._result_report = self.split_report
        # QC 按原始样本名分桶，这里的键也必须是原始样本名，否则孔位表标不上黄
        self._qc_flags = {
            (item.target, item.sample): item.cq_sd
            for item in result.qc
            if item.flagged and item.cq_sd is not None
        }

        self._populate_well_table()
        self._populate_wide_view()
        self._populate_summary_table()
        self._show_warnings()
        self._update_alerts()
        self._update_actions()
        self.tabs.setCurrentIndex(self.tabs.indexOf(self._result_page))
        self.statusBar().showMessage(
            f"计算完成：{len(result.targets)} 个基因 × {len(result.groups)} 个分组，"
            f"对照组「{result.control_group}」",
            8000,
        )
        return result

    def copy_to_clipboard(self, with_header: bool = True) -> tuple[int, int]:
        """把宽表制表符文本写进剪贴板，返回 (列数, 数据行数)。"""
        if self.wide is None:
            raise AnalysisError("还没有结果可复制，请先点「开始计算」。")
        QApplication.clipboard().setText(self.wide.to_tsv(include_header=with_header))
        columns, rows = len(self.wide.columns), len(self.wide.rows)
        suffix = "含表头" if with_header else "仅数值"
        self.statusBar().showMessage(
            f"已复制 {columns} 列 × {rows} 行（{suffix}），可直接粘进 Prism", 8000
        )
        return columns, rows

    def export_to(self, path: str) -> str:
        """导出 Excel 结果文件，返回实际写入路径。失败时抛 ExportError。"""
        if self.result is None or self.plate is None:
            raise AnalysisError("还没有结果可导出，请先点「开始计算」。")
        export_excel(self.result, self.plate, path, self._result_report)
        self.statusBar().showMessage(f"已导出到 {path}", 8000)
        return path

    def fill_direction(self) -> str:
        """下拉框选中的填板方向，取值为 auto / row / column。"""
        index = max(0, self.direction_combo.currentIndex())
        return FILL_DIRECTION_OPTIONS[index][1]

    def set_fill_direction(self, direction: str) -> None:
        """按 auto / row / column 选中填板方向，未知取值抛 ValueError。"""
        for index, (_, value) in enumerate(FILL_DIRECTION_OPTIONS):
            if value == direction:
                self.direction_combo.setCurrentIndex(index)
                return
        raise ValueError(f"未知的孔位排列方向：{direction!r}。")

    def set_split_enabled(self, enabled: bool) -> None:
        """开关生物学重复配对。等价于用户点那个复选框，但不依赖界面事件。"""
        if self.split_check.isChecked() == enabled:
            self._refresh_replicates()
            return
        self.split_check.setChecked(enabled)

    def default_export_path(self) -> str:
        """默认导出路径：源文件同目录、同名加「_分析结果」后缀。"""
        if self.plate is None:
            return "分析结果.xlsx"
        stem, _ = os.path.splitext(self.plate.file_path)
        return f"{stem}_分析结果.xlsx"

    def reference_targets(self) -> list[str]:
        """当前勾选的内参基因，顺序与列表一致。"""
        return [
            self.target_list.item(row).text()
            for row in range(self.target_list.count())
            if self.target_list.item(row).checkState() == Qt.CheckState.Checked
        ]

    def set_reference_targets(self, targets: list[str]) -> None:
        """按名字勾选内参（大小写不敏感），未列出的一律取消勾选。"""
        wanted = {name.strip().lower() for name in targets}
        self._updating = True
        try:
            for row in range(self.target_list.count()):
                item = self.target_list.item(row)
                state = (
                    Qt.CheckState.Checked
                    if item.text().strip().lower() in wanted
                    else Qt.CheckState.Unchecked
                )
                item.setCheckState(state)
        finally:
            self._updating = False
        self._update_reference_hint()
        self._mark_dirty()

    def control_group_name(self) -> str:
        """当前对照组名，没有分组时返回空串。"""
        if 0 <= self._control_index < len(self.groups):
            return self.groups[self._control_index].name
        return ""

    def set_control_group(self, name: str) -> None:
        """按组名选中对照组，找不到时抛 ValueError。"""
        for index, group in enumerate(self.groups):
            if group.name == name:
                self._select_control_row(index)
                self._mark_dirty()
                return
        raise ValueError(f"当前分组里没有「{name}」。")

    # ------------------------------------------------------------------ 生物学重复

    def _apply_replicate_split(self) -> None:
        """按当前开关与方向重做拆分。关掉时把样本名还原成下机表里的原名。

        每次都先还原再拆：换填板方向时要从干净状态重新编号，否则残留的旧编号会让
        「改方向」看起来没生效。
        """
        if self.plate is None:
            self.split_report = None
            return
        restore_original_samples(self.plate)
        if self.split_check.isChecked():
            self.split_report = split_biological_replicates(
                self.plate, self.fill_direction()
            )
        else:
            self.split_report = None

    def _refresh_replicates(self) -> None:
        """开关或方向变化后重做拆分并刷新三张表；按既有约定不自动重算结果。"""
        if self.plate is None:
            self._update_actions()
            return
        self._apply_replicate_split()
        self._rebuild_groups()
        self._populate_pairing_table()
        self._update_split_hint()
        self._update_alerts()
        self._update_actions()

    def _on_split_toggled(self, _checked: bool) -> None:
        self._refresh_replicates()

    def _on_direction_changed(self, _index: int) -> None:
        if self.split_check.isChecked():
            self._refresh_replicates()

    def _update_split_hint(self) -> None:
        """把当前的拆分口径用一句话说清楚，逐样本的明细放进 tooltip，别把左栏撑高。"""
        detail = ""
        if self.plate is None:
            text = "导入文件后这里会显示拆分结果"
        elif not self.split_check.isChecked():
            text = "当前按技术复孔处理：同名复孔的内参先取平均"
        elif self.split_report is None or not self.split_report.enabled:
            text = "没有可拆的同名复孔，当前与技术复孔模式等价"
        else:
            report = self.split_report
            total = sum(report.replicate_counts[n] for n in report.split_samples)
            text = (
                f"已把 {len(report.split_samples)} 个样本拆成 {total} 只动物；"
                "请到「配对预览」核对"
            )
            if report.skipped:
                text += f"（另有 {len(report.skipped)} 个样本孔数不一致未拆分）"
            detail = "、".join(
                f"{name}×{report.replicate_counts[name]}" for name in report.split_samples
            )
        self.split_hint.setText(text)
        self.split_hint.setToolTip(detail)

    def _populate_pairing_table(self) -> None:
        """每个原始样本一个小节：标题行写样本名与方向，下面每行是一只动物。"""
        table = self.pairing_table
        self.previews = build_pairing_preview(self.plate) if self.plate else []
        targets: list[str] = []
        for preview in self.previews:
            for target in preview.targets:
                if target not in targets:
                    targets.append(target)
        columns = ["重复", *targets]
        ambiguous = set(self.split_report.ambiguous) if self.split_report else set()

        table.clearSpans()
        table.clear()
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setRowCount(sum(1 + len(p.rows) for p in self.previews))

        bold = QFont(self.font())
        bold.setBold(True)
        row = 0
        flagged_samples: list[str] = []
        for preview in self.previews:
            risky = preview.sample in ambiguous or "不一致" in preview.orientation_note
            if risky:
                flagged_samples.append(preview.sample)
            title = QTableWidgetItem(f"{preview.sample}　·　{preview.orientation_note}")
            title.setFont(bold)
            title.setBackground(QBrush(COLOR_AMBIGUOUS if risky else COLOR_SECTION))
            table.setItem(row, 0, title)
            if len(columns) > 1:
                table.setSpan(row, 0, 1, len(columns))
            row += 1
            for index, cells in preview.rows:
                label = f"重复 {index}" if index else "全部孔（未配对）"
                texts = [label, *(cells.get(target, "-") for target in targets)]
                for column, text in enumerate(texts):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
                    )
                    if risky:
                        item.setBackground(QBrush(COLOR_FLAGGED))
                    table.setItem(row, column, item)
                row += 1

        self.pairing_hint.setText(self._pairing_hint_text(flagged_samples))

    def _pairing_hint_text(self, flagged: list[str]) -> str:
        if self.plate is None:
            return "导入文件后这里会显示每只动物的孔位配对"
        if flagged:
            return (
                f"⚠ {'、'.join(flagged)} 的排列方向有歧义或不一致，上面已高亮，"
                "请逐行确认；配对不对时改用左侧「孔位排列方向」手动指定。"
            )
        if not self.split_check.isChecked():
            return (
                "当前未开启生物学重复配对，同名复孔会被一起平均，"
                "所以这里只列出每个基因用到的全部孔。"
            )
        return "各样本的排列方向都能明确识别，按上表配对内参与目标基因。"

    # ------------------------------------------------------------------ 内参列表

    def _populate_target_list(self) -> None:
        """列出所有基因并预勾选常见管家基因。"""
        self._updating = True
        try:
            self.target_list.clear()
            for target in self.plate.targets if self.plate else []:
                item = QListWidgetItem(target, self.target_list)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if is_housekeeping(target) else Qt.CheckState.Unchecked
                )
        finally:
            self._updating = False
        self._update_reference_hint()

    def _on_reference_changed(self, _item: QListWidgetItem) -> None:
        if self._updating:
            return
        self._update_reference_hint()
        self._mark_dirty()

    def _update_reference_hint(self) -> None:
        selected = self.reference_targets()
        if not selected:
            self.reference_hint.setText("尚未选择内参基因，计算前必须至少勾选一个")
        else:
            self.reference_hint.setText(
                f"已选 {len(selected)} 个内参：{'、'.join(selected)}"
                + ("（按几何平均归一化）" if len(selected) > 1 else "")
            )

    # ------------------------------------------------------------------ 分组表格

    def _on_merge_toggled(self, _checked: bool) -> None:
        if self.plate is not None:
            self._rebuild_groups()

    def _rebuild_groups(self) -> None:
        """重新自动聚类分组，并猜一个对照组。

        开启生物学重复配对时按原始样本名成组，CT-1/CT-2/CT-3 自动回到 CT 组；
        关闭时才走样本名聚类那套。
        """
        if self.plate is None:
            return
        if self.split_check.isChecked():
            self.groups = group_by_original_sample(self.plate)
        else:
            self.groups = auto_group(self.plate.samples, self.merge_check.isChecked())
        self._control_index = guess_control_index([g.name for g in self.groups])
        self._populate_group_table()
        self._populate_well_table()
        self._mark_dirty()

    def _populate_group_table(self) -> None:
        table = self.group_table
        self._updating = True
        try:
            if self._control_buttons is not None:
                self._control_buttons.deleteLater()
            self._control_buttons = QButtonGroup(self)
            self._control_buttons.setExclusive(True)
            self._control_buttons.idToggled.connect(self._on_control_toggled)

            # 先清零行数，确保上一批单选按钮连同所在单元格控件一起被销毁
            table.setRowCount(0)
            table.setRowCount(len(self.groups))
            for row, group in enumerate(self.groups):
                host = QWidget(table)
                host.setObjectName("cellHost")
                host_layout = QHBoxLayout(host)
                host_layout.setContentsMargins(0, 0, 0, 0)
                host_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                radio = QRadioButton(host)
                radio.setToolTip("设为对照组")
                radio.setChecked(row == self._control_index)
                host_layout.addWidget(radio)
                self._control_buttons.addButton(radio, row)
                table.setCellWidget(row, 0, host)

                name_item = QTableWidgetItem(group.name)
                name_item.setToolTip("双击可修改组名")
                table.setItem(row, 1, name_item)

                samples_item = QTableWidgetItem("、".join(group.samples))
                samples_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
                samples_item.setToolTip(samples_item.text())
                table.setItem(row, 2, samples_item)
        finally:
            self._updating = False

    def _on_control_toggled(self, index: int, checked: bool) -> None:
        if checked and not self._updating:
            self._control_index = index
            self._mark_dirty()

    def _select_control_row(self, index: int) -> None:
        self._control_index = index
        if self._control_buttons is None:
            return
        button = self._control_buttons.button(index)
        if button is not None:
            self._updating = True
            try:
                button.setChecked(True)
            finally:
                self._updating = False

    def _on_group_item_changed(self, item: QTableWidgetItem) -> None:
        """把用户改过的组名同步回 SampleGroup，并刷新孔位表的分组列。"""
        if self._updating or item.column() != 1:
            return
        row = item.row()
        if not 0 <= row < len(self.groups):
            return
        new_name = item.text().strip()
        taken = {g.name for i, g in enumerate(self.groups) if i != row}
        if not new_name or new_name in taken:
            reason = "组名不能为空" if not new_name else f"组名「{new_name}」已被占用"
            self.statusBar().showMessage(f"{reason}，已还原", 6000)
            self._updating = True
            try:
                item.setText(self.groups[row].name)
            finally:
                self._updating = False
            return
        self.groups[row].name = new_name
        self._populate_well_table()
        self._mark_dirty()

    def _move_selected_group(self, delta: int) -> None:
        row = self.group_table.currentRow()
        if not 0 <= row < len(self.groups):
            self.statusBar().showMessage("请先在分组表里选中一行", 5000)
            return
        control_name = self.control_group_name()
        new_row = move_group(self.groups, row, delta)
        if new_row == row:
            return
        self._control_index = next(
            (i for i, g in enumerate(self.groups) if g.name == control_name), 0
        )
        self._populate_group_table()
        self.group_table.setCurrentCell(new_row, 1)
        self._mark_dirty()

    # ------------------------------------------------------------------ 孔位表格

    def _sorted_wells(self) -> list[tuple[int, WellRecord]]:
        """返回 (在 plate.wells 里的下标, 孔记录)，下标用于写回 included。"""
        if self.plate is None:
            return []
        items = list(enumerate(self.plate.wells))
        if self.sort_combo.currentIndex() == 1:
            items.sort(key=lambda pair: (pair[1].target, pair[1].sample, pair[1].well))
        return items

    def _qc_key(self, well: WellRecord) -> tuple[str, str]:
        """QC 是按原始样本名分桶的，孔位表要用同一把钥匙去查。"""
        return well.target, well.original_sample or well.sample

    def _note_for(self, well: WellRecord) -> str:
        if not well.valid:
            return f"Cq 无效：{well.cq_text or '空'}"
        sd = self._qc_flags.get(self._qc_key(well))
        if sd is not None and well.included:
            return f"复孔离散度偏高 (SD={sd:.2f})"
        return ""

    def _populate_well_table(self) -> None:
        table = self.well_table
        group_of = sample_to_group_map(self.groups)
        self._updating = True
        try:
            rows = self._sorted_wells()
            table.clearContents()
            table.setRowCount(len(rows))
            for row, (index, well) in enumerate(rows):
                check = QTableWidgetItem()
                check.setData(Qt.ItemDataRole.UserRole, index)
                check.setCheckState(
                    Qt.CheckState.Checked if well.included else Qt.CheckState.Unchecked
                )
                if well.valid:
                    check.setFlags(
                        Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                    )
                else:
                    check.setFlags(Qt.ItemFlag.ItemIsUserCheckable)
                table.setItem(row, 0, check)

                texts = (
                    well.well,
                    well.target,
                    well.sample,
                    str(well.replicate_index) if well.replicate_index >= 1 else "-",
                    group_of.get(well.sample, "(未分组)"),
                    f"{well.cq:.2f}" if well.valid else (well.cq_text or "-"),
                    self._note_for(well),
                )
                for offset, text in enumerate(texts, start=1):
                    cell = QTableWidgetItem(text)
                    if offset in (1, 4, 6):
                        cell.setTextAlignment(
                            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
                        )
                    table.setItem(row, offset, cell)
                self._paint_well_row(row, well)
        finally:
            self._updating = False

    def _paint_well_row(self, row: int, well: WellRecord) -> None:
        """无效孔浅灰、复孔离散度偏高淡黄。"""
        if not well.valid:
            color = COLOR_INVALID
        elif well.included and self._qc_key(well) in self._qc_flags:
            color = COLOR_FLAGGED
        else:
            return
        brush = QBrush(color)
        for column in range(self.well_table.columnCount()):
            item = self.well_table.item(row, column)
            if item is not None:
                item.setBackground(brush)

    def _on_well_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != 0 or self.plate is None:
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is None:
            return
        well = self.plate.wells[int(index)]
        well.included = item.checkState() == Qt.CheckState.Checked
        self._mark_dirty()

    def _set_all_included(self, included: bool) -> None:
        if self.plate is None:
            return
        for well in self.plate.wells:
            well.included = included and well.valid
        self._populate_well_table()
        self._mark_dirty()

    def _keep_valid_only(self) -> None:
        """恢复成「所有有效孔参与、无效孔剔除」的初始状态。"""
        if self.plate is None:
            return
        for well in self.plate.wells:
            well.included = well.valid
        self._populate_well_table()
        self._mark_dirty()

    # ------------------------------------------------------------------ 结果展示

    def _clear_results(self) -> None:
        self.wide_view.clearSpans()
        self.wide_view.clear()
        self.wide_view.setRowCount(0)
        self.wide_view.setColumnCount(0)
        self.summary_table.clearContents()
        self.summary_table.setRowCount(0)
        self._show_warnings()

    def _populate_wide_view(self) -> None:
        """第 1 行基因（合并居中加粗）、第 2 行组名，其下是各复孔 RQ。"""
        wide = self.wide
        view = self.wide_view
        if wide is None:
            return
        view.clearSpans()
        view.clear()
        view.setColumnCount(len(wide.columns))
        view.setRowCount(self.WIDE_HEADER_ROWS + len(wide.rows))

        control = self.result.control_group if self.result else ""
        control_columns = {
            index for index, (_, group) in enumerate(wide.columns) if group == control
        }
        bold = QFont(self.font())
        bold.setBold(True)

        for column in range(len(wide.columns)):
            view.setItem(0, column, QTableWidgetItem(""))
        for start, end, gene in wide.merge_spans():
            span = end - start + 1
            if span > 1:
                view.setSpan(0, start, 1, span)
            item = QTableWidgetItem(gene)
            item.setFont(bold)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
            )
            view.setItem(0, start, item)

        for column, group in enumerate(wide.group_header):
            item = QTableWidgetItem(group)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
            )
            view.setItem(1, column, item)

        for offset, values in enumerate(wide.rows):
            row = self.WIDE_HEADER_ROWS + offset
            for column, value in enumerate(values):
                item = QTableWidgetItem("" if value is None else f"{value:.4f}")
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                view.setItem(row, column, item)

        # 从组名行开始上色：基因行是跨列合并的，染色会把整个基因块都带上
        brush = QBrush(COLOR_CONTROL)
        for column in control_columns:
            for row in range(1, view.rowCount()):
                item = view.item(row, column)
                if item is not None:
                    item.setBackground(brush)

        view.resizeColumnsToContents()
        self.wide_hint.setText(
            f"第 1 行是基因、第 2 行是分组，下面每行是一个复孔的 RQ；"
            f"淡蓝列为对照组「{control}」。共 {len(wide.columns)} 列 × {len(wide.rows)} 行。"
        )

    def _populate_summary_table(self) -> None:
        result = self.result
        table = self.summary_table
        table.clearContents()
        if result is None:
            table.setRowCount(0)
            return
        pairs = [(target, group) for target in result.targets for group in result.groups]
        table.setRowCount(len(pairs))
        for row, (target, group) in enumerate(pairs):
            stat = result.stat(target, group)
            texts = (
                target,
                group,
                str(stat.n) if stat else "0",
                f"{stat.mean:.4f}" if stat and stat.mean is not None else "-",
                f"{stat.sd:.4f}" if stat and stat.sd is not None else "-",
            )
            for column, text in enumerate(texts):
                item = QTableWidgetItem(text)
                if column >= 2:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
                    )
                table.setItem(row, column, item)

    def _show_warnings(self) -> None:
        messages: list[str] = []
        if self.plate is not None:
            messages.extend(self.plate.warnings)
        if self.result is not None:
            messages.extend(self.result.warnings)
        if messages:
            self.warning_view.setStyleSheet("color: #A6510A;")
            self.warning_view.setPlainText("\n".join(f"· {text}" for text in messages))
        else:
            self.warning_view.setStyleSheet("color: #1B8A4B;")
            self.warning_view.setPlainText("无异常")

    def collect_alerts(self) -> list[str]:
        """需要顶到摘要行旁边的高危提示：拆分歧义、方向不一致、降级、整只动物出局。

        普通说明（如「没有发现需要拆分的生物学重复」）不算，免得警告条天天亮着。
        """
        alerts: list[str] = []
        if self.split_report is not None:
            alerts.extend(
                text for text in self.split_report.warnings if text != NO_SPLIT_NEEDED
            )
        if self.result is not None:
            alerts.extend(
                text for text in self.result.warnings
                if text.startswith((MISSING_REFERENCE_PREFIX, SPLIT_DROPOUT_PREFIX))
            )
        return alerts

    def _update_alerts(self) -> None:
        alerts = self.collect_alerts()
        self.alert_banner.setVisible(bool(alerts))
        if alerts:
            head = f"⚠ 需要注意（{len(alerts)} 条）"
            self.alert_banner.setText(
                head + "\n" + "\n".join(f"· {text}" for text in alerts)
            )
        else:
            self.alert_banner.clear()

    def _mark_dirty(self) -> None:
        """配置变了但没重算时提醒用户；不自动触发计算，避免中间态报错。"""
        if self.result is not None:
            self.statusBar().showMessage(
                "参数已修改，下方结果仍是上一次的，请重新点「开始计算」", 6000
            )

    def _update_actions(self) -> None:
        loaded = self.plate is not None
        computed = self.wide is not None
        split_on = self.split_check.isChecked()
        self.run_info_btn.setEnabled(loaded and bool(self.plate.run_info))
        self.calc_btn.setEnabled(loaded)
        for widget in (
            self.split_check, self.move_up_btn, self.move_down_btn, self.regroup_btn,
            self.check_all_btn, self.check_none_btn, self.check_valid_btn, self.sort_combo,
        ):
            widget.setEnabled(loaded)
        # 拆分开启时分组按原始样本名走，「合并末尾编号」无从谈起，置灰并说明原因
        self.direction_combo.setEnabled(loaded and split_on)
        self.merge_check.setEnabled(loaded and not split_on)
        self.merge_check.setToolTip(MERGE_DISABLED_TOOLTIP if split_on else MERGE_TOOLTIP)
        for widget in (self.copy_all_btn, self.copy_values_btn, self.export_btn):
            widget.setEnabled(computed)

    # ------------------------------------------------------------------ 槽与异常

    def _guard(self, action: Callable[[], object]) -> bool:
        """统一异常出口：业务异常用 warning，未预期异常用 critical，都不让程序崩。"""
        try:
            action()
            return True
        except (ReaderError, AnalysisError, ExportError) as exc:
            QMessageBox.warning(self, "无法继续", str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，保证窗口不被异常带走
            QMessageBox.critical(
                self, "出现未预期的错误", f"{type(exc).__name__}：{exc}"
            )
        return False

    def _on_browse(self) -> None:
        start_dir = os.path.dirname(self.plate.file_path) if self.plate else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 qPCR 下机 Excel 文件", start_dir, EXCEL_FILTER
        )
        if path:
            self._guard(lambda: self.load_file(path))

    def _on_show_run_info(self) -> None:
        if self.plate is None or not self.plate.run_info:
            return
        RunInfoDialog(self.plate.run_info, self).exec()

    def _on_calculate(self) -> None:
        self._guard(self.run_analysis)

    def _on_export(self) -> None:
        if self.result is None:
            QMessageBox.warning(self, "无法继续", "还没有结果可导出，请先点「开始计算」。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出分析结果", self.default_export_path(), "Excel 文件 (*.xlsx)"
        )
        if not path:
            return
        if not self._guard(lambda: self.export_to(path)):
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("导出成功")
        box.setText(f"结果已导出到：\n{path}")
        open_btn = box.addButton("打开所在文件夹", QMessageBox.ButtonRole.ActionRole)
        box.addButton("关闭", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(os.path.abspath(path))))

    # ------------------------------------------------------------------ 拖放支持

    @staticmethod
    def _excel_path_from(event: QDropEvent | QDragEnterEvent) -> str | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            path = url.toLocalFile()
            if path and path.lower().endswith(EXCEL_SUFFIXES):
                return path
        return None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._excel_path_from(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        path = self._excel_path_from(event)
        if path is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self._guard(lambda: self.load_file(path))
