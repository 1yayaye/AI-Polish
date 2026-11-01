# 部署指南（systemd + Nginx 裸机）

> 目标环境：1 核 / 512MB 内存的 Linux VPS。为什么不用 Docker 见 [ADR-0002](./adr/0002-systemd-bare-metal.md)。
> 部署模型：Nginx 伺服前端静态产物并反代 `/api`，后端以 systemd 服务运行在 `127.0.0.1:8000`。

## 1. 一次性准备

```bash
# 部署用系统用户（服务以此身份运行，无登录 shell）
sudo useradd --system --home /opt/ai-polish --shell /usr/sbin/nologin aipolish

# 基础软件
sudo apt update && sudo apt install -y python3 python3-venv nginx nodejs npm

# 拉取代码到约定路径（systemd/Nginx 配置写死此路径）
sudo mkdir -p /opt/ai-polish && sudo chown aipolish:aipolish /opt/ai-polish
sudo -u aipolish git clone <repo-url> /opt/ai-polish
cd /opt/ai-polish

# 配置：.env 是唯一配置来源（ADR-0005），改配置 = 改文件 + 重启服务
cp backend/.env.example backend/.env
```

生产目录职责：

| 路径 | 用途 |
|---|---|
| `/opt/ai-polish/backend/` | 后端代码 + `.venv` + `data/app.db`（SQLite，WAL 模式） |
| `/opt/ai-polish/frontend/dist/` | 前端构建产物（Nginx 直接伺服） |

## 2. 一键部署

```bash
sudo ./deploy.sh
```

脚本依次完成：构建前端（构建内存上限 384MB，防止 512MB 机器 OOM）→ 按 `backend/requirements.txt` 锁定版本安装后端依赖到 `.venv` → 数据库迁移 `alembic upgrade head` 建表 → 注入系统预设提示词种子（`python -m app.seeds`）→ 安装 systemd unit 与 Nginx 配置 → 重启服务 → 对 `127.0.0.1:8000/health` 与 Nginx 层 `/health` 做健康检查。任何一步失败即中止。

迁移与种子都是幂等的：首次部署建出全部表和系统提示词；日常发版重复执行时，alembic 停在 `head` 不重复建表，种子按 (stage, name) 判重、不覆盖已在库内改动过的系统提示词。应用启动本身不建表——跳过迁移时 `/health` 仍正常，但触库的 `/api` 调用会 500，因此不要手动绕过这一步。

## 3. 日常发版

```bash
cd /opt/ai-polish
sudo -u aipolish git pull
sudo ./deploy.sh
```

## 4. 验证

```bash
curl http://127.0.0.1:8000/health   # {"status":"ok"}
curl http://127.0.0.1/health        # 经 Nginx
```

`/health` 只证明服务活着，不触库。再确认 schema 与种子已就位（`/health` 通过但表缺失时，触库的 `/api` 调用会 500）：

```bash
cd /opt/ai-polish/backend
.venv/bin/alembic current           # 应停在 head（fbc9cd7b51eb）
```

浏览器访问服务器 IP 应看到卡密输入页。后端日志：`journalctl -u ai-polish -f`。

## 5. 内存约束（512MB）

- systemd `MemoryMax=256M` 限制后端上限，OOM 由 systemd 重启兜底；
- `PrivateTmp=true` 隔离临时目录（docx 等文件输出不污染系统 /tmp）；
- SQLite 连接 PRAGMA 限 `mmap_size≈30MB`（M1 后续票落地）；
- 不部署 Docker / Redis / 监控栈（见重写计划 §8）。

## 6. 依赖升级流程

直接依赖声明在 `backend/requirements.in`（运行时）与 `backend/requirements-dev.in`（含测试），锁定版本由 pip-compile 生成：

```bash
pip install pip-tools
cd backend
python -m piptools compile requirements.in && python -m piptools compile requirements-dev.in
```

提交生成的 `requirements.txt` / `requirements-dev.txt`，服务器上的环境即与锁定版本完全一致。**不要手改两个 lockfile。**

## 7. HTTPS（上线前必做）

Nginx 基线默认 HTTPS：80 端口只做 ACME 验证与 301 跳转，业务流量全部走 443（`deploy/nginx/ai-polish.conf`），管理端 JWT cookie 与卡密传输不落明文。443 块随配置附带但整体注释——证书在签发前不存在，先启用会让 `nginx -t` 失败、deploy.sh 中止。证书就绪前站点经 80 一律跳向尚不可用的 HTTPS，属预期。

```bash
# 1. 签发证书（webroot 走 80 端口的 ACME 验证路径；<域名> 换成实际域名）
sudo apt install -y certbot
sudo mkdir -p /var/www/certbot
sudo certbot certonly --webroot -w /var/www/certbot -d <域名>

# 2. 启用 443：取消 ai-polish.conf 中 443 server 块的注释，核对证书路径
sudo nginx -t && sudo systemctl reload nginx
```

续期由 certbot 的 systemd timer 自动完成（webroot 路径在签发时已记入续期配置）；为让新证书即时生效，启用 443 后执行 `sudo certbot renew --deploy-hook "systemctl reload nginx"` 把 reload 挂进续期流程。
