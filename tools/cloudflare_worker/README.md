# quant-guard（Cloudflare Worker）

部署在 `<你的子域名>/*` 路由上（免费套餐，超额时直接放行）。

作用：家庭网络到 Cloudflare 的通道每隔约 12 分钟会断 10~20 秒。断开期间：
- 普通页面请求（GET）在 12 秒内每 1.5 秒自动重试，恢复后照常返回，用户只感觉稍慢；
- 仍不通时返回“正在重新连接…”页面，5 秒后自动刷新，不再出现 Cloudflare 502 / 1033 错误页；
- WebSocket（Streamlit 实时连接）和 POST 请求直接透传，断开时由 Streamlit 页面自己重连。

更新方法：Cloudflare 控制台 → Workers & Pages → quant-guard → Edit code，粘贴 `quant-guard.js` 后 Deploy。
