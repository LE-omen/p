# POWE-158 第四轮交付报告（方案 A 主线：聚合精度经验自提炼 + 数值契约统一）

- 模式：迅捷开发（延续）｜审查方式：仅开发者自测｜当前开发者：GLM-迅捷开发者（858ad646）｜父任务：POWE-3
- 代码：分支 `swift/powe-158-r4`（基于第三轮冠军分支尖端 `b17aa3af`），提交见交付评论；datus 核心 agent loop 零改动（全部改动在实验 harness 与制品内容层）
- 预注册：`r4/freeze/prereg-r4.json`（sha256 758e7f24…，A1 前冻结）；诊断轮发射门 `diag-launch.json`（612afc73…）；冠军冻结 `champion-r4.json`（6bee4e06…，R×3 前）

## 第 0 步 canary（零模型 + 24 次因子实验）

**A0 引擎机制复现（零模型，`evidence/a0-canary.json`）**：OceanBase 5.7.25-v4.3.5.5，`div_precision_increment=4`。整数参数 `AVG` 只返回 4 位小数 DECIMAL；**末端 `ROUND(...,6)` 无法恢复精度**（尺度在 AVG 阶段已定）；`AVG(CAST(x AS DECIMAL(60,18)))`、`CAST(AVG(x) AS DECIMAL(30,10))`、`CAST(AVG(x) AS DECIMAL(20,10))`、`AVG(CAST(x AS DECIMAL(30,6)))` 均以 5e-7 匹配 V3-13/14 oracle。V3-13/14 固定 SQL 仅替换均值表达式的逐变体验证：bare/ROUND6 失败、四种 CAST 形式全部通过。

**A1 24 次因子实验（V3-13/14 × {N,S3V2} × {旧投递,完整契约} × 3）**：N-old 0/6、N-full 1/6、S3V2-old 0/6、S3V2-full 0/6。**预注册"契约单独修复"主判据未过**（verdict 冻结 `a1-verdict.json` 46ca0ac9…）：G1–G5 全文投递后模型仍发 `AVG(...)`/`ROUND(AVG(...),6)` 形式；唯一成功（N-full r2 V3-14）是模型自行探索出 CAST（s_agent=6，超步预算）。**因果方向结论**：修复必须以显式可执行规则（经验卡）到达模型——契约散文不够，这正好界定了自提炼交付物的必要性。B/C（适用性投递/时序描述符）仍以诊断轮后残余失败为条件。

## 精度经验自提炼（系统从失败 trace 提炼，非手写）

`distill.py` 处理 66 次运行（A1 24 + POWE-155 V3-13/14 42）：**51 次仅精度尺度失分、1 次提交回退（自动检出即 POWE-157 E3 的 C4 h31 V3-14 案例，丢失的有效修订 `AVG(CAST(m.day_dist AS DOUBLE))`）、7 unknown、7 correct**。失败形状（AVG(v)/ROUND(AVG(...),6)/AVG(Consumption)…）与成功修正形状（CAST(AVG(...) AS DECIMAL(30,10)) 等）全部从 trace 提取；修复形式仅保留 A0 引擎验证通过的集合。产出：

- **EXP-10 pre-aggregation-precision**：整数参数聚合先 CAST（内层 DECIMAL(60,18) 或外层 DECIMAL(30,10)），末端 ROUND 不能替代；NULL 守卫（CASE…END）保持；反过度 CAST 条款（COUNT/ID/文本不 CAST、尺度≥6）。
- **EXP-11 final-SQL revision consistency**：最终 SQL 必须是产出已验证值的最后执行语句（EXP-09 的精度强化）。
- 制品 `s4.txt` = s3v2.txt（冠军 common）+ EXP-10/11，sha256 e4af9661…；lineage manifest `s4-manifest.json`（逐 trace sha256、失配单元、修正形状来源）。

**反例测试全过（`evidence/ce-tests.json`，零模型）**：CE1 非时序迁移（ProductID=5 子集精确均值 4.3723849372…：bare/ROUND6=4.3724 失败、CAST 形式过）；CE2 NULL 边界（全 NULL CASE 的 AVG 带/不带 CAST 均 NULL，1000 行保留在 COUNT）；CE3a 外层尺度不足失败（DEC(30,4)→4.3724）；CE3b COUNT/DISTINCT/文本等值在 CAST 下不变。

## 曝光诊断轮（4 臂统一契约 × 12 道已曝光题 × 3 = 144 run）

| 臂 | C/36 | meanC | J-rate | temporal/12 | control/24 | meanS |
|---|---|---|---|---|---|---|
| **S4（新制品）** | **32** | **0.889** | **0.889** | **9** | 23 | 1.17 |
| P2 | 30 | 0.833 | 0.806 | 7 | 23 | 1.19 |
| H0 | 23 | 0.639 | 0.583 | 6 | 17 | 1.38 |
| N | 19 | 0.528 | 0.000 | 4 | 15 | 6.17 |

- **V3-13：S4 3/3，其余臂全部 0/3；V3-14：S4 3/3（P2 1/3，H0/N 0/3）**——S4 与 P2 同技能同契约，唯一差异即蒸馏经验卡：51 次精度失败族被精确修复，因果闭环。
- 配对差 S4 vs N：+1.083/题 CI95 [+0.166, +2.000]（下界>0）；vs P2 +0.167 [-0.683,+1.016]；vs H0 +0.750 [-0.371,+1.871]。
- 残余失败披露：S4 失误为语义性（V3-45×2 插值加权、V3-35 r3 协方差口径）+1 次 run_failed（V3-44 r3，留分母）；**未观察到过度 CAST 副作用**（正确运行同样含 CAST）。V3-45 S4 1/3 vs P2 3/3 为 n=3 内波动，不排除干扰，待后续轮监测。

## 冠军冻结 + 旧 R×3 回归

- 冠军 `POWE-3-R4-CHAMPION-1` = S4（选择规则：meanC 最高；R×3 前冻结）。
- 旧 R×3（23 题×3）：C [19,19,19]，**J [18,18,19] meanJ 18.33 ≥ 14 阶段线 ✓**，unknown=0，meanS≈1.16。与第三轮冠军 s3v2 的 R×3 J 18.33 持平、C 19.0 略升（s3v2 [19,18,18]）。

## 更好结果判定（预注册三条件全部成立）

1. S4 诊断轮 meanC 全场第一（0.889）✓；2. S4 V3-13、V3-14 各 ≥2/3（均 3/3）✓；3. 冠军 R×3 meanJ ≥14（18.33）✓。
**判定 TRUE → 按常设授权推分支 `swift/powe-158-r4`（无 PR、不合并）。**

## 成本纪律（失败 usage 全量入账）

237 run / 681 请求 / 输入 18,433,099 tok / 输出 1,272,440 tok。逐 trace 取末次累计 token_usage（含失败/unknown/run_failed 运行，POWE-157 发现的漏记缺口已闭合；fallback 计数 0）。

## 复现命令

```bash
# 环境：scoring venv = powe-152 workdir .scoring-venv；runtime venv = powe-152 workdir datus-runtime/.venv
# 密钥：OPENAI_API_KEY/OPENAI_BASE_URL 来自 /home/rongfeng.frf/workspace/.env；DB_PASSWORD 只读凭据（POWE-148 评论 01a09a71）
python <scoring-venv>/bin/python r4/harness/canary_a0.py     # A0 零模型引擎 canary
python <scoring-venv>/bin/python r4/harness/r4eval.py a1run && ... a1score     # A1 24 次
python r4/harness/distill.py                                 # 自提炼 → s4.txt + manifest
python <runtime-venv>/bin/python r4/harness/canary_ce.py     # 反例测试（零模型）
python <scoring-venv>/bin/python r4/harness/r4eval.py diagrun && ... diagscore # 诊断轮 144 次
python <scoring-venv>/bin/python r4/harness/r4eval.py rrun S4 && ... rscore S4 # 旧 R×3
python <scoring-venv>/bin/python r4/harness/r4eval.py cost   # 成本（含失败 usage）
```

仓库内对应文件在 `integrations/datus/powe158/`（harness/冻结件/证据摘要；原始 trace 留工作区 evidence/）。

## 风险与未覆盖项

- V3-45（时间加权积分）S4 回退至 1/3：语义性（加权边界），n=3 不能定位是否卡内容干扰，已列为下轮监测点。
- 诊断轮为已曝光集（H3 已解盲），只证因果与无回归，不声明泛化；**H4 盲测待数据线冻结后另派**（主判据沿用父任务：H4 门禁 vs H0/p2 配对 CI 下界>0 且 J 不回退）。
- EXP-10 外层 CAST 尺度下界证据：DEC(30,10) 验证通过、DEC(30,4) 失败；6–10 位之间的边界未逐位验证（卡取保守值 ≥6）。
- N 臂在统一契约下 J 崩塌（0.000，meanS 6.17）：契约文本对无经验臂可能诱发探索，仅记录，不影响 S4 判定。
