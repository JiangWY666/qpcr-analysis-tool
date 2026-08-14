"""把共用一个样本名的生物学重复拆成独立的虚拟样本。

Bio-Rad CFX 这类下机表里，同一个样本名下的 N 个孔常常是 N 只不同的动物
（生物学重复），而不是同一份 cDNA 的技术复孔。若不加区分，下游会先把这 N 个
内参孔取平均再拿去减目标基因，个体之间的上样量差异就被重新混了回去。

做法是就地把 `CT` 拆成 `CT-1 / CT-2 / CT-3`：每个虚拟样本下每个基因只剩一个孔，
`analysis.compute_reference_means` 的「内参在样本内取平均」自动退化成「就是它
自己」，内参与目标基因天然 1:1 配对，下游计算一行都不用改——这是本模块设计的
关键性质，改动时务必保持。

配对规则是按上样顺序：各基因的第 j 个孔同属第 j 号生物学重复，
即 1 号重复 = A01(GAPDH) + A04(IL1b) + E01(IL6) + E04(TNFa)。
「上样顺序」由孔位排列方向决定：横着上样按列号排，竖着上样按行号排，
排成方块时无法判断，退回 `fill_direction` 指定的填板方向并给出警告。
虚拟样本名的 "-N" 后缀与 `grouping.strip_trailing_index` 的识别规则兼容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .grouping import SampleGroup, merge_keys
from .reader import PlateData, WellRecord

# 虚拟样本名的连接符：CT -> CT-1 / CT-2 / CT-3
REPLICATE_SEPARATOR = "-"

# 允许的填板方向：自动识别 / 先横后竖 / 先竖后横
FILL_DIRECTIONS = ("auto", "row", "column")

# 没有任何可拆的重复时给出的说明原文。界面据此把它当普通提示而非警告展示。
NO_SPLIT_NEEDED = "没有发现需要拆分的生物学重复：每个样本的每个基因都只有 1 个孔。"

# 方向代号 -> 中文说明，界面、导出和配对预览共用同一套措辞
ORIENTATION_LABELS = {
    "single": "单孔",
    "horizontal": "横向排列",
    "vertical": "纵向排列",
    "grid": "方向有歧义（跨行跨列）",
    "unknown": "孔位无法解析",
}

# 孔位字符串：1~2 个字母的行号 + 1~3 位数字的列号，允许中间有空格
_WELL_PATTERN = re.compile(r"^([A-Za-z]{1,2})\s*(\d{1,3})$")


@dataclass
class SplitReport:
    """拆分结果，用于界面提示和 QC 记录。"""

    enabled: bool = False  # 本次是否真的执行了拆分（至少有一个样本被改名）
    split_samples: list[str] = field(default_factory=list)  # 被拆开的原始样本名，按出现顺序
    replicate_counts: dict[str, int] = field(default_factory=dict)  # 原始样本名 -> 生物学重复个数
    skipped: list[str] = field(default_factory=list)  # 孔数不一致等原因没能拆的原始样本名
    warnings: list[str] = field(default_factory=list)  # 给用户看的中文提示
    orientations: dict[tuple[str, str], str] = field(default_factory=dict)  # (原始样本名, 基因) -> 方向
    ambiguous: list[str] = field(default_factory=list)  # 存在 grid 区块、方向有歧义的原始样本名
    fill_direction: str = "auto"  # 本次拆分采用的填板方向参数


@dataclass
class PairingPreview:
    """一个原始样本的配对情况，用于界面上让用户一眼核对。"""

    sample: str                                  # 原始样本名，如 "CT"
    targets: list[str]                           # 基因列顺序
    rows: list[tuple[int, dict[str, str]]]       # [(重复编号, {基因: 孔位})]
    orientation_note: str                        # 如 "横向排列" / "方向有歧义（跨行跨列）"


def parse_well(well: str) -> tuple[int, int] | None:
    """把孔位字符串解析成 (行下标, 列下标)，均为 0-based。

    支持 A1 / A01 / H12 / P24，以及 384 板的双字母行号 AA01。解析不了返回 None。
    """
    match = _WELL_PATTERN.match(str(well).strip())
    if match is None:
        return None
    row = 0
    for char in match.group(1).upper():
        row = row * 26 + (ord(char) - ord("A") + 1)  # 双字母按 A=1 的 26 进制累加
    column = int(match.group(2))
    if column < 1:
        return None
    return row - 1, column - 1


def detect_block_orientation(wells: list[WellRecord]) -> str:
    """判断一个 (样本, 基因) 区块的排列方向，返回下列之一：

    'single'      只有一个孔
    'horizontal'  所有孔同一行、列号不同（横着上样）
    'vertical'    所有孔同一列、行号不同（竖着上样）
    'grid'        跨行又跨列，方向有歧义
    'unknown'     有孔位解析不了
    """
    positions = [parse_well(well.well) for well in wells]
    if any(position is None for position in positions):
        return "unknown"
    if len(positions) <= 1:
        return "single"
    rows = {row for row, _ in positions}
    columns = {column for _, column in positions}
    if len(rows) == 1 and len(columns) > 1:
        return "horizontal"
    if len(columns) == 1 and len(rows) > 1:
        return "vertical"
    return "grid"


def _sample_key(well: WellRecord) -> str:
    """取孔所属的原始样本名。已拆过的孔靠 original_sample 回溯，拆分才能幂等。"""
    return well.original_sample or well.sample


def _bucket_wells(plate: PlateData) -> dict[str, dict[str, list[WellRecord]]]:
    """原始样本名 -> 基因 -> 该基因下的孔，两层都按首次出现顺序。

    这里只按 row_index（源文件行号）排一遍作为稳定基准，真正决定生物学重复编号的
    顺序由 `_order_block` 按孔位方向再排一次。Cq 无效的孔在板子上同样占了一个位置，
    必须一起参与排序和编号，跳过它们会让后面所有孔的重复编号整体错位。
    """
    buckets: dict[str, dict[str, list[WellRecord]]] = {}
    for well in plate.wells:
        name = _sample_key(well)
        if not name:
            continue
        buckets.setdefault(name, {}).setdefault(well.target, []).append(well)
    for per_target in buckets.values():
        for wells in per_target.values():
            wells.sort(key=lambda w: w.row_index)
    return buckets


def _order_block(
    wells: list[WellRecord], orientation: str, fill_direction: str
) -> list[WellRecord]:
    """按识别出的方向给一个区块内的孔排序，排序结果即 1、2、3 号生物学重复的顺序。

    一维区块用各自变化的那根轴排（横向按列号、纵向按行号），这与按文件行序排的
    结果一致；方块区块按 fill_direction 决定先横后竖还是先竖后横；单孔和解析不了
    的区块退回文件行序。sorted 是稳定排序，键相同的孔仍保持文件行序。
    """
    ordered = sorted(wells, key=lambda w: w.row_index)
    if orientation in ("single", "unknown"):
        return ordered

    pairs = [(well, parse_well(well.well)) for well in ordered]
    if any(position is None for _, position in pairs):
        return ordered  # 与 orientation 不一致时宁可退回文件行序，也不猜
    if orientation == "horizontal":
        pairs.sort(key=lambda pair: pair[1][1])
    elif orientation == "vertical":
        pairs.sort(key=lambda pair: pair[1][0])
    elif fill_direction == "column":
        pairs.sort(key=lambda pair: (pair[1][1], pair[1][0]))
    else:
        pairs.sort(key=lambda pair: pair[1])
    return [well for well, _ in pairs]


def _replicate_count(per_target: dict[str, list[WellRecord]]) -> int | None:
    """各基因孔数一致时返回该孔数，否则返回 None 表示无法判定。"""
    counts = {len(wells) for wells in per_target.values()}
    if len(counts) != 1:
        return None
    return counts.pop()


def _mismatch_warning(name: str, per_target: dict[str, list[WellRecord]]) -> str:
    detail = "、".join(f"{target} {len(wells)} 孔" for target, wells in per_target.items())
    return (
        f"样本「{name}」各基因孔数不一致（{detail}），无法按孔位一一配对生物学重复，"
        "已跳过拆分；该样本将退回按内参均值归一化。"
    )


def _inconsistent_warning(name: str, orientations: dict[str, str]) -> str:
    detail = "、".join(
        f"{target} {ORIENTATION_LABELS[kind]}"
        for target, kind in orientations.items()
        if kind in ("horizontal", "vertical")
    )
    return (
        f"⚠ 样本「{name}」不同基因的上样方向不一致（{detail}）。"
        "这种板子极易把不同动物的孔配成一对，请务必到「配对预览」页签逐行核对；"
        "若配对不对，改用「孔位排列方向」手动指定，或关闭生物学重复配对。"
    )


def _grid_warning(name: str, blocks: dict[str, list[WellRecord]]) -> str:
    detail = "；".join(
        f"{target} 占 {'、'.join(w.well for w in wells)}" for target, wells in blocks.items()
    )
    return (
        f"样本「{name}」的 {'、'.join(blocks)} 排成了方块，跨行又跨列（{detail}），"
        "无法判断上样方向；已按「先横后竖」（同一行从左到右，再换下一行）编号。"
        "若实际是竖着上样，请把「孔位排列方向」改成「纵向优先」后重新计算，"
        "并到「配对预览」页签核对。"
    )


def _unparsed_warning(name: str, blocks: dict[str, list[WellRecord]]) -> str:
    bad = [w.well for wells in blocks.values() for w in wells if parse_well(w.well) is None]
    samples = "、".join(f"「{well}」" for well in bad[:3])
    return (
        f"样本「{name}」的 {'、'.join(blocks)} 有孔位无法解析（如 {samples}），"
        "已退回按文件行序编号；若文件行序不等于上样顺序，请到「配对预览」页签核对配对。"
    )


def _reset_sample(per_target: dict[str, list[WellRecord]], name: str) -> None:
    """把一个样本的孔恢复成未拆分状态，同时撤销可能残留的旧拆分。"""
    for wells in per_target.values():
        for well in wells:
            well.original_sample = name
            well.sample = name
            well.replicate_index = 0


def detect_replicate_layout(plate: PlateData) -> dict[str, int]:
    """探测每个原始样本名下有几个生物学重复。无法判定的样本不出现在返回值里。

    判据是「该样本下每个基因的孔数是否一致」：一致就认为各基因的孔按顺序一一对应，
    孔数即生物学重复个数（返回 1 表示本来就只有一个，无需拆分）。只读，不改动 plate。
    """
    layout: dict[str, int] = {}
    for name, per_target in _bucket_wells(plate).items():
        count = _replicate_count(per_target)
        if count is not None:
            layout[name] = count
    return layout


def split_biological_replicates(
    plate: PlateData, fill_direction: str = "auto"
) -> SplitReport:
    """就地把同名样本拆成 '原名-1 / 原名-2 / ...'，并填好 replicate_index。

    编号顺序按孔位排列方向决定：横向区块按列号、纵向区块按行号。``fill_direction``
    取 "auto" / "row" / "column"，"auto" 表示一维区块各自按方向排，遇到方块区块退回
    先横后竖并给出警告；显式指定 "row" / "column" 时方块区块按该方向排且不再警告，
    视为用户已确认。

    只有各基因孔数一致且 ≥2 的样本会被改名；孔数为 1 的样本只编号不改名；孔数
    不一致的样本原样保留并记进 skipped。函数幂等：对已拆过的 plate 再调用一次，
    得到的仍是同一批虚拟样本名，不会拆成 CT-1-1。
    """
    if fill_direction not in FILL_DIRECTIONS:
        raise ValueError(
            f"fill_direction 只能是 {'、'.join(FILL_DIRECTIONS)} 之一，收到 {fill_direction!r}。"
        )

    report = SplitReport(fill_direction=fill_direction)
    for name, per_target in _bucket_wells(plate).items():
        orientations = {
            target: detect_block_orientation(wells) for target, wells in per_target.items()
        }
        for target, kind in orientations.items():
            report.orientations[(name, target)] = kind
        if "grid" in orientations.values():
            report.ambiguous.append(name)

        count = _replicate_count(per_target)
        if count is None:
            report.skipped.append(name)
            report.warnings.append(_mismatch_warning(name, per_target))
            _reset_sample(per_target, name)
            continue

        report.replicate_counts[name] = count
        report.warnings.extend(
            _orientation_warnings(name, per_target, orientations, fill_direction)
        )
        for target, wells in per_target.items():
            ordered = _order_block(wells, orientations[target], fill_direction)
            for index, well in enumerate(ordered, start=1):
                well.original_sample = name
                well.replicate_index = index
                well.sample = (
                    f"{name}{REPLICATE_SEPARATOR}{index}" if count > 1 else name
                )
        if count > 1:
            report.split_samples.append(name)

    report.enabled = bool(report.split_samples)
    if not report.split_samples and not report.skipped:
        report.warnings.append(NO_SPLIT_NEEDED)
    return report


def _orientation_warnings(
    name: str,
    per_target: dict[str, list[WellRecord]],
    orientations: dict[str, str],
    fill_direction: str,
) -> list[str]:
    """一个样本的方向类警告，按严重程度从高到低排列。"""
    warnings: list[str] = []
    kinds = set(orientations.values())
    if {"horizontal", "vertical"} <= kinds:
        warnings.append(_inconsistent_warning(name, orientations))
    if "grid" in kinds and fill_direction == "auto":
        warnings.append(
            _grid_warning(name, {t: w for t, w in per_target.items() if orientations[t] == "grid"})
        )
    if "unknown" in kinds:
        warnings.append(
            _unparsed_warning(
                name, {t: w for t, w in per_target.items() if orientations[t] == "unknown"}
            )
        )
    return warnings


def build_pairing_preview(plate: PlateData) -> list[PairingPreview]:
    """拆分后调用，生成每个原始样本的配对预览。未拆分时也要能返回合理内容。

    已编号的样本每个重复一行，单元格是该基因归到这号重复的孔位；没编号的样本
    （未开启拆分，或因孔数不一致被跳过）合并成编号 0 的一行，把该基因的所有孔
    列出来，表示这些孔会被一起平均。
    """
    previews: list[PairingPreview] = []
    for name, per_target in _bucket_wells(plate).items():
        orientations = {
            target: detect_block_orientation(wells) for target, wells in per_target.items()
        }
        numbered = all(
            well.replicate_index >= 1
            for wells in per_target.values()
            for well in wells
        )
        rows: list[tuple[int, dict[str, str]]] = []
        if numbered:
            indices = sorted(
                {w.replicate_index for wells in per_target.values() for w in wells}
            )
            for index in indices:
                cells = {
                    target: "、".join(w.well for w in wells if w.replicate_index == index)
                    for target, wells in per_target.items()
                    if any(w.replicate_index == index for w in wells)
                }
                rows.append((index, cells))
        else:
            rows.append(
                (
                    0,
                    {
                        target: "、".join(w.well for w in wells)
                        for target, wells in per_target.items()
                    },
                )
            )
        previews.append(
            PairingPreview(
                sample=name,
                targets=list(per_target),
                rows=rows,
                orientation_note=_orientation_note(orientations, numbered, per_target),
            )
        )
    return previews


def _orientation_note(
    orientations: dict[str, str],
    numbered: bool,
    per_target: dict[str, list[WellRecord]],
) -> str:
    """把一个样本各基因的方向汇总成一句给人看的话。"""
    if not numbered:
        reason = (
            "各基因孔数不一致，未按重复配对"
            if _replicate_count(per_target) is None
            else "未拆分，同名复孔按技术复孔取平均"
        )
        return f"{_direction_label(orientations)}（{reason}）"
    return _direction_label(orientations)


def _direction_label(orientations: dict[str, str]) -> str:
    kinds = set(orientations.values())
    if "unknown" in kinds:
        return "孔位无法解析，按文件行序配对"
    if "grid" in kinds:
        return ORIENTATION_LABELS["grid"]
    directional = kinds - {"single"}
    if len(directional) > 1:
        return "不同基因的排列方向不一致，请重点核对"
    if directional:
        return ORIENTATION_LABELS[directional.pop()]
    return ORIENTATION_LABELS["single"]


def restore_original_samples(plate: PlateData) -> None:
    """撤销拆分，把 sample 还原成 original_sample，replicate_index 清零。"""
    for well in plate.wells:
        if well.original_sample:
            well.sample = well.original_sample
        well.replicate_index = 0


def group_by_original_sample(
    plate: PlateData, merge_trailing_numbers: bool = False
) -> list[SampleGroup]:
    """按拆分前的原始样本名成组，保证 CT-1/CT-2/CT-3 回到同一个 CT 组。

    组的顺序按原始样本名在文件里首次出现的顺序，组内虚拟样本名同理。
    未拆分的 plate 上调用同样成立，此时每个组恰好含一个样本。

    merge_trailing_numbers 打开时，再按 `grouping.merge_keys` 的规则把只差末尾编号
    的**原始**样本名并进同一组（PBS1 + PBS2 -> PBS）。这与拆分是两件正交的事：拆分
    处理「同名多孔」，合并处理「不同名但属于同一实验组」，下机表里两种情况会同时出现。
    """
    order: list[str] = []
    virtuals: dict[str, list[str]] = {}
    for well in plate.wells:
        name = _sample_key(well)
        if not name:
            continue
        members = virtuals.get(name)
        if members is None:
            members = virtuals[name] = []
            order.append(name)
        if well.sample not in members:
            members.append(well.sample)

    keys = merge_keys(order) if merge_trailing_numbers else {}
    groups: dict[str, SampleGroup] = {}
    ordered: list[SampleGroup] = []
    for name in order:
        key = keys.get(name, name)
        group = groups.get(key)
        if group is None:
            group = groups[key] = SampleGroup(name=key)
            ordered.append(group)
        for virtual in virtuals[name]:
            if virtual not in group.samples:
                group.samples.append(virtual)
    return ordered
