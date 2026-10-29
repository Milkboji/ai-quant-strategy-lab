// quant-guard 定时任务的离线测试（node t.mjs）：模拟健康检查结果、KV 和 Telegram。
import w from "./qg.mjs";
const kv = new Map(); const sent = [];
const env = { TG_TOKEN: "t", TG_CHAT: "c", GUARD_KV: { get: async k => kv.get(k) ?? null, put: async (k, v) => kv.set(k, v) } };
let mode = "ok";
globalThis.fetch = async (url, opt) => {
  if (String(url).includes("api.telegram.org")) { sent.push(JSON.parse(opt.body).text); return new Response("{}", { status: 200 }); }
  if (mode === "down") throw new Error("connect timeout");
  if (mode === "503") return new Response("reconnecting", { status: 503 });
  const ts = Math.floor(Date.now() / 1000) - (mode === "stale" ? 3600 : 0);
  return new Response(JSON.stringify({ status: "ok", ts }), { status: 200 });
};
const run = async () => { let p; await w.scheduled({}, env, { waitUntil: x => (p = x) }); await p; };
const check = (c, m) => { if (!c) { console.error("FAIL", m); process.exit(1); } };
await run(); check(sent.length === 0, "ok -> no message");
mode = "down"; await run(); check(sent.length === 0, "1 failure -> no message");
await run(); check(sent.length === 1 && sent[0].includes("失联"), "2 failures -> alert");
await run(); check(sent.length === 1, "no repeated alert");
mode = "ok"; await run(); check(sent.length === 2 && sent[1].includes("已恢复"), "recovery message");
mode = "stale"; await run(); await run(); check(sent.length === 3 && sent[2].includes("没有更新"), "stale heartbeat counts as failure");
mode = "503"; kv.clear(); await run(); await run(); check(sent.length === 4 && sent[3].includes("HTTP 503"), "503 counts as failure");
console.log("ALL OK");
