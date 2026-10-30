# quant-guard（Cloudflare Worker）

部署在 `your-domain.example/*` 路由上（免费套餐，超额时直接放行）。

作用：家庭网络到 Cloudflare 的通道每隔约 12 分钟会断 10~20 秒。断开期间：
- 普通页面请求（GET）在 12 秒内每 1.5 秒自动重试，恢复后照常返回，用户只感觉稍慢；
- 仍不通时返回“正在重新连接…”页面，5 秒后自动刷新，不再出现 Cloudflare 502 / 1033 错误页；
- WebSocket（Streamlit 实时连接）和 POST 请求直接透传，断开时由 Streamlit 页面自己重连。

更新方法：Cloudflare 控制台 → Workers & Pages → quant-guard → Edit code，粘贴 `quant-guard.js` 后 Deploy。

## 外部心跳监控（定时任务）

电脑整个停掉时，本机自己发不出通知，所以由这个 Worker 每 5 分钟读一次 `/app/static/health.json`
（只有 `{"status":"ok","ts":秒}`）。连续 2 次失败就通知；恢复后再发一条。

需要在控制台设置：
1. **Triggers → Cron Triggers：** `*/5 * * * *`
2. **Bindings → KV：** 变量名 `GUARD_KV`，绑定一个 KV 命名空间（例如 `quant-guard-state`）
3. **Variables and Secrets：** 以下几项都选 Secret 类型，地址和 token 不进仓库

   | 渠道 | Secret |
   | --- | --- |
   | 企业微信群机器人（国内直连，推荐） | `WECOM_WEBHOOK` = `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…` |
   | 飞书群机器人（可选） | `FEISHU_WEBHOOK` = `https://open.feishu.cn/open-apis/bot/v2/hook/…`；开了签名校验再加 `FEISHU_SECRET` |
   | Telegram（需要代理才能收到） | `TG_TOKEN`、`TG_CHAT` |

4. **各渠道单独开关（可选）：** 普通变量 `NOTIFY_CHANNELS`，例如 `wecom,feishu`。不设时，已配置的渠道全部启用。

启用的渠道并行发送，任一渠道成功就算已通知。邮件只由本机的通知模块发送：Worker 不能直接发 SMTP。
