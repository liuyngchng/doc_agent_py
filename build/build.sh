#!/usr/bin/env bash
# doc_agent_py Docker 镜像构建脚本
#
# Pip 离线安装（网络差时推荐）:
#   ./build.sh pip_download_dir=/path/to/wheels
#   构建前先 pip download 补齐，构建时完全离线。
#   也可通过环境变量: PIP_DOWNLOAD_DIR=/path/to/wheels ./build.sh
set -euo pipefail

BUILD_START=$SECONDS

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-doc_agent_py}"
PIP_OFFLINE_STAGING="${SCRIPT_DIR}/.pip-offline"

# ── Usage ────────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
用法: ./build.sh [version] [pip_download_dir=<dir>] [proxy...]

参数说明:
  version                 镜像版本号（位置参数）
  -h | --help             显示本帮助并退出

Pip 离线目录（网络差时推荐，之后构建跳过 pip 下载）:
  pip_download_dir=<dir>  本地 .whl 存放目录；构建前先 pip download 补齐，
                          构建时完全离线。

代理参数（可选，不带 = 时继承环境变量）:
  http_proxy=<url>        设置 HTTP 代理
  https_proxy=<url>       设置 HTTPS 代理

示例:
  ./build.sh 0.1.0 pip_download_dir=/media/rd/SD2TB/whl_py_dir_docx
  VERSION=0.1.0 ./build.sh
EOF
}

# ── 参数解析 ────────────────────────────────────────────────────
TAG_VERSION=""
HTTP_PROXY_VAL=""
HTTPS_PROXY_VAL=""
NO_PROXY_VAL=""
PIP_DOWNLOAD_DIR_VAL=""

for arg in "$@"; do
  case "$arg" in
    http_proxy=*|HTTP_PROXY=*)        HTTP_PROXY_VAL="${arg#*=}" ;;
    https_proxy=*|HTTPS_PROXY=*)      HTTPS_PROXY_VAL="${arg#*=}" ;;
    no_proxy=*|NO_PROXY=*)            NO_PROXY_VAL="${arg#*=}" ;;
    pip_download_dir=*|PIP_DOWNLOAD_DIR=*) PIP_DOWNLOAD_DIR_VAL="${arg#*=}" ;;
    -h|--help|help)                   usage; exit 0 ;;
    -*)                               echo "WARNING: 未知参数 '$arg'，已忽略（用 -h 查看用法）" ;;
    *)                                TAG_VERSION="$arg" ;;
  esac
done

# Priority: $VERSION env > positional arg > pyproject.toml > "0.1.0"
if [ -z "$TAG_VERSION" ]; then
  if [ -n "${VERSION:-}" ]; then
    TAG_VERSION="$VERSION"
  else
    TAG_VERSION="0.1.0"
  fi
fi

# 不带 = 时继承环境变量
HTTP_PROXY_VAL="${HTTP_PROXY_VAL:-${HTTP_PROXY:-${http_proxy:-}}}"
HTTPS_PROXY_VAL="${HTTPS_PROXY_VAL:-${HTTPS_PROXY:-${https_proxy:-}}}"
NO_PROXY_VAL="${NO_PROXY_VAL:-${NO_PROXY:-${no_proxy:-}}}"
PIP_DOWNLOAD_DIR_VAL="${PIP_DOWNLOAD_DIR_VAL:-${PIP_DOWNLOAD_DIR:-}}"

# ---- Interactive pip offline dir prompt (only when not already set) ----------
PIP_LAST_DIR_FILE="${SCRIPT_DIR}/.last-pip-dir"

# Restore last used directory as suggestion
PIP_SUGGESTED_DIR=""
if [[ -f "$PIP_LAST_DIR_FILE" ]]; then
  PIP_SUGGESTED_DIR="$(head -1 "$PIP_LAST_DIR_FILE")"
fi
# Fallback: if a known directory exists, suggest it
if [[ -z "$PIP_SUGGESTED_DIR" ]] || [[ ! -d "$PIP_SUGGESTED_DIR" ]]; then
  for guess in /media/rd/SD2TB/whl_py_dir_docx; do
    if [[ -d "$guess" ]]; then
      PIP_SUGGESTED_DIR="$guess"
      break
    fi
  done
fi

if [[ -z "$PIP_DOWNLOAD_DIR_VAL" ]] && [[ -t 0 ]]; then
  echo ""
  echo -n "使用 pip 离线目录？ [y/n]: "
  read -r USER_CHOICE
  case "$USER_CHOICE" in
    n|N|no|No|NO)
      echo " → 跳过离线目录，pip 将在线安装"
      ;;
    y|Y|yes|Yes|YES)
      if [[ -n "$PIP_SUGGESTED_DIR" ]]; then
        read -e -i "$PIP_SUGGESTED_DIR" -r -p "pip 离线目录路径: " PIP_DOWNLOAD_DIR_VAL
      else
        read -r -p "pip 离线目录路径: " PIP_DOWNLOAD_DIR_VAL
      fi
      echo " → 使用: $PIP_DOWNLOAD_DIR_VAL"
      ;;
    *)
      # Treat any non-n input as a direct path
      PIP_DOWNLOAD_DIR_VAL="$USER_CHOICE"
      echo " → 使用: $PIP_DOWNLOAD_DIR_VAL"
      ;;
  esac
fi

# Persist the chosen directory for next time
if [[ -n "$PIP_DOWNLOAD_DIR_VAL" ]]; then
  echo "$PIP_DOWNLOAD_DIR_VAL" > "$PIP_LAST_DIR_FILE"
fi

# 自动补全 scheme
add_scheme() {
  local v="$1"
  [[ -z "$v" || "$v" == *"://"* ]] && { printf '%s' "$v"; return; }
  printf 'http://%s' "$v"
}
HTTP_PROXY_VAL="$(add_scheme "$HTTP_PROXY_VAL")"
HTTPS_PROXY_VAL="$(add_scheme "$HTTPS_PROXY_VAL")"

FULL_TAG="${IMAGE_NAME}:${TAG_VERSION}"

echo "============================================"
echo " Building ${FULL_TAG}"
echo "============================================"
if [[ -n "$HTTP_PROXY_VAL" || -n "$HTTPS_PROXY_VAL" ]]; then
  echo " 代理: http=$HTTP_PROXY_VAL https=$HTTPS_PROXY_VAL"
fi
if [[ -n "$PIP_DOWNLOAD_DIR_VAL" ]]; then
  echo " Pip 离线目录: $PIP_DOWNLOAD_DIR_VAL"
fi
echo ""

# ---- Docker build args -----------------------------------------------------
DOCKER_BUILD_ARGS=()
if [[ -n "$HTTP_PROXY_VAL" || -n "$HTTPS_PROXY_VAL" ]]; then
  DOCKER_BUILD_ARGS=(
    --build-arg "HTTP_PROXY=$HTTP_PROXY_VAL"
    --build-arg "HTTPS_PROXY=$HTTPS_PROXY_VAL"
    --build-arg "NO_PROXY=$NO_PROXY_VAL"
  )
fi

# ---- prepare deploy/ --------------------------------------------------------
rm -rf "$PROJECT_ROOT/deploy"
mkdir -p "$PROJECT_ROOT/deploy/claude-code" "$PROJECT_ROOT/deploy/certs"

# ---- Node.js tarball --------------------------------------------------------
NODE_TARBALL="node-v22.23.2-linux-x64.tar.xz"
NODE_TARBALL_PATH="$PROJECT_ROOT/deploy/$NODE_TARBALL"

NODE_CACHE_DIR="${NODE_CACHE_DIR:-$HOME/.cache/doc-agent-py}"
if [ -f "$NODE_CACHE_DIR/$NODE_TARBALL" ]; then
  cp "$NODE_CACHE_DIR/$NODE_TARBALL" "$NODE_TARBALL_PATH"
  echo "[build] Node.js 从本地缓存复用"
else
  echo "[build] 下载 Node.js v22.23.2..."
  if [[ -n "$HTTP_PROXY_VAL" ]]; then
    export http_proxy="$HTTP_PROXY_VAL" https_proxy="$HTTPS_PROXY_VAL"
  fi
  mkdir -p "$NODE_CACHE_DIR"
  curl -sSL "https://nodejs.org/dist/v22.23.2/$NODE_TARBALL" -o "$NODE_TARBALL_PATH"
  cp "$NODE_TARBALL_PATH" "$NODE_CACHE_DIR/$NODE_TARBALL"
  echo "[build] Node.js 已缓存到 $NODE_CACHE_DIR"
fi

# ---- Pip offline preparation ------------------------------------------------
cleanup_pip_offline_staging() {
  if [[ -d "$PIP_OFFLINE_STAGING" ]]; then
    rm -rf "$PIP_OFFLINE_STAGING"
  fi
}

if [[ -n "$PIP_DOWNLOAD_DIR_VAL" ]]; then
  if [[ ! -d "$PIP_DOWNLOAD_DIR_VAL" ]]; then
    echo "ERROR: pip_download_dir 目录不存在: $PIP_DOWNLOAD_DIR_VAL"
    exit 1
  fi

  REQUIREMENTS_TXT="${SCRIPT_DIR}/requirements.txt"
  if [[ ! -f "$REQUIREMENTS_TXT" ]]; then
    echo "ERROR: requirements.txt not found: $REQUIREMENTS_TXT"
    exit 1
  fi

  echo "=== Pip download: 同步离线包到 $PIP_DOWNLOAD_DIR_VAL ==="
  if [[ -n "$HTTP_PROXY_VAL" ]]; then
    export HTTP_PROXY="$HTTP_PROXY_VAL" HTTPS_PROXY="$HTTPS_PROXY_VAL"
    export http_proxy="$HTTP_PROXY_VAL" https_proxy="$HTTPS_PROXY_VAL"
  fi
  pip download \
    --dest "$PIP_DOWNLOAD_DIR_VAL" \
    --exists-action i \
    -r "$REQUIREMENTS_TXT" \
    --prefer-binary \
    --timeout 120 \
    --retries 5 \
    || { echo "ERROR: pip download 失败。请检查网络 / 代理设置"; exit 1; }
  echo ""

  cleanup_pip_offline_staging
  mkdir -p "$PIP_OFFLINE_STAGING"
  cp -a "$PIP_DOWNLOAD_DIR_VAL"/* "$PIP_OFFLINE_STAGING/"
  echo "Pip 离线包已准备: $PIP_OFFLINE_STAGING ($(ls "$PIP_OFFLINE_STAGING" | wc -l) 个文件)"
  echo ""

  DOCKER_BUILD_ARGS+=(--build-arg "PIP_OFFLINE_=1")
else
  cleanup_pip_offline_staging
  mkdir -p "$PIP_OFFLINE_STAGING"
  touch "$PIP_OFFLINE_STAGING/.gitkeep"
fi

# ---- Claude Code CLI local binary sync ----------------------------------
CLAUDE_CODE_STAGING="$PROJECT_ROOT/deploy/claude-code"
CLAUDE_CODE_SRC=""
for candidate in \
  "$(command -v claude 2>/dev/null)" \
  "/home/linuxbrew/.linuxbrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude" \
  "$HOME/.local/bin/claude" \
  "$HOME/.npm-global/bin/claude"; do
  if [[ -n "$candidate" && -f "$candidate" ]]; then
    CLAUDE_CODE_SRC="$candidate"
    break
  fi
done

if [[ -n "$CLAUDE_CODE_SRC" ]]; then
  cp -f "$CLAUDE_CODE_SRC" "$CLAUDE_CODE_STAGING/claude"
  chmod +x "$CLAUDE_CODE_STAGING/claude"
  echo "[build] Claude Code CLI 已本地化 ($(du -h "$CLAUDE_CODE_STAGING/claude" | cut -f1))"
  DOCKER_BUILD_ARGS+=(--build-arg "CLAUDE_CODE_OFFLINE=1")
else
  echo "[build] 未找到本地 claude，将回退到镜像内 npm install -g（需联网）"
  touch "$CLAUDE_CODE_STAGING/.gitkeep"
fi

# ---- HTTPS certs ------------------------------------------------------------
# 证书持久化在 build/certs/（不随 deploy/ 清掉），build 时 copy 进 deploy/。
PERSIST_CERTS_DIR="${SCRIPT_DIR}/certs"
PERSIST_CERT_FILE="${PERSIST_CERTS_DIR}/cert.pem"
PERSIST_KEY_FILE="${PERSIST_CERTS_DIR}/key.pem"

if [[ -f "$PERSIST_CERT_FILE" && -f "$PERSIST_KEY_FILE" ]]; then
  echo "[build] HTTPS 证书已存在，直接复用: $PERSIST_CERTS_DIR/"
else
  if command -v openssl &>/dev/null; then
    echo "[build] 生成自签名 HTTPS 证书 (RSA-2048, 10年)"
    mkdir -p "$PERSIST_CERTS_DIR"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout "$PERSIST_KEY_FILE" \
      -out "$PERSIST_CERT_FILE" \
      -subj "/C=CN/O=doc_agent_py/CN=localhost" \
      -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
  else
    echo "[build] 警告: 未找到 openssl，跳过证书生成，服务将使用 HTTP"
  fi
fi

# Copy into deploy/ build context
DEPLOY_CERTS_DIR="$PROJECT_ROOT/deploy/certs"
mkdir -p "$DEPLOY_CERTS_DIR"
if [[ -f "$PERSIST_CERT_FILE" && -f "$PERSIST_KEY_FILE" ]]; then
  cp "$PERSIST_CERT_FILE" "$PERSIST_KEY_FILE" "$DEPLOY_CERTS_DIR/"
  DOCKER_BUILD_ARGS+=(--build-arg "HTTPS_CERT=1")
  echo "[build] HTTPS 证书已就绪"
fi

echo ""
echo "=== Docker build ==="
if ! docker image inspect ubuntu:24.04 >/dev/null 2>&1; then
  echo "ERROR: 本地缺少 ubuntu:24.04 基础镜像，请先拉取: docker pull ubuntu:24.04"
  exit 1
fi

docker buildx build \
    "${DOCKER_BUILD_ARGS[@]+"${DOCKER_BUILD_ARGS[@]}"}" \
    -t "${FULL_TAG}" \
    -t "${IMAGE_NAME}:latest" \
    -f "$SCRIPT_DIR/Dockerfile" \
    --load \
    "$PROJECT_ROOT"

cleanup_pip_offline_staging

docker image prune -f --filter "dangling=true" || true

BUILD_ELAPSED=$(( SECONDS - BUILD_START ))
echo "============================================"
echo " Build complete: ${FULL_TAG}"
printf " 总耗时: %dm %ds\n" $(( BUILD_ELAPSED / 60 )) $(( BUILD_ELAPSED % 60 ))
echo "============================================"
echo ""
echo "Run with:"
echo ""
echo "  ./build/run.sh"
echo ""
