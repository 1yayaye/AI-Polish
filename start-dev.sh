#!/usr/bin/env bash
set -euo pipefail

# --- 参数解析 ---
NO_BROWSER=false
CHECK_ONLY=false
SKIP_FRONTEND_BUILD=false
LOW_MEMORY=false

for arg in "$@"; do
    case "$arg" in
        --no-browser)       NO_BROWSER=true ;;
        --check-only)       CHECK_ONLY=true ;;
        --skip-frontend-build) SKIP_FRONTEND_BUILD=true ;;
        --low-memory)       LOW_MEMORY=true ;;
        -h|--help)
            echo "Usage: $0 [--no-browser] [--check-only] [--skip-frontend-build] [--low-memory]"
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
[[ -d "$BACKEND_DIR" ]]  || fail "backend directory not found. Run this script from the repository root."
[[ -d "$FRONTEND_DIR" ]] || fail "frontend directory not found. Run this script from the repository root."
ok "Project layout looks valid."
suggest_low_memory

# --- Python ---
step "Resolving runtimes..."
VENV_PYTHON="$BACKEND_DIR/.venv/bin/python"
if [[ -x "$VENV_PYTHON" ]]; then
    PYTHON_EXE="$VENV_PYTHON"
    ok "Using backend virtualenv Python."
else
    PYTHON_EXE="$(command -v python3 || command -v python || true)"
    [[ -n "$PYTHON_EXE" ]] || fail "Python not found. Install Python 3.9+ or create backend/.venv."
    ok "Using Python from PATH: $PYTHON_EXE"
fi

# --- Node / npm ---
NODE_EXE="$(command -v node || true)"
[[ -n "$NODE_EXE" ]] || fail "Node.js not found. Install Node.js 18+."
NPM_EXE="$(command -v npm || true)"
[[ -n "$NPM_EXE" ]] || fail "npm not found. Install npm with Node.js."
ok "Node and npm are available."

# --- .env 检查 ---
step "Checking configuration..."
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

# --- 前端依赖检查 ---
[[ -f "$FRONTEND_DIR/package.json" ]] || fail "frontend/package.json not found."
[[ -d "$FRONTEND_DIR/node_modules" ]] || fail "frontend/node_modules not found. Run: cd frontend && npm install"
ok "Frontend dependencies directory exists."

# --- 前端构建 ---
if [[ "$SKIP_FRONTEND_BUILD" == false ]]; then
    step "Building frontend dist..."
    (cd "$FRONTEND_DIR" && "$NPM_EXE" run build)
    ok "Frontend dist was rebuilt."
else
    step "Skipping frontend build (--skip-frontend-build)."
fi

[[ -f "$FRONTEND_DIST/index.html" ]] || fail "frontend/dist/index.html not found. Run: cd frontend && npm run build"
ok "Frontend dist is available."

# --- 端口检查 ---
check_port() {
    local port=$1
    if command -v lsof &>/dev/null; then
        if lsof -i :"$port" -sTCP:LISTEN &>/dev/null; then
            fail "Port $port is already in use."
        fi
    elif command -v ss &>/dev/null; then
        if ss -tlnp 2>/dev/null | grep -q ":$port "; then
            fail "Port $port is already in use."
        fi
    fi
}

step "Checking ports..."
check_port 9800
check_port 5174
ok "Ports 9800 and 5174 are free."

if [[ "$CHECK_ONLY" == true ]]; then
    ok "CheckOnly completed. No services were started."
    exit 0
fi

# --- 清理函数 ---
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
    echo ""
    step "Shutting down..."
    [[ -n "$FRONTEND_PID" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
    [[ -n "$BACKEND_PID" ]]  && kill "$BACKEND_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    ok "All services stopped."
}
trap cleanup EXIT INT TERM

# --- 启动后端 ---
step "Starting backend..."
cd "$BACKEND_DIR"
"$PYTHON_EXE" -m app.main --serve-static --static-dir "$FRONTEND_DIST" &
BACKEND_PID=$!
ok "Backend started (PID: $BACKEND_PID)"

# --- 启动前端 ---
step "Starting frontend dev server..."
cd "$FRONTEND_DIR"
"$NPM_EXE" run dev &
FRONTEND_PID=$!
ok "Frontend started (PID: $FRONTEND_PID)"

# --- 等待就绪 ---
wait_http() {
    local name=$1 url=$2 timeout=$3
    local deadline=$((SECONDS + timeout))
    while (( SECONDS < deadline )); do
        if curl -sf -o /dev/null --max-time 2 "$url" 2>/dev/null; then
            ok "$name is reachable at $url."
            return 0
        fi
        sleep 1
    done
    fail "$name did not become reachable at $url within ${timeout}s."
}

step "Waiting for services..."
wait_http "Backend"  "http://localhost:9800/health" 45
wait_http "Frontend" "http://localhost:5174"        60

# --- 打开浏览器 ---
if [[ "$NO_BROWSER" == false ]] && command -v xdg-open &>/dev/null; then
    xdg-open "http://localhost:5174" &>/dev/null &
fi

echo ""
ok "Development environment is running."
echo "  Frontend: http://localhost:5174"
echo "  Backend:  http://localhost:9800"
echo "  Admin:    http://localhost:9800/admin"
echo "  API docs: http://localhost:9800/docs"
echo ""
echo "Press Ctrl+C to stop all services."

# --- 等待子进程 ---
wait
