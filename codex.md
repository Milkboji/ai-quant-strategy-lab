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
