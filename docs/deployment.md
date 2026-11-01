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

脚本依次完成：构建前端（构建内存上限 384MB，防止 512MB 机器 OOM）→ 按 `backend/requirements.txt` 锁定版本安装后端依赖到 `.venv` → 安装 systemd unit 与 Nginx 配置 → 重启服务 → 对 `127.0.0.1:8000/health` 与 Nginx 层 `/health` 做健康检查。任何一步失败即中止。

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

当前样例为 HTTP 基线。绑定域名后用 certbot 签发证书并开启 443，管理端 JWT cookie 与卡密传输都应走 TLS：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d <域名>
```
