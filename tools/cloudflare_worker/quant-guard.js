// quant-guard：放在 your-domain.example 前面的 Cloudflare Worker（免费套餐）。
// 家里网络到 Cloudflare 每隔约 12 分钟会断 10~20 秒，这期间直接访问会看到 502 / 1033。
// 这个 Worker 在通道断开时先自动重试几秒（GET 请求），仍不通就返回一个会自动刷新的"正在重新连接"页面，
// 用户不会再看到 Cloudflare 的错误页。WebSocket（Streamlit 实时连接）直接透传，断开时由页面自己重连。
// 外部心跳监控（2026-10-30）：定时任务（Cron Trigger，每 5 分钟）读取健康检查文件 /app/static/health.json
// （只含 {"status":"ok","ts":秒}，不含任何数据）。连续 2 次失败 → 通知；恢复后再发一条。
// 通知渠道（并行发送，任一渠道成功即算已通知）：
//   Telegram：Secrets TG_TOKEN、TG_CHAT
//   企业微信群机器人：Secret WECOM_WEBHOOK（完整地址 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…）
//   飞书群机器人（可选）：Secret FEISHU_WEBHOOK（https://open.feishu.cn/open-apis/bot/v2/hook/…），
//     机器人开了「签名校验」时再加 Secret FEISHU_SECRET
// 每个渠道单独开关：普通变量 NOTIFY_CHANNELS，例如 "wecom,feishu"；不设 = 已配置的渠道全部启用。
// 需要的绑定：KV 命名空间 GUARD_KV（记连续失败次数）。所有地址 / token 只放在 Cloudflare 控制台的 Secrets 里，不进仓库。
const HEALTH_URL = "https://your-domain.example/app/static/health.json";
const STALE_S = 600;          // 健康文件超过 10 分钟没更新 = 后台交易循环停了（网页还在也算失败）
const FAILS_TO_ALERT = 2;
const CHECK_TRIES = 3;        // 每次检查内部重试 3 次（间隔 8 秒），避开家里网络每隔约 12 分钟 10~20 秒的断线
const RETRY_STATUS = new Set([502, 503, 504, 520, 521, 522, 523, 524, 530]);
const RETRY_FOR_MS = 12000;
const RETRY_GAP_MS = 1500;

const PAGE = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="6">
<title>正在重新连接… · AI Quant Strategy Lab</title>
<style>
:root{--bg:#F4F7FB;--ink:#0F172A;--sub:#64748B;--card:#fff;--line:#E5E7EB}
@media (prefers-color-scheme:dark){:root{--bg:#0B1120;--ink:#E5E7EB;--sub:#94A3B8;--card:#111827;--line:#1F2937}}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;padding:16px;box-sizing:border-box}
.c{max-width:420px;width:100%;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:28px 24px;text-align:center}
.m{width:56px;height:56px;border-radius:15px;margin:0 auto 14px;background:linear-gradient(135deg,#3B82F6,#7C3AED);position:relative}
.m:after{content:"";position:absolute;inset:16px;border:3px solid #fff;border-top-color:transparent;border-radius:50%;animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
h1{font-size:19px;margin:0 0 6px}p{margin:4px 0;color:var(--sub)}
a{color:#2563EB;text-decoration:none;font-weight:600}
</style></head><body><div class="c"><div class="m"></div>
<h1>正在重新连接…</h1><p>系统运行正常，网络通道正在自动恢复，通常 10~20 秒。</p>
<p>Reconnecting — the lab is fine, the network link is recovering (usually 10–20 s).</p>
<p style="margin-top:14px"><a href="">立即重试 / Retry now</a></p></div>
<script>setTimeout(function(){location.reload()},5000)</script></body></html>`;

async function probe() {
  let last = "";
  for (let i = 0; i < CHECK_TRIES; i++) {
    try {
      const r = await fetch(HEALTH_URL + "?t=" + Date.now(), {
        headers: { "Cache-Control": "no-cache", "Accept": "application/json" },
        cf: { cacheTtl: 0, cacheEverything: false },
      });
      if (r.status === 200) {
        const j = await r.json();
        const age = Math.floor(Date.now() / 1000) - Number(j.ts || 0);
        if (j.status === "ok" && age <= STALE_S) return { ok: true, age };
        last = `健康文件 ${age} 秒没有更新（后台循环可能停止）`;
      } else {
        last = `HTTP ${r.status}`;
      }
    } catch (e) {
      last = `连接失败：${String(e).slice(0, 80)}`;
    }
    if (i < CHECK_TRIES - 1) await new Promise((r) => setTimeout(r, 8000));
  }
  return { ok: false, err: last };
}

const CHANNELS = ["telegram", "wecom", "feishu"];

function enabled(env) {
  const raw = String(env.NOTIFY_CHANNELS || "").trim().toLowerCase();
  const want = raw ? raw.split(/[\s,;]+/).filter(Boolean) : CHANNELS;
  const configured = {
    telegram: !!(env.TG_TOKEN && env.TG_CHAT),
    wecom: !!env.WECOM_WEBHOOK,
    feishu: !!env.FEISHU_WEBHOOK,
  };
  return CHANNELS.filter((c) => want.includes(c) && configured[c]);
}

async function feishuSign(secret) {
  // 飞书签名：base64(HMAC-SHA256(key = timestamp + "\n" + secret, message = 空))
  const ts = Math.floor(Date.now() / 1000);
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(`${ts}\n${secret}`),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, new Uint8Array(0)));
  return { timestamp: String(ts), sign: btoa(String.fromCharCode(...sig)) };
}

async function sendOne(env, ch, text) {
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (ch === "telegram") {
    const r = await post(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`,
      { chat_id: env.TG_CHAT, text, disable_web_page_preview: true });
    return r.ok;
  }
  if (ch === "wecom") {
    const r = await post(env.WECOM_WEBHOOK, { msgtype: "text", text: { content: text.slice(0, 1800) } });
    if (!r.ok) return false;
    const j = await r.json().catch(() => ({}));
    return j.errcode === 0;
  }
  if (ch === "feishu") {
    const body = { msg_type: "text", content: { text } };
    if (env.FEISHU_SECRET) Object.assign(body, await feishuSign(env.FEISHU_SECRET));
    const r = await post(env.FEISHU_WEBHOOK, body);
    if (!r.ok) return false;
    const j = await r.json().catch(() => ({}));
    return (j.code ?? j.StatusCode) === 0;
  }
  return false;
}

// 已启用的渠道并行发送；任一成功即返回 true。失败只写日志（不打印地址 / token）。
async function notify(env, text) {
  const chans = enabled(env);
  if (!chans.length) { console.log("no notify channel configured:", text); return false; }
  const res = await Promise.all(chans.map((c) => sendOne(env, c, text).catch((e) => { console.log(c, "failed", String(e).slice(0, 80)); return false; })));
  chans.forEach((c, i) => { if (!res[i]) console.log(c, "send failed"); });
  return res.some(Boolean);
}

function bj(ms) {
  return new Date(ms + 8 * 3600 * 1000).toISOString().slice(5, 16).replace("T", " ");
}

async function heartbeat(env) {
  if (!env.GUARD_KV) { console.log("GUARD_KV binding missing"); return; }
  const st = JSON.parse((await env.GUARD_KV.get("health")) || "{}");
  const now = Date.now();
  const res = await probe();
  if (res.ok) {
    if (st.alerted) {
      const mins = Math.round((now - (st.down_since || now)) / 60000);
      await notify(env, `✅ 量化服务已恢复（外部心跳监控）\n中断约 ${mins} 分钟：${bj(st.down_since || now)} → ${bj(now)}（北京时间）\n`
        + "恢复后本机会立即补做一次全策略检查，详见事件流。");
    }
    if (st.fails || st.alerted || !st.last_ok || now - st.last_ok > 3600 * 1000) {
      await env.GUARD_KV.put("health", JSON.stringify({ fails: 0, alerted: false, last_ok: now }));
    }
    return;
  }
  const fails = (st.fails || 0) + 1;
  const down_since = st.down_since || now;
  let alerted = !!st.alerted;
  if (fails >= FAILS_TO_ALERT && !alerted) {
    alerted = await notify(env, `⚠️ 量化服务失联（外部心跳监控，连续 ${fails} 次检查失败）\n原因：${res.err}\n`
      + `最后一次正常：${st.last_ok ? bj(st.last_ok) : "未知"}（北京时间）\n`
      + "电脑可能关机、睡眠、断网或断电；本机此时自己发不出通知。");
  }
  await env.GUARD_KV.put("health", JSON.stringify({ fails, alerted, down_since, last_ok: st.last_ok || null, last_err: res.err }));
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(heartbeat(env));
  },

  async fetch(request) {
    const upgrade = (request.headers.get("Upgrade") || "").toLowerCase();
    if (upgrade === "websocket") return fetch(request);

    const retryable = request.method === "GET" || request.method === "HEAD";
    const deadline = Date.now() + RETRY_FOR_MS;
    let resp = null;
    while (true) {
      try {
        resp = await fetch(retryable ? request.clone() : request);
      } catch (e) {
        resp = null;
      }
      if (resp && !RETRY_STATUS.has(resp.status)) return resp;
      if (!retryable || Date.now() + RETRY_GAP_MS > deadline) break;
      await new Promise((r) => setTimeout(r, RETRY_GAP_MS));
    }
    if (!retryable && resp) return resp;
    const accept = request.headers.get("Accept") || "";
    if (!accept.includes("text/html")) {
      return new Response("reconnecting", { status: 503, headers: { "Retry-After": "5", "Cache-Control": "no-store" } });
    }
    return new Response(PAGE, {
      status: 503,
      headers: { "Content-Type": "text/html; charset=utf-8", "Retry-After": "5", "Cache-Control": "no-store" },
    });
  },
};
