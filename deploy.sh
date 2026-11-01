#!/usr/bin/env bash
# AI-Polish 一键部署：构建前端 + 安装后端锁定依赖 + 数据库迁移与种子 + 安装/重启 systemd 与 Nginx。
# 在服务器上以 root（或 sudo）于仓库根目录执行：sudo ./deploy.sh
# 一次性准备工作（用户、目录、.env、Nginx 安装）见 docs/deployment.md。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPECTED_DIR="/opt/ai-polish"
BACKEND_VENV="$REPO_DIR/backend/.venv"

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$REPO_DIR" != "$EXPECTED_DIR" ]; then
  log "警告：仓库位于 $REPO_DIR，而 systemd/Nginx 配置写死 $EXPECTED_DIR；路径不同请先改 deploy/ 下的配置文件。"
fi

for cmd in npm python3 systemctl nginx curl; do
  command -v "$cmd" >/dev/null || die "缺少命令：$cmd（安装步骤见 docs/deployment.md）"
done

log "构建前端（构建内存上限 384MB，适配 512MB 服务器）..."
cd "$REPO_DIR/frontend"
npm ci
NODE_OPTIONS="--max-old-space-size=384" npm run build
cd "$REPO_DIR"

log "安装后端依赖（backend/requirements.txt 为 pip-compile 锁定版本）..."
if [ ! -x "$BACKEND_VENV/bin/python" ]; then
  python3 -m venv "$BACKEND_VENV"
fi
"$BACKEND_VENV/bin/pip" install -q --upgrade pip
"$BACKEND_VENV/bin/pip" install -q -r "$REPO_DIR/backend/requirements.txt"

log "准备数据目录..."
mkdir -p "$REPO_DIR/backend/data"

# 建表与种子必须在服务重启前完成：schema 只能靠 alembic 建（应用启动不建表），
# 不跑迁移的话 /health 依旧正常，但任何触库的 /api 调用都会 500。
log "执行数据库迁移（alembic upgrade head，幂等）..."
(cd "$REPO_DIR/backend" && "$BACKEND_VENV/bin/alembic" upgrade head)

log "注入系统预设提示词种子（幂等）..."
(cd "$REPO_DIR/backend" && "$BACKEND_VENV/bin/python" -m app.seeds)

# 迁移/种子刚生成的 data/app.db 等文件归运行服务的 aipolish 所有
if id aipolish >/dev/null 2>&1; then
  chown -R aipolish:aipolish "$REPO_DIR/backend/data"
else
  log "警告：系统用户 aipolish 不存在，跳过 chown（见 docs/deployment.md 一次性准备）。"
fi

log "安装 systemd unit 与 Nginx 配置..."
install -m 644 "$REPO_DIR/deploy/systemd/ai-polish.service" /etc/systemd/system/ai-polish.service
systemctl daemon-reload
systemctl enable ai-polish >/dev/null

if [ -d /etc/nginx/sites-available ]; then
  install -m 644 "$REPO_DIR/deploy/nginx/ai-polish.conf" /etc/nginx/sites-available/ai-polish.conf
  ln -sf /etc/nginx/sites-available/ai-polish.conf /etc/nginx/sites-enabled/ai-polish.conf
else
  install -m 644 "$REPO_DIR/deploy/nginx/ai-polish.conf" /etc/nginx/conf.d/ai-polish.conf
fi
nginx -t
systemctl reload nginx 2>/dev/null || systemctl start nginx

log "重启后端服务并做健康检查..."
systemctl restart ai-polish
sleep 2
curl -fsS http://127.0.0.1:8000/health >/dev/null || die "后端 /health 未就绪，查看日志：journalctl -u ai-polish -n 50"
curl -fsS http://127.0.0.1/health >/dev/null || die "Nginx 层 /health 未通过，检查 Nginx 配置"

log "部署完成：后端 /health 正常，Nginx 已伺服前端静态产物。"
