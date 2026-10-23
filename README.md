# AI Quant Strategy Lab

一个在自己电脑上运行的中文量化研究与模拟交易工作台：用中文描述策略 → 严格分区回测 → AI 辅助优化 →
部署到 Alpaca Paper 模拟账户自动交易 → 策略自动进化（冠军–挑战者）。覆盖 BTC/USD 与美股 ETF。

> ⚠️ 本项目仅用于研究和学习，不构成任何投资建议。默认**不会提交任何订单**；开启后也只连接 Alpaca **Paper（模拟资金）**，
> 真实资金交易端点在代码里被永久禁止。历史回测与模拟盘表现都不代表未来收益。

[English summary](#english-summary)

## 功能

| 模块 | 说明 |
|---|---|
| 总览首页 | 总资产、累计/今日盈亏、资金分布、各策略状态卡、资产曲线、动态流、系统健康 |
| 布林带分析 | 1 小时 / 4 小时 / 日线：%B、带宽、收口 / 开口、沿轨运行、中轨方向的大白话解读；带宽与 %B 图；布林带策略的 6 年历史检验（见 `docs/bollinger.md`） |
| 实时看盘 | 秒级/分钟级 K 线、买卖点、指标大白话解读、本地虚拟账户与快速回放 |
| 策略实验室 | 中文自然语言 → 策略 Schema（白名单、可校验）；五策略并行回测；买入持有基准；DeepSeek 分析与候选版本 |
| 防过拟合 | 训练 / 验证（每条谱系最多 3 次）/ 独立测试（每版本 1 次）分区；只用已收盘 K 线；次根 K 线成交；手续费+滑点+点差 |
| 模拟盘 | 多策略共用一个 Alpaca Paper 账户，独立资金额度与订单台账、对账、重启恢复、风控暂停 |
| 美股 ETF 择时 | QQQ 150 日均线 ±2% 缓冲 → 持有 QLD（2 倍纳指 ETF），否则现金；每个交易日开盘后最多一次 |
| 策略自动进化 | 参数变异两轮搜索 → 季度分段稳健分 → 留出检验 → 自动替换（空仓时切换） |
| 冠军–挑战者 | 证据不足的候选作为挑战者：先做快速检验（更早历史 + ETH / 标普 500 交叉检验），再在实时新行情上与现役并行，达标自动上线 |
| 交易节奏 | 止损后冷却 72 小时、连亏 3 笔暂停 72 小时、波动率定仓；每天长周期体检（2021 年起 6 个年度），长期无优势的策略暂停并释放额度（见 `docs/trading_rhythm.md`） |
| 风控与资金 | 长期亏损自动暂停 / 恢复；按长周期 收益/回撤 自动分配资金额度 |
| 杠杆研究 | 2 倍杠杆影子账户（本地按逐仓规则记账，不下单）；`freqtrade_lab/` 用 Freqtrade 做合约杠杆离线回测 |
| 运维 | 一键启停、启动前自动备份数据库、守护进程（无响应/内存过高自动重启）、按天交易日志 |

## 快速开始（Windows）

1. 安装 Python 3.10+（安装时勾选 **Add python.exe to PATH**）。
2. 双击 `start.bat`：首次自动创建 `.venv`、安装依赖并打开 `http://127.0.0.1:8501`。
3. 复制 `.env.example` 为 `config/secrets.local`（或项目根目录 `.env`），填写：
   - `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`（行情）
   - `ALPACA_PAPER_KEY_ID` / `ALPACA_PAPER_SECRET_KEY`（Paper 账户，可与上面相同）
   - `DEEPSEEK_API_KEY`（可选，AI 优化用）
4. `stop.bat` → `start.bat` 重启生效。

其他系统：`python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/streamlit run app.py`。

### 后台常驻（推荐，Windows）

双击 `install_autostart.bat`（只需一次）：
- 登录 Windows 后自动在后台启动（无黑窗口），并每 5 分钟检查一次，停了自动拉起；
- 自动交易循环在服务启动时就运行，不需要打开网页；
- 桌面 / 开始菜单出现「AI量化交易」图标，双击即打开（没在运行会先启动）；
- `stop.bat` 停止并暂停自动拉起，下次点图标或 `start.bat` 恢复；`uninstall_autostart.bat` 移除自启。

电脑睡眠或关机时程序无法运行，需要 7×24 运行请在 Windows 电源设置里关闭自动睡眠。

### 外网访问（可选，Cloudflare Tunnel，免费）

1. 在 Cloudflare 添加你的域名（免费版），把 `config/tunnel.json` 写成 `{"hostname": "quant.你的域名"}`。
2. 双击 `setup_tunnel.bat`：自动下载官方 cloudflared（校验 Cloudflare 数字签名）、浏览器授权一次、创建隧道并写 DNS。
3. 之后服务启动时自动带上外网通道（掉线自动重连）。外网访问必须登录：账号在本机「系统设置 → 登录账号」创建
   （密码 PBKDF2 加密保存、连续输错 5 次锁 15 分钟、签名 Cookie 保持登录 7 天）。建议再在 Cloudflare 加一条
   Managed Challenge 规则或 Cloudflare Access。

### 开启模拟盘自动下单（可选）

双重锁，缺一不可：
1. `aiquant/config.py` 中 `PAPER_ORDER_SUBMISSION_UNLOCKED = True`
2. 创建 `config/paper_authorization.json`（内容可为 `{}`）

之后在「模拟盘交易」页点「一键部署并启动全部策略」。只会访问 `paper-api.alpaca.markets`。

### 其他脚本

| 文件 | 作用 |
|---|---|
| `status.bat` / `run_tests.bat` | 查看状态 / 运行测试（`python -m pytest -q tests`） |
| `fetch_etf.bat` | 下载 26 只美股 ETF 2016 年起日线（ETF 研究与进化用） |
| `fetch_history.bat` | 下载 BTC、ETH 2021 年起 5 分钟历史（快速进化检验用） |
| `freqtrade_lab/` | Freqtrade 合约杠杆离线回测工具包（见其 README） |

## 目录

```
app.py                  Streamlit 入口（总览 / 实时看盘 / 模拟盘 / 进化 + 研究与设置）
aiquant/data/           受控 HTTP（白名单+审计）、Alpaca 行情、数据快照与质量检查
aiquant/strategy/       Schema、中文解析器、模板、版本库
aiquant/engine/         指标、信号、撮合成本、风控、回测
aiquant/paper/          模拟盘 Runner、组合、ETF 择时、杠杆影子账户、后台服务循环
aiquant/evolve.py       自动进化；aiquant/challenger.py 冠军–挑战者与快速检验
aiquant/ui/             页面
tests/                  单元与集成测试（全部离线，使用合成数据）
codex.md                设计决策与开发记录（建议修改代码前先读）
```

## 安全设计

- 外部请求只允许 `data.alpaca.markets`、`paper-api.alpaca.markets`、`api.deepseek.com`，全部写入审计表；`api.alpaca.markets`（真实资金）被拒绝。
- DeepSeek 只读取训练集结果，不能下单；交易只由冻结的确定性规则触发。
- 密钥只从本地文件读取，`.gitignore` 已排除密钥、数据库、行情与日志。

## 免责声明

本软件按“原样”提供，不提供任何明示或暗示的担保。作者不对使用本软件造成的任何损失负责。
加密货币和杠杆产品风险极高，请遵守你所在地区的法律法规。

## 许可证

[MIT](LICENSE)

---

## English summary

A local, Chinese-language quant research and paper-trading workbench built with Python + Streamlit + SQLite.
Write a strategy in plain Chinese, backtest it with strict train / validation / one-shot test splits and realistic costs,
let DeepSeek suggest improvements, then run it on an **Alpaca Paper** account. Strategies evolve automatically
(parameter search → robustness scoring → holdout check → champion–challenger with fast out-of-sample checks on
older history and cross-asset data → automatic deployment). Includes a US-ETF trend-timing sleeve (QQQ 150-day SMA → QLD),
loss-based auto-pause, strength-weighted budgets, a 2x leverage shadow ledger, and a Freqtrade futures research kit.

Order submission is **off by default** (double lock); live-money endpoints are hard-blocked. For research and education only — not investment advice.
