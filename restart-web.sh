#!/bin/bash
set -u

BASE="/root/scripts/quark-follow"
PID_FILE="$BASE/web.pid"
START_SCRIPT="$BASE/start-web.sh"
LOCK_DIR="$BASE/web-restart.lock"
LOG_DIR="$BASE/logs"
LOG_FILE="$LOG_DIR/web_restart.log"
PORT=5233
MAX_STOP_WAIT=15
MAX_START_WAIT=20

mkdir -p "$LOG_DIR"
LOCK_ACQUIRED=0

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

cleanup() {
    if [ "$LOCK_ACQUIRED" -eq 1 ]; then
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
}
trap cleanup EXIT

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "ERROR Web 重启已在进行中，拒绝重复请求。"
    exit 1
fi
LOCK_ACQUIRED=1

cd "$BASE" || {
    log "ERROR 无法进入工作目录：$BASE"
    exit 1
}

if [ ! -f "$START_SCRIPT" ]; then
    log "ERROR 启动脚本不存在：$START_SCRIPT"
    exit 1
fi

OLD_PID="${1:-}"
if [ -z "$OLD_PID" ] && [ -f "$PID_FILE" ]; then
    OLD_PID="$(tr -dc '0-9' < "$PID_FILE" 2>/dev/null || true)"
fi

if [ -z "$OLD_PID" ]; then
    log "ERROR 没有可用的旧 Web PID，未执行重启。"
    exit 1
fi

case "$OLD_PID" in
    ''|*[!0-9]*)
        log "ERROR Web PID 无效：$OLD_PID"
        exit 1
        ;;
esac

is_web_process() {
    local pid="$1"
    [ -d "/proc/$pid" ] || return 1

    local cwd cmdline
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    [ "$cwd" = "$BASE" ] || return 1

    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmdline" in
        *"$BASE/.venv/bin/python3"*"web_app.py"*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

if ! kill -0 "$OLD_PID" 2>/dev/null; then
    log "WARN 旧 Web PID=$OLD_PID 已不存在，跳过停止，继续尝试启动。"
else
    if ! is_web_process "$OLD_PID"; then
        log "ERROR PID=$OLD_PID 存在，但不是 quark-follow 的 web_app.py，拒绝执行 kill。"
        exit 1
    fi

    log "INIT 开始停止旧 Web：pid=$OLD_PID"
    if ! kill -TERM "$OLD_PID" 2>/dev/null; then
        log "ERROR 发送 SIGTERM 失败：pid=$OLD_PID"
        exit 1
    fi

    stopped=0
    for ((i=1; i<=MAX_STOP_WAIT; i++)); do
        if ! kill -0 "$OLD_PID" 2>/dev/null; then
            stopped=1
            break
        fi
        sleep 1
    done

    if [ "$stopped" -ne 1 ]; then
        log "ERROR 旧 Web 在 ${MAX_STOP_WAIT}s 内没有退出，未启动新 Web。"
        exit 1
    fi

    if [ -f "$PID_FILE" ]; then
        current_pid="$(tr -dc '0-9' < "$PID_FILE" 2>/dev/null || true)"
        if [ "$current_pid" = "$OLD_PID" ]; then
            rm -f "$PID_FILE"
        fi
    fi
    log "INIT 旧 Web 已退出：pid=$OLD_PID"
fi

log "INIT 启动新 Web：$START_SCRIPT"
nohup /bin/bash "$START_SCRIPT" > "$LOG_DIR/web_app.log" 2>&1 < /dev/null &
NEW_PID=$!
printf '%s\n' "$NEW_PID" > "$PID_FILE"
chmod 600 "$PID_FILE" 2>/dev/null || true

started=0
healthy=0
for ((i=1; i<=MAX_START_WAIT; i++)); do
    if kill -0 "$NEW_PID" 2>/dev/null; then
        if is_web_process "$NEW_PID"; then
            started=1
        fi
    fi

    if [ "$started" -eq 1 ]; then
        http_code="$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/login" 2>/dev/null || true)"
        if [ "$http_code" = "200" ]; then
            healthy=1
            break
        fi
    fi
    sleep 1
done

if [ "$healthy" -eq 1 ]; then
    log "INIT Web 重启成功：old_pid=$OLD_PID new_pid=$NEW_PID http=200 port=$PORT"
    exit 0
fi

if [ "$started" -eq 0 ]; then
    log "ERROR 新 Web 进程未能正常存活：new_pid=$NEW_PID"
else
    log "ERROR 新 Web 进程存在，但健康检查未通过：new_pid=$NEW_PID port=$PORT"
fi

if kill -0 "$NEW_PID" 2>/dev/null && [ -f "$PID_FILE" ]; then
    current_pid="$(tr -dc '0-9' < "$PID_FILE" 2>/dev/null || true)"
    if [ "$current_pid" = "$NEW_PID" ]; then
        rm -f "$PID_FILE"
    fi
fi
exit 1
