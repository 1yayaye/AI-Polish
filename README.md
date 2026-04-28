## AI 学术写作助手

## 快速开始

### 环境要求

- Python 3.9+
- Node.js 18+

### 后端启动

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env  # 编辑 .env 填入 API Key 等配置
python -m app.main
```

后端默认运行在 `http://localhost:9800`，API 文档: `http://localhost:9800/docs`

### 前端启动

```bash
cd frontend
npm install
npm run dev
```

前端默认运行在 `http://localhost:5174`，通过 Vite proxy 自动转发 API 请求到后端。

### 合并部署（可选）

如果需要后端直接 serve 前端静态文件（单端口部署）：

```bash
cd frontend && npm run build && cd ..
cd backend
python -m app.main --serve-static --open-browser
```

或通过环境变量：

```bash
SERVE_STATIC=true python -m app.main
```

> 💡 数据库文件 `ai_polish.db` 和配置文件 `.env` 保存在 `backend/` 目录下。

### 配置文件说明

`.env` 配置文件包含以下重要配置项：

```properties
# 数据库配置
DATABASE_URL=sqlite:///./ai_polish.db
# 或使用 PostgreSQL: postgresql://user:password@IP/ai_polish

# Redis 配置 (用于并发控制和队列)
REDIS_URL=redis://IP:6379/0

# OpenAI API 配置
OPENAI_API_KEY=KEY
OPENAI_BASE_URL=http://IP:PORT/v1

# 第一阶段模型配置 (论文润色) - 推荐使用 gemini-2.5-pro
POLISH_MODEL=gemini-2.5-pro
POLISH_API_KEY=KEY
POLISH_BASE_URL=http://IP:PORT/v1

# 第二阶段模型配置 (原创性增强) - 推荐使用 gemini-2.5-pro
ENHANCE_MODEL=gemini-2.5-pro
ENHANCE_API_KEY=KEY
ENHANCE_BASE_URL=http://IP:PORT/v1

# 感情文章润色模型配置 - 推荐使用 gemini-2.5-pro
EMOTION_MODEL=gemini-2.5-pro
EMOTION_API_KEY=KEY
EMOTION_BASE_URL=http://IP:PORT/v1

# 并发配置
MAX_CONCURRENT_USERS=7

# 会话压缩配置
HISTORY_COMPRESSION_THRESHOLD=2000
COMPRESSION_MODEL=gemini-2.5-pro
COMPRESSION_API_KEY=KEY
COMPRESSION_BASE_URL=http://IP:PORT/v1

# 流式输出配置（推荐保持默认值）
USE_STREAMING=false  # 默认禁用，避免某些API（如Gemini）返回阻止错误

# JWT 密钥
SECRET_KEY=JWT-key
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60

# 管理员账户
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
DEFAULT_USAGE_LIMIT=1
SEGMENT_SKIP_THRESHOLD=15
```

**注意:** 
- 推荐使用 Google Gemini 2.5 Pro 模型以获得更好的性能和成本效益
- BASE_URL 使用 OpenAI 兼容格式，需要配置支持 OpenAI API 格式的代理服务
- **流式输出默认禁用**：为避免某些 API（如 Gemini）返回阻止错误，系统默认使用非流式模式。可在管理后台的"系统配置"中切换

### 访问地址

- 用户界面: http://localhost:9800（需启用 --serve-static）或 http://localhost:5174（开发模式）
- 管理后台: http://localhost:9800/admin（需启用后端静态托管；一键启动脚本已默认启用）
- API 文档: http://localhost:9800/docs

## 功能特性

- **双阶段优化**: 论文润色 + 学术增强
- **智能分段**: 自动识别标题，跳过短段落
- **使用限制**: 卡密系统，可配置使用次数
- **并发控制**: 队列管理，动态调整并发数
- **实时配置**: 修改配置无需重启服务
- **数据管理**: 可视化数据库管理界面

## 管理后台

访问 `http://localhost:9800/admin` 使用管理员账户登录。后端需启用静态托管；一键启动脚本已默认启用。前端开发地址不提供管理员入口。

### 功能模块
- 📊 **数据面板**: 用户统计、会话分析
- 👥 **用户管理**: 卡密生成、使用次数控制
- 📡 **会话监控**: 实时会话状态监控
- 💾 **数据库管理**: 查看、编辑、删除数据记录
- ⚙️ **系统配置**: 模型配置、并发设置、使用限制

## 核心配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `MAX_CONCURRENT_USERS` | 最大并发用户数 | 5 |
| `DEFAULT_USAGE_LIMIT` | 新用户默认使用次数 | 1 |
| `SEGMENT_SKIP_THRESHOLD` | 段落跳过阈值（字符数） | 15 |
| `HISTORY_COMPRESSION_THRESHOLD` | 历史压缩阈值 | 5000 |
| `USE_STREAMING` | 启用流式输出模式 | false（推荐）|

## 项目结构

```
BypassAIGC/
├── backend/               # FastAPI 后端
│   ├── app/
│   │   ├── main.py        # 后端入口
│   │   ├── config.py      # 配置管理
│   │   ├── database.py    # 数据库初始化
│   │   ├── schemas.py     # Pydantic 模型
│   │   ├── routes/        # API 路由
│   │   ├── services/      # 业务逻辑
│   │   ├── models/        # 数据模型
│   │   ├── utils/         # 工具函数
│   │   └── word_formatter/ # Word 格式化模块
│   ├── test/              # 测试
│   ├── requirements.txt   # Python 依赖
│   ├── .env.example       # 配置模板
│   └── .env               # 环境配置（需创建）
├── frontend/              # React 前端
│   ├── src/
│   │   ├── pages/         # 页面组件
│   │   ├── components/    # 通用组件
│   │   ├── api/           # API 调用
│   │   └── main.jsx       # 入口
│   ├── package.json       # Node 依赖
│   └── vite.config.js     # Vite 配置
└── README.md              # 本文件
```



**⚠️ 重要提示**: 生产环境部署前，请务必:
1. 复制 `backend/.env.example` 为 `backend/.env` 并编辑
2. 修改 `.env` 中的默认管理员密码
3. 生成强 SECRET_KEY (至少 32 字节随机字符串)
4. 填写有效的 API_KEY

## 常见问题

## 512MB 低内存部署

单核 512MB 服务器推荐使用 `systemd + Nginx`：Nginx 直接托管 `frontend/dist`，后端只运行 API，不要使用 `--serve-static` 或 `--open-browser`。

核心 `.env` 建议：

```properties
DEPLOYMENT_PROFILE=low_memory
MAX_CONCURRENT_USERS=1
WORD_FORMATTER_MAX_CONCURRENT_JOBS=1
WORD_FORMATTER_JOB_RETENTION_HOURS=1
MIN_FREE_MEMORY_MB=128
MAX_UPLOAD_FILE_SIZE_MB=5
MAX_TEXT_INPUT_CHARS=50000
UVICORN_ACCESS_LOG=false
AI_DEBUG_LOGGING=true
```

完整 systemd 与 Nginx 示例见 `docs/deployment-512mb.md`。保留 `AI_DEBUG_LOGGING=true` 时，请同时配置 journald/Nginx 日志轮转，避免日志占满磁盘。

**Q: 端口被占用？**
A: 关闭其他占用 9800 端口的程序，或在 `.env` 中修改 `SERVER_PORT`

**Q: 配置修改后未生效？**  
A: 重启程序使配置生效

**Q: 登录失败？**
A: 检查 `.env` 中的 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD`

**Q: AI 调用失败？**
A: 检查 API Key 和 Base URL 配置是否正确

**Q: Gemini API 返回 "Your request was blocked" 错误？**
A: 这是因为 Gemini API 可能阻止流式请求。解决方法：
1. 登录管理后台 (`http://localhost:9800/admin`)
2. 进入"系统配置"标签页
3. 找到"流式输出模式"开关，确保它是**禁用**状态（推荐）
4. 点击"保存配置"按钮
5. 重新运行优化任务

默认配置已经禁用了流式输出，如果仍然遇到此问题，请检查 `backend/.env` 文件中的 `USE_STREAMING` 设置是否为 `false`




















