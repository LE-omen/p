# POWE-166 第五轮交付报告（契约投递核验 → 解析修复 → EXP 2×2 消融 → 再提炼）

模式：迅捷开发｜审查：仅开发者自测｜开发者：DS-敏捷开发者（`b2eb973c-dc9c-4012-a4f4-e17d4d224211`，经额度交接自 GLM-迅捷开发者接手）｜父任务：POWE-3

全部 180 run 在固定 12 题（H3/H4 已曝光诊断材料）上完成；**本轮无新盲测数据，不得作泛化结论**。评测模型 `deepseek-v4-flash-0731`、temperature 0、max_turns 8、420s、concurrency 3、current_date 2026-09-13，与第四轮同。

---

## 第 0 步 契约投递核验（结论：历史从未投递，本轮已修复并逐 run 断言）

**结论：`numeric-contract-v1.md` 在 H4（483 run）与 POWE-158 的 A1/诊断/R×3（231 payload）中从未进入任何单题 payload。**静态疑点（POWE-164：`r4eval.py` 计划顶层写 contract_path、`run_qa_r4.py` 单题 payload 未传递）经运行时证据确认。

| 证据 | 事实 |
|---|---|
| E1 driver | H4 实跑 `h4eval.py run_batch` → `run_qa_r4.py --batch`；runner sha256 `895076bc…`，与仓库 `integrations/datus/powe158/run_qa_r4.py` @ `cb111e43` 逐字节一致 |
| E2 plan | 计划 JSON 顶层确有 `contract_path`（contract sha256 `3b94b35a…`） |
| E3 payload | 483 个 H4 payload + 231 个 R4 payload，**含 `contract_path` 者：0 / 0** |
| E4 digest | 实跑 `effective_config.common_sha256` == sha256(common + ANSWER_PROTOCOL)，**不含 contract**；含 contract 的摘要 `7aee0729…` 全场不出现 |
| E5 prompt | 首请求 system prompt 长度 116321，无 G1 合同标记 |

**影响**：H4 历史成绩**不回填**（同实验内各臂一致地无合同，臂间对照仍内部有效，仅"投递"声明作废）；POWE-158 A1 的"合同文案本身不改变模型行为"结论作废（其 old/full 两臂机制上完全相同）；本轮 r5 runner 修复 D1 payload 透传，并逐 run 记录 `delivered_common_sha256`，评分前断言等于"含合同"摘要。

**本轮投递完整性**：180/180 run `contract_delivered=true`，`delivery_violations` 全空。抽样独立复算：A00/A10/A01/A11/A11v2 期望摘要分别 `823f798e…/a7d06714…/5c3bf6fc…/6b676f05…/60915348…`，与各 run 记录一致。

---

## A 解析/观测修复（零模型 fixture，核心 loop 不动）

**根因**：模型最终回复在合法 `{"sql":…}` 之前含集合字面量（如 `{5,8,9,16}`）。`datus.utils.json_utils.strip_json_str` 从首个 `{` 取到末个 `}`，`json_repair` 把 `{5,8,9,16}` 修成**列表** `[5,8,9,16]`，`llm_result2json(expected_type=dict)` 不拒绝列表 → `gen_sql` 的 dict 判定失败 → `result.sql` 为 null → 不写 SQLContext → 以 `No SQL context` 终止。此即状态栏 3 例"已求出正确子集却丢失 SQL 交接"的公共解析根因。

**修复**（`r5patch.py`，仅包装共享 JSON 工具，不改 datus 核心流程）：
- 仅在原解析未得到请求的 dict 时触发（对原本可用路径零行为改变）；
- 字符串感知的配平 `{...}` 扫描，优先取携带非空 `sql` 的显式对象，其次任意合法 dict；`{5,8,9,16}` 非法 JSON 被跳过；歧义仍算失败（不引入 oracle 知识、不按 qid 取卡、不裸取 SELECT）。

**零模型 fixture 电池 10/10 通过**（`fixture-battery.py`，真实 datus 运行时解析器，无模型/DB）：

| id | 要求 | baseline | patched |
|---|---|---|---|
| F1/F2/F3 真实 H4 前言集合+合法信封 | MUST_FIX | `type:list` | `dict_with_sql` |
| F4/F5/F10 无 sql/纯 prose/截断信封 | MUST_HOLD | 不变 | 不变 |
| F6/F7/F9 合法对象/围栏/双对象 | MUST_FIX/HOLD | — | `dict_with_sql` |
| F8 信封内 SQL 含花括号 | MUST_FIX | `dict_with_sql` | `dict_with_sql`（花括号保留） |

**monitor 字段失配修复**：H4 monitor 读 `workflow_action.action.args.sql`（恒 null / `sql_spans:[]`）。r5 monitor 改绑 `output.sql_query_final`（终 SQL）与 `result.sql_results`（实执行 span），并按 `exp10_admission.typology` 打失败分型。180 行观测中 `cast_in_final_sql` 仅 6 行为 null（均为 run 未产出终 SQL），其余为真实布尔。

**修正在诊断集上的可见效果**：strict_structure 族（V4-32/36/38/39）在**含解析修复的每一臂**（含基线 A00）均为 11/12；第四轮同族失分（8/12）中的解析性失分被消除。此为已曝光诊断材料，不作泛化声明。

---

## B EXP-10/11 2×2 消融（+ 反例驱动再提炼 v2）

底座 s3v2（a00 sha256 `1d848b16…`）；a11 与 POWE-158 冠军 s4.txt **逐字节一致**（`e4af9661…`，冠军延续）；EXP-11 段落跨臂逐字节相同。

| 臂 | EXP-10 | EXP-11 | C/36 | unknown | J₂ | J₄ | meanS |
|---|---|---|---|---|---|---|---|
| A00 s3v2 底座 | – | – | 17 | 3 | 9 | 14 | 3.03 |
| A10 +EXP-10 v1 | ✓ | – | **35** | 0 | 18 | 29 | 2.86 |
| A01 +EXP-11 | – | ✓ | 20 | 1 | 10 | 16 | 3.14 |
| A11 +EXP-10 v1 +EXP-11（冠军延续） | ✓ | ✓ | 33 | 1 | 16 | 32 | 2.66 |
| A11v2 +EXP-10 v2 +EXP-11（反例精炼） | v2 | ✓ | 34 | 1 | 17 | 31 | 2.57 |

按族正确数（/n）：

| 族 | A00 | A10 | A01 | A11 | A11v2 |
|---|---|---|---|---|---|
| precision (15) | 1 | **15** | 3 | 14 | **15** |
| null_semantics (3) | 0 | 3 | 0 | 2 | 3 |
| state_lookup (3) | 3 | 3 | 3 | 3 | 2 |
| strict_structure (12) | 11 | 11 | 11 | 11 | 11 |
| weighted_semantics (3) | 2 | 3 | 3 | 3 | 3 |

**配对正确率差（rep 匹配，×12 题×3 rep）**：

- **EXP-10 主效应（precision）**：A10−A00 = **+0.933** CI95[+0.790,+1.076]（W14/L0），A11−A01 = **+0.733** CI95[+0.480,+0.987]（W11/L0）——两条同背景通路一致显著为正。
- **EXP-10 非 precision（干扰探针）**：A10−A00 = +0.191 CI95[−0.028,+0.409]，A11−A01 = +0.095 CI95[−0.091,+0.282]——**均为正**，无一为负。
- **EXP-11 主效应**：precision A01−A00 = +0.133 CI 跨 0，A11−A10 = −0.067 CI 跨 0；全 12 题 +0.083 / −0.056，均跨 0 → EXP-11 无独立可辨贡献。
- **v2 精炼（A11v2−A11）**：precision +0.067 CI[−0.076,+0.210]，非 precision 0.000 CI[−0.191,+0.191]，全 12 题 +0.028 CI[−0.100,+0.155]。

**失败分型**：A00 16×precision_miss + 3×run_failed；A10 仅 1×semantic；A01 14×precision_miss；A11v2 1×run_failed + 1×semantic。分型由 `exp10_admission.typology`（多重集对齐的单元格比对，rel≤1e-3 判精度）给出。

---

## C 条件判定回传（预注册规则）

`interference_supported = false`（A10−A00 与 A11−A01 于非 precision 题的均值**均为正**，且无归因于 EXP-10 的 null-族 typed 失败；`typed_attributed_failures=[]`）。

> **C 路由建议：C（结构经验扩展）优先，B（条件投递）本轮不成立。** EXP-10 经验卡在非目标题上未观察到受控干扰证据，A 案卡片保持无条件投递；条件投递（B 案）缺少前置门禁证据，不启动。

同时 `exp10_benefit_retained = true`：EXP-10 在 precision 族的两条配对通路均显著为正 —— **"目标族完胜"可归因到 EXP-10 单卡**，而非 EXP-11 或整组 s4 文本。

---

## 判据预注册（读题前冻结）

`freeze/prereg-r5.json`（sha256 `dc60dd9c…`，冻结于 2026-09-14T15:02Z，任何模型 run 之前）：矩阵与臂摘要、运行参数与 runner digest、fixture 电池、J₂（correct 且 S_agent≤2）/J₄（探索：correct 且 S_agent≤4）/unknown 分型口径、配对单元（rep 匹配，3 rep 估稳定非 3 独立样本）、干扰/收益/更好结果判定规则、预算，及已知局限（12 题全为已曝光诊断材、A11 的 EXP-10 文案曾针对 V3-13/14 家族存在曝光偏置、各臂 common 长度不等、n=3 功效低）。无 post-hoc 改判。

---

## 更好结果与分支

预注册 `better_result_branch` 规则（A11v2 vs A11：全 12 题均值≥0 且 precision 均值>0 且非 precision 均值≥−0.5 且无 typed null 族回归）：

| 条件 | 值 | 通过 |
|---|---|---|
| all12 ≥ 0 | +0.028 | ✓ |
| precision > 0 | +0.067 | ✓ |
| 非 precision ≥ −0.5 | 0.000 | ✓ |
| 无 typed 回归 | [] | ✓ |

→ **better_result = true**，按常设授权推分支 `swift/powe-166-r5`（**不开 PR、不合并**）。

**诚实披露**：该"更好"是**边缘**的——两条差值 CI 均跨 0，v2 相对 v1 仅多 1 个正确、J₂ +1、meanS 略降；且**全 12 题正确数最高的臂其实是 A10（EXP-10 单卡，35/36，J₂=18）**，EXP-11 叠加后反而略降（A11 33、A11v2 34）。因此本轮最稳的结论是"EXP-10 有效、EXP-11 无独立贡献、v2≈v1"，分支承载的是冠军延续 + v2 精炼候选，是否采用由协调者/用户裁定。

---

## 成本（含失败 usage，无 fallback 缺口）

180 run / 637 请求 / 输入 **33.788M** / 输出 **2.548M** token；`requests_with_usage == requests_started == 637`，无缺最终 usage 的请求。预算 900 请求 / 45M 输入 / 3.5M 输出均在限内。

| 臂 | run | 请求 | 输入 | 输出 |
|---|---|---|---|---|
| A00 | 36 | 134 | 6.946M | 0.482M |
| A10 | 36 | 120 | 6.299M | 0.479M |
| A01 | 36 | 135 | 7.227M | 0.521M |
| A11 | 36 | 124 | 6.626M | 0.524M |
| A11v2 | 36 | 124 | 6.690M | 0.541M |

---

## 复现

```
# 0) 环境：评分用 powe-152/workdir/.scoring-venv/bin/python；实跑用 powe-152/workdir/datus-runtime/.venv/bin/python
# 1) 解析修复零模型 fixture（无模型/DB）
$RUNTIME_PY r5/harness/fixture_battery.py
# 2) 预注册（读题前）与矩阵实跑（rep-major）
$SV r5/harness/r5eval.py prereg
$SV r5/harness/r5eval.py matrix A00,A10,A01,A11,A11v2
# 3) 评分 / 观测 / 成本 / 判定
$SV r5/harness/r5eval.py score && $SV r5/harness/r5eval.py monitor \
  && $SV r5/harness/r5eval.py cost && $SV r5/harness/r5eval.py verdict
```
`r5eval.py run <arm> <rep>` 可单批续跑（幂等，已完成题跳过）；`run_batch` 对装配失败题自动重试并记账。

---

## 风险与未覆盖

- **无泛化结论**：12 题全为 H3/H4 已曝光诊断材料，第五轮不构成独立盲测；泛化需独立 H5（POWE-165 建议 48–60 题、陌生门禁 ≥12 独立组件，届时另行预注册）。
- **n=3 功效极低**：v2 与 EXP-11 的差值 CI 均跨 0，只能报"不可辨"，不能报"更优/更差"。
- **EXP-11 独立贡献未验**：其主效应在两条通路上均跨 0，且与 EXP-10 同场时略负；本轮不足以裁定保留或移除。
- **common 长度不等**：A00 最短、A11/A11v2 最长；若后续出现 EXP-10 差异，等长占位对照是预注册的后续动作，本轮不做 ad-hoc。
- **组件污染**：本轮四/五臂在已曝光题上对照，未做 H5 独立组件校验。
- **来源边界**：执行由本任务上一段 run（同一 harness/计划）完成，因运行中额度中断被交接；接管后所有聚合结果均从冻结 evidence 重算，并独立复核了投递摘要、fixture 电池与解析修复。574MB trace evidence 保留在工作区，未入库（仅提交摘要与报告）。

---

## 制品 digest

| 制品 | sha256（前 16） |
|---|---|
| a00/a10/a01/a11/a11v2.txt | `1d848b16…` / `1d01feff…` / `d838310e…` / `e4af9661…` / `f1b4f769…` |
| r5eval.py | `3112b1c5…` |
| run_qa_r5.py | `c2726756…` |
| r5patch.py | `d36fabd2…` |
| exp10_admission.py | `cf3cb433…` |
| prereg-r5.json | `dc60dd9c…` |
| r5-distill-manifest.json | `305d7a67…` |
| contract-delivery-audit.json | `8853a826…` |
| r5-score.json | `4860ccee…` |
| r5-monitor.json | `da058df1…` |
| r5-cost.json | `74f33cc7…` |
| r5-verdict.json | `de11dd50…` |
