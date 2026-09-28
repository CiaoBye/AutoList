#!/usr/bin/env bash
# AutoList - 飞牛 (fnOS) 局域网本地极速构建与部署脚本
# 作用：跳过 Docker Hub，直接在局域网内同步最新源码至飞牛 NAS 并秒级完成原生打包与重启

set -euo pipefail

FNOS_HOST="${FNOS_HOST:-fnos}"
REMOTE_SRC="/vol1/1000/Docker/Autolist-src"
REMOTE_COMPOSE="/vol1/1000/Docker/Autolist/docker-compose.yml"
WEB_URL="${AUTOLIST_WEB_URL:-http://192.0.2.10:8585}"
# 飞牛的 Docker 镜像加速源拉取 node:22-* 会返回 401，默认使用 NAS 本地已有的 Node 镜像构建前端。
NODE_IMAGE="${AUTOLIST_NODE_IMAGE:-node:22.16.0-alpine}"

echo "==============================================="
echo " 🚀 AutoList -> 飞牛 (fnOS) 本地极速构建与部署"
echo "==============================================="

# 1. 检查 SSH 连通性
echo "📡 [1/4] 检查飞牛主机连通性 (${FNOS_HOST})..."
if ! ssh -o ConnectTimeout=5 "${FNOS_HOST}" "docker --version >/dev/null 2>&1"; then
  echo "❌ 无法通过 SSH 连接到 ${FNOS_HOST} 或当前用户无 Docker 权限！"
  echo "请确认 ~/.ssh/config 中已配置 host ${FNOS_HOST}，且账号已在 docker 组。"
  exit 1
fi

# 2. 局域网增量同步源码
echo "📦 [2/4] 正在局域网增量同步代码到飞牛 (${REMOTE_SRC})..."
ssh "${FNOS_HOST}" "mkdir -p ${REMOTE_SRC}"
rsync -az --delete \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='app/static/ui' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  --exclude='.scratch' \
  --exclude='.pytest_cache' \
  --exclude='data' \
  ./ "${FNOS_HOST}:${REMOTE_SRC}/"

# 3. 飞牛本地 Docker 原生构建
echo "🔨 [3/4] 在飞牛 NAS 上执行 Docker 原生构建 (利用本地缓存)..."
ssh "${FNOS_HOST}" "docker build --build-arg NODE_IMAGE=${NODE_IMAGE} -t autolist:local -t ayuanaa/autolist:latest ${REMOTE_SRC}"

# 4. 平滑重启容器
echo "🔄 [4/4] 正在重启飞牛上的 AutoList 容器..."
ssh "${FNOS_HOST}" "docker compose -f ${REMOTE_COMPOSE} up -d --force-recreate"

# 5. 验证健康状态
echo "🩺 正在验证服务健康状态..."
for i in {1..15}; do
  if curl -fsSL "${WEB_URL}/api/health" -o /tmp/autolist-health.json 2>/dev/null; then
    VERSION=$(python3 -c "import json; print(json.load(open('/tmp/autolist-health.json')).get('version', 'unknown'))")
    echo "✅ 部署成功！AutoList 当前运行版本: v${VERSION}"
    echo "👉 访问地址: ${WEB_URL}"
    break
  fi
  sleep 1
  if [ "$i" -eq 15 ]; then
    echo "⚠️ 容器已启动，但健康检查接口暂未就绪，请稍后访问 ${WEB_URL} 查看日志。"
  fi
done
echo "==============================================="
