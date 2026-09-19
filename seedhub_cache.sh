#!/bin/bash

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONTAINER="seedhub-playwright"

if [ $# -ne 1 ]; then
    echo "用法: $0 <SeedHub一级页面URL>"
    exit 2
fi

MOVIE_URL="$1"

RUNNING="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)"

if [ "$RUNNING" != "true" ]; then
    echo "错误：seedhub-playwright 没有运行。"
    echo "请先执行："
    echo "  $BASE_DIR/seedhub_start.sh"
    exit 1
fi

if ! docker exec "$CONTAINER" test -f /work/seedhub_cache.py; then
    echo "错误：容器中找不到 /work/seedhub_cache.py"
    exit 1
fi

exec docker exec \
    -e DISPLAY=:99 \
    "$CONTAINER" \
    python3 /work/seedhub_cache.py "$MOVIE_URL"
