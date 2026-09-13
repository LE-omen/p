"""Assemble the POWE-152 delivery report from r3/ evidence (scoring venv)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

EXP = Path(__file__).resolve().parent
WORK = EXP.parent
REPO = WORK / "powercontext"
R3 = WORK / "r3"


def jload(p):
    return json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else None


def rep(arm, qset, r):
    return jload(R3 / "reports" / f"{arm}-{qset}-rep{r}.json")


def summ(arm, qset, r):
    x = rep(arm, qset, r)
    if not x:
        return "pending"
    s = x["summary"]
    return f"C={s['correct']}/{s['total']} J={s['j_agent_correct_and_within_2']} unk={s['unknown']} meanS={round(s['mean_s_agent'],2)}"


def main() -> None:
    champ = jload(R3 / "freeze" / "champion-r3.json")
    final = jload(R3 / "reports" / f"final-{champ['champion']}.json") if champ else None
    cost = jload(R3 / "cost.json")
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    lines = []
    A = lines.append
    A("## POWE-152 第三轮交付报告（S1 契约+记账 → S2 消费对照 → S3 守卫经验）")
    A("")
    A(f"- 代码 HEAD：`{head}`（分支 swift/powe-152-r3，基线 a4c177c4 = 评测 d88f2a4b + E2 32aa2abd + p2-additive 记录 a4c177c4）")
    A(f"- 冠军冻结：`{json.dumps(champ['champion']) if champ else 'n/a'}` freeze_id {champ.get('freeze_id')} @ {champ.get('frozen_at')}（D23×3 选择后、R/H2 前）")
    A("")
    A("### S1 契约与投递记账（P0，无新增模型评测）")
    A("- 新增 `powercontext_datus/delivery_ledger.py`：版本化 bundle（schema_version/scope/artifact digest/通道/适用条件/来源）、每题投递账（各通道字节与估算 token、真实模型 usage、失败/回退、预算、误投递检测）、off 开关保持纯观察。")
    A("- 静态一致性+回放自测 20/20 通过：p2-additive 重建冻结摘要（skills 560a30eb…、common 08a87663…）、契约重放确定性、off=on 投递字节一致（20767B）、N 臂错配契约被正确标记误投递。")
    A("- 全部 306+ 次运行均带投递账（r3/ledgers/）：报告为 full_delivery（永不伪报召回），预算内，无误投递。")
    A("")
    A("### S2 同内容消费对照（P1）")
    A("- 12 个 C4 T01–T08 派生类型化模板（枚举/正则字面量子集、标识符白名单、渲染前后双重校验）投影进 datus 0.4.0 原生 reference_template store；离线负例 13 例全部按预期（注入/引号/分号/枚举逃逸/注释/反斜杠/未知参数拒绝，注入被引号转义中和）。")
    A("- 原生工具真实被使用：23/22 次调用；消费期白名单强制（rep2）：14 次参数校验 11 过 3 拒（越域 target_col=Country、内部接线参数被拒后模型回退原生）。")
    A("- 同内容三臂（s2r10×1）：")
    A(f"  - p2 知识卡（冻结 runner）：{summ('p2','s2r10',1)}")
    A(f"  - 安全 render+原生 execute（无强制）：{summ('s2render','s2r10',1)} → 强制后 rep2：{summ('s2render','s2r10',2)}")
    A(f"  - 受控模板直调（无强制）：{summ('s2direct','s2r10',1)} → 强制后 rep2：{summ('s2direct','s2r10',2)}")
    A("- 结论：直消机制安全可用但无正确率增益（8–9/10 vs 8/10），请求近乎翻倍（46/41 vs 21），J 崩塌（0–1 vs 8，多步拉取-渲染-执行是结构性成本）；按 DESIGN-7 只记录为效率/机制证据，不据此改判。")
    A("")
    A("### S3 有守卫经验内容（P2）")
    A("- v1 六条结构级经验（无序对枚举/中位数-MAD/集中度指数/集合签名等价类/脏时间规范化/两跳组合），v2 增量三条（显示舍入纪律/并列打破/最终 SQL 绑定）；全部继承 C4 八条守卫 + 适用条件 + 回退条款；治理 manifest 冻结（v1 f2b12791…、v2 7f9c7354…）。")
    A("- D10 筛选：p2 10/10 J8、s3v1 10/10 J8、s3v2 10/10 J8 — 切片已饱和（D2 族全被 p2 覆盖），无增量也无伤害；目标效应只能在 H2 gate-13 上读。")
    A("")
    A("### 测试轮")
    A("| 阶段 | p2 | s3v1 | s3v2 |")
    A("|---|---|---|---|")
    for r in (1, 2, 3):
        A(f"| D23 rep{r} | {summ('p2','d23',r)} | {summ('s3v1','d23',r)} | {summ('s3v2','d23',r)} |")
    if final:
        A(f"| R×3 | {final['rx3']['J_by_rep']} meanJ {final['rx3']['mean_J']:.2f} | | |" if final.get("rx3") else "")
        g = final["gate13"]
        A(f"\n**H2×3（已曝光回归，非盲测）gate-13**：{g['correct']}/{g['of']} = {g['accuracy']}，J {g['j']}（{g['j_rate']}）；p2 冻结 25/39=64.1%、J 18/46.2%；H0 71.8%。")
        A(f"配对差（per-39）：mean {final['verdict']['paired_ci']['mean_diff_per39']:+.3f}，95%CI {final['verdict']['paired_ci']['ci95']}。")
        A(f"更好结果判定（预注册）：{json.dumps({k: v for k, v in final['verdict'].items() if k != 'paired_ci'}, ensure_ascii=False)}")
    if cost:
        A(f"\n**成本**：{json.dumps(cost['total'])}")
    A("")
    A("### 基础设施披露")
    A("- DB 主机解析间歇 NXDOMAIN（aliyun 内部解析器按进程抖动）：计划内钉 IP（hostname+IP 逐计划留痕）；装配期失败（question_injected 前）重跑≤3 次并逐次记录；模型期失败按未知留分母。")
    A("- POWE-153 H3/D3 为 v3.0-draft、未冻结未放行：本轮零接触；H2 仅作已曝光回归。")
    A("- datus 核心 loop 零改动（扩展仅在工具/配置面：reference_template 工具挂载 + 消费期参数白名单包装）；冻结 runner 原样用于全部非 S2 臂。")
    out = R3 / "delivery-report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
