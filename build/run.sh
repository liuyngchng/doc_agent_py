#!/usr/bin/env bash
# doc_agent_py deploy script
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="doc_agent_py:latest"
CONTAINER="${CONTAINER:-doc_agent_py}"
HOST_PORT="${HOST_PORT:-20001}"
DATA_DIR="${DATA_DIR:-$(pwd)/data}"
NETWORK="${NETWORK:-llm_net}"

mkdir -p "$DATA_DIR" "$DATA_DIR/tasks" "$DATA_DIR/logs"

if [ ! -f "$DATA_DIR/cfg.yml" ]; then
  echo "[deploy] cfg.yml 不存在，从模板复制..."
  cp "$(pwd)/data/cfg.yml.template" "$DATA_DIR/cfg.yml"
  echo "[deploy] 请编辑 $DATA_DIR/cfg.yml 后再启动"
fi

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "[deploy] Creating network: $NETWORK"
  docker network create "$NETWORK"
fi

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "")

case "${1:-start}" in
  start)
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
      echo "[deploy] Image not found. Run ./build/build.sh first."
      exit 1
    fi

    if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
      docker stop "$CONTAINER" 2>/dev/null || true
      docker rm "$CONTAINER" 2>/dev/null || true
    fi

    echo "[deploy] Starting container: $CONTAINER"
    docker run -d \
      --name "$CONTAINER" \
      --network "$NETWORK" \
      -p "$HOST_PORT:20001" \
      -v "$DATA_DIR:/app/data" \
      --restart unless-stopped \
      "$IMAGE"
    echo "[deploy] Started on port $HOST_PORT"
    echo ""

    if [ -n "$LOCAL_IP" ]; then
      echo "  管理后台:  https://$LOCAL_IP:$HOST_PORT/admin"
      echo "  健康检查:  https://$LOCAL_IP:$HOST_PORT/health"
      echo "  业务接口:  POST https://$LOCAL_IP:$HOST_PORT/api/v1/<path>"
    else
      echo "  管理后台:  https://localhost:$HOST_PORT/admin"
      echo "  健康检查:  https://localhost:$HOST_PORT/health"
      echo "  业务接口:  POST https://localhost:$HOST_PORT/api/v1/<path>"
    fi
    echo ""
    echo "  (自签证书，浏览器访问请点\"高级\"→\"继续\")"
    echo ""
    ;;

  stop)
    docker stop "$CONTAINER" || true
    docker rm "$CONTAINER" || true
    echo "[deploy] Stopped"
    ;;

  restart)
    "$0" stop; "$0" start
    ;;

  logs)
    docker logs -f --tail=100 "$CONTAINER"
    ;;

  status)
    if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
      docker inspect "$CONTAINER" --format 'Status: {{.State.Status}} | Started: {{.State.StartedAt}}'
    else
      echo "Container not running"
    fi
    ;;
  *)
    echo "Usage: $0 [start|stop|restart|logs|status]"
    exit 1
    ;;
esac
