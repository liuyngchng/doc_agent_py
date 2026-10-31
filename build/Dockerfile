# ============================================================================
# dp_agt_py — Python 运行时镜像
# ============================================================================
FROM ubuntu:24.04

ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY=""
ARG http_proxy="$HTTP_PROXY"
ARG https_proxy="$HTTPS_PROXY"
ARG no_proxy="$NO_PROXY"

RUN if [ -n "$HTTP_PROXY" ]; then \
        echo 'Acquire::http::Proxy "'$HTTP_PROXY'";' > /etc/apt/apt.conf.d/01proxy; \
    fi; \
    if [ -n "$HTTPS_PROXY" ]; then \
        echo 'Acquire::https::Proxy "'$HTTPS_PROXY'";' >> /etc/apt/apt.conf.d/01proxy; \
    fi

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Shanghai

# ---- System dependencies (Python 3.12 + Claude Code CLI 运行时) ----
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    python3.12 \
    python3.12-venv \
    python3-pip \
    ripgrep \
    sqlite3 \
    unzip \
    vim-tiny \
    xz-utils \
    && rm -f /etc/apt/apt.conf.d/01proxy \
    && rm -rf /var/lib/apt/lists/*

# 设置 python3 / pip3 为默认
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.12 1 \
    && update-alternatives --install /usr/bin/pip pip /usr/bin/pip3 1

# ---- Node.js (root 安装到 /usr/local) ----
COPY deploy/node-v22.23.2-linux-x64.tar.xz /tmp/
RUN tar -xJf /tmp/node-v22.23.2-linux-x64.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node-v22.23.2-linux-x64.tar.xz

# ---- Python 依赖（root 安装到系统 site-packages）--------------------------
# 离线优先：build.sh pip_download_dir=<dir> 会设置 PIP_OFFLINE_=1 并预下载
# .whl 到 build/.pip-offline/。离线路径零网络；否则回退在线安装。
COPY build/requirements.txt /tmp/requirements.txt

ARG PIP_OFFLINE_=""
COPY build/.pip-offline/ /tmp/.pip-offline/

# 离线路径
RUN if [ -n "$PIP_OFFLINE_" ] && ls /tmp/.pip-offline/*.whl /tmp/.pip-offline/*.tar.gz 2>/dev/null; then \
      echo "Pip offline install from /tmp/.pip-offline"; \
      pip install \
        --no-index \
        --find-links /tmp/.pip-offline \
        --break-system-packages \
        --no-cache-dir \
        -r /tmp/requirements.txt; \
    fi

# 在线路径（离线未执行或失败时回退）
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -z "$PIP_OFFLINE_" ] || ! ls /tmp/.pip-offline/*.whl /tmp/.pip-offline/*.tar.gz 2>/dev/null; then \
      pip install \
        --break-system-packages \
        --no-cache-dir \
        -r /tmp/requirements.txt; \
    fi

RUN rm -rf /tmp/.pip-offline /tmp/requirements.txt

# ---- Runtime user ----
RUN userdel -r ubuntu 2>/dev/null || true \
    && useradd -r -m -d /home/agent -u 1000 -s /bin/bash agent \
    && mkdir -p /home/agent/.claude && chown agent:agent /home/agent/.claude

USER agent
ENV HOME=/home/agent

# ---- Claude Code CLI ----
# 当 build.sh 找到宿主机 claude 二进制时，设置 CLAUDE_CODE_OFFLINE=1 走离线安装
ARG CLAUDE_CODE_OFFLINE=""
COPY --chown=agent:agent deploy/claude-code/ /tmp/claude-code/

RUN if [ -n "$CLAUDE_CODE_OFFLINE" ] && [ -f /tmp/claude-code/claude ]; then \
      echo "Installing Claude Code CLI from local binary"; \
      mkdir -p /home/agent/.local/bin; \
      cp /tmp/claude-code/claude /home/agent/.local/bin/claude; \
      chmod +x /home/agent/.local/bin/claude; \
    fi

# 在线回退（无本地 binary 时 npm install）
RUN --mount=type=cache,target=/home/agent/.npm,uid=1000,gid=1000 \
    if [ -z "$CLAUDE_CODE_OFFLINE" ] || [ ! -f /tmp/claude-code/claude ]; then \
      echo "Claude Code CLI local binary not found, falling back to npm install"; \
      npm set prefix /home/agent/.local; \
      npm install -g @anthropic-ai/claude-code \
        --fetch-retries=5 \
        --fetch-retry-mintimeout=20000 \
        --fetch-retry-maxtimeout=120000 \
        --fetch-timeout=600000 \
        --no-audit \
        --no-fund; \
    fi

RUN rm -rf /tmp/claude-code

ENV PATH=/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin

# ---- App ----
WORKDIR /app
COPY --chown=agent:agent src/ /app/src/
COPY --chown=agent:agent web/ /app/web/
COPY --chown=agent:agent data/ /app/data/

# ---- HTTPS certs ----
ARG HTTPS_CERT=""
COPY --chown=agent:agent deploy/certs/ /tmp/certs/
RUN if [ -n "$HTTPS_CERT" ] && [ -f /tmp/certs/cert.pem ]; then \
      mkdir -p /app/deploy/certs && \
      cp /tmp/certs/cert.pem /tmp/certs/key.pem /app/deploy/certs/; \
    fi && \
    rm -rf /tmp/certs

WORKDIR /app
EXPOSE 20001

ENV DATA_DIR=/app/data

CMD ["python", "-m", "src.index"]
