#!/bin/bash

CONTAINER="seedhub-playwright"
IMAGE="playwright-python:chromium"

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
WORK_DIR="$BASE_DIR/docker"

echo "检查 SeedHub Playwright 容器..."

# 容器已经存在
if docker inspect "$CONTAINER" >/dev/null 2>&1; then

    RUNNING="$(docker inspect -f '{{.State.Running}}' "$CONTAINER")"

    if [ "$RUNNING" = "true" ]; then
        echo "seedhub-playwright 已经在运行。"
        exit 0
    fi

    echo "容器存在但没有运行，正在启动..."
    docker start "$CONTAINER" >/dev/null || {
        echo "错误：容器启动失败。"
        exit 1
    }

    echo "seedhub-playwright 已启动。"
    exit 0
fi

# 容器不存在，创建并启动
echo "容器不存在，正在创建..."

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
    echo "错误：容器创建失败。"
    exit 1
fi

echo "seedhub-playwright 创建并启动成功。"
echo
echo "挂载："
echo "  $WORK_DIR -> /work"
echo "  $BASE_DIR -> /data"
echo
echo "DISPLAY=:99"
