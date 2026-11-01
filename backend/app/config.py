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


settings = Settings()
