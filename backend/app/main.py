from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import os
import argparse
from typing import Optional

# 先导入 config 以便加载环境变量
from app.config import settings
from app.database import init_db
from app.routes import access_tokens, admin, prompts, optimization
from app.word_formatter import router as word_formatter_router
from app.word_formatter.services import get_job_manager
from app.services.concurrency import concurrency_manager
from app.services.resource_guard import get_resource_status
from app.models.models import CustomPrompt
from app.database import SessionLocal
from app.services.ai_service import get_default_polish_prompt, get_default_enhance_prompt


# 响应缓存头中间件 - 优化浏览器缓存
class CacheControlMiddleware(BaseHTTPMiddleware):
    """添加缓存控制头，优化浏览器缓存"""

    # 可缓存的静态资源路径
    CACHEABLE_PATHS = {
        "/api/prompts/system": 300,  # 系统提示词缓存5分钟
        "/api/health/models": 60,    # 模型健康检查缓存1分钟
        "/health": 30,               # 健康检查缓存30秒
    }

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # 只对 GET 请求添加缓存头
        if request.method == "GET":
            path = request.url.path
            # 检查是否是可缓存的路径
            for cacheable_path, max_age in self.CACHEABLE_PATHS.items():
                if path.endswith(cacheable_path):
                    response.headers["Cache-Control"] = f"public, max-age={max_age}"
                    break
            else:
                # 默认不缓存动态内容
                if "/api/" in path:
                    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"

        return response

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：替代已弃用的 @app.on_event"""
    # === 启动逻辑 ===
    init_db()
    job_manager = get_job_manager()
    await job_manager.start_cleanup_loop()

    db = SessionLocal()
    try:
        polish_prompt = db.query(CustomPrompt).filter(
            CustomPrompt.is_system.is_(True),
            CustomPrompt.stage == "polish"
        ).first()

        if not polish_prompt:
            polish_prompt = CustomPrompt(
                name="默认润色提示词",
                stage="polish",
                content=get_default_polish_prompt(),
                is_default=True,
                is_system=True
            )
            db.add(polish_prompt)

        enhance_prompt = db.query(CustomPrompt).filter(
            CustomPrompt.is_system.is_(True),
            CustomPrompt.stage == "enhance"
        ).first()

        if not enhance_prompt:
            enhance_prompt = CustomPrompt(
                name="默认增强提示词",
                stage="enhance",
                content=get_default_enhance_prompt(),
                is_default=True,
                is_system=True
            )
            db.add(enhance_prompt)

        db.commit()
    finally:
        db.close()

    yield  # 应用运行期间

    # === 关闭逻辑 ===
    job_manager = get_job_manager()
    await job_manager.shutdown()


app = FastAPI(
    title="AI 论文润色增强系统",
    description="高质量论文润色与原创性学术表达增强",
    version="1.0.0",
    lifespan=lifespan
)

# 添加 Gzip 压缩中间件以减少响应体积
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 添加缓存控制中间件
app.add_middleware(CacheControlMiddleware)

# CORS 配置
_cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()] if settings.CORS_ORIGINS != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由（添加 /api 前缀）
app.include_router(admin.router, prefix="/api")
app.include_router(access_tokens.router, prefix="/api")
app.include_router(prompts.router, prefix="/api")
app.include_router(optimization.router, prefix="/api")
app.include_router(word_formatter_router, prefix="/api")

@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "AI 论文润色增强系统 API",
        "version": "1.0.0",
        "docs": "/docs"
    }


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy"}


@app.get("/api/health/resources")
async def resource_health_check():
    """Lightweight resource health check without external API calls."""
    job_manager = get_job_manager()
    return {
        **get_resource_status(),
        "optimization_active_sessions": concurrency_manager.get_active_count(),
        "word_formatter_jobs": job_manager.get_stats(),
    }


def _check_url_format(base_url: Optional[str]) -> tuple:
    """检查 URL 格式是否正确
    
    Returns:
        tuple: (is_valid, error_message)
    """
    import re
    
    if not base_url or not base_url.strip():
        return False, "Base URL 未配置"
    
    # 验证 base_url 是否符合 OpenAI API 格式
    # 使用更严格的 URL 验证模式
    url_pattern = re.compile(r'^https?://[^\s/$.?#].[^\s]*$', re.IGNORECASE)
    if not url_pattern.match(base_url):
        return False, "Base URL 格式不正确，应为有效的 HTTP/HTTPS URL"
    
    return True, None


# 缓存已检查的 URL 结果，避免重复检查
_url_check_cache: dict = {}


async def _check_model_health(model_name: str, api_key: Optional[str], base_url: Optional[str]) -> dict:
    """检查单个模型配置的健康状态 - 不回显敏感配置"""
    
    try:
        # 检查必需的配置项
        if not api_key or not api_key.strip():
            return {
                "status": "unavailable",
                "error": "API Key 未配置"
            }
        
        # 先检查 URL 格式是否有效
        is_valid, error_msg = _check_url_format(base_url)
        
        if not is_valid:
            return {
                "status": "unavailable",
                "error": error_msg
            }
        
        # URL 有效时才检查缓存（此时 base_url 不为 None）
        if base_url in _url_check_cache:
            cached_result = _url_check_cache[base_url]
            result = {
                "status": cached_result["status"],
            }
            if cached_result["status"] == "unavailable":
                result["error"] = cached_result.get("error")
            return result
        
        # URL 格式正确，认为配置有效
        result = {
            "status": "available"
        }
        # 缓存检查结果
        _url_check_cache[base_url] = {"status": "available"}
        return result
        
    except Exception as e:
        error_msg = str(e) if str(e) else "未知错误"
        return {
            "status": "unavailable",
            "error": error_msg
        }


@app.get("/api/health/models")
async def check_models_health():
    """检查 AI 模型可用性 - 只验证URL格式，如果URL相同则只检查一次"""
    global _url_check_cache
    # 清空缓存以确保每次请求都重新检查
    _url_check_cache = {}
    
    results = {
        "overall_status": "healthy",
        "models": {}
    }
    
    # 检查全局 fallback 配置
    results["models"]["global"] = await _check_model_health(
        "global",
        settings.OPENAI_API_KEY,
        settings.OPENAI_BASE_URL,
    )
    if results["models"]["global"]["status"] == "unavailable":
        results["overall_status"] = "degraded"

    return results


def _setup_static_serving(static_dir: str):
    """挂载前端静态文件并添加 SPA 路由 fallback"""
    if not os.path.exists(static_dir):
        print(f"[Warning] Static dir not found: {static_dir}, API only")
        return

    # 挂载 assets 目录（JS, CSS 等）
    assets_dir = os.path.join(static_dir, 'assets')
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    index_file = os.path.join(static_dir, 'index.html')

    # SPA 路由：所有前端页面路径都返回 index.html
    spa_paths = [
        "/", "/admin", "/admin/{path:path}",
        "/workspace", "/workspace/{path:path}",
        "/word-formatter", "/word-formatter/{path:path}",
        "/session/{session_id}",
        "/access/{card_key}",
        "/spec-generator", "/spec-generator/{path:path}",
        "/article-preprocessor", "/article-preprocessor/{path:path}",
        "/format-checker", "/format-checker/{path:path}",
    ]

    for spa_path in spa_paths:
        # 避免重复注册根路径
        if spa_path == "/":
            # 覆盖默认的根路径响应
            app.routes[:] = [r for r in app.routes if not (hasattr(r, 'path') and r.path == "/" and hasattr(r, 'methods') and 'GET' in r.methods)]

        async def serve_spa(path: str = ""):
            if os.path.exists(index_file):
                return FileResponse(index_file)
            raise HTTPException(status_code=404, detail="index.html not found")

        app.add_api_route(spa_path, serve_spa, methods=["GET"])

    # 兜底：其他未匹配的路径尝试返回静态文件或 index.html
    @app.get("/{file_path:path}")
    async def serve_static(file_path: str):
        if file_path.startswith('api/') or file_path.startswith('docs') or file_path.startswith('openapi'):
            raise HTTPException(status_code=404, detail="Not found")
        full_path = os.path.join(static_dir, file_path)
        if os.path.exists(full_path) and os.path.isfile(full_path):
            return FileResponse(full_path)
        if os.path.exists(index_file):
            return FileResponse(index_file)
        raise HTTPException(status_code=404, detail="File not found")

    print(f"[Static] Serving from: {static_dir}")


def _print_runtime_limits():
    print("[Runtime] Deployment profile:", settings.DEPLOYMENT_PROFILE, flush=True)
    print("[Runtime] Max optimization concurrency:", settings.MAX_CONCURRENT_USERS, flush=True)
    print("[Runtime] Word formatter concurrency:", settings.WORD_FORMATTER_MAX_CONCURRENT_JOBS, flush=True)
    print("[Runtime] Word formatter retention hours:", settings.WORD_FORMATTER_JOB_RETENTION_HOURS, flush=True)
    print("[Runtime] Max upload MB:", settings.MAX_UPLOAD_FILE_SIZE_MB, flush=True)
    print("[Runtime] Max text chars:", settings.MAX_TEXT_INPUT_CHARS, flush=True)
    print("[Runtime] Min free memory MB:", settings.MIN_FREE_MEMORY_MB, flush=True)
    print("[Runtime] AI debug logging:", settings.AI_DEBUG_LOGGING, flush=True)
    print("[Runtime] Uvicorn access log:", settings.UVICORN_ACCESS_LOG, flush=True)


def main():
    """主入口函数"""
    parser = argparse.ArgumentParser(description="AI 学术写作助手后端")
    parser.add_argument("--serve-static", action="store_true",
                        help="启用前端静态文件服务")
    parser.add_argument("--static-dir", type=str, default=None,
                        help="前端静态文件目录路径（默认: ../frontend/dist）")
    parser.add_argument("--open-browser", action="store_true",
                        help="启动后自动打开浏览器")
    args = parser.parse_args()

    # 确定是否启用静态文件服务
    serve_static = args.serve_static or os.environ.get("SERVE_STATIC", "").lower() in ("true", "1", "yes")

    if serve_static:
        static_dir = args.static_dir or os.environ.get("STATIC_DIR")
        if not static_dir:
            # 默认使用 ../frontend/dist
            static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "dist")
        _setup_static_serving(static_dir)

    if args.open_browser:
        import threading
        import webbrowser
        import time

        def open_browser_delayed():
            time.sleep(2)
            url = f"http://localhost:{settings.SERVER_PORT}"
            print(f"\n[Browser] Opening: {url}")
            webbrowser.open(url)

        thread = threading.Thread(target=open_browser_delayed, daemon=True)
        thread.start()

    port = settings.SERVER_PORT
    host = settings.SERVER_HOST
    _print_runtime_limits()
    print(f"\n[Server] Address: http://{host}:{port}")
    print(f"[Server] API Docs: http://{host}:{port}/docs")
    print("\n按 Ctrl+C 停止服务\n")

    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=settings.UVICORN_ACCESS_LOG)


if __name__ == "__main__":
    main()
