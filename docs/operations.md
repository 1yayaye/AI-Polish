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
| `MAX_WORD_TEXT_CHARS` | Word 排版/预处理提交文本上限（默认 200000，超限 413） |
| `MAX_WORD_UPLOAD_MB` | Word 上传文件上限 MB（默认 20，超限 413；Nginx `client_max_body_size` 需同步，默认 21m） |
| `WORD_JOB_RETENTION_SECONDS` / `WORD_JOB_PURGE_INTERVAL_SECONDS` | Word 任务保留期（默认 24h）与清理巡检间隔；过期终态任务连库行与产物一并删除 |
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

## 验收/排障日志查证（T7 速查）

日志四层，各管一段：**journald**（应用与请求生命周期，持久化于 /var/log/journal，重启不丢）、**nginx access/error**（`/var/log/nginx/`，含 Cloudflare 边缘 IP 的访问）、**SQLite 账本**（资金权威证据）、**model_snapshot 列**（客诉时查当时模型）。卡密只走请求头，任何日志里都不出现。

| T7 步骤 | 查证命令 | 应看到 |
|---|---|---|
| Worker 是否启动 | `journalctl -u ai-polish \| grep -iE "worker\|startup"` | LLM 三项配置后 startup 且 Worker 拉取日志；缺配置时只有"流水线未配置"类日志 |
| 提交与预扣费 | `journalctl -u ai-polish \| grep "POST /api/optimization/sessions"` | 201；配合账本 charge 行 |
| LLM 重试/失败 | `journalctl -u ai-polish -p warning..alert` | 瞬时错误退避重试 warning；内容性失败后任务 failed + refund |
| kill -9 演练 | `kill -9 $(systemctl show -p MainPID --value ai-polish)` 后 `journalctl -u ai-polish` | `Main process exited, code=killed, status=9/KILL` → systemd 重启 → 启动恢复日志（断点续跑/兜底退款） |
| SSE 连接 | nginx `access.log` + `journalctl` | 200 流式响应；卡密不在任何日志 |
| 资金权威证据 | `sudo -u aipolish sqlite3 backend/data/app.db "SELECT u.id,u.balance_fen,COALESCE(SUM(l.amount_fen),0) FROM users u LEFT JOIN ledger_entries l ON l.user_id=u.id GROUP BY u.id;"` | 每用户 `balance == SUM(账本)` |
| 客诉查模型 | `... SELECT id,model_snapshot,status FROM optimization_sessions WHERE id=?` | 当时所用模型只读快照 |

> sqlite3 CLI 未装时用 `sudo -u aipolish backend/.venv/bin/python -c "..."` 等价查询。
