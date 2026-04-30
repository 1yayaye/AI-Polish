from pydantic_settings import BaseSettings
from pydantic import model_validator
from typing import Optional
import os
import secrets as _secrets


def get_app_dir():
    """获取应用根目录（backend/），用于定位 .env 和数据库文件"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_env_file_path():
    """获取 .env 文件路径"""
    return os.path.join(get_app_dir(), '.env')


def get_default_database_url():
    """获取默认数据库 URL，指向 backend/ 目录"""
    app_dir = get_app_dir()
    db_path = os.path.join(app_dir, 'ai_polish.db')
    return f"sqlite:///{db_path}"


class Settings(BaseSettings):
    DEPLOYMENT_PROFILE: str = "standard"

    # 服务器配置
    SERVER_HOST: str = "0.0.0.0"
    SERVER_PORT: int = 9800
    UVICORN_ACCESS_LOG: bool = True

    # CORS 允许的源（逗号分隔），* 表示允许所有
    CORS_ORIGINS: str = "http://localhost:5174,http://localhost:9800,http://127.0.0.1:9800"

    # 数据库配置 - 默认使用 backend/ 目录
    DATABASE_URL: str = get_default_database_url()
    
    # OpenAI API 配置 (全局 fallback，模型通过 ModelProfile 管理)
    OPENAI_API_KEY: str = "pwd"
    OPENAI_BASE_URL: str = "http://IP:PORT/v1"
    
    # 并发配置
    MAX_CONCURRENT_USERS: int = 5
    WORD_FORMATTER_MAX_CONCURRENT_JOBS: int = 5
    WORD_FORMATTER_JOB_RETENTION_HOURS: int = 24
    MIN_FREE_MEMORY_MB: int = 0
    DEFAULT_USAGE_LIMIT: int = 1
    WORKSPACE_PRICE_PER_10K_CENTS: int = 0
    SEGMENT_SKIP_THRESHOLD: int = 15

    # Word Formatter upload and text limits.
    MAX_UPLOAD_FILE_SIZE_MB: int = 20
    MAX_TEXT_INPUT_CHARS: int = 200000
    
    # 会话压缩配置
    HISTORY_COMPRESSION_THRESHOLD: int = 5000  # 汉字数量阈值
    COMPRESSION_MODEL: str = "gpt-5"
    COMPRESSION_API_KEY: Optional[str] = None
    COMPRESSION_BASE_URL: Optional[str] = None

    # 流式输出配置
    USE_STREAMING: bool = False  # 默认使用非流式模式，避免被API阻止

    # API 请求间隔（秒），用于避免触发 RATE_LIMIT
    API_REQUEST_INTERVAL: int = 6

    # 思考模式配置
    THINKING_MODE_ENABLED: bool = True  # 默认启用思考模式
    THINKING_MODE_EFFORT: str = "high"  # 思考强度: none, low, medium, high, xhigh

    # AI 调试日志（默认关闭，避免记录用户正文和模型输出）
    AI_DEBUG_LOGGING: bool = False
    
    # JWT 密钥
    SECRET_KEY: str = "your-secret-key-change-this-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    # 管理员账户
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"

    @model_validator(mode="after")
    def apply_deployment_profile_defaults(self):
        if (self.DEPLOYMENT_PROFILE or "").lower() != "low_memory":
            return self

        low_memory_defaults = {
            "MAX_CONCURRENT_USERS": 1,
            "WORD_FORMATTER_MAX_CONCURRENT_JOBS": 1,
            "WORD_FORMATTER_JOB_RETENTION_HOURS": 1,
            "MIN_FREE_MEMORY_MB": 128,
            "MAX_UPLOAD_FILE_SIZE_MB": 5,
            "MAX_TEXT_INPUT_CHARS": 50000,
            "UVICORN_ACCESS_LOG": False,
            "HISTORY_COMPRESSION_THRESHOLD": 2000,
        }
        explicit_fields = set(self.model_fields_set)
        for field_name, default_value in low_memory_defaults.items():
            if field_name not in explicit_fields:
                setattr(self, field_name, default_value)
        return self
    
    class Config:
        env_file = get_env_file_path()
        case_sensitive = True
        extra = "ignore"


# 加载 backend/ 目录下的 .env 文件
_env_path = get_env_file_path()
if os.path.exists(_env_path):
    from dotenv import load_dotenv
    load_dotenv(_env_path)

settings = Settings()


def _auto_fix_default_credentials():
    """检测到默认凭据时自动生成随机值并写入 .env"""
    env_path = get_env_file_path()
    needs_update = False
    updates = {}

    if settings.SECRET_KEY == "your-secret-key-change-this-in-production":
        new_key = _secrets.token_urlsafe(32)
        updates["SECRET_KEY"] = new_key
        settings.SECRET_KEY = new_key
        needs_update = True

    if settings.ADMIN_PASSWORD == "admin123":
        new_password = _secrets.token_urlsafe(18)
        updates["ADMIN_PASSWORD"] = new_password
        settings.ADMIN_PASSWORD = new_password
        needs_update = True

    if not needs_update:
        return

    # 读取现有 .env 内容
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    # 更新已有的 key 或追加
    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.rstrip("\r\n")
        if "=" in stripped and not stripped.strip().startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.add(key)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}\n")

    # 确保目录存在
    os.makedirs(os.path.dirname(env_path) or ".", exist_ok=True)
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    # 同步到环境变量
    for key, value in updates.items():
        os.environ[key] = value

    print("\n" + "=" * 60)
    print("[Auth] Auto-generated credentials written to .env:")
    for key in updates:
        print(f"   {key} = {updates[key][:8]}...")
    print(f"   配置文件: {env_path}")
    print("=" * 60 + "\n")


_auto_fix_default_credentials()


def reload_settings():
    """重新加载配置 - 直接更新现有 settings 对象的属性"""
    global settings
    
    # 重新读取 .env 文件到环境变量
    env_path = get_env_file_path()
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    os.environ[key] = value
                    
                    # 直接更新 settings 对象的属性
                    if hasattr(settings, key):
                        # 获取字段类型并转换
                        field_type = type(getattr(settings, key))
                        try:
                            if field_type == int:
                                setattr(settings, key, int(value))
                            elif field_type == bool:
                                setattr(settings, key, value.lower() in ('true', '1', 'yes'))
                            else:
                                setattr(settings, key, value)
                        except (ValueError, TypeError):
                            setattr(settings, key, value)
    
    return settings

