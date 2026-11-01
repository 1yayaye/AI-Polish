# AI-Polish

面向中文论文作者的 AI 学术写作辅助小型商业服务：两阶段润色流水线（润色 + 原创性增强）与 Word 排版工具，卡密访问、预付余额计费，运营商自备全部 LLM 接入。

> 本仓库正在按 [docs/rewrite-plan.md](docs/rewrite-plan.md) 推倒重写（分支 `rewrite/m1`）：旧代码仅存在于 git 历史，不做兼容层。

## 架构

- **后端** `backend/`：Python + FastAPI，SQLite（WAL 模式）+ Alembic，单进程 asyncio；持久状态唯一事实源是数据库（[ADR-0001](docs/adr/0001-sqlite-wal-over-postgres.md)、[ADR-0003](docs/adr/0003-single-process-state-in-db.md)）
- **前端** `frontend/`：Vite + React + TypeScript；生产由 Nginx 伺服静态产物
- **部署**：systemd（MemoryMax / PrivateTmp）+ Nginx 裸机部署，不用 Docker（[ADR-0002](docs/adr/0002-systemd-bare-metal.md)），目标环境 1 核 / 512MB
- **配置**：`.env` 唯一来源，API key 只属于运营商（[ADR-0005](docs/adr/0005-operator-only-api-keys-env-single-source.md)）
- **术语**：见 [GLOSSARY.md](GLOSSARY.md)（运营商、卡密、余额、账本等业务词汇以此为唯一口径）

## 本地开发

要求：Python 3.13、Node.js 20+。

```bash
# 后端
pip install -r backend/requirements-dev.txt   # 锁定版本（含 pytest/httpx）
cd backend && python -m uvicorn app.main:app --reload

# 前端（另开终端）
npm install --prefix frontend
npm run dev --prefix frontend                  # /api 已代理到 127.0.0.1:8000
```

## 测试门禁

从仓库根目录运行（CI 同款命令）：

```bash
python -m pytest backend/tests -q
npm --prefix frontend run build    # Windows 下为 npm.cmd --prefix frontend run build
```

## 部署

见 [docs/deployment.md](docs/deployment.md)。服务器上一条命令完成构建、安装与重启：

```bash
sudo ./deploy.sh
```

## 里程碑

| 里程碑 | 内容 |
|---|---|
| M1 | 骨架 + 卡密认证 + 双轨计费 + 数据迁移（当前） |
| M2 | Workspace 润色流水线 |
| M3 | 管理后台 |
| M4 | word_formatter 整体移植 |

进度与验收标准见 [docs/rewrite-plan.md §6](docs/rewrite-plan.md)。
