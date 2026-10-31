# 裸机 systemd 部署而非 Docker

512MB 内存下 Docker 守护进程（dockerd + containerd）常驻占用约 100–150MB，占可用内存 20–30%，内存税不可接受。决定沿用 systemd + Nginx 裸机部署路线（见 docs/deployment-512mb.md）。环境可复现性改用锁定依赖版本（lockfile）+ 部署脚本保证；待服务器升级后再评估迁 Docker。
