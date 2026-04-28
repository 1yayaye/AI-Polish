# 512MB Low-Memory Deployment

This guide targets a 1 CPU / 512 MB RAM server. Use Nginx for static files and run FastAPI as an API-only systemd service.

## Build Frontend

Run this on the server or before uploading the release:

```bash
cd /opt/bypassaigc/frontend
npm ci
npm run build
```

Nginx should serve `/opt/bypassaigc/frontend/dist` directly. Do not run the backend with `--serve-static` in this profile.

## Recommended backend/.env

```properties
DEPLOYMENT_PROFILE=low_memory
SERVER_HOST=127.0.0.1
SERVER_PORT=9800

OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1

MAX_CONCURRENT_USERS=1
WORD_FORMATTER_MAX_CONCURRENT_JOBS=1
WORD_FORMATTER_JOB_RETENTION_HOURS=1
MIN_FREE_MEMORY_MB=128
MAX_UPLOAD_FILE_SIZE_MB=5
MAX_TEXT_INPUT_CHARS=50000
UVICORN_ACCESS_LOG=false

# Keep this enabled only when you need request/response diagnostics.
AI_DEBUG_LOGGING=true

DATABASE_URL=sqlite:////opt/bypassaigc/backend/ai_polish.db
SECRET_KEY=replace-with-a-random-32-byte-string
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-this-password
WORKSPACE_PRICE_PER_10K_CENTS=300
```

`AI_DEBUG_LOGGING` is intentionally not disabled by `DEPLOYMENT_PROFILE=low_memory`. If it stays enabled, configure log rotation.

## systemd service

Create `/etc/systemd/system/bypassaigc.service`:

```ini
[Unit]
Description=BypassAIGC API
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/bypassaigc/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/bypassaigc/backend/venv/bin/python -m app.main
Restart=on-failure
RestartSec=5
MemoryMax=420M
MemoryHigh=380M
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bypassaigc
sudo systemctl status bypassaigc
```

## Nginx site

```nginx
server {
    listen 80;
    server_name your-domain.example;

    root /opt/bypassaigc/frontend/dist;
    index index.html;

    client_max_body_size 5m;

    location /assets/ {
        try_files $uri =404;
        expires 7d;
        add_header Cache-Control "public";
    }

    location /api/ {
        proxy_pass http://127.0.0.1:9800/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }

    location /health {
        proxy_pass http://127.0.0.1:9800/health;
    }

    location /docs {
        proxy_pass http://127.0.0.1:9800/docs;
    }

    location /openapi.json {
        proxy_pass http://127.0.0.1:9800/openapi.json;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

## Log rotation

If `AI_DEBUG_LOGGING=true`, keep journald bounded:

```ini
SystemMaxUse=100M
RuntimeMaxUse=50M
MaxRetentionSec=7day
```

Put those settings in `/etc/systemd/journald.conf`, then restart journald.

## Health checks

- `GET /health` returns a lightweight liveness response.
- `GET /api/health/resources` returns deployment profile, memory status, concurrency limits, job counts, and logging flags without exposing API keys.

When available memory is below `MIN_FREE_MEMORY_MB`, new heavy tasks are rejected with HTTP 503 instead of risking an OOM kill.
