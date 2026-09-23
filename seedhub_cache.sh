#!/bin/bash

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONTAINER="seedhub-playwright"
LOG_DIR_DEFAULT="$BASE_DIR/logs"
LOG_DIR="${LOG_DIR:-$LOG_DIR_DEFAULT}"
LOG_FILE="$LOG_DIR/seedhub_cache.log"
CONFIG="$BASE_DIR/config.local"

# 只读取低频二级入口确认相关的两个数值配置；不向容器传递 Cookie/Token。
ENTRY_RECHECK_HOURS=72
ENTRY_RECHECK_MAX=2

if [ -f "$CONFIG" ]; then
    # shellcheck disable=SC1090
    . "$CONFIG"
    ENTRY_RECHECK_HOURS="${RESOURCE_SEEDHUB_ENTRY_RECHECK_HOURS:-72}"
    ENTRY_RECHECK_MAX="${RESOURCE_SEEDHUB_ENTRY_RECHECK_MAX:-2}"
fi

[[ "$ENTRY_RECHECK_HOURS" =~ ^[0-9]+$ ]] || ENTRY_RECHECK_HOURS=72
[[ "$ENTRY_RECHECK_MAX" =~ ^[0-9]+$ ]] || ENTRY_RECHECK_MAX=2

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE" 2>/dev/null || true

log() {
    printf '[%s] [PARSE] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

if [ "$#" -ne 1 ] && [ "$#" -ne 4 ]; then
    log "ERROR: 用法: $0 <SeedHub一级页面URL> [--range <start_rank> <count>]"
    exit 2
fi

MOVIE_URL="$1"
shift

if [ "$#" -ne 0 ]; then
    if [ "$#" -ne 3 ] || [ "$1" != "--range" ]; then
        log "ERROR: 可选参数必须是：--range <start_rank> <count>"
        exit 2
    fi
fi

RUNNING="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)"

if [ "$RUNNING" != "true" ]; then
    log "ERROR: seedhub-playwright 没有运行。请先执行：$BASE_DIR/seedhub_start.sh"
    exit 1
fi

if ! docker exec "$CONTAINER" test -f /work/seedhub_cache.py; then
    log "ERROR: 容器中找不到 /work/seedhub_cache.py"
    exit 1
fi

log "INFO: 开始解析 SeedHub 页面：$MOVIE_URL${*:+ 参数=$*}"
docker exec \
    -e DISPLAY=:99 \
    -e RESOURCE_SEEDHUB_ENTRY_RECHECK_HOURS="$ENTRY_RECHECK_HOURS" \
    -e RESOURCE_SEEDHUB_ENTRY_RECHECK_MAX="$ENTRY_RECHECK_MAX" \
    "$CONTAINER" \
    python3 /work/seedhub_cache.py "$MOVIE_URL" "$@" >> "$LOG_FILE" 2>&1
