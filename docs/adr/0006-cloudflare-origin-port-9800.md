# 生产域名经 Cloudflare 橙云回源 9800，源站不做 TLS

生产域名解析在 Cloudflare（橙云代理），回源端口沿用旧版部署的 9800（HTTP）；源站 nginx 只监听 9800（静态 + /api 反代 127.0.0.1:8000），不装 443、不做 certbot——HTTPS 由 Cloudflare 边缘终结。选择理由：切换时保持回源端口不变，域名无感迁移；橙云下源站 TLS 的收益（防回源劫持）由 Cloudflare Origin Rules/证书可后补，不阻塞上线。站点配置变体在 deploy/nginx/ai-polish-cloudflare.conf。

注意两个"看起来像问题但不是"的点，勿擅自"修复"：① 源站 443 无监听、无 /etc/letsencrypt——不是遗漏；② 浏览器侧 cookie 为 Secure 而源站是 HTTP——Secure 判定在浏览器，浏览器看到的是 CF 的 HTTPS，ADMIN_COOKIE_SECURE=true 保持正确。
