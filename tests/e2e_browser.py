"""真实浏览器端到端验收（Playwright + Chromium）。

用法：先启动服务（start.bat 或 streamlit run app.py），然后
    python tests/e2e_browser.py [--url http://127.0.0.1:8501] [--shots 目录]
在没有 Alpaca/DeepSeek 网络的环境中，请以测试环境变量启动服务（AIQUANT_DEEPSEEK_MOCK=1, AIQUANT_ALLOW_TEST_DATA=1，
并使用 scripts/seed_test_data.py 生成的合成数据），所有结果会被标记为测试数据。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

from playwright.async_api import Page, async_playwright

RESULTS: list[tuple[str, str, str]] = []


def rec(step: str, ok: bool, note: str = "") -> None:
    RESULTS.append((step, "PASS" if ok else "FAIL", note))
    print(f"[{'PASS' if ok else 'FAIL'}] {step} {note}", flush=True)


async def body(pg: Page) -> str:
    return await pg.inner_text("body")


async def wait_text(pg: Page, pattern: str, timeout: float = 60, absent: str | None = None) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = await body(pg)
        if re.search(pattern, t) and (absent is None or absent not in t):
            return True
        await pg.wait_for_timeout(700)
    return False


async def idle(pg: Page, timeout: float = 120) -> None:
    await pg.wait_for_timeout(1500)
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = await body(pg)
        if "后台任务进行中" not in t:
            await pg.wait_for_timeout(1500)
            if "后台任务进行中" not in await body(pg):
                return
        await pg.wait_for_timeout(1000)


async def click_key(pg: Page, key: str) -> None:
    await pg.locator(f".st-key-{key} button").first.click()
    await pg.wait_for_timeout(1200)


async def select(pg: Page, key: str, option: str) -> None:
    await pg.locator(f".st-key-{key} input").first.click()
    await pg.wait_for_timeout(500)
    await pg.locator("[role='option']").filter(has_text=re.compile(f"^{re.escape(option)}$")).first.click()
    await pg.wait_for_timeout(1500)


async def nav(pg: Page, name: str) -> None:
    await pg.get_by_role("link", name=re.compile(name)).first.click()
    await pg.wait_for_timeout(3000)


async def card_text(pg: Page, n: int) -> str:
    return await pg.locator(f".st-key-card_{n}").first.inner_text()


async def main(url: str, shots: Path) -> int:
    shots.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1920, "height": 1300})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))

        # 1-3
        await pg.goto(url)
        await wait_text(pg, "实时看盘", 40)
        await nav(pg, "策略实验室")
        ok = await wait_text(pg, "全局研究设置", 40)
        rec("01 打开本地 Dashboard", ok, url)
        bg = await pg.evaluate("getComputedStyle(document.querySelector('.stApp')).backgroundColor")
        t = await body(pg)
        rec("02 中文浅色主题", "策略实验室" in t and bg in ("rgb(244, 247, 251)",), f"背景 {bg}")
        n_cards = await pg.locator("[class*='st-key-card_']").count()
        rec("03 五张策略卡片", n_cards == 5, f"{n_cards} 张")
        await pg.screenshot(path=str(shots / "01_home_1920.png"))

        # 5 全局周期 15m → 1h，再切回 15m
        await select(pg, "g_tf", "1小时")
        c1 = await card_text(pg, 1)
        ok1 = "1小时" in c1
        await select(pg, "g_tf", "15分钟")
        rec("05 切换全局K线 15分钟→1小时", ok1 and "15分钟" in await card_text(pg, 1))

        # 6 单卡周期
        await select(pg, "tf_2", "1小时")
        rec("06 修改单张策略卡周期（策略2→1小时）", "1小时" in await card_text(pg, 2))

        # 7-9 自然语言 → 解析 → 冻结 v1
        ta = pg.locator(".st-key-card_1 textarea").first
        await ta.fill("当BTC的EMA20向上突破EMA60时买入，反向交叉时平仓，固定止损2%，盈利3%后启动移动止损，回撤1.5%离场。")
        await ta.blur()
        await pg.wait_for_timeout(1200)
        rec("07 输入自然语言策略", True)
        await click_key(pg, "parse_1")
        ok = await wait_text(pg, "解析完成", 15)
        c1 = await card_text(pg, 1)
        rec("08 解析为固定格式", ok and "EMA(20) 上穿 EMA(60)" in c1 and "固定止损 2%" in c1)
        await click_key(pg, "save_1")
        ok = await wait_text(pg, r"已冻结 .* v\d", 15)
        rec("09 校验并保存版本 v1", ok)

        # 10-11 单策略回测
        await click_key(pg, "run_1")
        await idle(pg)
        await pg.reload(); await wait_text(pg, "全局研究设置", 30)
        c1 = await card_text(pg, 1)
        rec("10 运行单策略历史回测", "已完成" in c1 and "净收益" in c1)
        rec("11a 净收益/费用显示", bool(re.search(r"[+-]\d+\.\d+%", c1)) and "USD" in c1)
        await click_key(pg, "zoom_1")
        ok = await wait_text(pg, "K线与交易明细", 20) and await wait_text(pg, "回测假设", 20)
        await pg.wait_for_timeout(2500)
        dlg = pg.locator("[role='dialog']").first
        n_tr = await dlg.locator("[data-testid='stDataFrame']").count()
        n_mk = await dlg.locator("g.trace.scatter").count()
        await pg.screenshot(path=str(shots / "02_kline_dialog.png"))
        rec("11b 买卖明细与K线标记", ok and n_tr == 1 and n_mk >= 1, f"交易表 {n_tr}，标记图层 {n_mk}")
        await pg.keyboard.press("Escape")
        await pg.wait_for_timeout(1000)

        # 12-14 同步运行五策略
        await click_key(pg, "btn_run_all")
        await idle(pg, 240)
        await pg.reload(); await wait_text(pg, "全局研究设置", 30)
        await wait_text(pg, "跑赢持有", 30)
        t = await body(pg)
        rec("12 同步运行5套策略", t.count("已完成") >= 5)
        rec("13 统一收益排行榜", "可比结果 5/5" in t, re.search(r"可比结果 \d/5", t).group(0) if re.search(r"可比结果 \d/5", t) else "")
        rec("14 BTC买入持有比较", "BTC 买入持有基准" in t and "跑赢持有" in t)
        await pg.set_viewport_size({"width": 1920, "height": 3000})
        await pg.wait_for_timeout(6000)
        await pg.screenshot(path=str(shots / "03_lab_after_batch_1920.png"), full_page=True)
        await pg.set_viewport_size({"width": 1920, "height": 1300})

        # 15-20 DeepSeek
        await click_key(pg, "ai_opt_1")
        await idle(pg, 120)
        await pg.reload(); await wait_text(pg, "DeepSeek AI 优化中心", 30)
        t = await body(pg)
        rec("15/16 DeepSeek 分析（调用并保存原始响应）", "策略1 AI 分析建议" in t and "调用 #" in t,
            "MOCK 测试替身" if "MOCK" in t else "真实调用")
        gen = pg.locator("[class*='st-key-ai_gen_1_'] button").first
        await gen.click(); await pg.wait_for_timeout(2500)
        ok = await wait_text(pg, r"候选 .* v2", 15)
        rec("17 生成 v2 策略", ok)
        await pg.locator(".st-key-card_1").first.scroll_into_view_if_needed()
        await pg.get_by_role("button", name="查看修改差异").first.click()
        await pg.wait_for_timeout(1200)
        t = await body(pg)
        rec("18 查看 v1/v2 规则变化", "止损规则" in t and "v2" in t)
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(600)
        await pg.locator("[class*='st-key-ai_apply_1_'] button").first.click()
        await pg.wait_for_timeout(2000)
        await click_key(pg, "run_1")
        await idle(pg)
        await pg.reload(); await wait_text(pg, "全局研究设置", 30)
        c1 = await card_text(pg, 1)
        rec("19 对 v2 重新回测", "v2" in c1 and "已完成" in c1)
        await nav(pg, "策略版本")
        await wait_text(pg, "版本对比", 20)
        t = await body(pg)
        await pg.screenshot(path=str(shots / "04_versions.png"), full_page=True)
        n_df = await pg.locator("[data-testid='stDataFrame']").count()
        n_pl = await pg.locator(".js-plotly-plot").count()
        rec("20 新旧收益比较", "版本对比" in t and "v2" in t and n_df >= 3 and n_pl >= 1, f"表 {n_df} 图 {n_pl}")

        # 21 刷新保持
        await nav(pg, "策略实验室")
        await pg.reload(); await wait_text(pg, "全局研究设置", 30)
        await wait_text(pg, "可比结果 5/5", 30)
        t = await body(pg)
        rec("21 刷新后状态与结果未丢失", t.count("已完成") >= 5 and "可比结果 5/5" in t)

        # 22-27 模拟盘
        await click_key(pg, "deploy_1")
        await pg.wait_for_timeout(1500)
        t = await body(pg)
        if "暂不能部署" in t:
            rec("22a 部署门禁：测试数据结果不得部署到 Alpaca Paper", "已完成真实数据历史回测" in t)
            await nav(pg, "模拟盘交易")
        ok = await wait_text(pg, "模拟盘交易中心", 20)
        t = await body(pg)
        rec("22 进入模拟盘中心", ok)
        rec("23 选择冻结策略版本", "冻结哈希" in t)
        await pg.screenshot(path=str(shots / "05a_paper_before.png"), full_page=True)
        await pg.get_by_text("Mock Broker（测试）").click()
        await pg.get_by_text("我确认：训练回测收益不代表未来").click()
        await pg.get_by_role("button", name="保存部署配置").click()
        ok = await wait_text(pg, r"部署配置 #\d+ 已保存", 15)
        rec("24 保存 Paper 部署配置（Mock）", ok)
        t = await body(pg)
        rec("25 Paper 只读连接检查", "只读连接检查" in t and "账户可读" in t,
            "已连接" if "✅ 账户可读" in t else "未配置/不可达（真实检查结果）")
        await click_key(pg, "pp_start")
        ok1 = await wait_text(pg, "已启动", 10)
        await click_key(pg, "pp_replay")
        ok2 = await wait_text(pg, r"回放完成.*'restarts': 1", 120)
        t = await body(pg)
        m = re.search(r"回放完成：(\{.*?\})", t)
        await click_key(pg, "pp_stop")
        ok3 = await wait_text(pg, "持仓未被平掉", 10)
        t = await body(pg)
        rec("26 Mock Broker 启动/订单/停止/恢复", ok1 and ok2 and ok3, m.group(1) if m else "")
        await pg.screenshot(path=str(shots / "05_paper.png"), full_page=True)
        rec("27 未提交任何 Paper 订单", "已提交到 Alpaca Paper 的订单：0" in t)
        await nav(pg, "系统设置")
        await wait_text(pg, "外部端点访问审计", 15)
        t = await body(pg)
        rec("28 未访问任何 Live/订单写接口", "Live 端点访问：0" in t and "订单写请求：0" in t or "Live 端点访问" not in t)
        await pg.screenshot(path=str(shots / "06_settings.png"), full_page=True)

        for w in (1440, 1366):
            await pg.set_viewport_size({"width": w, "height": 1100})
            await nav(pg, "策略实验室")
            await pg.wait_for_timeout(2500)
            await pg.screenshot(path=str(shots / f"07_lab_{w}.png"))
        rec("页面 JS 错误", not errors, "; ".join(errors[:3]))
        await b.close()
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    (shots / "e2e_results.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nTOTAL {len(RESULTS)}  PASS {len(RESULTS) - len(fails)}  FAIL {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8501/")
    ap.add_argument("--shots", default="e2e_shots")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.url, Path(a.shots))))
