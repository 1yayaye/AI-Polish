#!/usr/bin/env bash
set -euo pipefail

# --- 参数解析 ---
CHECK_ONLY=false
REBUILD=false
LOW_MEMORY=false

for arg in "$@"; do
    case "$arg" in
        --check-only) CHECK_ONLY=true ;;
        --rebuild)    REBUILD=true ;;
        --low-memory) LOW_MEMORY=true ;;
        -h|--help)
            echo "Usage: $0 [--check-only] [--rebuild] [--low-memory]"
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown argument: $arg"
            exit 1
            ;;
    esac
done

# --- 工具函数 ---
step()  { echo -e "\033[34m[INFO]\033[0m $*"; }
ok()    { echo -e "\033[32m[OK]\033[0m $*"; }
fail()  { echo -e "\033[31m[ERROR]\033[0m $*"; exit 1; }

suggest_low_memory() {
    local mem_kb
    if [[ -f /proc/meminfo ]]; then
        mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
        local mem_mb=$((mem_kb / 1024))
        if (( mem_mb < 1024 )) && [[ "$LOW_MEMORY" == false ]]; then
            echo -e "\033[33m[WARN]\033[0m System has ${mem_mb}MB RAM. Consider using --low-memory for better stability."
        fi
    fi
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
BACKEND_ENV="$BACKEND_DIR/.env"
FRONTEND_DIST="$FRONTEND_DIR/dist"

# --- 项目结构检查 ---
step "Checking project layout..."
[[ -d "$BACKEND_DIR" ]]  || fail "backend directory not found."
[[ -d "$FRONTEND_DIR" ]] || fail "frontend directory not found."
ok "Project layout looks valid."
suggest_low_memory

# --- Python ---
step "Resolving Python..."
VENV_PYTHON="$BACKEND_DIR/.venv/bin/python"
if [[ -x "$VENV_PYTHON" ]]; then
    PYTHON_EXE="$VENV_PYTHON"
    ok "Using backend virtualenv Python."
else
    PYTHON_EXE="$(command -v python3 || command -v python || true)"
    [[ -n "$PYTHON_EXE" ]] || fail "Python not found. Install Python 3.9+ or create backend/.venv."
    ok "Using Python from PATH: $PYTHON_EXE"
fi

# --- .env 检查 ---
[[ -f "$BACKEND_ENV" ]] || fail "backend/.env not found. Create it from backend/.env.example."
ok "backend/.env exists."

# --- 低内存模式 ---
if [[ "$LOW_MEMORY" == true ]]; then
    step "Applying low-memory profile to .env..."
    if grep -q "^DEPLOYMENT_PROFILE=" "$BACKEND_ENV"; then
        sed -i 's/^DEPLOYMENT_PROFILE=.*/DEPLOYMENT_PROFILE=low_memory/' "$BACKEND_ENV"
    else
        echo "DEPLOYMENT_PROFILE=low_memory" >> "$BACKEND_ENV"
    fi
    ok "DEPLOYMENT_PROFILE=low_memory applied."
fi

# --- 后端依赖检查 ---
step "Checking backend dependencies..."
if ! "$PYTHON_EXE" -c "import fastapi, uvicorn, sqlalchemy, pydantic, dotenv, openai" 2>/dev/null; then
    fail "Backend dependencies missing. Run: cd backend && pip install -r requirements.txt"
fi
ok "Backend dependencies are importable."

# --- 前端构建 ---
if [[ ! -f "$FRONTEND_DIST/index.html" ]] || [[ "$REBUILD" == true ]]; then
    step "Building frontend..."
    NPM_EXE="$(command -v npm || true)"
    [[ -n "$NPM_EXE" ]] || fail "npm not found."
    (cd "$FRONTEND_DIR" && "$NPM_EXE" ci && "$NPM_EXE" run build)
    ok "Frontend built."
else
    ok "Frontend dist already exists (use --rebuild to force)."
fi

[[ -f "$FRONTEND_DIST/index.html" ]] || fail "frontend/dist/index.html not found."

if [[ "$CHECK_ONLY" == true ]]; then
    ok "CheckOnly completed."
    exit 0
fi

# --- 启动 ---
step "Starting backend with static serving..."
cd "$BACKEND_DIR"
exec "$PYTHON_EXE" -m app.main --serve-static --static-dir "$FRONTEND_DIST"
