# AI-Polish 重构计划

> 状态：已与项目所有者达成共识（2026-11-01）
> 修订：v2 并入两位外部评委的评审结论（采纳项已逐条对照源码验证，驳回项见 §9）
> 术语表：[GLOSSARY.md](../GLOSSARY.md) · 架构决策：[docs/adr/](./adr/)

## 1. 背景与动机

AI-Polish 是面向中文论文作者的 AI 学术写作服务（两阶段润色流水线 + Word 排版工具），目标是**在 Linux 服务器上作为小型商业服务运作**。

现有代码（~22k LOC：FastAPI 后端 + React 前端）存在致命缺陷，无法承载商业运营：

- **零测试**，核心计费与流水线无任何行为保护
- **运行时状态全在单进程内存**（并发队列、SSE、排版任务），且 QueueStatus 表与内存队列**双轨记账**，重启即丢、两源必漂移
- **明文密钥落库**（会话 API key、模型 key）、管理端密码走 URL query 参数
- **双配置源**（.env 与数据库 system_settings 需人工对齐）
- Alembic 装而不用，手写迁移烂摊子；1,429 行的 AdminDashboard 巨型组件
- word_formatter 的 JobManager 将 docx 字节流驻留内存最长 24 小时，同步编译器阻塞事件循环
- 为绕过 EventSource 无法带 header 的限制，专门维护了一套 120 秒短时资源令牌系统

结论：**推倒重写**（同仓库新分支），唯一例外是 word_formatter 子系统作为库整体移植（ADR-0004）。

## 2. 目标环境与约束

| 项 | 值 |
|---|---|
| 服务器 | 1 核 / 512MB 内存 Linux VPS |
| 并发目标 | 峰值 ~50 用户，日处理几十篇文档 |
| 部署 | 裸机 systemd + Nginx（ADR-0002） |
| 设计纪律 | 单节点，但持久状态唯一事实源是数据库，未来扩容不推翻设计（ADR-0003） |

## 3. 商业模型（保持不变）

- **访问**：卡密制。用户匿名、无密码，卡密即全部身份凭证
- **双轨额度**（经确认保留）：
  - **余额（分）**：Workspace 润色按万字计费扣减，任务失败自动退款
  - **次数额度**：Word Formatter 按次预留/释放（`reserve/release` 语义）
  - 两条路径统一在**同一个计费服务**内实现与测试，共享事务边界与账本，禁止各写各的
- **充值**：运营商手工操作，不接支付网关
- **模型接入**：运营商自备全部 LLM API key，用户永远不接触 key（ADR-0005）
- 商业规则在有真实流水数据之前不做变更

## 4. 功能范围

| 功能 | 处置 |
|---|---|
| Workspace 两阶段润色流水线，处理模式三种：`paper_polish` / `paper_enhance` / `paper_polish_enhance`（**`emotion_polish` 砍掉**，经确认） | **重写** |
| Workspace 导出 | txt + **新增 docx 导出**（复用 word_formatter 的 compiler+renderer，见 M2） |
| Word Formatter（排版规格生成 / 文章预处理 / 格式检查 / docx 渲染） | **整体移植，不重写**（ADR-0004） |
| 卡密访问 + 管理后台（用户/余额/会话监控/系统配置） | **重写** |
| 数据迁移 | **迁**：卡密用户 + 余额 + 次数额度 + 账本 + custom_prompts + saved_specs；`is_system=True` 的调优 Prompt 另行固化为代码种子。**不迁**：旧会话、变更记录、SessionHistory。迁移后硬校验 `SUM(账本流水) == 用户当前余额`，脚本幂等可重跑 |

## 5. 技术决策总表

| 领域 | 决策 | 依据 |
|---|---|---|
| 后端 | Python + FastAPI | docx/OOXML 依赖链（python-docx/lxml/mistune）无跨语言替代；瓶颈是 LLM 等待而非 CPU |
| 前端 | React + TypeScript（Vite）+ 组件库 | JSX→TSX 成本低；TS 是重构安全网；拆掉巨型组件 |
| 数据库 | SQLite WAL + Alembic 正经迁移；连接时固化 PRAGMA：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`、`temp_store=MEMORY`、`mmap_size≈30MB` | Postgres 常驻 50–100MB 不值（ADR-0001）；PRAGMA 防锁与防内存爆 |
| 事务纪律 | **短事务**：读取段落 → 提交/关闭事务 → 等待 LLM → 开短事务写结果与进度。**禁止在 LLM 网络等待期间持有写事务** | 单进程异步下 SQLite 永不 `database is locked` |
| 并发/状态 | 单进程 asyncio；**砍掉 QueueStatus 表与内存队列双轨**——任务表 `status IN ('queued','running','completed','failed')` 是唯一事实源，单个 Worker 协程 `SELECT ... WHERE status='queued' ORDER BY created_at LIMIT 1` 拉取；内存只做缓存与通知 | 旧版双轨记账漂移的根治（ADR-0003） |
| SSE | 前端用 fetch-event-source 携带 `X-Card-Key` header + `Last-Event-ID`；连接时先读已完成段落快照再订阅增量；**删除旧 120s 短时令牌系统**；卡密不进 URL（防 Nginx 日志泄密） | EventSource 不能带 header 是旧版绕弯的根源 |
| 计费 | 预扣费沿用**条件原子 UPDATE + rowcount 校验**（已验证源码 `billing_service.py:67-80` 属实）；旧版退款/管理员调整是非原子 ORM 写，重写时**统一收口到账本原子路径**；账本表用无外键的 `task_id`（迁移旧流水不因缺旧会话而断裂） | 评委冲突裁决结论 |
| 配置 | .env 唯一来源；后台模型状态只读展示；会话表**删除全部 `*_api_key` / `*_base_url` 字段**，只留只读 `model_snapshot` 便于排查客诉 | ADR-0005 |
| 安全 | 管理端 JWT header / HttpOnly cookie；消灭 query 传密码；无用户侧 key 落库 | 修复明文/泄密硬伤 |
| 部署 | systemd + Nginx；lockfile（uv / pip-tools）锁依赖；部署脚本；docx 输出目录用 `PrivateTmp=true` + 定时清理 | Docker 守护进程税 ~100–150MB 不可接受（ADR-0002） |
| word_formatter 适配 | 生成的 .docx **落盘不驻留内存**（DB 只存任务状态与文件路径）；`compile_document` 走 `asyncio.to_thread` + `Semaphore(1)`（同时只编译一个文档） | 512MB OOM 高危点；防阻塞 SSE 心跳 |
| 日志 | 结构化 JSON → journald + 健康端点；账本即业务监控 | 512MB 不养监控栈 |
| 测试 | pytest 覆盖**计费账本（双轨两条路径）、用量预留、流水线状态机（含僵尸恢复）、卡密认证**（mock LLM）+ 少量集成测试 + CI；前端不写单测，靠 TS + 手动验收 | 火力集中在算钱和不丢数据 |

## 6. 里程碑

> 原则：每一步结束系统都可用、可部署、可收钱，不存在"写完 80% 才能上线"的悬崖。

### M1 — 骨架 + 认证 + 计费（能收钱的最小系统）

- 新仓库结构（backend/ + frontend/，新分支）
- SQLite WAL + 全套 PRAGMA + Alembic 初始迁移；短事务纪律写进数据访问层约定
- 表：用户/卡密/余额/次数额度/账本（账本 `task_id` 无外键设计）
- 卡密认证中间件；管理端登录（JWT/cookie）
- 计费服务：**双轨**（余额扣减/退款 + 次数预留/释放），全部条件原子 UPDATE——**双轨都带 pytest**
- 一次性迁移脚本：迁用户 + 双轨额度 + 账本 + custom_prompts + saved_specs，`is_system` Prompt 固化为代码种子；**硬校验 SUM(账本) == 余额**，脚本幂等
- systemd + Nginx 部署链路一次跑通（lockfile、部署脚本、PrivateTmp）
- **验收**：部署到生产机，卡密可访问、双轨额度可扣可退、账本对得上、重启服务无状态丢失

### M2 — Workspace 润色流水线

- 三种处理模式（`emotion_polish` 不迁）逐段处理管线，重写实现
- 任务表状态机（唯一事实源）+ 单 Worker 协程拉取——**带 pytest（mock LLM）**
- **僵尸恢复协议**（本次评审新增）：
  1. lifespan 启动钩子扫描所有 `queued`/`running` 会话，从首个未完成段落（`segment.status != 'completed'`）续跑
  2. 续跑超最大重试次数、或任务滞留异常状态超阈值（如 2 小时），由启动恢复器或巡检协程**兜底退款**——任何进程崩溃不得吞掉用户预付余额
- SSE：快照续传（连接时先读已完成段落，再订阅增量）；fetch-event-source + header 认证
- 变更记录；txt 导出
- **引入 word_formatter 的 compiler + renderer 作为纯函数库**（解决 M2 需要 docx 渲染而完整移植在 M4 的依赖倒挂），实现 docx 导出
- **验收**：完整跑通一篇论文；中途 `kill -9` 进程后重启，任务从断点续跑、余额分毫不差

### M3 — 管理后台

- React + TS 重写管理界面（组件库拆分）
- 卡密批量生成、双轨额度充值/调整、用量管理；会话监控
- 系统配置面（.env 只读展示 + 模型状态只读 + `model_snapshot` 客诉排查）
- **验收**：运营商日常操作（发卡、充值、查看流水）全部在后台完成

### M4 — word_formatter 整体移植

- 范围（评审修正后）：**只迁 API 路由、JobManager 适配层、前端三页**——渲染核心已在 M2 作为纯函数库引入
- JobManager 改造：任务状态落 SQLite、输出 docx 落盘、`to_thread` + `Semaphore(1)` 编译
- 前端 Word Formatter 三页迁移到 TS
- **验收**：全功能回归 + 执行 M1 迁移脚本 + 旧版下线

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 512MB 内存下 Python 峰值 OOM | 继承 low_memory profile：docx 编译限并发 1、输出落盘不驻内存、systemd `MemoryMax`、`mmap_size` 限 30MB、健康端点监控 |
| 同步 docx 编译阻塞事件循环（SSE 断连） | `asyncio.to_thread` + `asyncio.Semaphore(1)`，写进架构测试约定 |
| 进程崩溃吞掉预付余额 | 僵尸恢复协议（M2）：断点续跑 + 最大重试 + 巡检兜底退款；验收标准含 `kill -9` 场景 |
| 账本迁移外键断裂 | 账本表无外键 `task_id` 设计 + 迁移硬校验 `SUM(账本) == 余额` |
| SQLite 写冲突 | WAL + busy_timeout + 短事务纪律（LLM 等待期间不持写事务） |
| word_formatter 移植后接口失配 | 当黑盒库对待：M2 先用纯函数库形态引入核心渲染并测试，M4 只做外围适配 |
| 单人重写中途失速 | 里程碑制：每步结束都可部署收钱，随时可以停在可用状态 |

## 8. 不做什么（明确排除）

- 支付网关集成（等真实流水）
- 注册账号/密码体系（卡密模型可平滑演进，现阶段不加）
- Redis / 消息队列 / 多实例横向扩展
- Prometheus/Grafana 等监控栈
- 用户自备 API key 的任何路径
- `emotion_polish` 处理模式（经确认砍掉）
- 旧 120 秒短时资源令牌系统（被 fetch-event-source + header 方案取代）
- QueueStatus 双轨记账表（任务表状态是唯一事实源）
- 旧会话、变更记录、SessionHistory 的数据迁移
- 前端单元测试

## 9. 评审记录（2026-11-01，两位外部评委）

- **采纳**（12 项）：docx 字节流落盘 + 单并发编译；僵尸任务恢复与兜底退款协议；SQLite PRAGMA 全套与短事务纪律；SSE 改 fetch-event-source + header（删短时令牌）；账本去外键 + 硬校验；custom_prompts/saved_specs 纳入迁移（经确认全迁）；M2/M4 依赖倒挂修正（渲染库提前到 M2）；砍 QueueStatus 双轨；会话表删 `*_api_key` 留 `model_snapshot`；文档同步入库
- **驳回**（1 项）：评委 2 称"billing 非原子、存在透支漏洞"——经源码验证 `billing_service.py:67-80` 为条件原子 UPDATE + rowcount 校验，断言不成立；但同文件退款/调整路径确为非原子 ORM 写，重写时统一收口
- **修正评委 1**（1 项）：其"旧版计费完全原子"的说法仅对预扣费成立，退款路径不是

## 10. 待办

- [ ] 将本计划、GLOSSARY.md、docs/adr/ 一并提交入库（避免计划内引用死链）
- [ ] M1 开工前产出详细设计：表结构 DDL、模块划分、卡密认证中间件契约
