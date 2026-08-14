"""把 Sample 名聚类成实验分组。

两层概念要分清：
- sample：下机表里的原始样本名，内参归一化在这一层做。
- group ：画图时的实验组，由一个或多个 sample 组成，决定导出宽表的列。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 末尾的重复编号：LPS-1 / LPS_2 / LPS 3 / LPS#4 / LPS(5) / LPS-rep2 / LPS_S1
# rep / r / n / s 这类标记必须紧跟在分隔符后面（靠 lookbehind 保证），
# 否则 "LPS-1" 会被当成 "LP" + "S-1"，把基名最后一个字母一起吃掉。
TRAILING_INDEX = re.compile(
    r"[\s_\-#.]*"
    r"(?:(?<=[\s_\-#.])(?:rep|repeat|r|no|n|sample|s))?"
    r"[\s_\-#.]*[(（\[]?\d+[)）\]]?$",
    re.IGNORECASE,
)


@dataclass
class SampleGroup:
    """一个实验组。"""

    name: str
    samples: list[str] = field(default_factory=list)

    def copy(self) -> "SampleGroup":
        return SampleGroup(name=self.name, samples=list(self.samples))


def strip_trailing_index(name: str) -> str:
    """去掉样本名末尾的复孔/重复编号；结果为空则保留原名。"""
    stripped = TRAILING_INDEX.sub("", name).strip(" _-#.")
    return stripped or name


def merge_keys(samples: list[str]) -> dict[str, str]:
    """两趟决定每个样本用哪个组名，避免把带数字后缀的完整样本名误剥。

    第一趟统计每个剥离键收拢了几个**不同**样本；第二趟只在收拢到 2 个以上时才采用
    剥离后的名字。这样 LPS-1/LPS-2/LPS-3 照常并成 LPS，而 "LPS BALF CIT013" 剥完
    只有它自己，就原样保留，不会静默变成 "LPS BALF CIT"。
    """
    collected: dict[str, set[str]] = {}
    for sample in samples:
        if sample:
            collected.setdefault(strip_trailing_index(sample), set()).add(sample)
    return {
        sample: (stripped if len(collected[stripped]) > 1 else sample)
        for stripped, group in collected.items()
        for sample in group
    }


def auto_group(samples: list[str], merge_trailing_numbers: bool = False) -> list[SampleGroup]:
    """按出现顺序把样本聚成组。

    默认同名样本归为一组（下机表里复孔通常同名）。
    打开 merge_trailing_numbers 后，LPS-1 / LPS-2 / LPS-3 会并进同一个 LPS 组；
    但剥掉编号后并不能与别的样本合并的名字（如 "LPS BALF CIT013"）保持原样。
    """
    keys = merge_keys(samples) if merge_trailing_numbers else {}
    groups: dict[str, SampleGroup] = {}
    for sample in samples:
        if not sample:
            continue
        key = keys.get(sample, sample)
        group = groups.get(key)
        if group is None:
            group = SampleGroup(name=key)
            groups[key] = group
        if sample not in group.samples:
            group.samples.append(sample)
    return list(groups.values())


def sample_to_group_map(groups: list[SampleGroup]) -> dict[str, str]:
    """样本名 -> 组名。同一样本被分到多组时以最先出现的为准。"""
    mapping: dict[str, str] = {}
    for group in groups:
        for sample in group.samples:
            mapping.setdefault(sample, group.name)
    return mapping


def move_group(groups: list[SampleGroup], index: int, delta: int) -> int:
    """在列表里上下移动一个组，返回移动后的下标。"""
    target = index + delta
    if index < 0 or index >= len(groups) or target < 0 or target >= len(groups):
        return index
    groups[index], groups[target] = groups[target], groups[index]
    return target


def reassign_sample(groups: list[SampleGroup], sample: str, new_group_name: str) -> None:
    """把某个样本挪到指定组，并清掉因此变空的组。"""
    for group in groups:
        if sample in group.samples:
            group.samples.remove(sample)
    for group in groups:
        if group.name == new_group_name:
            group.samples.append(sample)
            break
    else:
        groups.append(SampleGroup(name=new_group_name, samples=[sample]))
    groups[:] = [g for g in groups if g.samples]
