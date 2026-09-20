#!/bin/bash

CONTAINER="seedhub-playwright"
IMAGE="playwright-python:chromium"

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
WORK_DIR="$BASE_DIR/docker"
LOG_DIR_DEFAULT="$BASE_DIR/logs"
LOG_DIR="${LOG_DIR:-$LOG_DIR_DEFAULT}"
LOG_FILE="$LOG_DIR/seedhub_start.log"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE" 2>/dev/null || true

log() {
    printf '[%s] [PARSE] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

log "INFO: 检查 SeedHub Playwright 容器..."

# 容器已经存在
if docker inspect "$CONTAINER" >/dev/null 2>&1; then

    RUNNING="$(docker inspect -f '{{.State.Running}}' "$CONTAINER")"

    if [ "$RUNNING" = "true" ]; then
        log "INFO: seedhub-playwright 已经在运行。"
        exit 0
    fi

    log "INFO: 容器存在但没有运行，正在启动..."
    docker start "$CONTAINER" >/dev/null || {
        log "ERROR: 容器启动失败。"
        exit 1
    }

    log "INFO: seedhub-playwright 已启动。"
    exit 0
fi

# 容器不存在，创建并启动
log "INFO: 容器不存在，正在创建..."

docker run -d \
    --name "$CONTAINER" \
    -v "$WORK_DIR:/work" \
    -v "$BASE_DIR:/data" \
    "$IMAGE" \
    bash -c '
        Xvfb :99 -screen 0 1920x1080x24 >/tmp/xvfb.log 2>&1 &
        export DISPLAY=:99
        while true; do
            sleep 3600
        done
    '

if [ $? -ne 0 ]; then
    log "ERROR: 容器创建失败。"
    exit 1
fi

log "INFO: seedhub-playwright 创建并启动成功。"
log "INFO: 挂载：$WORK_DIR -> /work；$BASE_DIR -> /data"
log "INFO: DISPLAY=:99"
