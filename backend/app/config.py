"""配置加载：.env 是唯一配置来源（ADR-0005）。

后端不提供任何数据库/后台改配置的路径；改配置 = 改 .env + 重启服务。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """全部服务配置。新增配置项只改这里，并在 backend/.env.example 补样例。"""

    model_config = SettingsConfigDict(
        env_file=_BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # sqlite:/// 路径相对于 backend/ 工作目录；生产环境由部署文档固定为 data/app.db
    database_url: str = "sqlite:///data/app.db"

    # 管理端登录凭据与 JWT 签名密钥（ADR-0005：与全部配置同源，改凭据 = 改 .env + 重启）。
    # 三项任一为空即视为"管理端未配置"，登录一律拒绝。
    admin_username: str = ""
    admin_password: str = ""
    jwt_secret: str = ""

    # 管理端会话 cookie 的 Secure 标志：生产走 HTTPS 保持默认 true；
    # 仅本地纯 HTTP 调试时改为 false（浏览器对 localhost 豁免 Secure 限制，
    # TestClient 等非浏览器客户端不豁免）
    admin_cookie_secure: bool = True

    # 模型接入（ADR-0005：运营商自备，全部来自 .env，无任何数据库配置路径；
    # 会话表只落只读 model_snapshot，不落 key/base_url）。三项齐备才启动
    # 润色 Worker，缺项视为"流水线未配置"（本地开发/纯计费部署可安全跳过）。
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # 僵尸恢复协议（app.recovery）：同一会话最多续跑 recovery_max_attempts
    # 次（每次崩溃重启 +1），再崩即兜底退款；running 会话超过
    # recovery_stuck_after_seconds 无任何推进由巡检协程兜底退款；
    # recovery_patrol_interval_seconds 是巡检轮询间隔。
    recovery_max_attempts: int = 3
    recovery_stuck_after_seconds: int = 2 * 60 * 60
    recovery_patrol_interval_seconds: float = 300.0

    # 单次会话提交文本的字符数上限（len 口径，非计费字数）：资源护栏，
    # 兼顾整篇论文场景（5 万字 ≈ 一篇硕士论文全文）；超限创建请求 422。
    # 调大时须同步调大 deploy/nginx/ai-polish.conf 的 client_max_body_size
    max_session_text_chars: int = 50_000


settings = Settings()
