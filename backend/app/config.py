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


settings = Settings()
