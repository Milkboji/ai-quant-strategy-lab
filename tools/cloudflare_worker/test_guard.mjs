// quant-guard 定时任务的离线测试（node t.mjs）：模拟健康检查结果、KV 和各通知渠道。
import w from "./qg.mjs";
const kv = new Map(); const sent = []; const hits = { telegram: 0, wecom: 0, feishu: 0 }; const bodies = {};
let failing = new Set();
const WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k";
const FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/h";
const KV = { get: async k => kv.get(k) ?? null, put: async (k, v) => kv.set(k, v) };
let env = { TG_TOKEN: "t", TG_CHAT: "c", GUARD_KV: KV };
let mode = "ok";
globalThis.fetch = async (url, opt) => {
  const u = String(url);
  const ch = u.includes("api.telegram.org") ? "telegram" : u.startsWith(WECOM) ? "wecom" : u.startsWith(FEISHU) ? "feishu" : null;
  if (ch) {
    hits[ch]++; bodies[ch] = JSON.parse(opt.body);
    if (failing.has(ch)) return new Response("{}", { status: 500 });
    if (ch === "telegram") sent.push(bodies[ch].text);
    if (ch === "wecom") return new Response(JSON.stringify({ errcode: 0 }), { status: 200 });
    if (ch === "feishu") return new Response(JSON.stringify({ code: 0 }), { status: 200 });
    return new Response("{}", { status: 200 });
  }
  if (mode === "down") throw new Error("connect timeout");
  if (mode === "503") return new Response("reconnecting", { status: 503 });
  const ts = Math.floor(Date.now() / 1000) - (mode === "stale" ? 3600 : 0);
  return new Response(JSON.stringify({ status: "ok", ts }), { status: 200 });
};
const run = async () => { let p; await w.scheduled({}, env, { waitUntil: x => (p = x) }); await p; };
const check = (c, m) => { if (!c) { console.error("FAIL", m); process.exit(1); } };

// 1. Telegram 单渠道：连续 2 次才告警、不重复、恢复通知、过期 / 503 都算失败
await run(); check(sent.length === 0, "ok -> no message");
mode = "down"; await run(); check(sent.length === 0, "1 failure -> no message");
await run(); check(sent.length === 1 && sent[0].includes("失联"), "2 failures -> alert");
await run(); check(sent.length === 1, "no repeated alert");
mode = "ok"; await run(); check(sent.length === 2 && sent[1].includes("已恢复"), "recovery message");
mode = "stale"; await run(); await run(); check(sent.length === 3 && sent[2].includes("没有更新"), "stale heartbeat counts as failure");
mode = "503"; kv.clear(); await run(); await run(); check(sent.length === 4 && sent[3].includes("HTTP 503"), "503 counts as failure");

// 2. 三个渠道并行；飞书带签名
const reset = () => { kv.clear(); for (const k in hits) hits[k] = 0; };
env = { TG_TOKEN: "t", TG_CHAT: "c", WECOM_WEBHOOK: WECOM, FEISHU_WEBHOOK: FEISHU, FEISHU_SECRET: "s", GUARD_KV: KV };
reset(); mode = "down"; await run(); await run();
check(hits.telegram === 1 && hits.wecom === 1 && hits.feishu === 1, "all three channels in parallel");
check(bodies.wecom.msgtype === "text" && bodies.wecom.text.content.includes("失联"), "wecom text body");
check(bodies.feishu.msg_type === "text" && bodies.feishu.sign && bodies.feishu.timestamp, "feishu signed body");

// 3. 单独开关：NOTIFY_CHANNELS 只开企业微信
env.NOTIFY_CHANNELS = "wecom";
reset(); await run(); await run();
check(hits.wecom === 1 && hits.telegram === 0 && hits.feishu === 0, "NOTIFY_CHANNELS=wecom only sends wecom");

// 4. 一个渠道失败、另一个成功 → 算已通知，不重复告警；全部失败 → 下一轮再试
env.NOTIFY_CHANNELS = "telegram,wecom"; failing = new Set(["telegram"]);
reset(); await run(); await run(); await run();
check(hits.wecom === 1 && hits.telegram === 1, "telegram failing, wecom ok -> alerted once");
failing = new Set(["telegram", "wecom"]);
reset(); await run(); await run(); await run();
check(hits.wecom === 2 && hits.telegram === 2, "all channels failing -> retried next round");
failing = new Set();

// 5. 没有任何渠道配置：不报错
env = { GUARD_KV: KV }; reset(); await run(); await run();
console.log("ALL OK");
