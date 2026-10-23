// quant-guard：放在 your-domain.example 前面的 Cloudflare Worker（免费套餐）。
// 家里网络到 Cloudflare 每隔约 12 分钟会断 10~20 秒，这期间直接访问会看到 502 / 1033。
// 这个 Worker 在通道断开时先自动重试几秒（GET 请求），仍不通就返回一个会自动刷新的"正在重新连接"页面，
// 用户不会再看到 Cloudflare 的错误页。WebSocket（Streamlit 实时连接）直接透传，断开时由页面自己重连。
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

export default {
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
