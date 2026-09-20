#!/bin/bash

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONTAINER="seedhub-playwright"
LOG_DIR_DEFAULT="$BASE_DIR/logs"
LOG_DIR="${LOG_DIR:-$LOG_DIR_DEFAULT}"
LOG_FILE="$LOG_DIR/seedhub_cache.log"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE" 2>/dev/null || true

log() {
    printf '[%s] [PARSE] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

if [ $# -ne 1 ]; then
    log "ERROR: 用法: $0 <SeedHub一级页面URL>"
    exit 2
fi

MOVIE_URL="$1"

RUNNING="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)"

if [ "$RUNNING" != "true" ]; then
    log "ERROR: seedhub-playwright 没有运行。请先执行：$BASE_DIR/seedhub_start.sh"
    exit 1
fi

if ! docker exec "$CONTAINER" test -f /work/seedhub_cache.py; then
    log "ERROR: 容器中找不到 /work/seedhub_cache.py"
    exit 1
fi

log "INFO: 开始解析 SeedHub 页面：$MOVIE_URL"
docker exec \
    -e DISPLAY=:99 \
    "$CONTAINER" \
    python3 /work/seedhub_cache.py "$MOVIE_URL" >> "$LOG_FILE" 2>&1
