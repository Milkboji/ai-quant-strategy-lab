# codex.md — AI Quant Strategy Lab 项目记忆

> 给后续开发者 / AI 代理阅读。修改代码前先读本文件。

## 项目状态（2026-10-19，AI_QUANT_FULLSTACK_WORKSPACE_V1）

  本项目为**全新重建**，未复用任何旧代码、旧数据库或旧实验记录（不存在可复用的 TQQQ / 策略进化记录）。
- 技术栈：Python 3.10+ · Streamlit ≥1.46（顶部导航）· SQLite · pandas · plotly · pydantic。无 Docker / Redis / Next.js。
- 只在本地 `http://127.0.0.1:8501` 运行，不部署公网。

## 交易权限（最重要）

- `PAPER_ORDERS = 0`，`LIVE_ORDERS = 0`。
- 双重锁：`aiquant/config.py: PAPER_ORDER_SUBMISSION_UNLOCKED = False` **且** `config/paper_authorization.json` 存在，才可能放行 Paper 下单。开源版本默认 False。
- `aiquant/data/http.py` 是唯一的外部 HTTP 出口：主机白名单（data.alpaca.markets / paper-api.alpaca.markets / api.deepseek.com），
  拒绝 `api.alpaca.markets`（Live），写请求只允许 DeepSeek `/chat/completions`。所有访问写入 `endpoint_audit` 表。
- `AlpacaPaperAdapter` 的 submit/cancel/close 都会抛 `OrderPermissionLocked`。`paper.runner.start()` 拒绝 Alpaca 部署。
- **未经用户单独明确授权，不得修改上述锁。** 研究 API Key / 只读凭证不构成授权。

## 目录

```
app.py                     Streamlit 入口（st.navigation 顶部导航，6 个页面）
aiquant/config.py          路径、常量、密钥读取(.env)、交易锁
aiquant/db.py              SQLite schema（只追加）
aiquant/data/http.py       受控 HTTP（白名单 + 审计）
aiquant/data/alpaca.py     Alpaca crypto 5m bars（v1beta3 /crypto/us）、最新报价、Paper 只读
aiquant/data/market_data.py 缓存/覆盖区间/数据快照(SHA256)/质量报告/UTC 聚合/合成测试数据
aiquant/strategy/schema.py Strategy Schema（pydantic 白名单），describe() 固定格式字段
aiquant/strategy/parser.py 中文自然语言 → Schema 的确定性规则解析器（不猜测，未识别片段列出）
aiquant/strategy/templates.py 五个模板
aiquant/strategy/registry.py 草稿、冻结版本、谱系、diff、复制
aiquant/engine/            indicators / signals（无未来数据）/ execution（成本）/ risk / backtest（含买入持有基准）
aiquant/research.py        全局研究设置、训练/验证/测试分区、回测执行与持久化
aiquant/jobs.py            SQLite 持久化任务队列（去重/停止/重启恢复，最多 2 并发）
aiquant/ai/deepseek.py     DeepSeek 调用（原始请求/响应保存到 data/llm_raw/），MOCK 测试替身
aiquant/ai/optimizer.py    单策略分析、五策略横向分析、候选版本生成（只基于训练集）
aiquant/paper/brokers.py   AlpacaPaperAdapter（只读，写锁定）、MockBroker（测试）
aiquant/paper/runner.py    部署门禁、Paper Runner、订单台账、对账、重启恢复、风控停止、Mock 回放
aiquant/actions.py         UI 统一业务入口
aiquant/ui/                样式与 6 个页面
scripts/service.py         一键启动/停止/状态（start.bat / stop.bat / status.bat 调用）
scripts/seed_test_data.py  仅测试：写入合成行情（TEST_SYNTHETIC，is_test=1）
tests/                     pytest 单元/集成测试 + e2e_browser.py（Playwright）
```

## 关键研究语义

- 基础数据：Alpaca `crypto/us` 5 分钟 OHLCV，UTC。只缓存实际返回的 K 线，缺失不补造。
  `coverage::*` 设置记录“已请求过的区间”，用来区分“未下载”与“无成交”。
- 每次回测引用不可变数据快照（`datasets` 表 + `data/market_cache/snapshots/*.csv.gz`，加载时校验 SHA256）。
- 聚合：`resample(origin="epoch")` UTC 边界；K 线需 ≥90% 的 5m 子 K 线且时间已走完才算完整，不完整 K 线不参与指标/信号。
- 信号：主周期 K 线收盘后计算；成交在其后第一根 5m K 线开盘价；若 15 分钟内无可用 K 线则放弃信号。
- 辅助周期：`merge_asof(backward)`，只用已收盘的辅助 K 线。
- 止损/止盈/移动止损：5m 内检查；同根同时触发按先止损；跳空按开盘价；移动止损峰值只用此前 K 线。
- 成本：手续费 25bps/边、滑点 2bps/成交、点差代理 1bp 全价差（每笔付一半）。各扣一次。
- 窗口前另取 60 天数据仅用于指标预热。窗口结束仍持仓按最后收盘价平仓（计成本）。
- 买入持有基准：同窗口、同初始资金、同成本模型。

## 数据分区与防过拟合

- 首次启动时冻结 `data_split`：测试集 = 最近 90 天，验证集 = 其前 60 天，更早 = 训练集。
- 默认回测窗口“结束于训练集末尾”。研究回测若触及测试集会被拒绝；若强制允许，测试集记为已污染。
- 验证集：每条谱系最多 3 次（提交时 + 执行时双重检查）。
- 独立最终测试：测试集未被触及且 ≥90 天时，每个版本只能运行一次。
- DeepSeek 只读取训练集结果（单策略分析、横向分析、历史版本均过滤 split=训练集）。

## 测试

- `python -m pytest -q tests`（43 个，使用临时目录与 MOCK DeepSeek，不访问网络）
- 浏览器 E2E：以测试环境启动服务后 `python tests/e2e_browser.py`（29 项）。
- 测试数据全部 `is_test=1`，不进入正式研究；部署门禁拒绝测试数据结果。

## 已知限制 / 待办

- 云端开发环境无法访问 Alpaca / DeepSeek：真实行情下载、真实 DeepSeek 调用、Paper 只读账户检查需在用户 Windows 上首次验证。
- 点差使用冻结代理模型，未接入 Alpaca 历史逐笔报价。
- 实时 Paper Runner 目前只通过 Mock 回放驱动；Alpaca Paper 自动运行循环需在用户授权后再接线并测试。
- MockBroker 状态存于 settings 表（测试用途）。

## 2026-10-20 新增：实时看盘（新手版，默认首页）

- `aiquant/live/feed.py`：后台每秒拉取 Alpaca quotes/trades/1Min bars（只读）。秒级K线 = 报价中间价聚合；
  分钟级 = 1Min 成交K线本地聚合（1/3/5/15/30 分钟、1/4 小时），当前K线用秒级数据实时补全。
  `AIQUANT_FEED=synthetic` 仅用于离线测试（页面醒目标注"模拟行情"）。
- `aiquant/live/analysis.py`：EMA9/21/50、MACD、RSI、布林、ATR 的大白话解读与 -100~+100 综合分。
- `aiquant/live/sim.py`：本地虚拟账户（不发任何订单）。三套预设：趋势跟随 / 超跌反弹 / 突破追涨。
  实时：K线收盘判断信号，每秒检查止损/止盈；买用卖一价、卖用买一价 + 1bp 滑点 + 手续费。
  快速回放：同一规则逐根重放最近 1~10 天K线。表 sim_sessions / sim_trades / sim_equity。
- 实测（2026-10-20，真实 Alpaca 数据，最近 3 天）：1 分钟预设含 0.25% 手续费亏损 13%~41%，不含手续费也小幅亏损；
  15 分钟亏 1.5%~2.6%。页面新增"成本提醒"：K线平均波动 < 一买一卖成本时警告。
- 注意：device_commit_files 复用同一 stagedPath 可能写入旧缓存内容，部署时请用新的暂存路径并核对文件大小。

## 2026-10-20 新增：策略自动进化（aiquant/evolve.py，页面「策略进化」）

- 服务循环每 60 秒检查 `evolve.due()`（默认每 7 天，设置存 `evolve_config`），到期提交任务队列 `evolve`。
- 对每个运行中的模拟盘部署：参数变异两轮爬山（指标周期/阈值/止损止盈/移动止损/相邻K线周期，最多 120 个）+ 可选 DeepSeek 候选。
- 评分只用训练集（验证集开始前 365 天），4 季度分段：稳健分 = 均值 − 0.5×波动 − 0.25×|最大回撤|；成交不足淘汰；需高出当前 ≥1 分。
- 留出检验：前向新数据（split_label「前向新数据」，test_end 之后，按周期需 7~30 天）优先，否则验证集（谱系限额 3 次，共用）；
  候选须在留出数据上有成交、收益 ≥ 当前−0.5pt、回撤不深于当前 3pt。独立测试集不参与。
- 晋级：freeze(source=auto_evolve)，保存训练/留出回测（research.store_backtest），部署写 `pending_switch`；
  `evolve.apply_pending` 在空仓且无未完成订单时切换 version_id / signal_tf（service.tick 每轮调用）。
- 预设谱系的最新版本记在 settings `preset_current::<名称>`；portfolio.deploy_all 按谱系匹配部署，避免重复。
- 表：evolve_runs / evolve_results。测试：tests/test_evolve.py。
- 只读看板数据导出：scripts/export_dashboard.py 导出 JSON（账户、策略、订单、日志、进化记录）。
- 长期亏损自动暂停（evolve.health_check，服务循环每轮 + 每轮进化后调用）：当前版本训练集一年收益 ≤ pause_train_loss_pct（默认 -10%）
  或本版本上线以来模拟盘亏损 ≤ 额度×pause_live_loss_pct（默认 -10%）→ 状态 paused_entries（不买，持仓照常按规则卖），
  config.auto_pause 记录原因；进化换上训练集为正的新版本后自动恢复；用户手动恢复的同一版本记 auto_pause_override，不再自动暂停。

## 2026-10-21 稳定性修复（进程 02:19 内存耗尽退出）
- db._connect 改为每个数据库文件一个共享连接（所有访问都在全局锁内）；旧版按线程缓存连接，Streamlit 每次刷新换线程导致连接只增不减。
- market_data.load_master 按文件 mtime/size 复用解析结果，避免每分钟重复解析整份 CSV。
- UI st.cache_data 加 max_entries。
- scripts/service.py 新增守护进程 `watch`（start 时自动拉起）：每 60 秒检查，进程退出/连续 3 次无响应/内存超 2500 MB（AIQUANT_MEM_LIMIT_MB）
  自动重启，记录 logs/watchdog.log；stop 先删 watchdog.pid 让守护进程退出。
- 入场信号过期保护（runner.ENTRY_MAX_DELAY=15 分钟，与回测一致）：服务中断重启后不再追执行旧K线的入场信号；退出信号不受限。
  （2026-10-21 07:09 重启后 #4 曾按 04:00 收盘的旧信号买入，由此修复。）
- actions.init 写 logs/app.pid（真实进程号），守护进程据此监控内存。
- 2026-10-21 08:20 修复“页面打不开/报错”：07:08 重启后 07:15 又更新了代码，Streamlit 文件监视把部分模块热重载，
  缓存里的 Account 对象与新类不一致 → 模拟盘页报 PicklingError。改为 `.streamlit/config.toml` `fileWatcherType = "none"`
  （更新代码后必须 stop/start 重启才生效），`_acct` 只缓存内置类型。
- 2026-10-21 策略方向（由 Claude 决定）：训练年 BTC 买入持有 -32.7%，只做多时最有效的是「日线均线金叉 + 移动止盈」低频趋势跟随，
  弱势时空仓。进化新增变异：移动止损启动点、"盈利×1.5/回撤×0.75" 组合、大周期过滤（日线收盘 > EMA50/100）。
- 按实力分配资金（evolve.rebalance，每轮进化后自动执行）：可分配=权益×96%，暂停策略各保留 6%，
  运行策略权重=训练集收益/|回撤| 限制在 [0.25, 2]；只影响之后买入金额。
- 实时看盘的 1 分钟虚拟账户已停止（20 笔交易亏 400 USD，其中手续费 490 USD）。
- 2026-10-21 杠杆研究：选定 Freqtrade（freqtrade_lab/，离线回测启动器 ft_offline.py 用本地市场定义；用户所在地区访问 Binance 合约返回 restricted location，不做绕过）。
  结论：只做多 2 倍逐仓最佳（训练 +52.9%/回撤 8.7%，最近 90 天 +15.2%）；做空更差；hyperopt 参数样本外变差（过拟合），不采用。
- 2 倍杠杆模拟账户（aiquant/paper/leverage_shadow.py）：跟随部署 #1 的真实 Paper 成交，本地按逐仓规则记账
  （0.07% 费率、0.01%/8h 资金费、强平），服务循环每轮 update；表 lev_shadow_trades / lev_shadow_equity；模拟盘页“2 倍杠杆模拟账户”面板。
- 2026-10-21 多资产研究（data/etf_daily，26 只 ETF 2016-2026 日线，scripts/fetch_etf_daily.py）：挑选 2016-22 / 验证 2023-24 / 检验 2025-26。
  采用「QQQ 150 日均线 ±2% 择时 → QLD（2 倍）否则现金」：全期年化 +30.1%、回撤 -34%（持有 QLD +31.5% / -64%）；
  最终检验期牛市跑输持有（+34.6% vs +63%），价值在于熊市降回撤。实现 aiquant/paper/etf_trend.py（服务循环每轮 step，
  美东 9:35 后按前一日收盘信号下当日市价单，每天最多一次；订单表 etf_orders，独立于 BTC 台账），占账户 30%（evolve.plan_budgets 扣除）。
- 2026-10-21 前端：新增默认首页「总览」（aiquant/ui/page_home.py）：总资产/累计/今日盈亏、资金分布条、四张策略卡（BTC 组合、美股 ETF、杠杆模拟、进化）、
  资产曲线（服务循环每小时写 account_snapshots）、动态流、系统健康条。导航分组：常用 4 页 + “研究与设置”下拉。顶栏权限提示精简。
- 2026-10-21 冠军–挑战者（aiquant/challenger.py，表 evolve_challengers）：进化中“留出数据不足/无证据”的更优候选登记为挑战者，
  从登记时刻起用之后的真实行情与现役版本并行计算（同一引擎），服务循环每小时 evaluate_all；BTC 满 FORWARD_MIN_DAYS 且有成交、
  收益≥现役−0.5pt、回撤不深于 3pt → 冻结新版本并 schedule_switch 自动部署；更差 → 淘汰；现役被替换 → 作废。
  美股 ETF：每轮进化 etf_round 在 2016-2024 比较相邻均线/缓冲参数（Calmar 高 10% 以上）登记挑战者，前向 60 个交易日后决定是否改 etf_trend_config。
  etf_trend.step 每天把最近日线并入 data/etf_daily/*.csv。页面：策略进化页“挑战者”表、总览进化卡显示运行中挑战者数。
- 2026-10-21 快速进化：data/history/{BTCUSD,ETHUSD}_5m.csv.gz（scripts/fetch_crypto_history.py，2021 起，独立于主缓存）。
  挑战者登记后先做快速检验（challenger.fast_check_btc：训练窗口之前的 BTC 年度分段 + ETH 全部年度分段；ETF 用 SPY→SSO 三段 Calmar），
  通过 → 立即晋级部署；明显更差 → 立即淘汰；证据不足 → 前向模拟。前向满 1/3 时间且成交≥3：领先≥2pt 提前晋级，落后≥3pt 提前淘汰。
## 发布流程（2026-10-24 起的固定要求）

每轮优化完成、全部测试通过后：
1. 私有仓库 `Milkboji/ai-quant-lab`：提交并推送到 `main`（含 docs 文档）。
2. 公开仓库 `Milkboji/ai-quant-strategy-lab`：同步代码改动并推送；保持 `PAPER_ORDER_SUBMISSION_UNLOCKED = False`，
   不放入含本机路径 / 私有域名的文档（docs 下的报告、进度、使用说明）、`.env`、`config/secrets.local`、数据和备份。
3. 把改动文件同步到运行电脑的项目文件夹并更新 `BUILD_INFO.json`，由管理员在页面上「安全重启」生效。

## 美股链路（2026-10-29，见 docs/美股链路现状报告.md 的改动清单）

- 市场配置 `aiquant/market.py`：CRYPTO / US_EQUITY（年化 365/252、时长单位 小时/交易日、成本、交易日历）。
  交易日历优先 Alpaca `/v2/calendar`（settings.us_calendar，服务循环每周刷新），否则内置纽交所规则 + SPECIAL_CLOSURES。
  `Market.after(t, n)`：美股 = t 所在交易日之后 n 个完整交易日，第 n+1 个交易日开盘解除。
- 美股日线库 `aiquant/data/us_daily.py`：只写已收盘日线（美东日期 < 今天）；每日一次多品种请求更新全部 UNIVERSE；
  同日收盘价差 > 0.2% 视为复权口径变化 → 整只重下。`get_dataset()` 写 datasets 表（venue=alpaca_stocks，timeframe 1440，
  SHA256，交易日缺口报告）；快照列 ts=开盘、close_time=收盘（UTC）。
- 唯一回测配置入口 `research.bt_config(start, end, r, market)`：实验室 / 进化 / 挑战者 / 长周期体检都用它（成本按市场、节奏按市场）。
  `params_key` 含节奏规则哈希。数据分区按市场：`get_split(market)`（美股 settings.data_split_us：测试 90 天、验证 365 天），
  留出触及记录美股为 test_contaminated_us / final_test_us（验证集按谱系计数）。
- 主引擎日线：快照带 close_time → `signals.build_frame` 走日线（只允许 1440、无辅助周期）；成交 = 下一交易日开盘
  （MAX_FILL_DELAY_DAILY 6 天）。`run(..., df_signal=)` 支持「信号品种 ≠ 交易品种」（schema.signal_symbol，例 QQQ → QLD）。
  schema.signal_symbol 为空时不参与哈希（已有版本哈希不变）。解析器识别 UNIVERSE 代码、均线倍数（1.02 倍 / 2% 以上）。
- 交易节奏：`rhythm.current(market)` → rules（加密货币，小时）/ rules_us（美股，交易日：连亏 2 笔停 20 交易日，不用波动率定仓）。
  Gate(rh, market)、daily_context(df, rh, market) 同一份代码。ETF 回测 `engine/regime_bt.py` 与模拟盘 `etf_trend.step`
  共用 Gate（状态 settings.rhythm_state::etf）。ETF 长周期体检 `rhythm.etf_long_metrics`（2017 起逐年），不达标暂停新开仓。
- ETF 运行参数唯一写入口 `etf_versions.activate()`（手动部署 / 批准晋级 / 挑战者晋级），写审计，手动部署自动锁定。
  锁 `etf_version_lock`（until 为空 = 手动锁定）；上下线 `set_online`（有持仓 → pending_offline，空仓后下线）；
  资金比例 `set_budget_pct`（5~60%，审计）。BTC 可分配资金扣除 `etf_trend.capital_share(equity)` = max(设定比例, 持仓市值占比)。
- ETF 进化：`etf_round` 挑选期 2016-2024 → `etf_holdout`（前向 ≥60 交易日，否则验证集 2025-01-01 至冻结日，限 3 次，
  holdout_touches.split='etf_validation'）→ 通过即晋级 / 进待确认；都不可用 → 挑战者（SPY→SSO 快速检验 + 前向，1/3 时间可提前决策）。
- ETF 下单：被拒当天不重试（通知 order_failed）；部分成交次日补足（state.entry）；`etf_trend.set_why()` 结构化原因（WHY_CODES），
  策略总台 `console.etf_row / etf_explain`、模拟盘面板、首页卡片、每日摘要都读它。
- 策略位指针：进化 / 挑战者晋级只在 `registry.slot_points_to(slot, 被替换版本)` 时移动指针。
- 策略位里的美股版本（`research.version_market`）部署到通用美股日线执行器（2026-10-29 起，见最后一节）。

## 第六步后核对（2026-10-29，见 docs/第六步后核对报告.md）

- 美股唯一分区：`research.US_SPLITS` / `us_split()`（v1：训练 2017-01-01~2025-07-30，验证 2025-07-31~2026-06-22，
  测试 2026-06-23~2026-10-28），固定日期、带版本号（settings.us_split_version）；美股回测假设里记 split_version。
  ETF 进化 `challenger.etf_select()`（训练期）、`etf_holdout()`、快速检验、实验室 ETF 预览（只训练期）全部用它。
- 留出计次统一 `aiquant/holdout.py`：holdout_touches.lineage_key；`used(lineage)` / `record()`；BTC 谱系兼容旧记录（按 version 关联）。
  启动 `holdout.migrate()`：etf_validation 并入 validation（去重，报告在 settings.holdout_merge_report）、写 lineage_exposure 默认值、
  给旧分区登记的 ETF 挑战者加 split_version=0 / split_note。
- 已暴露谱系 `holdout.exposure(lineage)`（settings.lineage_exposure，默认 etf:QQQ->QLD，forward_from 2026-10-21）：
  验证检验不作晋级依据，留出改用 forward_from 之后的前向数据（≥60 交易日），界面显示「验证/测试数据已暴露」。
- 美股节奏影子模式：`rhythm.us_mode()`（默认 shadow，`ensure_us_shadow_default` 写审计）、`is_shadow`、`configured`（规则本身）
  vs `current`（实际执行，影子时美股为 None）、`set_us_mode`（管理员/交易员，审计）。live_gate / on_entry / on_exit 用 configured，
  etf_trend 在影子模式下不阻止、只写 settings.etf_rhythm_shadow。`etf_long_paused(include_shadow)`。
- 进化 / 挑战者晋级 `registry.freeze(..., set_current=False)`：只改运行版本；实验室 `registry.promoted_running` 提示，
  `registry.adopt_running`（管理员/交易员，审计「实验室切换到运行版本」）。
- 引擎回归：`tests/test_engine_regression.py` + `tests/golden/`（a72ed6a 生成）。改动 engine/* 或 regime_bt 后必须通过；
  若有意改变结果，重新生成 golden 并在报告里逐条解释差异。
- 美股空仓额度借用：etf_trend 配置 lend_pct（默认 50，`etf_versions.set_lend_pct`）；状态 settings.etf_lend（none/lent/reclaim）；
  `capital_share` 借出时只扣未借部分；收回时 `runner` 拦截 BTC 新开仓（reclaim_active），ETF 现金不够时不记当天已动作；
  每次状态切换写策略审计并 rebalance。

## 第六步后核对 · 用户决定（2026-10-29 16:41，见 docs/第六步后核对报告.md D 节）

- 美股长周期体检只提示：`rhythm.DEFAULTS["long_us"]="hint"`、`us_long_hint_only()`、`etf_long_hint()`、常量 `US_LONG_HINT_TEXT`；
  `etf_long_paused()` 在只提示时恒为 False；`_etf_long_refresh` 第一次不达标发 `long_hint` 通知（etf_long_state.hint）。
- 美股时长单位：`Rhythm.*_hours` 字段在美股按 `Market.duration_unit="trading_days"` 解释（`Market.after`）；连亏刹车 = 20 个交易日。
- 影子日志与转正：`rhythm.shadow_record(day, now)`（etf_trend.step 每个交易日调用一次）、`shadow_error(day, msg)`、
  `us_promotion_status()`（SHADOW_MIN_DAYS=20、自 settings.us_shadow_count_from 起、无异常）、`_maybe_review_notice`（`rhythm_review` 通知一次）；
  `set_us_mode("active")` 条件不满足时 ValueError，转正时 rules_us=RULES_US_V1、long_us=hint。
- 借出收回：`etf_trend.after_close(now)`（服务循环调用；收盘后 15 分钟起确认下一交易日信号，settings.etf_after_close 每个收盘日一次）；
  `_set_lend("reclaim")` 记 cuts{部署: before/after/frac/done} 与 lent_since；`pending_cut(did)` / `mark_cut_done` / `reclaim_pending()`；
  `PaperRunner._reclaim_cut` 卖出借用期间所开仓位的 frac；ETF 开盘后先等 cuts 完成（最晚美东 10:30，RECLAIM_DEADLINE_MIN），
  然后按可用现金买入，缩量时保持 reclaim，直到补足。
- 现金与杠杆：`aiquant/paper/cash_guard.py`：`reserved_cash`（未成交买单占用）、`check_buy`（≤ (现金−占用)×98%，不用 buying_power，
  缩量发 `cash_short`）、`exposure` / `exposure_check` / `maybe_exposure_check`（>100% 或现金为负发 `exposure_high`，settings.account_exposure）。
  BTC（runner）与 ETF（etf_trend 买入、补足）下单前都调用 check_buy；服务循环写 settings.btc_last_price 供估算 BTC 未成交买单。
- 通知事件新增：cash_short / exposure_high / long_hint / rhythm_review。

## 通用美股日线执行器（2026-10-29，见 docs/通用美股执行器报告.md）

- 部署：`paper_deployments` broker='alpaca_us'；config.kind = 'etf_sma'（原 ETF 择时迁移，参数仍读 etf_trend_config，
  version_id = etf_versions.id）或 'lab'（strategy_versions，trade_symbol = 最近一次美股日线回测的标的，signal_symbol）。
  BTC 专用的循环都按 broker 过滤（evolve.run / apply_all_pending / health_check、runner 的平仓 / 撤单 / 紧急停止对美股禁用）。
- `aiquant/paper/us_exec.py`：`step(now)`（etf_trend.step 转调；规则同原 ETF：9:35 后 DAY 市价单、每部署每天一次、被拒不重试、
  部分成交次日补足、per-deployment why：ETF 用 etf_trend_why，其他 us_why::{id}；状态 ETF 用 etf_trend_state，其他 us_state::{id}）；
  `after_close(now)`（收盘 15 分钟后：先 `us_daily.refresh(closed_through=当天)`，再确认信号 → 借出转收回、偏离 → rebalance_due）；
  `migrate_etf()` / `ensure_migrated()`（actions.init 调用，settings.us_exec_migration 为对照报告）；`deploy_lab` / `switch_version`；
  `explain(dep)`；节奏 Gate 键 ETF = 'etf'，其他 'us:{id}'；实验室版本的止损 / 止盈 / 移动止损用入场后已收盘日线检查，下一开盘卖出。
  券商调用经 etf_trend 的钩子：`_submit`（只提交，coid 由执行器给）、`_position`（404=空仓，其他错误带 error=True → 不下单）。
- `aiquant/paper/us_ledger.py`：us_orders / us_order_allocs / us_ledger。`plan_net`（同标的净额）、`create_order`、`allocate`
  （按金额 / 股数比例拆回，对冲部分 source='cross'，合计严格等于成交，幂等）、`position`（移动平均成本、已实现）、`reconcile`、
  `record_opening`（迁移期初）、`entry_filled_usd`（补足用，含期初）。
- `aiquant/allocation.py`：settings.capital_layers {us_equity: target/min/max, crypto: min/max, rebalance_pp}；target 为空时取
  etf_trend_config.budget_pct（`etf_versions.set_budget_pct` 同步写）。`layer1(equity)`（steps 为计算过程）、`plan_us`（同一实力公式：
  ETF 用 etf_long_metrics，实验室用 evolve._train_metrics；不取整）、`rebalance_us`（evolve.rebalance 末尾调用）、`explain`、
  `set_config`（管理员 / 交易员，审计「修改资金分层」）。`etf_trend.capital_share` = layer1.us_deduct_pct；evolve.plan_budgets 的
  加密货币总额 = layer1.crypto_pct（默认上下限时与原公式相同）。
- 策略总台：`console._with_us` 把实验室美股部署并入策略位行（market / trade_symbol / value 来自台账），`_deploy_us`，
  `is_flat` / `_ledger_qty` 按 broker 区分；页面「资金两层分配」面板（第一层步骤、第二层表格、上下限与阈值设置）。
- 脚本：`scripts/migration_check.py`（数据库副本上的迁移前后对照）、`scripts/replay_us_week.py`（共用账户的多日回放）、
  `scripts/etf_exec_regress.py`（旧 / 新 ETF 执行器逐日比对）。

