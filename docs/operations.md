# 运维手册（生产实例）

> 服务器真实地址与凭据**不入本公开仓库**——放在本地 `OPS-LOCAL.md`（已 gitignore）。
> 拓扑与端口决策见 [ADR-0006](./adr/0006-cloudflare-origin-port-9800.md)；通用部署流程见 [deployment.md](./deployment.md)。

## 拓扑速览

```
浏览器 ──HTTPS──> Cloudflare 边缘 ──HTTP:9800──> nginx（静态 + /api 反代）──> uvicorn 127.0.0.1:8000
```

- 代码：`/opt/ai-polish`（服务账号 `aipolish`，systemd 单元 `ai-polish.service`）
- 数据：`/opt/ai-polish/backend/data/app.db`（SQLite WAL）
- 配置：`/opt/ai-polish/backend/.env`（唯一来源，ADR-0005；改配置 = 改文件 + `systemctl restart ai-polish`）
- 旧版遗留（可回滚）：代码 `/opt/AI-Polish`、退场单元 `/etc/systemd/system/ai-polish-old.service.bak`、旧库备份 `/opt/ai_polish.db.bak-*`

## 日常操作

```bash
ssh sys_admin@<服务器IP>            # 公钥已装，免密；sudo 需登录密码

systemctl status ai-polish          # 服务状态（MemoryMax=256M）
journalctl -u ai-polish -n 50       # 后端日志
systemctl restart ai-polish         # 改 .env 后重启生效
curl -s http://127.0.0.1:9800/health   # 本机健康检查
```

## 发版

```bash
cd /opt/ai-polish && sudo -u aipolish git pull
sudo ./deploy.sh
# ⚠️ Cloudflare 拓扑专属步骤：deploy.sh 会用 80/443 直连变体覆盖站点文件，
#    必须重新套用 9800 变体（见 ADR-0006）：
sudo cp deploy/nginx/ai-polish-cloudflare.conf /etc/nginx/sites-available/ai-polish.conf
sudo nginx -t && sudo systemctl reload nginx
```

## 配置项速查（backend/.env）

| 项 | 说明 |
|---|---|
| `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL` | 模型接入，三项齐备重启后流水线 Worker 才启动 |
| `ADMIN_USERNAME / ADMIN_PASSWORD` | 管理后台登录（部署时随机生成，`sudo cat` 查看） |
| `JWT_SECRET` | 管理端会话签名密钥 |
| `MAX_SESSION_TEXT_CHARS` | 单次提交文本上限（默认 50000，Nginx `client_max_body_size` 需同步） |
| `RECOVERY_*` | 僵尸恢复：最大续跑次数 / 滞留阈值 / 巡检间隔 |

## 回滚（切回旧版）

```bash
sudo systemctl stop ai-polish nginx
sudo mv /etc/systemd/system/ai-polish-old.service.bak /etc/systemd/system/ai-polish.service
sudo systemctl daemon-reload && sudo systemctl start ai-polish
# 旧版自伺服静态与 API 于 9800，无需 nginx；新代码目录保留不动
```

## 安全与已知事项

- **待办：修改服务器登录密码**（初始密码曾经由项目所有者交付，按轮换纪律应更换：`passwd`）
- 服务器 IP / 密码 / 管理凭据不写入本仓库与本机 shell 历史
- 内存 426MB + 1G swap（`/swapfile`，已入 fstab）：构建期可能短暂吃 swap，属预期
- 业务监控以**账本对账**为准：`SUM(账本) == 每用户余额`；巡检对滞留 queued 只告警不退款（日志可见即需人工排查）
