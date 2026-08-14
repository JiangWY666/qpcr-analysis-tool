"""2^-ΔΔCt 相对定量计算。

计算口径：
  ref_mean(sample) = 内参基因在该 sample 内所有复孔 Cq 的平均
                     （多内参时先各自求均值再平均，等价于表达量的几何平均）
  ΔCt_i            = Cq_i − ref_mean(sample)          逐孔计算，不先平均目标基因
  ΔΔCt_i           = ΔCt_i − mean(对照组该基因的 ΔCt)
  RQ_i             = 2^(−ΔΔCt_i)
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .grouping import SampleGroup, sample_to_group_map
from .reader import WellRecord

# 技术复孔 Cq 标准差超过这个值就在界面里标黄提示
CQ_SD_WARN = 0.5

# 两条需要在界面上醒目展示的警告，界面靠这两个前缀把它们从普通警告里挑出来
MISSING_REFERENCE_PREFIX = "以下样本缺少完整的内参数据，已整体跳过："
SPLIT_DROPOUT_PREFIX = "注意：生物学重复配对模式下，内参孔失效会让整个生物学重复的所有基因一起退出计算。"


class AnalysisError(Exception):
    """参数不足以完成计算时抛出，消息直接展示给用户。"""


@dataclass
class WellResult:
    """单孔的完整计算链路，用于明细表与溯源。"""

    well: str
    target: str
    sample: str
    group: str
    cq: float
    ref_mean: float
    dct: float
    ddct: float
    rq: float


@dataclass
class GroupStat:
    """某个基因在某个组上的汇总。"""

    target: str
    group: str
    values: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float | None:
        return statistics.fmean(self.values) if self.values else None

    @property
    def sd(self) -> float | None:
        return statistics.stdev(self.values) if len(self.values) > 1 else None


@dataclass
class ReplicateQC:
    """复孔一致性检查：同一原始样本同一基因的 Cq 离散度。

    分桶用的是**原始样本名**，所以拆分成 CT-1/CT-2/CT-3 之后，这里看到的仍然是
    CT 下 GAPDH 的三个孔一起算离散度，而不是三桶各一个孔、标准差全空。
    """

    target: str
    sample: str  # 原始样本名（未拆分时就等于 sample 本身）
    n: int
    cq_mean: float
    cq_sd: float | None

    @property
    def flagged(self) -> bool:
        return self.cq_sd is not None and self.cq_sd > CQ_SD_WARN


@dataclass
class AnalysisResult:
    """一次计算的全部产出。"""

    targets: list[str]
    groups: list[str]
    control_group: str
    reference_targets: list[str]
    well_results: list[WellResult] = field(default_factory=list)
    stats: dict[tuple[str, str], GroupStat] = field(default_factory=dict)
    ref_means: dict[str, float] = field(default_factory=dict)
    qc: list[ReplicateQC] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    undetected_policy: str = "max_cycle"
    max_cycle: float = 40.0

    def stat(self, target: str, group: str) -> GroupStat | None:
        return self.stats.get((target, group))

    def values(self, target: str, group: str) -> list[float]:
        stat = self.stats.get((target, group))
        return list(stat.values) if stat else []


def compute_reference_means(
    wells: list[WellRecord], reference_targets: list[str]
) -> tuple[dict[str, float], list[str]]:
    """算出每个 sample 的内参基线 Cq，返回 (映射, 缺内参的 sample 列表)。"""
    per_sample: dict[str, dict[str, list[float]]] = {}
    all_samples: list[str] = []
    for well in wells:
        if well.sample not in per_sample:
            per_sample[well.sample] = {}
            all_samples.append(well.sample)
        if well.target in reference_targets and well.usable:
            per_sample[well.sample].setdefault(well.target, []).append(well.cq)

    ref_means: dict[str, float] = {}
    missing: list[str] = []
    for sample in all_samples:
        per_gene = per_sample[sample]
        gene_means = [statistics.fmean(v) for v in per_gene.values() if v]
        if len(gene_means) < len(reference_targets):
            missing.append(sample)
            continue
        ref_means[sample] = statistics.fmean(gene_means)
    return ref_means, missing


def compute_replicate_qc(wells: list[WellRecord]) -> list[ReplicateQC]:
    """按 (基因, 原始样本) 统计参与计算的孔的 Cq 离散度。

    分桶键取 ``original_sample``：未拆分时它等于 ``sample``，行为与从前完全一致；
    拆分后仍按拆分前的样本归拢，复孔一致性检查才不会退化成每桶一个孔、SD 全空。
    """
    buckets: dict[tuple[str, str], list[float]] = {}
    order: list[tuple[str, str]] = []
    for well in wells:
        if not well.usable:
            continue
        key = (well.target, well.original_sample or well.sample)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(well.cq)

    qc: list[ReplicateQC] = []
    for target, sample in order:
        values = buckets[(target, sample)]
        qc.append(
            ReplicateQC(
                target=target,
                sample=sample,
                n=len(values),
                cq_mean=statistics.fmean(values),
                cq_sd=statistics.stdev(values) if len(values) > 1 else None,
            )
        )
    return qc


def analyze(
    wells: list[WellRecord],
    groups: list[SampleGroup],
    reference_targets: list[str],
    control_group: str,
    undetected_policy: str = "max_cycle",
    max_cycle: float = 40.0,
) -> AnalysisResult:
    """执行 2^-ΔΔCt 计算。groups 的顺序即导出宽表的列顺序。"""
    if not reference_targets:
        raise AnalysisError("请先选择至少一个内参基因。")
    if not control_group:
        raise AnalysisError("请先选择对照组。")
    if not any(g.name == control_group for g in groups):
        raise AnalysisError(f"对照组「{control_group}」不在当前分组里。")

    group_of = sample_to_group_map(groups)
    group_names = [g.name for g in groups]
    warnings: list[str] = []

    ref_means, missing_ref = compute_reference_means(wells, reference_targets)
    if missing_ref:
        labels = _sample_labels(wells)
        warnings.append(
            MISSING_REFERENCE_PREFIX + "、".join(labels[s] for s in missing_ref)
        )
        # 拆分之后一个内参孔失效就带走整个生物学重复的所有基因，这个代价必须说清楚
        dropped = [
            w for w in wells
            if w.usable and w.sample in set(missing_ref) and w.target not in reference_targets
        ]
        if any(labels[s] != s for s in missing_ref):
            warnings.append(
                SPLIT_DROPOUT_PREFIX
                + f"本次共有 {len(dropped)} 个目标基因孔因此被排除。"
                "请检查上述重复的内参孔，或取消勾选「同名样本的复孔视为生物学重复」"
                "改用内参均值归一化。"
            )

    target_genes = [
        t for t in _ordered_targets(wells) if t not in reference_targets
    ]
    if not target_genes:
        raise AnalysisError("除内参外没有其它基因可以分析，请检查内参选择。")

    # 第一遍：逐孔 ΔCt
    dct_rows: list[tuple[WellRecord, str, float, float]] = []
    for well in wells:
        if not well.usable or well.target in reference_targets:
            continue
        group = group_of.get(well.sample)
        if group is None:
            continue  # 样本没被分进任何组
        ref_mean = ref_means.get(well.sample)
        if ref_mean is None:
            continue  # 缺内参，上面已警告
        dct_rows.append((well, group, ref_mean, well.cq - ref_mean))

    # 第二遍：每个基因在对照组的 ΔCt 均值作为基线
    baseline: dict[str, float] = {}
    for target in target_genes:
        control_dcts = [
            dct for well, group, _, dct in dct_rows
            if well.target == target and group == control_group
        ]
        if control_dcts:
            baseline[target] = statistics.fmean(control_dcts)
        else:
            warnings.append(
                f"基因「{target}」在对照组「{control_group}」里没有有效数据，已跳过。"
            )

    result = AnalysisResult(
        targets=[t for t in target_genes if t in baseline],
        groups=group_names,
        control_group=control_group,
        reference_targets=list(reference_targets),
        ref_means=ref_means,
        warnings=warnings,
        undetected_policy=undetected_policy,
        max_cycle=max_cycle,
    )
    if not result.targets:
        raise AnalysisError(
            f"对照组「{control_group}」里没有任何基因有有效数据，无法归一化。"
        )

    # 第三遍：ΔΔCt 与 2^-ΔΔCt
    for well, group, ref_mean, dct in dct_rows:
        if well.target not in baseline:
            continue
        ddct = dct - baseline[well.target]
        result.well_results.append(
            WellResult(
                well=well.well,
                target=well.target,
                sample=well.sample,
                group=group,
                cq=well.cq,
                ref_mean=ref_mean,
                dct=dct,
                ddct=ddct,
                rq=2.0 ** (-ddct),
            )
        )

    for item in result.well_results:
        key = (item.target, item.group)
        stat = result.stats.get(key)
        if stat is None:
            stat = GroupStat(target=item.target, group=item.group)
            result.stats[key] = stat
        stat.values.append(item.rq)

    result.qc = compute_replicate_qc(wells)
    empty = [
        f"{t} / {g}" for t in result.targets for g in group_names
        if not result.values(t, g)
    ]
    if empty:
        warnings.append("以下基因-分组组合没有数据，导出时会留空：" + "、".join(empty))

    return result


def _sample_labels(wells: list[WellRecord]) -> dict[str, str]:
    """样本名 -> 便于回溯的说法。虚拟样本写成「CT 的第 2 号生物学重复」，其余用原名。

    拆分后界面上只剩 CT-2 这种虚拟名，直接抛给用户会让人一头雾水，所以警告文案里
    统一换成能对回下机表的说法。
    """
    labels: dict[str, str] = {}
    for well in wells:
        if well.sample in labels:
            continue
        if well.replicate_index >= 1 and well.original_sample and well.original_sample != well.sample:
            labels[well.sample] = f"{well.original_sample} 的第 {well.replicate_index} 号生物学重复"
        else:
            labels[well.sample] = well.sample
    return labels


def _ordered_targets(wells: list[WellRecord]) -> list[str]:
    seen: dict[str, None] = {}
    for well in wells:
        seen.setdefault(well.target, None)
    return list(seen)
