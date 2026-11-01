"""应用入口：FastAPI 实例与路由装配。

路由约定：业务 API 全部挂在 /api 前缀下（Nginx 按此反代）；/health 是
进程级存活探针，供 systemd 与 Nginx 健康检查直接访问，不参与业务。
"""

from fastapi import FastAPI

app = FastAPI(
    title="AI-Polish",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
