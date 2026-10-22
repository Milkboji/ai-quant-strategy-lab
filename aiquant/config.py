"""全局配置。

所有密钥只从本地 .env / 环境变量读取，绝不写入前端、日志或数据库。
交易权限采用"双重锁"：代码常量 + 本地授权文件，二者同时满足才可能解锁 Paper 下单。
本版本代码常量保持关闭，因此 PAPER_ORDERS = 0, LIVE_ORDERS = 0。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("AIQUANT_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "market_cache"
RAW_LLM_DIR = DATA_DIR / "llm_raw"
LOG_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups"
CONFIG_DIR = ROOT / "config"
DB_PATH = Path(os.environ.get("AIQUANT_DB", DATA_DIR / "aiquant.db"))

for _d in (DATA_DIR, CACHE_DIR, RAW_LLM_DIR, LOG_DIR, BACKUP_DIR, CONFIG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 交易权限（硬锁）
# ---------------------------------------------------------------------------
# 第一道锁：默认关闭，系统不会向 Alpaca Paper 提交任何订单（只做研究、回测、只读检查）。
# 想让模拟盘自动下单：把下面改为 True，并在 config/ 下创建 paper_authorization.json（内容可为 {}）。
# 两者同时满足才会放行（双重锁）。只连接 paper-api.alpaca.markets，真实资金端点永久禁止。
PAPER_ORDER_SUBMISSION_UNLOCKED: bool = False
# Live 交易永远不在本项目中实现。
LIVE_TRADING_ENABLED: bool = False

ALPACA_DATA_URL = "https://data.alpaca.markets"
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
# 明确禁止的 Live 端点，任何适配器都会拒绝使用
ALPACA_LIVE_URL_FORBIDDEN = "https://api.alpaca.markets"

DEEPSEEK_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

TIMEFRAMES = {  # 标签 -> 分钟
    "5分钟": 5,
    "15分钟": 15,
    "30分钟": 30,
    "1小时": 60,
    "4小时": 240,
    "1天": 1440,
}
TF_CODE = {5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1d"}
BASE_TIMEFRAME_MIN = 5  # 基础行情粒度与默认撮合粒度


def _load_dotenv() -> None:
    # 优先 .env；其次 config/secrets.local（同样仅本地、已加入 .gitignore）
    for env_path in (ROOT / ".env", ROOT / "config" / "secrets.local"):
        if env_path.exists():
            _load_file(env_path)


def _load_file(env_path: Path) -> None:
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and not os.environ.get(k):
            os.environ[k] = v


_load_dotenv()


def secret(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None


def mask(value: str | None) -> str:
    if not value:
        return "未配置"
    if len(value) <= 6:
        return "***"
    return value[:3] + "***" + value[-2:]


@dataclass
class ResearchDefaults:
    symbol: str = "BTC/USD"
    timeframe_min: int = 15
    window_days: int = 90
    initial_cash: float = 10_000.0
    max_alloc_pct: float = 100.0
    fee_bps: float = 25.0      # 每边
    slippage_bps: float = 2.0  # 每笔成交
    spread_bps_proxy: float = 1.0  # 无历史报价时的冻结点差代理（全价差，成交时各付一半）
    target_return_pct: float = 30.0
    leverage: float = 1.0
    long_only: bool = True


DEFAULTS = ResearchDefaults()

# 研究风控（仅限隔离 BTC 现货研究，不影响任何生产配置）
@dataclass
class ResearchRisk:
    max_alloc_pct: float = 100.0
    cost_reserve_pct: float = 0.5   # 下单时预留的成本缓冲，保证现金不为负
    min_order_usd: float = 10.0


RESEARCH_RISK = ResearchRisk()
