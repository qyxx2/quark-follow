#!/bin/bash

# ============================================================
# Quark 自动资源维护系统
# resource_check.sh
#
# 唯一人工入口：resources.json
#   {
#     "resources": [
#       {
#         "url": "https://www.seedhub.cc/movies/127174/",
#         "directory": "/kuake/电视剧"
#       }
#     ]
#   }
#
# directory 为空/省略：使用 config 的 WEBDAV_DEFAULT_ROOT
# 例如：
#   directory="/kuake/电视剧"
#   SeedHub 标题="test"
#   -> /kuake/电视剧/test
#
# 本脚本只负责：
#   1. 读取 resources.json
#   2. 调用 seedhub_cache.sh 发现/更新 shows / shares
#   3. 根据剧集名称决定 WebDAV 目标目录
#   4. 必要时自动创建 WebDAV 目标目录
#   5. 扫描当前 WebDAV，更新 webdav_files
#   6. 判断缺集
#   7. 调用 source_check.sh 更新 share_files
#   8. 选择少量最合适的 Share，自动生成 tasks
#   9. 调用 addfile.sh 补缺
#  10. 在 02:00 <= 当前时间 < 07:00 时，额外生成/执行 replace_queue
#
# 不实现：
#   - SeedHub HTML/Playwright 解析      -> seedhub_cache.sh
#   - Quark 分享内部扫描                 -> source_check.sh
#   - Quark 转存/补缺细节               -> addfile.sh
#   - 替换执行                           -> replace.sh
#
# 本脚本没有 --test / --dry-run / --show-id 等额外入口。
# ============================================================

set -u

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONFIG="$BASE_DIR/config.local"
RESOURCES_FILE="$BASE_DIR/resources.json"
DB="$BASE_DIR/resource.db"
TASKS="$BASE_DIR/tasks"
LOG_DIR_DEFAULT="$BASE_DIR/logs"
LOG_DIR="$LOG_DIR_DEFAULT"
LOG_FILE=""

SEEDHUB_CACHE_SH="$BASE_DIR/seedhub_cache.sh"
SOURCE_CHECK_SH="$BASE_DIR/source_check.sh"
ADDFILE_SH="$BASE_DIR/addfile.sh"
REPLACE_SH="$BASE_DIR/replace.sh"

TMP_ROOT="/tmp/quark-follow-resource-$$"
LOCK_DIR="$BASE_DIR/resource_check.lock"

RESOURCE_RESULT="FAILED"
TARGET_PATH=""
RESOURCE_SEASON=1

mkdir -p "$TMP_ROOT"

cleanup() {
    rm -rf "$TMP_ROOT"
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ============================================================
# 读取配置
# ============================================================

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: 找不到 config.local：$CONFIG" >&2
    exit 2
fi

# shellcheck disable=SC1090
. "$CONFIG"

: "${QUARK_COOKIE:?config.local 中没有 QUARK_COOKIE}"
: "${OPENLIST_URL:?config.local 中没有 OPENLIST_URL}"
: "${OPENLIST_TOKEN:?config.local 中没有 OPENLIST_TOKEN}"
: "${WEBDAV_URL:?config.local 中没有 WEBDAV_URL}"
: "${WEBDAV_USER:?config.local 中没有 WEBDAV_USER}"
: "${WEBDAV_PASS:?config.local 中没有 WEBDAV_PASS}"

LOG_DIR="${LOG_DIR:-$LOG_DIR_DEFAULT}"
LOG_FILE="$LOG_DIR/resource_check.log"

WEBDAV_DEFAULT_ROOT="${WEBDAV_DEFAULT_ROOT:-/kuake/其他}"

# 每次运行都做补缺，因此默认开启。
RESOURCE_AUTO_ADD="${RESOURCE_AUTO_ADD:-true}"
RESOURCE_AUTO_DISCOVER="${RESOURCE_AUTO_DISCOVER:-true}"

# 已有 Share 的重新验证上限，防止设备负载过高。
RESOURCE_RECHECK_EXISTING_MAX="${RESOURCE_RECHECK_EXISTING_MAX:-3}"
RESOURCE_RECHECK_HOURS="${RESOURCE_RECHECK_HOURS:-24}"

# 每轮 SeedHub 增量发现次数。
RESOURCE_SEEDHUB_ROUNDS="${RESOURCE_SEEDHUB_ROUNDS:-3}"

# tasks 给 addfile.sh 的 Share 数量上限。
RESOURCE_TASK_MAX="${RESOURCE_TASK_MAX:-3}"

# 替换阶段每次最多主动重新验证多少个旧 Share。
REPLACE_SOURCE_CHECK_MAX="${REPLACE_SOURCE_CHECK_MAX:-5}"

# 替换条件：只有同时满足才进入 replace_queue。
REPLACE_MIN_RATIO="${REPLACE_MIN_RATIO:-1.30}"
REPLACE_MIN_GAIN_MB="${REPLACE_MIN_GAIN_MB:-500}"

# 替换任务开关。时间窗口仍然由本脚本强制限制。
REPLACE_ENABLED="${REPLACE_ENABLED:-true}"
REPLACE_WINDOW_START_HOUR="${REPLACE_WINDOW_START_HOUR:-2}"
REPLACE_WINDOW_END_HOUR="${REPLACE_WINDOW_END_HOUR:-7}"

# 与已有脚本保持一致。
OPENLIST_PASSWORD="${OPENLIST_PASSWORD:-}"
CURL_TLS_MAX="${CURL_TLS_MAX:-1.2}"

# addfile 返回后必须通过真实 WebDAV 再确认文件已经出现。
RESOURCE_ADD_VERIFY_TIMEOUT="${RESOURCE_ADD_VERIFY_TIMEOUT:-900}"
RESOURCE_ADD_VERIFY_INTERVAL="${RESOURCE_ADD_VERIFY_INTERVAL:-10}"

# OpenList 新建/变更目录后，WebDAV 驱动可能需要短暂同步时间。
RESOURCE_WEBDAV_READY_TIMEOUT="${RESOURCE_WEBDAV_READY_TIMEOUT:-60}"
RESOURCE_WEBDAV_READY_INTERVAL="${RESOURCE_WEBDAV_READY_INTERVAL:-2}"

# ============================================================
# 唯一入口：不接受额外参数
# ============================================================

if [ "$#" -ne 0 ]; then
    echo "ERROR: resource_check.sh 不接受命令行参数；请只修改 resources.json 和 config.local。" >&2
    exit 2
fi

# ============================================================
# 全局锁
# ============================================================

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "ERROR: resource_check 正在运行：$LOCK_DIR" >&2
    exit 1
fi

# ============================================================
# 日志
# ============================================================

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE" 2>/dev/null || true
chmod 600 "$RESOURCES_FILE" 2>/dev/null || true
chmod 600 "$CONFIG" 2>/dev/null || true
chmod 700 "$0" 2>/dev/null || true

log() {
    printf '[%s] [CHECK] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}

info() {
    printf '[CHECK] INFO: %s\n' "$*"
    log "$*"
}

warn() {
    printf '[CHECK] WARN: %s\n' "$*" >&2
    log "WARN: $*"
}

error() {
    printf '[CHECK] ERROR: %s\n' "$*" >&2
    log "ERROR: $*"
}

# ============================================================
# 依赖 / 文件
# ============================================================

for CMD in bash curl jq awk sed grep sort cut tr date sleep mktemp sqlite3 md5sum python3; do
    if ! command -v "$CMD" >/dev/null 2>&1; then
        error "缺少命令：$CMD"
        exit 2
    fi
done

for FILE in "$RESOURCES_FILE" "$DB" "$SEEDHUB_CACHE_SH" "$SOURCE_CHECK_SH" "$ADDFILE_SH"; do
    if [ ! -f "$FILE" ]; then
        error "缺少文件：$FILE"
        exit 2
    fi
done

if ! jq -e . "$RESOURCES_FILE" >/dev/null 2>&1; then
    error "resources.json 不是有效 JSON：$RESOURCES_FILE"
    exit 2
fi

if ! jq -e '(.resources? // .) | type == "array"' "$RESOURCES_FILE" >/dev/null 2>&1; then
    error "resources.json 格式错误：需要对象中的 resources 数组，或直接使用数组。"
    exit 2
fi

# ============================================================
# 数据库结构检查
# 不创建、不修改表结构；数据库设计以既有数据库设计文档为准。
# ============================================================

sqlite_table_exists() {
    sqlite3 "$DB" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='$1' LIMIT 1;" |
        grep -qx '1'
}

sqlite_column_exists() {
    sqlite3 "$DB" "SELECT 1 FROM pragma_table_info('$1') WHERE name='$2' LIMIT 1;" |
        grep -qx '1'
}

for table in shows shares share_files webdav_files replace_queue; do
    if ! sqlite_table_exists "$table"; then
        error "数据库缺少表：$table"
        exit 2
    fi
done

for spec in \
    'shows:id' 'shows:name' 'shows:seedhub_url' 'shows:webdav_path' \
    'shows:total_episodes' 'shows:latest_episode' 'shows:share_scan_rank' \
    'shows:last_scan' 'shows:updated_at' \
    'shares:id' 'shares:show_id' 'shares:url' 'shares:seedhub_rank' 'shares:status' \
    'shares:fail_count' 'shares:last_check' 'shares:last_success' \
    'share_files:id' 'share_files:share_id' 'share_files:episode' 'share_files:filename' \
    'share_files:size' 'share_files:last_check' \
    'webdav_files:id' 'webdav_files:show_id' 'webdav_files:episode' 'webdav_files:filename' \
    'webdav_files:size' 'webdav_files:mtime' 'webdav_files:last_check' 'webdav_files:source_share_id' \
    'replace_queue:id' 'replace_queue:show_id' 'replace_queue:episode' 'replace_queue:source_share_id' \
    'replace_queue:source_file' 'replace_queue:old_filename' 'replace_queue:old_size' \
    'replace_queue:new_size' 'replace_queue:status' 'replace_queue:created_at' \
    'replace_queue:finished_at' 'replace_queue:error'; do
    table="${spec%%:*}"
    column="${spec#*:}"
    if ! sqlite_column_exists "$table" "$column"; then
        error "数据库表 $table 缺少字段：$column"
        exit 2
    fi
done

SQL_BUSY_TIMEOUT='PRAGMA busy_timeout=10000;'

sql_quote() {
    local value="${1:-}"
    value="${value//\'/\'\'}"
    printf "'%s'" "$value"
}

normalize_directory() {
    local value="${1:-}"
    # Remove spaces between Chinese characters, trim surrounding whitespace,
    # and collapse repeated path separators.
    value="$(printf '%s' "$value" | sed -E 's/([一-龥])[[:space:]]+([一-龥])/\1\2/g')"
    value="$(printf '%s' "$value" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//; s#/{2,}#/#g')"
    printf '%s' "$value"
}

# ============================================================
# curl / OpenList / WebDAV
# ============================================================

CURL_TLS_ARGS=()
[ -n "$CURL_TLS_MAX" ] && CURL_TLS_ARGS+=(--tls-max "$CURL_TLS_MAX")

openlist_curl() {
    curl -sS \
        --connect-timeout 20 \
        --max-time 60 \
        "${CURL_TLS_ARGS[@]}" \
        -H "Authorization: $OPENLIST_TOKEN" \
        -H 'Content-Type: application/json' \
        "$@"
}

webdav_request() {
    curl -sS \
        --connect-timeout 20 \
        --max-time 60 \
        "${CURL_TLS_ARGS[@]}" \
        -u "$WEBDAV_USER:$WEBDAV_PASS" \
        "$@"
}

webdav_exists() {
    local path="$1"
    local code

    code="$(
        webdav_request \
            -o /dev/null \
            -w '%{http_code}' \
            -X PROPFIND \
            -H 'Depth: 0' \
            -H 'Content-Type: application/xml' \
            "${WEBDAV_URL%/}${path}" 2>/dev/null
    )" || code="000"

    case "$code" in
        200|207) return 0 ;;
        *) return 1 ;;
    esac
}
webdav_wait_available() {
    local path="$1"
    local timeout="${RESOURCE_WEBDAV_READY_TIMEOUT:-60}"
    local interval="${RESOURCE_WEBDAV_READY_INTERVAL:-2}"
    local deadline now remaining

    [[ "$timeout" =~ ^[0-9]+$ ]] || timeout=60
    [[ "$interval" =~ ^[0-9]+$ ]] || interval=2
    [ "$interval" -gt 0 ] || interval=1

    if webdav_exists "$path"; then
        return 0
    fi

    info "WebDAV 尚未看到目录，等待目录同步：$path timeout=${timeout}s"
    deadline=$(( $(date +%s) + timeout ))

    while :; do
        now=$(date +%s)
        [ "$now" -ge "$deadline" ] && break

        if webdav_exists "$path"; then
            info "WebDAV 目录已可访问：$path"
            return 0
        fi

        remaining=$((deadline-now))
        if [ "$interval" -lt "$remaining" ]; then
            sleep "$interval"
        else
            sleep "$remaining"
        fi
    done

    return 1
}

openlist_target_exists() {
    local target="$1"
    local result code is_dir

    result="$(
        openlist_curl \
            -X POST \
            "$OPENLIST_URL/api/fs/get" \
            --data "$(jq -nc --arg p "$target" --arg pw "$OPENLIST_PASSWORD" '{path:$p,password:$pw}')"
    )" || return 1

    code="$(printf '%s' "$result" | jq -r '.code // 0' 2>/dev/null || echo 0)"
    [ "$code" = "200" ] || return 1
    is_dir="$(printf '%s' "$result" | jq -r '.data.is_dir // false' 2>/dev/null || echo false)"
    [ "$is_dir" = "true" ]
}

ensure_webdav_dir() {
    local target="$1"
    local normalized part prefix
    local IFS='/'
    local parts=()

    case "$target" in
        /kuake|/kuake/*) ;;
        *) error "WebDAV 目标必须以 /kuake 开头：$target"; return 1 ;;
    esac

    normalized="${target%/}"
    [ -n "$normalized" ] || normalized='/kuake'
    read -r -a parts <<< "${normalized#/}"
    prefix=""

    for part in "${parts[@]}"; do
        [ -n "$part" ] || continue
        prefix="${prefix}/${part}"
        if openlist_target_exists "$prefix"; then
            continue
        fi

        info "创建 OpenList 目录：$prefix"
        local result code
        result="$(openlist_curl -X POST "$OPENLIST_URL/api/fs/mkdir" \
            --data "$(jq -nc --arg p "$prefix" '{path:$p}')")" || result=''
        code="$(printf '%s' "$result" | jq -r '.code // 0' 2>/dev/null || echo 0)"

        # 已存在时部分驱动可能返回非 200；只要 get 能确认目录就继续。
        if [ "$code" != "200" ] && ! openlist_target_exists "$prefix"; then
            error "创建 OpenList 目录失败：$prefix code=$code message=$(printf '%s' "$result" | jq -r '.message // "unknown"' 2>/dev/null)"
            return 1
        fi
        if ! openlist_target_exists "$prefix"; then
            error "OpenList 目录创建后仍无法确认：$prefix"
            return 1
        fi
    done

    if ! webdav_wait_available "$normalized"; then
        error "OpenList 目录存在，但 WebDAV 在等待窗口内仍不可访问：$normalized"
        return 1
    fi
    return 0
}

# ============================================================
# WebDAV 文件名 -> episode
# 这里只承担 WebDAV 当前文件映射；Quark 分享内部解析仍由 source_check。
# ============================================================

normalize_circled() {
    local s="$1"
    s="${s//⓪/0}"; s="${s//①/1}"; s="${s//②/2}"; s="${s//③/3}"; s="${s//④/4}"
    s="${s//⑤/5}"; s="${s//⑥/6}"; s="${s//⑦/7}"; s="${s//⑧/8}"; s="${s//⑨/9}"
    printf '%s' "$s"
}

cn_number() {
    local s="$1"
    case "$s" in
        零) printf 0;; 一) printf 1;; 二|两) printf 2;; 三) printf 3;; 四) printf 4;;
        五) printf 5;; 六) printf 6;; 七) printf 7;; 八) printf 8;; 九) printf 9;;
        十) printf 10;;
        *)
            if [[ "$s" =~ ^[0-9]+$ ]]; then printf '%s' "$s"; return 0; fi
            if [[ "$s" == *十* ]]; then
                local a="${s%%十*}" b="${s#*十}" tens ones
                [ "$a" = "$s" ] || [ -z "$a" ] && tens=1 || tens="$(cn_number "$a")" || return 1
                if [ -n "$b" ] && [ "$b" != "$s" ]; then ones="$(cn_number "$b")" || return 1; else ones=0; fi
                printf '%s' "$((tens*10+ones))"
                return 0
            fi
            if [[ "$s" == *百* ]]; then
                local h="${s%%百*}" rest="${s#*百}" hv rv=0
                [ -n "$h" ] || h=一
                hv="$(cn_number "$h")" || return 1
                if [ -n "$rest" ] && [ "$rest" != "$s" ]; then rv="$(cn_number "$rest")" || return 1; fi
                printf '%s' "$((hv*100+rv))"
                return 0
            fi
            return 1
            ;;
    esac
}

extract_season_from_text() {
    local text="${1:-}"
    local raw

    if [[ "$text" =~ 第[[:space:]]*([0-9]+|[零〇兩两一二三四五六七八九十百]+)[[:space:]]*季 ]]; then
        raw="${BASH_REMATCH[1]}"
    elif [[ "$text" =~ [Ss]eason[[:space:]]*([0-9]{1,2}) ]]; then
        raw="${BASH_REMATCH[1]}"
    elif [[ "$text" =~ (^|[^A-Za-z0-9])[Ss]([0-9]{1,2})([^A-Za-z0-9]|$) ]]; then
        raw="${BASH_REMATCH[2]}"
    else
        return 1
    fi

    raw="${raw//兩/二}"
    if [[ "$raw" =~ ^[0-9]+$ ]]; then
        printf '%d' "$((10#$raw))"
        return 0
    fi
    cn_number "$raw"
}

seasonize_resource_name() {
    local text="${1:-}"
    local season="${2:-1}"
    local base chinese_base

    text="$(printf '%s' "$text" | sed -E 's/[[:space:]]*[-|｜][[:space:]]*SeedHub.*$//I')"
    text="$(printf '%s' "$text" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
    [ -n "$text" ] || return 1

    # 中文标题已经包含“第N季”时，只保留中文标题到季度标记为止。
    if [[ "$text" =~ ^(.*第[[:space:]]*([0-9]+|[零〇兩两一二三四五六七八九十百]+)[[:space:]]*季) ]]; then
        base="${BASH_REMATCH[1]}"
        base="$(printf '%s' "$base" | sed -E 's/[[:space:]]+//g')"
        [[ "$base" =~ [一-龥] ]] && { printf '%s' "$base"; return 0; }
    fi

    # 中文标题后带英文片名时，只保留中文标题；S01 单季不强行增加“第1季”。
    chinese_base="$(printf '%s' "$text" | sed -nE 's/^([一-龥][一-龥[:space:]·・\-—_]*).*/\1/p')"
    chinese_base="$(printf '%s' "$chinese_base" | sed -E 's/[[:space:]]+//g; s/^[[:space:]–—_|｜-]+//; s/[[:space:]–—_|｜-]+$//')"
    if [ -n "$chinese_base" ]; then
        if [ "$season" -gt 1 ] && ! [[ "$chinese_base" =~ 第[一二三四五六七八九十百0-9]+季$ ]]; then
            printf '%s 第%d季' "$chinese_base" "$season"
        else
            printf '%s' "$chinese_base"
        fi
        return 0
    fi

    # 纯英文/混合非中文标题：删除已有 Season/SN 季数，再按季数补中文季度名。
    base="$(printf '%s' "$text" | sed -E 's/[[:space:]]*Season[[:space:]]*[0-9]{1,2}//Ig; s/(^|[^A-Za-z0-9])[Ss][[:space:]]*[0-9]{1,2}([^A-Za-z0-9]|$)/\1\2/g; s/[[:space:]]{2,}/ /g; s/^[[:space:]–—_|｜-]+//; s/[[:space:]–—_|｜-]+$//')"
    [ -n "$base" ] || base="$text"
    if [ "$season" -gt 1 ]; then
        printf '%s 第%d季' "$base" "$season"
    else
        printf '%s' "$base"
    fi
}

parse_episode() {
    local name="$1"
    local context="${2:-}"
    local stem="${name%.*}"
    local season="${RESOURCE_SEASON:-1}"
    local context_season episode season_raw episode_raw
    local explicit_season=0

    stem="$(normalize_circled "$stem")"

    if [[ "$stem" =~ [Ss]([0-9]{1,2})[[:space:]_.-]*[Ee][Pp]?([0-9]{1,4}) ]]; then
        season=$((10#${BASH_REMATCH[1]}))
        episode=$((10#${BASH_REMATCH[2]}))
        explicit_season=1
    elif [[ "$stem" =~ [Ee][Pp]?([0-9]{1,4}) ]]; then
        episode=$((10#${BASH_REMATCH[1]}))
    else
        if [[ "$stem" =~ 第[[:space:]]*([0-9]+|[零〇兩两一二三四五六七八九十百]+)[[:space:]]*季 ]]; then
            season_raw="${BASH_REMATCH[1]}"
            season_raw="${season_raw//兩/二}"
            season="$(cn_number "$season_raw")" || return 1
            explicit_season=1
        fi

        if [[ "$stem" =~ 第[[:space:]]*([0-9]+|[零〇兩两一二三四五六七八九十百]+)[[:space:]]*集 ]]; then
            episode_raw="${BASH_REMATCH[1]}"
            episode_raw="${episode_raw//兩/二}"
            episode="$(cn_number "$episode_raw")" || return 1
        elif [[ "$stem" =~ ^[[:space:]]*([0-9]{1,3})[[:space:]_.-]*$ ]]; then
            episode=$((10#${BASH_REMATCH[1]}))
        elif [[ "$stem" =~ [Ee]pisode[[:space:]_.-]*([0-9]{1,4}) ]]; then
            episode=$((10#${BASH_REMATCH[1]}))
        else
            return 1
        fi
    fi

    if [ "$explicit_season" -eq 0 ] && [ -n "$context" ]; then
        context_season="$(extract_season_from_text "$context" 2>/dev/null || true)"
        if [[ "$context_season" =~ ^[0-9]+$ ]] && [ "$context_season" -ge 1 ]; then
            season="$context_season"
        fi
    fi

    [ "${season:-0}" -ge 1 ] 2>/dev/null || return 1
    [ "${episode:-0}" -ge 1 ] 2>/dev/null || return 1
    printf 'S%02dE%02d' "$season" "$episode"
}

scan_webdav_actual() {
    local target="$1"
    local output="$2"
    local season="${3:-${RESOURCE_SEASON:-1}}"
    local xml="$TMP_ROOT/webdav-propfind-$$.xml"
    local raw="$TMP_ROOT/webdav-raw-$$.tsv"

    : > "$output"
    if ! webdav_exists "$target"; then
        error "WebDAV 目录不可访问：$target"
        return 1
    fi
    if ! webdav_request -X PROPFIND -H 'Depth: 1' -H 'Content-Type: application/xml' -o "$xml" "${WEBDAV_URL%/}${target}"; then
        error "WebDAV PROPFIND 失败：$target"
        rm -f "$xml"
        return 1
    fi

    if ! python3 -c 'import sys,xml.etree.ElementTree as E;from urllib.parse import urlsplit,unquote; b=sys.argv[1].rstrip("/"); r=E.parse(sys.argv[2]).getroot(); ln=lambda t:t.rsplit("}",1)[-1];
for n in r.iter():
  if ln(n.tag)!="response": continue
  h=next(((x.text or "").strip() for x in n if ln(x.tag)=="href"),"")
  if not h: continue
  p=unquote(urlsplit(h).path).rstrip("/") or "/"
  if p.rstrip("/")==b.rstrip("/"): continue
  name=p.rsplit("/",1)[-1]
  if not name: continue
  rt_any=any(ln(x.tag)=="collection" for x in n.iter())
  if rt_any: continue
  z=next(((x.text or "").strip() for x in n.iter() if ln(x.tag)=="getcontentlength"),"0") or "0"
  if not z.isdigit(): z="0"
  mt=next(((x.text or "").strip() for x in n.iter() if ln(x.tag)=="getlastmodified"),"")
  print(name+"\\t"+z+"\\t"+mt)' "$target" "$xml" > "$raw"; then
        error "解析 WebDAV PROPFIND XML 失败：$target"
        rm -f "$xml" "$raw"
        return 1
    fi

    local name size mtime key
    while IFS=$'\t' read -r name size mtime; do
        [ -n "$name" ] || continue
        key="$(parse_episode "$name" "$target" 2>/dev/null || true)"
        [ -n "$key" ] || continue

        # WebDAV 缓存保留目标目录中所有可识别季度；
        # 当前季度隔离只在 get_max_known_episode/get_missing_file/candidate
        # 等计算阶段完成，不能在这里把其它季度从 webdav_files 中删掉。
        [[ "$key" =~ ^S[0-9]{2}E[0-9]{2}$ ]] || continue
        [[ "$size" =~ ^[0-9]+$ ]] || size=0
        printf '%s\t%s\t%s\t%s\n' "$key" "$name" "$size" "$mtime" >> "$output"
    done < "$raw"

    sort -u "$output" -o "$output" 2>/dev/null || true
    rm -f "$xml" "$raw"
    return 0
}

scan_webdav_dir() {
    local target="$1"
    local output="$2"
    local page=1 per_page=100
    local result count total name size mtime key is_dir

    : > "$output"

    if ! webdav_exists "$target"; then
        error "WebDAV 目录不可访问：$target"
        return 1
    fi

    while :; do
        result="$(
            openlist_curl \
                -X POST \
                "$OPENLIST_URL/api/fs/list" \
                --data "$(jq -nc \
                    --arg p "$target" \
                    --arg pw "$OPENLIST_PASSWORD" \
                    --argjson pg "$page" \
                    --argjson pp "$per_page" \
                    --argjson rf "$([ "$page" -eq 1 ] && echo true || echo false)" \
                    '{path:$p,password:$pw,refresh:$rf,page:$pg,per_page:$pp}')"
        )" || return 1

        if ! printf '%s' "$result" | jq -e . >/dev/null 2>&1; then
            error "OpenList 返回非 JSON：$target page=$page"
            return 1
        fi

        if [ "$(printf '%s' "$result" | jq -r '.code // 0')" != "200" ]; then
            error "OpenList 列目录失败：$target page=$page message=$(printf '%s' "$result" | jq -r '.message // "unknown"')"
            return 1
        fi

        while IFS=$'\t' read -r name size mtime is_dir; do
            [ -n "$name" ] || continue
            [ "$is_dir" = "false" ] || continue
            key="$(parse_episode "$name" "$target" 2>/dev/null || true)"
            [ -n "$key" ] || continue
            [[ "$key" == "$(printf 'S%02dE' "$RESOURCE_SEASON")"* ]] || continue
            [[ "${size:-0}" =~ ^[0-9]+$ ]] || size=0
            printf '%s\t%s\t%s\t%s\n' "$key" "$name" "$size" "$mtime" >> "$output"
        done < <(
            printf '%s' "$result" |
            jq -r '.data.content[]? | [(.name // ""),(.size // 0),(.modified // .mtime // .updated_at // ""),(.is_dir // false)] | @tsv'
        )

        count="$(printf '%s' "$result" | jq '.data.content | length')"
        total="$(printf '%s' "$result" | jq '.data.total // .data.count // 0')"
        [ "$count" -eq 0 ] && break

        if [ "$total" -gt 0 ]; then
            [ $((page * per_page)) -ge "$total" ] && break
        elif [ "$count" -lt "$per_page" ]; then
            break
        fi

        page=$((page + 1))
    done

    sort -u "$output" -o "$output" 2>/dev/null || true
    return 0
}

update_webdav_cache() {
    local show_id="$1"
    local scan_file="$2"
    local sql_file="$TMP_ROOT/webdav-write-${show_id}.sql"
    local latest=0 key n

    # latest_episode 取当前季度从 E01 开始的最高连续集数，
    # 不把其它季度或中间断集的最大编号误当成最新。
    local season_prefix
    printf -v season_prefix 'S%02dE' "$RESOURCE_SEASON"

    declare -A present=()
    while IFS=$'\t' read -r key _filename _size _mtime; do
        [ -n "$key" ] || continue
        [[ "$key" == "$season_prefix"* ]] || continue
        present["$key"]=1
    done < "$scan_file"

    local i
    while :; do
        i=$((latest + 1))
        printf -v key 'S%02dE%02d' "$RESOURCE_SEASON" "$i"
        [ "${present[$key]:-0}" = "1" ] || break
        latest="$i"
    done

    {
        printf '%s\n' "$SQL_BUSY_TIMEOUT"
        printf 'BEGIN IMMEDIATE;\n'
        printf 'DELETE FROM webdav_files WHERE show_id=%s;\n' "$(sql_quote "$show_id")"

        while IFS=$'\t' read -r key name size mtime; do
            [ -n "$key" ] || continue
            printf 'INSERT INTO webdav_files (show_id,episode,filename,size,mtime,last_check,source_share_id) VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,NULL);\n' \
                "$(sql_quote "$show_id")" \
                "$(sql_quote "$key")" \
                "$(sql_quote "$name")" \
                "${size:-0}" \
                "$(sql_quote "${mtime:-}")"
        done < "$scan_file"

        printf 'UPDATE shows SET latest_episode=%s,last_scan=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=%s;\n' \
            "$latest" "$(sql_quote "$show_id")"
        printf 'COMMIT;\n'
    } > "$sql_file"

    if ! sqlite3 "$DB" < "$sql_file"; then
        error "写入 webdav_files 失败：show_id=$show_id"
        return 1
    fi

    # 已存在文件可按“文件名 + 大小”与 share_files 做安全的 source_share_id 回填。
    sqlite3 "$DB" <<SQL >/dev/null 2>&1 || true
$SQL_BUSY_TIMEOUT
UPDATE webdav_files
SET source_share_id = (
    SELECT sf.share_id
    FROM share_files sf
    JOIN shares ss ON ss.id=sf.share_id
    WHERE ss.show_id=webdav_files.show_id
      AND sf.episode=webdav_files.episode
      AND sf.filename=webdav_files.filename
      AND COALESCE(sf.size,0)=COALESCE(webdav_files.size,0)
      AND COALESCE(ss.status,'unknown') NOT IN ('dead','excluded')
    ORDER BY COALESCE(ss.seedhub_rank,999999), ss.id
    LIMIT 1
)
WHERE show_id=$(sql_quote "$show_id")
  AND source_share_id IS NULL;
SQL

    return 0
}

# ============================================================
# 缺集 / share_files 候选
# ============================================================

declare -A WEBDAV_NAME=()
declare -A WEBDAV_SIZE=()

load_webdav_best() {
    local file="$1" key name size mtime
    WEBDAV_NAME=(); WEBDAV_SIZE=()
    while IFS=$'\t' read -r key name size mtime; do
        [ -n "$key" ] || continue
        [[ "${size:-0}" =~ ^[0-9]+$ ]] || size=0
        if [ -z "${WEBDAV_NAME[$key]+x}" ] || [ "$size" -gt "${WEBDAV_SIZE[$key]:-0}" ]; then
            WEBDAV_NAME["$key"]="$name"
            WEBDAV_SIZE["$key"]="$size"
        fi
    done < "$file"
}

get_max_known_episode() {
    local show_id="$1"
    local webdav_file="${2:-}"
    local share_max=0 webdav_max=0 max=0
    local season_padded
    printf -v season_padded '%02d' "$RESOURCE_SEASON"

    share_max="$(sqlite3 "$DB" "SELECT COALESCE(MAX(CAST(substr(episode,5) AS INTEGER)),0) FROM share_files sf JOIN shares s ON s.id=sf.share_id WHERE s.show_id=$(sql_quote "$show_id") AND COALESCE(s.status,'unknown') NOT IN ('dead','excluded') AND sf.episode GLOB 'S${season_padded}E[0-9][0-9]';")"

    if [ -n "$webdav_file" ] && [ -s "$webdav_file" ]; then
        webdav_max="$(awk -F '\t' -v prefix="S${season_padded}E" '
            $1 ~ ("^" prefix "[0-9][0-9]$") {
                n=substr($1,5)+0
                if (n>m) m=n
            }
            END {print m+0}
        ' "$webdav_file")"
    fi

    [ "${share_max:-0}" -gt "${webdav_max:-0}" ] && max="$share_max" || max="$webdav_max"
    printf '%s' "$max"
}

get_missing_file() {
    local max_episode="$1" output="$2" i key
    : > "$output"
    [ "${max_episode:-0}" -gt 0 ] || return 0
    for ((i=1; i<=max_episode; i++)); do
        printf -v key 'S%02dE%02d' "$RESOURCE_SEASON" "$i"
        [ -n "${WEBDAV_NAME[$key]+x}" ] || printf '%s\n' "$key" >> "$output"
    done
}

build_candidates() {
    local show_id="$1" missing_file="$2" output="$3"
    local in_sql='' ep
    : > "$output"

    while IFS= read -r ep; do
        [ -n "$ep" ] || continue
        [ -n "$in_sql" ] && in_sql+=","
        in_sql+="$(sql_quote "$ep")"
    done < "$missing_file"
    [ -n "$in_sql" ] || return 0

    sqlite3 -tabs "$DB" \
        "SELECT s.id,s.url,COALESCE(s.seedhub_rank,999999),COALESCE(s.fail_count,0),sf.episode,COALESCE(sf.size,0),sf.filename FROM share_files sf JOIN shares s ON s.id=sf.share_id WHERE s.show_id=$(sql_quote "$show_id") AND COALESCE(s.status,'unknown') NOT IN ('dead','excluded') AND sf.episode IN ($in_sql) ORDER BY sf.episode,COALESCE(sf.size,0) DESC,COALESCE(s.seedhub_rank,999999),s.id;" \
        > "$output"
}

count_unresolved() {
    awk -F '\t' 'NR==FNR{m[$1]=1;next}{f[$5]=1}END{c=0;for(e in m)if(!(e in f))c++;print c}' "$1" "$2"
}

select_task_shares() {
    local show_id="$1" candidate_file="$2" missing_file="$3" output="$4"
    : > "$output"

    # 只有当前缺集至少存在一个可用候选来源时才生成 tasks。
    # candidate_file 已经按 show_id + missing episode 过滤；这里再次直接从数据库
    # 建立候选 Share 集合，并对这些 Share 的“全部已识别集数”进行统计。
    [ -s "$candidate_file" ] || return 0
    [ -s "$missing_file" ] || return 0

    local in_sql='' ep
    while IFS= read -r ep; do
        [ -n "$ep" ] || continue
        [ -n "$in_sql" ] && in_sql+=","
        in_sql+="$(sql_quote "$ep")"
    done < "$missing_file"
    [ -n "$in_sql" ] || return 0

    # 重要：
    # 1. candidate_shares 只允许当前 show_id 且至少覆盖一个当前缺集的 Share；
    # 2. episode_count 对候选 Share 的全部 share_files 统计 DISTINCT episode，
    #    不再只统计 missing_file 中的集；
    # 3. 因此排序含义为：完整已识别集数多 -> SeedHub rank 靠前 -> share_id 小。
    # 这样不会把不同 show 的 Share 混入排名，也不会因为当前缺集不同而改变
    # 一个 Share 的“完整集数”排名定义。
    sqlite3 -tabs "$DB" <<SQL > "$output"
$SQL_BUSY_TIMEOUT
WITH candidate_shares AS (
    SELECT DISTINCT s.id
    FROM share_files sf
    JOIN shares s ON s.id=sf.share_id
    WHERE s.show_id=$(sql_quote "$show_id")
      AND COALESCE(s.status,'unknown') NOT IN ('dead','excluded')
      AND sf.episode IN ($in_sql)
)
SELECT id,url,seedhub_rank
FROM (
    SELECT
        s.id,
        s.url,
        COALESCE(s.seedhub_rank,999999) AS seedhub_rank,
        COUNT(DISTINCT CASE WHEN sf_all.episode GLOB ('S' || printf('%02d', $RESOURCE_SEASON) || 'E*') THEN sf_all.episode END) AS episode_count
    FROM candidate_shares cs
    JOIN shares s ON s.id=cs.id
    JOIN share_files sf_all ON sf_all.share_id=s.id
    WHERE s.show_id=$(sql_quote "$show_id")
      AND COALESCE(s.status,'unknown') NOT IN ('dead','excluded')
    GROUP BY s.id,s.url,s.seedhub_rank
) ranked
ORDER BY episode_count DESC,seedhub_rank ASC,id ASC
LIMIT $RESOURCE_TASK_MAX;
SQL

    # tasks 内部按 SeedHub rank 输出，保持稳定、可读。
    sort -t $'\t' -k3,3n -k1,1n "$output" -o "$output" 2>/dev/null || true
}
write_task() {
    local name="$1" target="$2" selected_file="$3" missing_file="$4"
    local tmp="$TMP_ROOT/tasks.tmp"
    local line='' episode_csv='' ep

    : > "$tmp"

    # tasks 必须带本轮明确缺集白名单；addfile.sh 将严格按该名单转存。
    if [ -s "$selected_file" ]; then
        while IFS= read -r ep; do
            [ -n "$ep" ] || continue
            [ -n "$episode_csv" ] && episode_csv+=","
            episode_csv+="$ep"
        done < "$missing_file"

        if [ -z "$episode_csv" ]; then
            warn "[$name] 有候选 Share，但缺集白名单为空，拒绝生成补缺任务"
        else
            line="TASK|$name|$target|EPISODES=$episode_csv"
            while IFS=$'\t' read -r _sid url _rank; do
                [ -n "$url" ] || continue
                line+="|$url"
            done < "$selected_file"
            printf '%s\n' "$line" > "$tmp"
        fi
    fi

    mv -f "$tmp" "$TASKS"
    chmod 600 "$TASKS" 2>/dev/null || true
}

lookup_show() {
    local url="$1"
    sqlite3 -tabs "$DB" \
        "SELECT id,name,COALESCE(total_episodes,0),webdav_path,COALESCE(share_scan_rank,0) FROM shows WHERE seedhub_url=$(sql_quote "$url") ORDER BY id LIMIT 1;"
}

sync_show_path() {
    local show_id="$1" name="$2" directory="$3" target_root title_component target
    TARGET_PATH=""

    if [ -n "$directory" ]; then
        target_root="$(normalize_directory "${directory%/}")"
    else
        target_root="$(normalize_directory "${WEBDAV_DEFAULT_ROOT%/}")"
    fi

    case "$target_root" in
        /kuake) ;;
        /kuake/*) ;;
        *)
            error "resources.json 的 directory 必须是 /kuake/...：$directory"
            return 1
            ;;
    esac

    title_component="$name"
    title_component="$(printf '%s' "$title_component" | tr '\r\n\t' '   ' | sed 's#/#_#g; s#\\#_#g; s/[[:space:]]\{1,\}/ /g; s/^ *//; s/ *$//')"
    [ -n "$title_component" ] || title_component="show-$show_id"
    [ "$title_component" != "." ] || title_component="_"
    [ "$title_component" != ".." ] || title_component="_"

    target="$target_root/$title_component"

    if [ "$(sqlite3 "$DB" "SELECT COALESCE(webdav_path,'') FROM shows WHERE id=$(sql_quote "$show_id");")" != "$target" ]; then
        sqlite3 "$DB" \
            "UPDATE shows SET webdav_path=$(sql_quote "$target"),updated_at=CURRENT_TIMESTAMP WHERE id=$(sql_quote "$show_id");"
        info "[$name] WebDAV 路径：$target"
    fi

    TARGET_PATH="$target"
    return 0
}

source_check_share() {
    local show_name="$1" sid="$2"
    info "[$show_name] source_check share_id=$sid"
    if ! bash "$SOURCE_CHECK_SH" --share-id "$sid" >> "$LOG_FILE" 2>&1; then
        warn "[$show_name] source_check 失败：share_id=$sid"
        return 1
    fi
    return 0
}

scan_unchecked_shares() {
    local show_id="$1" show_name="$2" output="$3" sid

    # SeedHub 首次缓存会一次性写入一批 Share，并同时把 share_scan_rank 推进。
    # 这些 Share 并不属于“增量 rank > old_rank”逻辑，因此首次空库时必须单独
    # 扫描所有尚未成功检查的 Share，否则 share_files 只有极少数来源，tasks 无法
    # 按完整集数进行排序。
    sqlite3 "$DB" \
        "SELECT s.id FROM shares s WHERE s.show_id=$(sql_quote "$show_id") AND COALESCE(s.status,'unknown') NOT IN ('dead','excluded') AND (s.last_success IS NULL OR s.last_check IS NULL) ORDER BY COALESCE(s.seedhub_rank,999999),s.id;" \
        > "$output"

    while IFS= read -r sid; do
        [ -n "$sid" ] || continue
        if source_check_share "$show_name" "$sid"; then
            info "[$show_name] 未扫描 Share 已写入 share_files：share_id=$sid"
        else
            warn "[$show_name] 跳过失败的未扫描 Share：share_id=$sid"
        fi
    done < "$output"
}

recheck_existing_shares() {
    local show_id="$1" output="$2"
    local threshold_epoch now_epoch
    now_epoch="$(date +%s)"
    threshold_epoch=$((now_epoch - RESOURCE_RECHECK_HOURS * 3600))

    sqlite3 "$DB" \
        "SELECT s.id FROM shares s LEFT JOIN (SELECT share_id,MAX(strftime('%s',last_check)) AS checked_epoch,COUNT(*) AS file_count FROM share_files GROUP BY share_id) x ON x.share_id=s.id WHERE s.show_id=$(sql_quote "$show_id") AND COALESCE(s.status,'unknown') NOT IN ('dead','excluded') AND (COALESCE(x.file_count,0)=0 OR COALESCE(x.checked_epoch,0)<$threshold_epoch) ORDER BY CASE WHEN COALESCE(x.file_count,0)=0 THEN 0 ELSE 1 END,COALESCE(s.seedhub_rank,999999),s.id LIMIT $RESOURCE_RECHECK_EXISTING_MAX;" \
        > "$output"
}

scan_new_seedhub_shares() {
    local show_id="$1" url="$2" name="$3"
    local rounds old_rank new_rank list sid round

    [ "$RESOURCE_AUTO_DISCOVER" = true ] || return 0

    for ((round=1; round<=RESOURCE_SEEDHUB_ROUNDS; round++)); do
        old_rank="$(sqlite3 "$DB" "SELECT COALESCE(share_scan_rank,0) FROM shows WHERE id=$(sql_quote "$show_id");")"
        info "[$name] SeedHub 增量扫描开始：rank=$old_rank"

        if ! bash "$SEEDHUB_CACHE_SH" "$url" >> "$LOG_FILE" 2>&1; then
            warn "[$name] seedhub_cache.sh 失败"
            return 1
        fi

        new_rank="$(sqlite3 "$DB" "SELECT COALESCE(share_scan_rank,0) FROM shows WHERE id=$(sql_quote "$show_id");")"
        info "[$name] SeedHub rank：$old_rank -> $new_rank"

        if [ "$new_rank" -le "$old_rank" ]; then
            info "[$name] 没有新的 SeedHub rank，停止增量扫描"
            return 0
        fi

        list="$TMP_ROOT/new-shares-${show_id}-${round}.txt"
        sqlite3 "$DB" \
            "SELECT s.id FROM shares s WHERE s.show_id=$(sql_quote "$show_id") AND COALESCE(s.seedhub_rank,0)>$old_rank AND COALESCE(s.status,'unknown') NOT IN ('dead','excluded') AND (s.last_success IS NULL OR s.last_check IS NULL) ORDER BY s.seedhub_rank,s.id;" \
            > "$list"

        if [ ! -s "$list" ]; then
            info "[$name] 新 rank 主要是重复 Share；继续请求下一批"
            continue
        fi

        while IFS= read -r sid; do
            [ -n "$sid" ] || continue
            if source_check_share "$name" "$sid"; then
                info "[$name] 新 Share 已写入 share_files：share_id=$sid"
            else
                warn "[$name] 跳过失败的新 Share：share_id=$sid"
            fi
        done < "$list"
    done

    return 0
}

# ============================================================
# 替换逻辑
# ============================================================

replacement_window_open() {
    local hour
    hour="$(date +%H)"
    hour=$((10#$hour))
    [ "$hour" -ge "$REPLACE_WINDOW_START_HOUR" ] && [ "$hour" -lt "$REPLACE_WINDOW_END_HOUR" ]
}

refresh_replacement_sources() {
    local show_id="$1" show_name="$2" output="$3"
    sqlite3 "$DB" \
        "SELECT s.id FROM shares s WHERE s.show_id=$(sql_quote "$show_id") AND COALESCE(s.status,'unknown') NOT IN ('dead','excluded') ORDER BY CASE WHEN s.last_success IS NULL THEN 0 ELSE 1 END,COALESCE(strftime('%s',s.last_check),0),COALESCE(s.seedhub_rank,999999),s.id LIMIT $REPLACE_SOURCE_CHECK_MAX;" \
        > "$output"

    while IFS= read -r sid; do
        [ -n "$sid" ] || continue
        source_check_share "$show_name" "$sid" || true
    done < "$output"
}

queue_replacements() {
    local show_id="$1" show_name="$2"
    local all_file="$3" best_file="$TMP_ROOT/repl-best-${show_id}.tsv"
    local episode sid source_file rank new_size old_file old_size gain need_ratio threshold existing pending_new
    local season_padded season_pattern
    printf -v season_padded '%02d' "$RESOURCE_SEASON"
    season_pattern="S${season_padded}E*"

    sqlite3 -tabs "$DB" \
        "SELECT sf.episode,s.id,sf.filename,COALESCE(s.seedhub_rank,999999),COALESCE(sf.size,0) FROM share_files sf JOIN shares s ON s.id=sf.share_id WHERE s.show_id=$(sql_quote "$show_id") AND COALESCE(s.status,'unknown') NOT IN ('dead','excluded') AND sf.episode GLOB $(sql_quote "$season_pattern") ORDER BY sf.episode,COALESCE(sf.size,0) DESC,COALESCE(s.seedhub_rank,999999),s.id;" \
        > "$all_file"

    awk -F '\t' '!seen[$1]++ {print}' "$all_file" > "$best_file"

    while IFS=$'\t' read -r episode sid source_file rank new_size; do
        [ -n "$episode" ] || continue
        [[ "$new_size" =~ ^[0-9]+$ ]] || continue
        [ -n "${WEBDAV_NAME[$episode]+x}" ] || continue

        old_file="${WEBDAV_NAME[$episode]}"
        old_size="${WEBDAV_SIZE[$episode]:-0}"
        [[ "$old_size" =~ ^[0-9]+$ ]] || old_size=0

        # 如果已知当前文件正来自同一 Share，则不重复排队。
        local current_source
        current_source="$(sqlite3 "$DB" "SELECT COALESCE(source_share_id,'') FROM webdav_files WHERE show_id=$(sql_quote "$show_id") AND episode=$(sql_quote "$episode") AND filename=$(sql_quote "$old_file") LIMIT 1;")"
        [ "$current_source" != "$sid" ] || continue

        [ "$new_size" -gt "$old_size" ] || continue
        gain=$((new_size-old_size))
        [ "$gain" -ge $((REPLACE_MIN_GAIN_MB*1024*1024)) ] || continue

        if [ "$old_size" -gt 0 ]; then
            need_ratio="$(awk -v o="$old_size" -v r="$REPLACE_MIN_RATIO" 'BEGIN{printf "%.0f",o*r+0.999999}')"
            [ "$new_size" -ge "$need_ratio" ] || continue
        fi

        existing="$(sqlite3 "$DB" "SELECT COUNT(*) FROM replace_queue WHERE show_id=$(sql_quote "$show_id") AND episode=$(sql_quote "$episode") AND status IN ('pending','running');")"

        if [ "$existing" -gt 0 ]; then
            pending_new="$(sqlite3 "$DB" "SELECT COALESCE(MAX(new_size),0) FROM replace_queue WHERE show_id=$(sql_quote "$show_id") AND episode=$(sql_quote "$episode") AND status IN ('pending','running');")"
            [ "$new_size" -gt "$pending_new" ] || continue

            # running 任务不能被修改；只有 pending 可以升级候选。
            if sqlite3 "$DB" "SELECT 1 FROM replace_queue WHERE show_id=$(sql_quote "$show_id") AND episode=$(sql_quote "$episode") AND status='pending' LIMIT 1;" | grep -qx 1; then
                sqlite3 "$DB" \
                    "UPDATE replace_queue SET source_share_id=$(sql_quote "$sid"),source_file=$(sql_quote "$source_file"),old_filename=$(sql_quote "$old_file"),old_size=$old_size,new_size=$new_size,error=NULL WHERE show_id=$(sql_quote "$show_id") AND episode=$(sql_quote "$episode") AND status='pending';"
            fi
        else
            sqlite3 "$DB" <<SQL
$SQL_BUSY_TIMEOUT
BEGIN IMMEDIATE;
INSERT INTO replace_queue
(show_id,episode,source_share_id,source_file,old_filename,old_size,new_size,status,created_at)
VALUES
($(sql_quote "$show_id"),$(sql_quote "$episode"),$(sql_quote "$sid"),$(sql_quote "$source_file"),$(sql_quote "$old_file"),$old_size,$new_size,'pending',CURRENT_TIMESTAMP);
COMMIT;
SQL
        fi

        info "[$show_name] 替换候选：$episode $old_size -> $new_size，source_share=$sid"
    done < "$best_file"
}

# ============================================================
# 单个资源
# ============================================================

verify_addfile_webdav() {
    local show_name="$1" show_id="$2" target="$3" total="$4" webdav_file="$5" missing_file="$6" candidate_file="$7"
    local deadline now remaining count expected_file actual_missing_file

    expected_file="$TMP_ROOT/verify-expected-${show_id}.txt"
    actual_missing_file="$TMP_ROOT/verify-missing-${show_id}.txt"

    # 只验证本轮至少存在一个 Share 候选的缺集。
    # 没有候选的缺集属于 UNRESOLVED，不能让它们阻塞 WebDAV 确认。
    awk -F '\t' 'NR==FNR { missing[$1]=1; next } ($5 in missing) { found[$5]=1 } END { for (ep in found) print ep }' \
        "$missing_file" "$candidate_file" | sort -n > "$expected_file"

    if [ ! -s "$expected_file" ]; then
        info "[$show_name] 本轮没有可验证的补缺集（候选为空），跳过 WebDAV 等待"
        return 0
    fi

    deadline=$(( $(date +%s) + RESOURCE_ADD_VERIFY_TIMEOUT ))
    info "[$show_name] addfile 已返回，开始等待真实 WebDAV 确认本轮可补缺集：timeout=${RESOURCE_ADD_VERIFY_TIMEOUT}s interval=${RESOURCE_ADD_VERIFY_INTERVAL}s"

    while :; do
        if scan_webdav_actual "$target" "$webdav_file"; then
            update_webdav_cache "$show_id" "$webdav_file" || true
            load_webdav_best "$webdav_file"
            get_missing_file "$total" "$actual_missing_file"
            count="$(awk 'NR==FNR { missing[$1]=1; next } ($1 in missing) { c++ } END { print c+0 }' "$expected_file" "$actual_missing_file")"
            if [ "$count" -eq 0 ]; then
                info "[$show_name] WebDAV 已确认本轮可补缺的集数全部出现"
                return 0
            fi
            now=$(date +%s); remaining=$((deadline-now)); [ "$remaining" -gt 0 ] || break
            info "[$show_name] WebDAV 尚未确认本轮完成：仍缺 $count 集，剩余等待 ${remaining}s"
        else
            warn "[$show_name] WebDAV 实际扫描失败，继续等待"
        fi
        now=$(date +%s); remaining=$((deadline-now)); [ "$remaining" -gt 0 ] || break
        if [ "$RESOURCE_ADD_VERIFY_INTERVAL" -lt "$remaining" ]; then sleep "$RESOURCE_ADD_VERIFY_INTERVAL"; else sleep "$remaining"; fi
    done

    count="$(awk 'NR==FNR { missing[$1]=1; next } ($1 in missing) { c++ } END { print c+0 }' "$expected_file" "$actual_missing_file" 2>/dev/null)"
    warn "[$show_name] addfile 后在 ${RESOURCE_ADD_VERIFY_TIMEOUT}s 内未确认本轮可补缺集全部出现：仍缺=${count:-未知}"
    return 1
}

process_resource() {
    local url="$1" directory="$2"
    RESOURCE_RESULT="FAILED"
    local show_id resource_name total current_path target old_rank
    local webdav_file missing_file candidate_file selected_file recheck_file
    local missing_count_val unresolved
    local status='UNRESOLVED'
    local add_rc=0

    info "============================================================"
    info "处理资源：$url"

    # 第一次进入该 URL 必须先让 seedhub_cache 创建/更新 show。
    if ! bash "$SEEDHUB_CACHE_SH" "$url" >> "$LOG_FILE" 2>&1; then
        error "SeedHub 入口解析失败：$url"
        return 1
    fi

    IFS=$'\t' read -r show_id resource_name total current_path old_rank < <(lookup_show "$url")
    if [ -z "${show_id:-}" ]; then
        error "SeedHub 解析后仍找不到 shows：$url"
        return 1
    fi

    info "[$resource_name] show_id=$show_id total_episodes=$total share_scan_rank=$old_rank"

    # 单季剧的一级标题没有季度标记时，按 S01 处理；季度信息不是继续流程的硬门槛。
    RESOURCE_SEASON="$(extract_season_from_text "$resource_name" 2>/dev/null || printf '1')"
    if ! [[ "$RESOURCE_SEASON" =~ ^[0-9]+$ ]] || [ "$RESOURCE_SEASON" -lt 1 ]; then
        RESOURCE_SEASON=1
    fi

    # 即使数据库中残留旧的长名称，也在这里重新规范化，避免错误目录名阻塞后续 WebDAV / addfile 流程。
    resource_name="$(seasonize_resource_name "$resource_name" "$RESOURCE_SEASON" 2>/dev/null || printf '%s' "$resource_name")"
    if [ -z "$resource_name" ]; then
        error "无法生成有效资源名称：show_id=$show_id"
        return 1
    fi
    sqlite3 "$DB" \
        "UPDATE shows SET name=$(sql_quote "$resource_name"),updated_at=CURRENT_TIMESTAMP WHERE id=$(sql_quote "$show_id");"

    if [ "$RESOURCE_SEASON" -eq 1 ] && ! extract_season_from_text "$resource_name" >/dev/null 2>&1; then
        info "[$resource_name] 未检测到显式季度标记，按单季剧处理：S01"
    else
        info "[$resource_name] 当前季度上下文：S$(printf '%02d' "$RESOURCE_SEASON")"
    fi

    if ! sync_show_path "$show_id" "$resource_name" "$directory"; then
        return 1
    fi
    target="$TARGET_PATH"

    if ! ensure_webdav_dir "$target"; then
        error "[$resource_name] 无法准备 WebDAV 目标目录：$target"
        return 1
    fi

    # 首次 SeedHub 缓存后，share_scan_rank 可能已经是 20/40 等最新值；
    # 不能再用“rank > old_rank”寻找这些刚写入的 Share。先把所有未检查的 Share
    # 扫描一次，建立完整 share_files，后面的 tasks 才能按 Share 集数正确排名。
    local initial_unchecked="$TMP_ROOT/initial-unchecked-${show_id}.txt"
    scan_unchecked_shares "$show_id" "$resource_name" "$initial_unchecked"

    webdav_file="$TMP_ROOT/webdav-${show_id}.tsv"
    missing_file="$TMP_ROOT/missing-${show_id}.txt"
    candidate_file="$TMP_ROOT/candidates-${show_id}.tsv"
    selected_file="$TMP_ROOT/selected-${show_id}.tsv"
    recheck_file="$TMP_ROOT/recheck-${show_id}.txt"

    if ! scan_webdav_actual "$target" "$webdav_file"; then
        return 1
    fi
    update_webdav_cache "$show_id" "$webdav_file" || return 1
    load_webdav_best "$webdav_file"

    if [ "$total" -le 0 ]; then
        warn "[$resource_name] SeedHub 未提供有效 total_episodes，无法建立资源范围；保留数据库和 WebDAV 缓存"
        status='UNRESOLVED'
        RESOURCE_RESULT="$status"
        return 1
    fi

    # --------------------------------------------------------
    # 资源范围：绝不再直接使用 SeedHub total_episodes 作为“缺集范围”。
    #
    # total_episodes 是 SeedHub 的事实元数据，例如 18；
    # 但当前已发现的 Share 可能只有 E01-E14。
    # 此时本轮只能判断 E01-E14 是否完整，E15-E18 属于“尚未发现资源”，
    # 不能错误地称为 WebDAV 缺失并无限等待。
    # --------------------------------------------------------
    known_max="$(get_max_known_episode "$show_id" "$webdav_file")"

    # 如果数据库已有 Share 但缓存为空，先有限重新验证；不能因为 total=18
    # 就人为制造 E01-E18 的 missing_file。
    if [ "$known_max" -eq 0 ] && [ "$RESOURCE_RECHECK_EXISTING_MAX" -gt 0 ]; then
        recheck_existing_shares "$show_id" "$recheck_file"
        if [ -s "$recheck_file" ]; then
            while IFS= read -r sid; do
                [ -n "$sid" ] || continue
                if source_check_share "$resource_name" "$sid"; then
                    known_max="$(get_max_known_episode "$show_id" "$webdav_file")"
                    [ "$known_max" -gt 0 ] && break
                else
                    warn "[$resource_name] 跳过失败的旧 Share：share_id=$sid"
                fi
            done < "$recheck_file"
        fi
    fi

    # 当前仍完全没有可识别剧集时，才进行 SeedHub 增量发现。
    if [ "$known_max" -eq 0 ]; then
        scan_new_seedhub_shares "$show_id" "$url" "$resource_name" || true
        known_max="$(get_max_known_episode "$show_id" "$webdav_file")"
    fi

    if [ "$known_max" -eq 0 ]; then
        write_task "$resource_name" "$target" /dev/null "$missing_file"
        warn "[$resource_name] !!! 强提醒：当前没有任何 Share 被识别出可用剧集文件；SeedHub 标注总集数=$total，但当前无法建立可补缺范围。"
        status='UNRESOLVED'
        RESOURCE_RESULT="$status"
        return 1
    fi

    # total 是上限事实；如果异常 Share 出现超过 total 的集数，本轮仍不把它们
    # 当成额外待补集，但会在日志中提示。
    if [ "$known_max" -gt "$total" ]; then
        warn "[$resource_name] Share 已发现最高集数 E$(printf '%02d' "$known_max")，高于 SeedHub total=$total；本轮补缺范围按 SeedHub total=$total 截断。"
        known_max="$total"
    fi

    info "[$resource_name] 当前可判断资源范围：E01-E$(printf '%02d' "$known_max")；SeedHub total=$total"

    get_missing_file "$known_max" "$missing_file"
    missing_count_val="$(wc -l < "$missing_file" | awk '{print $1}')"

    if [ "$missing_count_val" -gt 0 ]; then
        info "[$resource_name] 当前已发现范围内 WebDAV 缺失 $missing_count_val 集：$(tr '\n' ' ' < "$missing_file")"
    else
        info "[$resource_name] 当前已发现范围 E01-E$(printf '%02d' "$known_max") 已完整存在于 WebDAV"
    fi

    # 如果当前最高资源集数低于 SeedHub total，尝试继续发现后续 Share。
    # 注意：发现失败/无新增不会把 E(known_max+1)..total 当成 WebDAV missing。
    if [ "$known_max" -lt "$total" ]; then
        scan_new_seedhub_shares "$show_id" "$url" "$resource_name" || true
        known_max="$(get_max_known_episode "$show_id" "$webdav_file")"
        [ "$known_max" -gt "$total" ] && known_max="$total"
        get_missing_file "$known_max" "$missing_file"
        missing_count_val="$(wc -l < "$missing_file" | awk '{print $1}')"
        info "[$resource_name] 增量发现后当前资源范围：E01-E$(printf '%02d' "$known_max")；WebDAV 缺失=$missing_count_val"
    fi

    # --------------------------------------------------------
    # 1. 先使用 DB 缓存
    # --------------------------------------------------------
    build_candidates "$show_id" "$missing_file" "$candidate_file"
    select_task_shares "$show_id" "$candidate_file" "$missing_file" "$selected_file"
    unresolved="$(count_unresolved "$missing_file" "$candidate_file")"
    info "[$resource_name] 当前资源范围内 share_files 仍有 $unresolved 集没有候选"

    # --------------------------------------------------------
    # 2. 只重新验证少量过期/没有缓存的旧 Share
    # --------------------------------------------------------
    if [ "$unresolved" -gt 0 ] && [ "$RESOURCE_RECHECK_EXISTING_MAX" -gt 0 ]; then
        recheck_existing_shares "$show_id" "$recheck_file"
        if [ -s "$recheck_file" ]; then
            while IFS= read -r sid; do
                [ -n "$sid" ] || continue
                if source_check_share "$resource_name" "$sid"; then
                    # source_check 可能发现更高的集数，所以重新确定范围。
                    known_max="$(get_max_known_episode "$show_id" "$webdav_file")"
                    [ "$known_max" -gt "$total" ] && known_max="$total"
                    get_missing_file "$known_max" "$missing_file"
                    build_candidates "$show_id" "$missing_file" "$candidate_file"
                    unresolved="$(count_unresolved "$missing_file" "$candidate_file")"
                    if [ "$unresolved" -eq 0 ]; then
                        info "[$resource_name] 重新验证旧 Share 后，当前已发现范围全部具备候选"
                        break
                    fi
                else
                    warn "[$resource_name] 跳过失败的旧 Share：share_id=$sid"
                fi
            done < "$recheck_file"
        fi
    fi

    # --------------------------------------------------------
    # 3. 缓存仍不够 -> SeedHub 增量发现
    # --------------------------------------------------------
    known_max="$(get_max_known_episode "$show_id" "$webdav_file")"
    [ "$known_max" -gt "$total" ] && known_max="$total"
    get_missing_file "$known_max" "$missing_file"
    build_candidates "$show_id" "$missing_file" "$candidate_file"
    unresolved="$(count_unresolved "$missing_file" "$candidate_file")"

    if [ "$unresolved" -gt 0 ]; then
        scan_new_seedhub_shares "$show_id" "$url" "$resource_name" || true
        known_max="$(get_max_known_episode "$show_id" "$webdav_file")"
        [ "$known_max" -gt "$total" ] && known_max="$total"
        get_missing_file "$known_max" "$missing_file"
    fi

    # --------------------------------------------------------
    # 4. 重新计算候选并生成 tasks
    # --------------------------------------------------------
    build_candidates "$show_id" "$missing_file" "$candidate_file"
    select_task_shares "$show_id" "$candidate_file" "$missing_file" "$selected_file"
    unresolved="$(count_unresolved "$missing_file" "$candidate_file")"
    write_task "$resource_name" "$target" "$selected_file" "$missing_file"

    if [ -s "$selected_file" ]; then
        info "[$resource_name] 自动生成 tasks：$(cut -f2 "$selected_file" | tr '\n' ' ')"
    else
        info "[$resource_name] 当前没有可用 Share，tasks 保持为空"
    fi

    # 如果当前已发现范围仍有缺集且所有已知 Share 联合起来都无法覆盖，
    # 必须显式强提醒；这表示存在真实中间缺集，而不是简单的 total 尚未发现。
    if [ "$unresolved" -gt 0 ]; then
        warn "[$resource_name] !!! 强提醒：当前已发现资源范围 E01-E$(printf '%02d' "$known_max") 存在 $unresolved 集没有任何可用 Share 候选：$(tr '\n' ' ' < "$missing_file")"
    fi

    # 如果当前范围完整，但仍低于 SeedHub total，也要提醒用户“尚未发现”，
    # 防止把 E15-E18 静默当成不存在。
    if [ "$known_max" -lt "$total" ]; then
        warn "[$resource_name] !!! 强提醒：SeedHub 标注总集数=$total，但当前已发现 Share 最高仅 E$(printf '%02d' "$known_max")；E$(printf '%02d' "$((known_max+1))")-E$(printf '%02d' "$total") 尚未发现可用分享资源。不会把这些集数伪装成 WebDAV 缺失，但请注意资源并不完整。"
    fi

    # --------------------------------------------------------
    # 5. 每次启动都做补缺；只把当前合适 Share 给 addfile.sh
    # --------------------------------------------------------
    if [ -s "$selected_file" ] && [ "$RESOURCE_AUTO_ADD" = true ]; then
        info "[$resource_name] 调用 addfile.sh 执行补缺"
        if bash "$ADDFILE_SH" >> "$LOG_FILE" 2>&1; then
            add_rc=0
            info "[$resource_name] addfile.sh 进程结束，但这不代表 WebDAV 已经看到文件"
            if ! verify_addfile_webdav "$resource_name" "$show_id" "$target" "$known_max" "$webdav_file" "$missing_file" "$candidate_file"; then
                RESOURCE_RESULT='UNRESOLVED'
                return 1
            fi
        else
            add_rc=$?
            warn "[$resource_name] addfile.sh 返回失败：rc=$add_rc"
            RESOURCE_RESULT='UNRESOLVED'
            return 1
        fi
    fi

    # --------------------------------------------------------
    # 6. 替换属于额外任务：仅在凌晨时间窗口处理
    # --------------------------------------------------------
    if [ "$REPLACE_ENABLED" = true ] && replacement_window_open; then
        info "[$resource_name] 进入替换窗口：$(date '+%H:%M:%S')"

        local replace_shares="$TMP_ROOT/replace-shares-${show_id}.txt"
        refresh_replacement_sources "$show_id" "$resource_name" "$replace_shares"

        if scan_webdav_actual "$target" "$webdav_file"; then
            update_webdav_cache "$show_id" "$webdav_file" || true
            load_webdav_best "$webdav_file"
            queue_replacements "$show_id" "$resource_name" "$TMP_ROOT/repl-all-${show_id}.tsv"
        else
            warn "[$resource_name] 替换前 WebDAV 扫描失败，跳过替换"
        fi

        if [ -f "$REPLACE_SH" ]; then
            if sqlite3 "$DB" "SELECT 1 FROM replace_queue WHERE show_id=$(sql_quote "$show_id") AND status='pending' LIMIT 1;" | grep -qx 1; then
                info "[$resource_name] 调用 replace.sh 执行 pending 替换"
                if ! bash "$REPLACE_SH" >> "$LOG_FILE" 2>&1; then
                    warn "[$resource_name] replace.sh 返回失败，请查看日志"
                fi
            else
                info "[$resource_name] 当前没有 pending 替换任务"
            fi
        else
            info "[$resource_name] replace.sh 尚未安装，保留 replace_queue"
        fi
    else
        info "[$resource_name] 本次不执行替换：不在 $REPLACE_WINDOW_START_HOUR:00-$REPLACE_WINDOW_END_HOUR:00 时间窗口"
    fi

    # --------------------------------------------------------
    # 7. 最终状态
    # --------------------------------------------------------
    if scan_webdav_actual "$target" "$webdav_file"; then
        update_webdav_cache "$show_id" "$webdav_file" || true
        load_webdav_best "$webdav_file"
    fi

    known_max="$(get_max_known_episode "$show_id" "$webdav_file")"
    [ "$known_max" -gt "$total" ] && known_max="$total"
    get_missing_file "$known_max" "$missing_file"
    missing_count_val="$(wc -l < "$missing_file" | awk '{print $1}')"
    build_candidates "$show_id" "$missing_file" "$candidate_file"
    unresolved="$(count_unresolved "$missing_file" "$candidate_file")"

    if [ "$missing_count_val" -eq 0 ] && [ "$unresolved" -eq 0 ] && [ "$known_max" -ge "$total" ]; then
        status='SUCCESS'
    else
        # 当前已发现范围可以完整，并不代表 SeedHub 总集数已经有资源。
        # 这种情况不再算 WebDAV 缺集，但仍返回 UNRESOLVED，让调度/监控能够发现。
        status='UNRESOLVED'
    fi

    info "[$resource_name] 状态=$status WebDAV已发现范围=$((known_max-missing_count_val))/$known_max SeedHub总集数=$total 当前范围缺失=$missing_count_val 无候选=$unresolved"
    RESOURCE_RESULT="$status"

    [ "$add_rc" -eq 0 ] || { RESOURCE_RESULT='FAILED'; return 1; }
    [ "$status" != 'UNRESOLVED' ] || return 1
    return 0
}

# ============================================================
# 读取 resources.json
# ============================================================

RESOURCES_LIST="$TMP_ROOT/resources.jsonl"
jq -c '(.resources? // .)[]' "$RESOURCES_FILE" > "$RESOURCES_LIST"

if [ ! -s "$RESOURCES_LIST" ]; then
    error "resources.json 没有资源条目"
    exit 2
fi

TOTAL=0 SUCCESS=0 READY=0 UNRESOLVED=0 FAILED=0

while IFS= read -r item; do
    [ -n "$item" ] || continue
    TOTAL=$((TOTAL+1))

    url="$(printf '%s' "$item" | jq -r '.url // empty')"
    directory="$(printf '%s' "$item" | jq -r '.directory // ""')"
    directory="$(normalize_directory "$directory")"

    if [ -z "$url" ]; then
        error "resources.json 第 $TOTAL 项缺少 url"
        FAILED=$((FAILED+1))
        continue
    fi

    case "$url" in
        http://*|https://*) ;;
        *)
            error "resources.json 第 $TOTAL 项 url 不是 HTTP/HTTPS：$url"
            FAILED=$((FAILED+1))
            continue
            ;;
    esac

    if [ -n "$directory" ]; then
        case "$directory" in
            /kuake|/kuake/*) ;;
            *)
                error "resources.json 第 $TOTAL 项 directory 必须是 /kuake/...：$directory"
                FAILED=$((FAILED+1))
                continue
                ;;
        esac
    fi

    process_resource "$url" "$directory"
    case "$RESOURCE_RESULT" in
        SUCCESS) SUCCESS=$((SUCCESS+1)) ;;
        READY) READY=$((READY+1)) ;;
        UNRESOLVED) UNRESOLVED=$((UNRESOLVED+1)) ;;
        *) FAILED=$((FAILED+1)) ;;
    esac

done < "$RESOURCES_LIST"

# tasks 是主脚本与 addfile.sh 之间的临时自动接口；下一次循环会重新生成。
# 这里保留最后一个成功生成的任务，方便查看本次最后处理到哪里。

info "============================================================"
info "resource_check 完成：total=$TOTAL success=$SUCCESS ready=$READY unresolved=$UNRESOLVED failed=$FAILED"
info "日志：$LOG_FILE"

if [ "$FAILED" -gt 0 ] || [ "$UNRESOLVED" -gt 0 ]; then
    exit 1
fi
exit 0
