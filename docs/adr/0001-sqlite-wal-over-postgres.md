# SQLite（WAL 模式）而非 PostgreSQL

生产服务器为 1 核 / 512MB 内存。PostgreSQL 常驻需 50–100MB，而本服务并发规模（峰值约 50 用户、单写者场景）远低于其收益门槛。决定使用 SQLite 并开启 WAL 模式，配合 Alembic 做正式迁移（旧版声明了 Alembic 却从未使用，手写迁移是已知的技术债）。若未来迁到更大机器且需要多实例，再重估此决策。
