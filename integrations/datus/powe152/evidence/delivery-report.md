## POWE-152 第三轮交付报告（S1 契约+记账 → S2 消费对照 → S3 守卫经验）

- 代码 HEAD：`58ccb93f7423b51350246eb17343883e4f41f4e2`（分支 swift/powe-152-r3，基线 a4c177c4 = 评测 d88f2a4b + E2 32aa2abd + p2-additive 记录 a4c177c4）
- 冠军冻结：`"s3v2"` freeze_id POWE-3-R3-CHAMPION-1 @ 2026-09-13T21:03:26Z（D23×3 选择后、R/H2 前）

### S1 契约与投递记账（P0，无新增模型评测）
- 新增 `powercontext_datus/delivery_ledger.py`：版本化 bundle（schema_version/scope/artifact digest/通道/适用条件/来源）、每题投递账（各通道字节与估算 token、真实模型 usage、失败/回退、预算、误投递检测）、off 开关保持纯观察。
- 静态一致性+回放自测 20/20 通过：p2-additive 重建冻结摘要（skills 560a30eb…、common 08a87663…）、契约重放确定性、off=on 投递字节一致（20767B）、N 臂错配契约被正确标记误投递。
- 全部 306+ 次运行均带投递账（r3/ledgers/）：报告为 full_delivery（永不伪报召回），预算内，无误投递。

### S2 同内容消费对照（P1）
- 12 个 C4 T01–T08 派生类型化模板（枚举/正则字面量子集、标识符白名单、渲染前后双重校验）投影进 datus 0.4.0 原生 reference_template store；离线负例 13 例全部按预期（注入/引号/分号/枚举逃逸/注释/反斜杠/未知参数拒绝，注入被引号转义中和）。
- 原生工具真实被使用：23/22 次调用；消费期白名单强制（rep2）：14 次参数校验 11 过 3 拒（越域 target_col=Country、内部接线参数被拒后模型回退原生）。
- 同内容三臂（s2r10×1）：
  - p2 知识卡（冻结 runner）：C=8/10 J=8 unk=0 meanS=1.1
  - 安全 render+原生 execute（无强制）：C=9/10 J=0 unk=0 meanS=4.3 → 强制后 rep2：C=6/10 J=0 unk=2 meanS=4.5
  - 受控模板直调（无强制）：C=8/10 J=1 unk=0 meanS=3.8 → 强制后 rep2：C=7/10 J=0 unk=1 meanS=3.33
- 结论：直消机制安全可用但无正确率增益（8–9/10 vs 8/10），请求近乎翻倍（46/41 vs 21），J 崩塌（0–1 vs 8，多步拉取-渲染-执行是结构性成本）；按 DESIGN-7 只记录为效率/机制证据，不据此改判。

### S3 有守卫经验内容（P2）
- v1 六条结构级经验（无序对枚举/中位数-MAD/集中度指数/集合签名等价类/脏时间规范化/两跳组合），v2 增量三条（显示舍入纪律/并列打破/最终 SQL 绑定）；全部继承 C4 八条守卫 + 适用条件 + 回退条款；治理 manifest 冻结（v1 f2b12791…、v2 7f9c7354…）。
- D10 筛选：p2 10/10 J8、s3v1 10/10 J8、s3v2 10/10 J8 — 切片已饱和（D2 族全被 p2 覆盖），无增量也无伤害；目标效应只能在 H2 gate-13 上读。

### 测试轮
| 阶段 | p2 | s3v1 | s3v2 |
|---|---|---|---|
| D23 rep1 | C=20/23 J=17 unk=3 meanS=1.55 | C=22/23 J=17 unk=1 meanS=1.77 | C=23/23 J=17 unk=0 meanS=2.57 |
| D23 rep2 | C=20/23 J=19 unk=3 meanS=1.1 | C=23/23 J=18 unk=0 meanS=1.61 | C=22/23 J=17 unk=1 meanS=1.77 |
| D23 rep3 | C=23/23 J=18 unk=0 meanS=1.7 | C=22/23 J=17 unk=1 meanS=1.77 | C=23/23 J=18 unk=0 meanS=2.65 |
| R×3 | [19, 18, 18] meanJ 18.33 | | |

**H2×3（已曝光回归，非盲测）gate-13**：30/39 = 0.769，J 19（0.487）；p2 冻结 25/39=64.1%、J 18/46.2%；H0 71.8%。
配对差（per-39）：mean +1.154，95%CI [-0.588, 2.896]。
更好结果判定（预注册）：{"h2_gate13_beats_p2": true, "rx3_stage_line": true, "d23x3_c_no_regression": true, "push_branch": true}

**成本**：{"runs": 425, "requests": 1176, "input_tokens": 52013526, "output_tokens": 1395207}

### 基础设施披露
- DB 主机解析间歇 NXDOMAIN（aliyun 内部解析器按进程抖动）：计划内钉 IP（hostname+IP 逐计划留痕）；装配期失败（question_injected 前）重跑≤3 次并逐次记录；模型期失败按未知留分母。
- POWE-153 H3/D3 为 v3.0-draft、未冻结未放行：本轮零接触；H2 仅作已曝光回归。
- datus 核心 loop 零改动（扩展仅在工具/配置面：reference_template 工具挂载 + 消费期参数白名单包装）；冻结 runner 原样用于全部非 S2 臂。