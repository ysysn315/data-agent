"""两份评测报告对比 → Markdown 差异表（指标闭环的产出侧）。

    python -m evals.text2sql.compare_reports baseline.json after.json [--out diff.md]

典型用法：知识回流前跑一次基线 → 导入失败 case 并转正 → 再跑一次 → 对比出
「few-shot 增强使执行准确率 X% → Y%」的数字。输出总体对比、按题型标签分解、
case 级翻转列表（新增通过 / 新增失败），供 README / 面试材料直接引用。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(report_path: Path) -> dict:
    return json.loads(report_path.read_text(encoding="utf-8"))


def _pct(bucket: dict) -> str:
    return f"{bucket['accuracy'] * 100:.2f}%（{bucket['correct']}/{bucket['total']}）"


def _run_desc(report: dict) -> str:
    """把模型与消融配置压成一行，旧报告缺字段时保持兼容。"""
    meta = report.get("meta", {})
    parts = [f"model={meta.get('model', '?')}"]
    if "skill_enabled" in meta:
        parts.append(f"skill={'on' if meta['skill_enabled'] else 'off'}")
    if meta.get("schema_mode"):
        parts.append(f"schema={meta['schema_mode']}")
    if meta.get("run_name"):
        parts.append(f"run={meta['run_name']}")
    return ", ".join(parts)


def compare(baseline: dict, after: dict, base_name: str, after_name: str) -> str:
    """生成 Markdown 对比报告。"""
    lines: list[str] = []
    b_sum, a_sum = baseline.get("summary", {}), after.get("summary", {})

    lines.append("# Text-to-SQL 评测报告对比")
    lines.append("")
    lines.append(f"- 基线：`{base_name}`（{_run_desc(baseline)}）")
    lines.append(f"- 对比：`{after_name}`（{_run_desc(after)}）")
    lines.append("")

    delta = a_sum.get("accuracy", 0) - b_sum.get("accuracy", 0)
    sign = "+" if delta >= 0 else ""
    lines.append("## 总体")
    lines.append("")
    lines.append("| 指标 | 基线 | 对比 | 变化 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| 执行准确率 | {_pct(b_sum)} | {_pct(a_sum)} | {sign}{delta * 100:.2f}pp |")
    lines.append("")

    # 样本集一致性：case ID 集合不同时，总体准确率差不构成可比的"知识增强收益"，
    # 显式警示并单独列出新增/移除 case（不算进翻转）。
    b_ids = {c["id"] for c in baseline.get("cases", [])}
    a_ids = {c["id"] for c in after.get("cases", [])}
    only_base, only_after = sorted(b_ids - a_ids), sorted(a_ids - b_ids)
    if only_base or only_after:
        lines.append(
            f"> ⚠️ 两份报告样本集不一致（基线 {len(b_ids)} 例 / 对比 {len(a_ids)} 例），"
            "总体准确率差不可直接解读为知识增强收益。"
        )
        lines.append("")
        if only_after:
            lines.append(f"对比报告新增 case：{', '.join(only_after)}")
        if only_base:
            lines.append(f"对比报告移除 case：{', '.join(only_base)}")
        lines.append("")

    b_fp = baseline.get("meta", {}).get("dataset_fingerprint")
    a_fp = after.get("meta", {}).get("dataset_fingerprint")
    if b_fp and a_fp and b_fp != a_fp:
        lines.append("> ⚠️ 两份报告的数据集内容指纹不同，题面、golden SQL、标签或难度可能发生过变化。")
        lines.append("")

    # 按题型标签分解（对比两边并集；只在一侧出现的标签也列出）
    b_tags, a_tags = baseline.get("by_tag", {}), after.get("by_tag", {})
    lines.append("## 按题型标签")
    lines.append("")
    lines.append("| 标签 | 基线 | 对比 | 变化 |")
    lines.append("|---|---|---|---|")
    for tag in sorted(set(b_tags) | set(a_tags)):
        b, a = (
            b_tags.get(tag, {"accuracy": 0.0, "correct": 0, "total": 0}),
            a_tags.get(tag, {"accuracy": 0.0, "correct": 0, "total": 0}),
        )
        d = a["accuracy"] - b["accuracy"]
        lines.append(f"| {tag} | {_pct(b)} | {_pct(a)} | {'+' if d >= 0 else ''}{d * 100:.2f}pp |")
    lines.append("")

    # 难度是独立于 SQL 能力标签的第二统计轴，便于观察增强是否只改善简单题。
    b_levels, a_levels = baseline.get("by_difficulty", {}), after.get("by_difficulty", {})
    if b_levels or a_levels:
        level_names = {"easy": "简单", "medium": "中等", "hard": "困难"}
        lines.append("## 按难度")
        lines.append("")
        lines.append("| 难度 | 基线 | 对比 | 变化 |")
        lines.append("|---|---|---|---|")
        for level in ("easy", "medium", "hard"):
            if level not in b_levels and level not in a_levels:
                continue
            b = b_levels.get(level, {"accuracy": 0.0, "correct": 0, "total": 0})
            a = a_levels.get(level, {"accuracy": 0.0, "correct": 0, "total": 0})
            d = a["accuracy"] - b["accuracy"]
            lines.append(f"| {level_names[level]} | {_pct(b)} | {_pct(a)} | {'+' if d >= 0 else ''}{d * 100:.2f}pp |")
        lines.append("")

    # case 级翻转：只统计两份报告都有的 case（交集），新增/移除的已在上方单独列出
    b_cases = {c["id"]: c for c in baseline.get("cases", [])}
    a_cases = {c["id"]: c for c in after.get("cases", [])}
    common_ids = b_ids & a_ids
    newly_pass = [a_cases[i] for i in common_ids if not b_cases[i].get("correct") and a_cases[i].get("correct")]
    newly_fail = [a_cases[i] for i in common_ids if b_cases[i].get("correct") and not a_cases[i].get("correct")]

    lines.append("## case 级翻转")
    lines.append("")
    lines.append(f"新增通过 {len(newly_pass)} 例：")
    for c in newly_pass:
        lines.append(f"- ✓ {c['id']}：{c.get('question', '')}")
    lines.append("")
    lines.append(f"新增失败 {len(newly_fail)} 例：")
    for c in newly_fail:
        lines.append(f"- ✗ {c['id']}：{c.get('question', '')}")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="对比两份评测报告，输出 Markdown 差异表")
    parser.add_argument("baseline", type=Path, help="基线报告 JSON")
    parser.add_argument("after", type=Path, help="对比报告 JSON")
    parser.add_argument("--out", type=Path, default=None, help="输出文件（默认 stdout）")
    args = parser.parse_args()

    report = compare(_load(args.baseline), _load(args.after), args.baseline.name, args.after.name)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
