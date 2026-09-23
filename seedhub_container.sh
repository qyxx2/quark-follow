#!/bin/bash
set -u

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONTAINER="seedhub-playwright"
START_SCRIPT="$BASE_DIR/seedhub_start.sh"
LOG_DIR_DEFAULT="$BASE_DIR/logs"
LOG_DIR="${LOG_DIR:-$LOG_DIR_DEFAULT}"
LOG_FILE="$LOG_DIR/seedhub_container.log"
LOCK_DIR="$BASE_DIR/seedhub-container.lock"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE" 2>/dev/null || true
chmod 600 "$LOG_FILE" 2>/dev/null || true

log() {
    printf '[%s] [PARSE] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

usage() {
    cat <<EOF
用法：
  $0 status
  $0 start
  $0 stop
  $0 restart

说明：
  start   复用现有 seedhub_start.sh；容器不存在时按现有配置创建。
  stop    停止 SeedHub 解析容器；资源检查/SeedHub 解析运行时拒绝执行。
  restart 运行中的容器执行 restart；已停止容器执行 start；不存在时按现有配置创建。
EOF
}

docker_bin() {
    command -v docker 2>/dev/null || true
}

container_exists() {
    local docker="$1"
    "$docker" inspect "$CONTAINER" >/dev/null 2>&1
}

container_running() {
    local docker="$1" state
    state="$("$docker" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)"
    [ "$state" = "true" ]
}

wait_xvfb_ready() {
    local docker="$1"
    local max_wait="${2:-20}"

    for ((i=1; i<=max_wait; i++)); do
        if ! container_running "$docker"; then
            return 1
        fi

        if "$docker" exec "$CONTAINER" test -S /tmp/.X11-unix/X99 >/dev/null 2>&1; then
            log "INFO SeedHub Xvfb :99 已就绪。"
            return 0
        fi

        sleep 1
    done

    return 1
}

busy_reason() {
    local self_pid="$$"
    local proc pid raw cmd

    for proc in /proc/[0-9]*; do
        pid="${proc##*/}"
        [ "$pid" = "$self_pid" ] && continue
        [ -r "$proc/cmdline" ] || continue
        raw="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
        [ -n "$raw" ] || continue
        cmd=" $raw "
        case "$cmd" in
            *" $BASE_DIR/resource_check.sh "*)
                printf '%s' '资源检查'
                return 0
                ;;
            *" $BASE_DIR/seedhub_cache.sh "*)
                printf '%s' 'SeedHub 解析'
                return 0
                ;;
            *" $BASE_DIR/seedhub_start.sh "*)
                printf '%s' 'SeedHub 容器启动'
                return 0
                ;;
            *" /work/seedhub_cache.py "*)
                printf '%s' 'SeedHub 解析器'
                return 0
                ;;
        esac
    done
    return 1
}

acquire_lock() {
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        log "WARN SeedHub 容器已有操作正在执行：$LOCK_DIR"
        return 1
    fi
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    printf '%s\n' "${1:-unknown}" > "$LOCK_DIR/action"
    chmod 600 "$LOCK_DIR/pid" "$LOCK_DIR/action" 2>/dev/null || true
    return 0
}

cleanup() {
    rm -f "$LOCK_DIR/pid" "$LOCK_DIR/action" 2>/dev/null || true
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

ACTION="${1:-}"
case "$ACTION" in
    status|start|stop|restart) ;;
    -h|--help|help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

DOCKER="$(docker_bin)"
if [ -z "$DOCKER" ]; then
    log "ERROR 系统中找不到 docker 命令。"
    exit 2
fi

if [ "$ACTION" = "status" ]; then
    if ! container_exists "$DOCKER"; then
        log "INFO 容器不存在：$CONTAINER"
        printf '%s\n' 'not_found'
        exit 0
    fi
    "$DOCKER" inspect --format '{{.State.Status}}' "$CONTAINER"
    exit $?
fi

if ! acquire_lock "$ACTION"; then
    exit 3
fi

case "$ACTION" in
    start)
        if [ -x "$START_SCRIPT" ] || [ -f "$START_SCRIPT" ]; then
            log "INFO 启动请求：复用 $START_SCRIPT"
            if /bin/bash "$START_SCRIPT" >> "$LOG_FILE" 2>&1; then
                if wait_xvfb_ready "$DOCKER" 20; then
                    log "INFO SeedHub 容器启动流程完成。"
                    exit 0
                fi
                log "ERROR SeedHub 容器已启动，但 Xvfb :99 未在 20s 内就绪。"
                exit 1
            fi
            rc=$?
            log "ERROR SeedHub 容器启动失败：rc=$rc"
            exit "$rc"
        fi
        log "ERROR 缺少现有容器启动脚本：$START_SCRIPT"
        exit 1
        ;;

    stop)
        if ! container_exists "$DOCKER"; then
            log "ERROR 容器不存在，无法停止：$CONTAINER"
            exit 1
        fi
        if ! container_running "$DOCKER"; then
            log "INFO 容器已经停止：$CONTAINER"
            exit 0
        fi
        if reason="$(busy_reason)"; then
            log "WARN 拒绝停止容器：当前正在执行 $reason。"
            exit 3
        fi
        log "INFO 停止 SeedHub 容器：$CONTAINER"
        if "$DOCKER" stop --time 15 "$CONTAINER" >> "$LOG_FILE" 2>&1; then
            log "INFO SeedHub 容器已停止。"
            exit 0
        fi
        rc=$?
        log "ERROR SeedHub 容器停止失败：rc=$rc"
        exit "$rc"
        ;;

    restart)
        if reason="$(busy_reason)"; then
            log "WARN 拒绝重启容器：当前正在执行 $reason。"
            exit 3
        fi
        if ! container_exists "$DOCKER"; then
            if [ -f "$START_SCRIPT" ]; then
                log "INFO 容器不存在，复用 $START_SCRIPT 创建并启动。"
                if /bin/bash "$START_SCRIPT" >> "$LOG_FILE" 2>&1; then
                    if wait_xvfb_ready "$DOCKER" 20; then
                        log "INFO SeedHub 容器创建/启动完成。"
                        exit 0
                    fi
                    log "ERROR SeedHub 容器已创建并运行，但 Xvfb :99 未在 20s 内就绪。"
                    exit 1
                fi
                rc=$?
                log "ERROR SeedHub 容器创建/启动失败：rc=$rc"
                exit "$rc"
            fi
            log "ERROR 容器不存在且缺少启动脚本：$START_SCRIPT"
            exit 1
        fi
        if container_running "$DOCKER"; then
            log "INFO 重启 SeedHub 容器：$CONTAINER"
            if "$DOCKER" restart --time 15 "$CONTAINER" >> "$LOG_FILE" 2>&1; then
                if wait_xvfb_ready "$DOCKER" 20; then
                    log "INFO SeedHub 容器重启完成。"
                    exit 0
                fi
                log "ERROR SeedHub 容器重启成功，但 Xvfb :99 未在 20s 内就绪。"
                exit 1
            fi
            rc=$?
            log "ERROR SeedHub 容器重启失败：rc=$rc"
            exit "$rc"
        fi
        log "INFO 容器当前已停止，改为启动：$CONTAINER"
        if "$DOCKER" start "$CONTAINER" >> "$LOG_FILE" 2>&1; then
            if wait_xvfb_ready "$DOCKER" 20; then
                log "INFO SeedHub 已停止容器重新启动完成。"
                exit 0
            fi
            log "ERROR SeedHub 已停止容器启动成功，但 Xvfb :99 未在 20s 内就绪。"
            exit 1
        fi
        rc=$?
        log "ERROR 已停止容器启动失败：rc=$rc"
        exit "$rc"
        ;;
esac