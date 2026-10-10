# 系统结构

市场雷达是一个 Windows 本地 Python 程序，浏览器工作台使用原生 JavaScript、HTML 和 CSS。无远程服务、账号系统或前端构建框架。当前版本以 `version.py` 为准。

## 从入口到存档

`run.bat` → 创建/检查 `.venv` 和锁定依赖 → `dashboard.py` → 浏览器工作台。

页面更新交给 `update_research.py`：按选择执行当前研究、续采或历史复盘；行情采集使用 `run.py` → `market_radar.Radar.run`。AI 独立更新使用 `refresh_ai_review.refresh_review`，不会改写原正式发布文件。

| 维护问题 | 入口及职责 |
|---|---|
| 数据从哪里来 | `collector.py` 管任务、完整性、续采；`akshare_worker.py` 在子进程访问既有接口；`enrichment.py` 补已有资金、政策、新闻渠道 |
| 历史数据如何清洗 | `history.py` 规范日线、截断目标日、保存行业/个股历史 |
| 市场评分在哪里 | `decision.build` 计算原五维；`prepare_final_score` 补政策并形成唯一有效 `decision.market` |
| 板块判断在哪里 | `decision.build` 的行业规则；`opportunities.build` 的四类独立机会诊断 |
| 个股研究在哪里 | `market_radar.py` 选择研究候选；`decision.build` 生成计划；`participation.snapshot` 检查原参与条件 |
| AI在哪里调用 | `decision.interpret` 编排；`daily_ai.py` 限定事实输入；`deepseek_analysis.analyze` 配置、预算、缓存与 HTTP |
| AI在哪里校验 | `decision.py` 的结构/价格/预测校验；`research_facts.py` 的事实契约；`deepseek_analysis.finalize_validation` 保存业务状态 |
| 报告在哪里生成 | `market_radar.write_market_report` 生成HTML及正式JSON；`reporting.py` 提供HTML基础；`dashboard.research_analysis` 读取正式存档及合格补充研究 |
| 前瞻记录在哪里 | `predictions.py` 保存 `data/predictions`、`data/forward-predictions`、`data/research-predictions`，资格与原输入一并冻结 |
| 复盘在哪里 | `predictions.evaluate` / `review`；`opportunities.review_conditions` 核对冻结的行业条件 |
| 配置在哪里 | `config.json`（模板 `config.example.json`）；Key在 `.env`（模板 `.env.example`），系统环境变量优先 |
| 故障看哪里 | `data/last_run.json`、`data/checkpoint.json`、AI缓存/失败记录；参见 [OPERATIONS](OPERATIONS.md) |

## 数据流与依赖边界

采集结果 → 行情/分类完整性门槛 → 统计与原规则 → 政策补评 → 最终有效市场评分 → 参与核验 → 有界 AI 输入 → 自动校验 → 报告及不可变预测记录 → 后续复盘。

`storage.py` 统一原子写入与本机互斥锁。采集失败保留上次正式报告；草稿、后补研究、模型失败、前瞻资格分别记录，不能从文件存在推断合格。`research_context.py` 保留事件日期、来源、正文、覆盖缺口与后补时点；`participation.py` 不以公告标题样本证明全部风险已排除。

界面由 `dashboard.js` 管导航、表格与交互，`dashboard-visuals.js` 管市场图示、五项评分和五档仪表，`dashboard-research.js` 管 AI 研究展示。页面消费现有报告，不在浏览时调用模型。颜色、文案和图示不是另一套投资决策。

## 文件边界

- 公共源码、模板、Prompt和测试由Git管理；公开发行清单在 `release.py`，自动核对本地模块导入。
- `data/`、`reports/`、`.env`、`config.json` 和 `backups/` 是本机私有资料，不进入公开发行包。
- `artifacts/` 保存本机验收样本、日志、截图和临时安装；`docs/archive/` 保存有复现或算法口径价值的历史材料。归档文档中的相对路径按原版本根目录理解。
- 不同指纹用途保留各自输入规范：普通JSON排序指纹、去技术元数据的语义指纹、事件身份、成员身份不能混用。源码签名用于使校验缓存失效，不等于业务事实变化。

修改任何规则或数据传递前，先看 [RESEARCH_RULES](RESEARCH_RULES.md) 与 [VALIDATION](VALIDATION.md)。
