#!/bin/bash

set -u

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

CONFIG="$BASE_DIR/config"
TASKS="$BASE_DIR/tasks"
LOG_DIR_DEFAULT="$BASE_DIR/logs"
LOG_DIR="$LOG_DIR_DEFAULT"
LOG_FILE="$LOG_DIR/addfile.log"

TMP_ROOT="/tmp/quark-follow-$$"

mkdir -p "$TMP_ROOT"

cleanup() {
    rm -rf "$TMP_ROOT"
}

trap cleanup EXIT INT TERM

# ============================================================
# 读取配置
# ============================================================

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: 找不到 config"
    exit 1
fi

# shellcheck disable=SC1090
. "$CONFIG"

LOG_DIR="${LOG_DIR:-$LOG_DIR_DEFAULT}"
LOG_FILE="$LOG_DIR/addfile.log"

: "${QUARK_COOKIE:?config 中没有 QUARK_COOKIE}"
: "${OPENLIST_URL:?config 中没有 OPENLIST_URL}"
: "${OPENLIST_TOKEN:?config 中没有 OPENLIST_TOKEN}"
: "${WEBDAV_URL:?config 中没有 WEBDAV_URL}"
: "${WEBDAV_USER:?config 中没有 WEBDAV_USER}"
: "${WEBDAV_PASS:?config 中没有 WEBDAV_PASS}"

QUARK_API_DELAY="${QUARK_API_DELAY:-2}"
QUARK_TASK_POLL="${QUARK_TASK_POLL:-8}"
QUARK_TASK_TIMEOUT="${QUARK_TASK_TIMEOUT:-7200}"

MAX_PARALLEL_TASKS="${MAX_PARALLEL_TASKS:-2}"

VIDEO_MIN_MB="${VIDEO_MIN_MB:-100}"
VIDEO_MAX_GB="${VIDEO_MAX_GB:-40}"
ARCHIVE_LOG_MB="${ARCHIVE_LOG_MB:-500}"

OPENLIST_REFRESH_WAIT="${OPENLIST_REFRESH_WAIT:-3}"
OPENLIST_REFRESH_RETRY="${OPENLIST_REFRESH_RETRY:-5}"
OPENLIST_REFRESH_INTERVAL="${OPENLIST_REFRESH_INTERVAL:-3}"

CURL_TLS_MAX="${CURL_TLS_MAX:-1.2}"
DRY_RUN="${DRY_RUN:-false}"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"

chmod 600 "$CONFIG" 2>/dev/null || true
chmod 600 "$LOG_FILE" 2>/dev/null || true
chmod 700 "$0" 2>/dev/null || true


# ============================================================
# 日志
# ============================================================

log() {
    printf '[%s] [ADD] %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" \
        "$*" >> "$LOG_FILE"
}

error() {
    log "ERROR: $*"
}

warn() {
    log "WARN: $*"
}


# ============================================================
# 依赖检查
# ============================================================

for CMD in bash curl jq sed awk grep sort md5sum date sleep mktemp; do
    if ! command -v "$CMD" >/dev/null 2>&1; then
        error "变量/环境失效：缺少命令 $CMD"
        exit 1
    fi
done


# ============================================================
# curl TLS
# ============================================================

CURL_TLS_ARGS=()

if [ -n "$CURL_TLS_MAX" ]; then
    CURL_TLS_ARGS+=(--tls-max "$CURL_TLS_MAX")
fi


# ============================================================
# Quark 全局限速
#
# 即使两个任务并行，Quark API 请求也必须排队。
# ============================================================

RATE_LOCK="$TMP_ROOT/quark-rate.lock"
RATE_FILE="$TMP_ROOT/quark-last-request"

quark_rate_limit() {

    while ! mkdir "$RATE_LOCK" 2>/dev/null; do
        sleep 0.2
    done

    local now last wait

    now="$(date +%s)"
    last="$(cat "$RATE_FILE" 2>/dev/null || echo 0)"

    wait=$((last + QUARK_API_DELAY - now))

    if [ "$wait" -gt 0 ]; then
        sleep "$wait"
    fi

    date +%s > "$RATE_FILE"

    rmdir "$RATE_LOCK" 2>/dev/null || true
}


# ============================================================
# Quark HTTP
# ============================================================

quark_curl() {

    quark_rate_limit

    curl -sS \
        --connect-timeout 20 \
        --max-time 120 \
        "${CURL_TLS_ARGS[@]}" \
        -H "Cookie: $QUARK_COOKIE" \
        -H 'Origin: https://pan.quark.cn' \
        -H 'Referer: https://pan.quark.cn/' \
        -H 'Accept: application/json, text/plain, */*' \
        -H 'Content-Type: application/json' \
        "$@"
}


# ============================================================
# OpenList HTTP
# ============================================================

openlist_curl() {

    curl -sS \
        --connect-timeout 20 \
        --max-time 60 \
        "${CURL_TLS_ARGS[@]}" \
        -H "Authorization: $OPENLIST_TOKEN" \
        -H 'Content-Type: application/json' \
        "$@"
}


# ============================================================
# 提取 Quark 分享 ID
# ============================================================

get_pwd_id() {

    printf '%s\n' "$1" |
        sed -n 's#.*pan\.quark\.cn/s/\([^/#?]*\).*#\1#p'
}


# ============================================================
# stoken 临时缓存
#
# 不写日志。
# 同一个分享在本次运行中只获取一次。
# ============================================================

STOKEN_CACHE="$TMP_ROOT/stoken.cache"

get_stoken() {

    local share="$1"
    local pwd_id="$2"

    local token
    local result
    local code
    local message
    local lock

    token="$(
        awk -F '\t' -v id="$pwd_id" '
            $1 == id {
                print $3
                exit
            }
        ' "$STOKEN_CACHE" 2>/dev/null
    )"

    if [ -n "$token" ]; then
        printf '%s' "$token"
        return 0
    fi

    lock="$TMP_ROOT/stoken-$pwd_id.lock"

    while ! mkdir "$lock" 2>/dev/null; do
        sleep 0.2
    done

    # 防止并行任务刚好同时请求
    token="$(
        awk -F '\t' -v id="$pwd_id" '
            $1 == id {
                print $3
                exit
            }
        ' "$STOKEN_CACHE" 2>/dev/null
    )"

    if [ -n "$token" ]; then
        rmdir "$lock" 2>/dev/null || true
        printf '%s' "$token"
        return 0
    fi

    result="$(
        quark_curl \
            -X POST \
            'https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc&uc_param_str=' \
            --data "{\"pwd_id\":\"$pwd_id\",\"passcode\":\"\"}"
    )"

    code="$(printf '%s' "$result" | jq -r '.code // -1')"
    token="$(printf '%s' "$result" | jq -r '.data.stoken // empty')"
    message="$(printf '%s' "$result" | jq -r '.message // "unknown"')"

    if [ "$code" != "0" ] || [ -z "$token" ]; then

        error "分享失效或 stoken 获取失败：$share：$message"

        rmdir "$lock" 2>/dev/null || true

        return 1
    fi

    # stoken 只存临时文件，不进日志
    printf '%s\t%s\t%s\n' \
        "$pwd_id" \
        "$(date +%s)" \
        "$token" >> "$STOKEN_CACHE"

    rmdir "$lock" 2>/dev/null || true

    printf '%s' "$token"
}


# ============================================================
# 中文数字
# ============================================================

cn_number() {

    local s="$1"

    if [[ "$s" =~ ^[0-9]+$ ]]; then
        printf '%s' "$s"
        return 0
    fi

    s="${s//〇/零}"
    s="${s//两/二}"

    if [[ "$s" == *百* ]]; then

        local a="${s%%百*}"
        local b="${s#*百}"
        local hundreds

        [ -n "$a" ] || a="一"

        hundreds="$(cn_number "$a")" || return 1

        if [ -n "$b" ] && [ "$b" != "$s" ]; then
            printf '%s' "$((hundreds * 100 + $(cn_number "$b")))"
        else
            printf '%s' "$((hundreds * 100))"
        fi

        return 0
    fi

    if [[ "$s" == *十* ]]; then

        local a="${s%%十*}"
        local b="${s#*十}"
        local tens

        if [ "$a" = "$s" ] || [ -z "$a" ]; then
            tens=1
        else
            tens="$(cn_number "$a")" || return 1
        fi

        if [ -n "$b" ] && [ "$b" != "$s" ]; then
            printf '%s' "$((tens * 10 + $(cn_number "$b")))"
        else
            printf '%s' "$((tens * 10))"
        fi

        return 0
    fi

    case "$s" in
        零) printf 0 ;;
        一) printf 1 ;;
        二) printf 2 ;;
        三) printf 3 ;;
        四) printf 4 ;;
        五) printf 5 ;;
        六) printf 6 ;;
        七) printf 7 ;;
        八) printf 8 ;;
        九) printf 9 ;;
        十) printf 10 ;;
        *) return 1 ;;
    esac
}


# ============================================================
# ①②③... 转普通数字
# ============================================================

normalize_circled() {

    local s="$1"

    s="${s//⓪/0}"
    s="${s//①/1}"
    s="${s//②/2}"
    s="${s//③/3}"
    s="${s//④/4}"
    s="${s//⑤/5}"
    s="${s//⑥/6}"
    s="${s//⑦/7}"
    s="${s//⑧/8}"
    s="${s//⑨/9}"

    printf '%s' "$s"
}


# ============================================================
# 解析 S01E01
# ============================================================

parse_episode() {

    local name="$1"
    local stem
    local season=1
    local episode

    stem="${name%.*}"

    stem="$(normalize_circled "$stem")"

    # S01E01 / S1E1 / S01 EP01
    if [[ "$stem" =~ [Ss]([0-9]{1,2})[[:space:]_.-]*[Ee][Pp]?([0-9]{1,4}) ]]; then

        season=$((10#${BASH_REMATCH[1]}))
        episode=$((10#${BASH_REMATCH[2]}))

    # E01 / EP01
    elif [[ "$stem" =~ [Ee][Pp]?([0-9]{1,4}) ]]; then

        episode=$((10#${BASH_REMATCH[1]}))

    else

        # 第一季
        if [[ "$stem" =~ 第[[:space:]]*([0-9]+|[零一二两三四五六七八九十百]+)[[:space:]]*季 ]]; then
            season="$(cn_number "${BASH_REMATCH[1]}")" || return 1
        fi

        # 第01集 / 第1集 / 第一集 / 第①⑤集
        if [[ "$stem" =~ 第[[:space:]]*([0-9]+|[零一二两三四五六七八九十百]+)[[:space:]]*集 ]]; then

            episode="$(cn_number "${BASH_REMATCH[1]}")" || return 1

        # 01.mp4 / 1.mkv / 001.mp4
        elif [[ "$stem" =~ ^[[:space:]]*([0-9]{1,3})[[:space:]_.-]*$ ]]; then

            episode=$((10#${BASH_REMATCH[1]}))

        # Episode 01
        elif [[ "$stem" =~ [Ee]pisode[[:space:]_.-]*([0-9]{1,4}) ]]; then

            episode=$((10#${BASH_REMATCH[1]}))

        else
            return 1
        fi
    fi

    [ "${season:-0}" -ge 1 ] 2>/dev/null || return 1
    [ "${episode:-0}" -ge 1 ] 2>/dev/null || return 1

    printf 'S%02dE%02d' "$season" "$episode"
}


# ============================================================
# 视频扩展名
# ============================================================

is_video() {

    local ext="${1##*.}"
    ext="${ext,,}"

    case "$ext" in
        mkv|mp4|m2ts|ts|avi|mov|wmv|flv|webm)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}


# ============================================================
# 压缩包
# ============================================================

is_archive() {

    local ext="${1##*.}"
    ext="${ext,,}"

    case "$ext" in
        zip|rar|7z|tar|gz|bz2|xz|zst|001|iso|cab)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}


# ============================================================
# 递归扫描 Quark 分享
#
# 输出：
# key size fid share_fid_token filename extension relative_path
# ============================================================

scan_share_dir() {

    local pwd_id="$1"
    local stoken="$2"
    local pdir_fid="$3"
    local relative="$4"
    local output="$5"

    local page=1
    local result
    local count
    local total

    while :; do

        result="$(
            quark_curl \
                -G \
                'https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail' \
                --data-urlencode 'pr=ucpro' \
                --data-urlencode 'fr=pc' \
                --data-urlencode "pwd_id=$pwd_id" \
                --data-urlencode "stoken=$stoken" \
                --data-urlencode "pdir_fid=$pdir_fid" \
                --data-urlencode 'force=0' \
                --data-urlencode "_page=$page" \
                --data-urlencode '_size=50' \
                --data-urlencode '_fetch_total=1' \
                --data-urlencode '_sort=file_type:asc,file_name:asc' \
                --data-urlencode 'ver=2' \
                --data-urlencode 'fetch_share_full_path=0'
        )"

        if [ "$(printf '%s' "$result" | jq -r '.code // -1')" != "0" ]; then

            error "分享目录读取失败：$relative"

            return 1
        fi

        while IFS=$'\t' read -r name fid share_token size is_dir; do

            [ -n "$name" ] || continue

            # 目录继续递归
            if [ "$is_dir" = "true" ]; then

                scan_share_dir \
                    "$pwd_id" \
                    "$stoken" \
                    "$fid" \
                    "$relative$name/" \
                    "$output"

                continue
            fi

            # 大压缩包记录
            if is_archive "$name"; then

                if [ "${size:-0}" -ge $((ARCHIVE_LOG_MB * 1024 * 1024)) ]; then
                    warn "疑似视频压缩包：$relative$name size=$size"
                fi

                continue
            fi

            # 非视频全部忽略
            is_video "$name" || continue

            # 过小
            if [ "${size:-0}" -lt $((VIDEO_MIN_MB * 1024 * 1024)) ]; then
                warn "过滤异常小视频：$relative$name size=$size"
                continue
            fi

            # 过大
            if [ "$VIDEO_MAX_GB" -gt 0 ] &&
               [ "${size:-0}" -gt $((VIDEO_MAX_GB * 1024 * 1024 * 1024)) ]; then

                warn "过滤异常大视频：$relative$name size=$size"
                continue
            fi

            local key
            local ext="${name##*.}"

            key="$(parse_episode "$name" || true)"

            if [ -n "$key" ]; then

                printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$key" \
                    "$size" \
                    "$fid" \
                    "$share_token" \
                    "$name" \
                    "${ext,,}" \
                    "$relative" >> "$output"
            fi

        done < <(
            printf '%s' "$result" |
            jq -r '
                .data.list[]? |
                [
                    (.file_name // ""),
                    (.fid // ""),
                    (.share_fid_token // ""),
                    (.size // 0),
                    (.dir // false)
                ] | @tsv
            '
        )

        count="$(printf '%s' "$result" | jq '.data.list | length')"
        total="$(printf '%s' "$result" | jq '.metadata._total // 0')"

        [ "$count" -eq 0 ] && break

        if [ "$total" -eq 0 ] ||
           [ $((page * 50)) -ge "$total" ]; then
            break
        fi

        page=$((page + 1))
    done
}


# ============================================================
# OpenList：检查目录
# ============================================================

openlist_target_exists() {

    local target="$1"
    local result

    result="$(
        openlist_curl \
            -X POST \
            "$OPENLIST_URL/api/fs/get" \
            --data "$(jq -nc \
                --arg p "$target" \
                --arg pw "$OPENLIST_PASSWORD" \
                '{path:$p,password:$pw}')"
    )"

    if [ "$(printf '%s' "$result" | jq -r '.code // 0')" != "200" ]; then

        error "OpenList target 不存在或无法访问：$target"

        return 1
    fi

    if [ "$(printf '%s' "$result" | jq -r '.data.is_dir // false')" != "true" ]; then

        error "OpenList target 不是目录：$target"

        return 1
    fi

    return 0
}


# ============================================================
# OpenList：刷新目录
# ============================================================

openlist_refresh() {

    local target="$1"

    openlist_curl \
        -X POST \
        "$OPENLIST_URL/api/fs/list" \
        --data "$(jq -nc \
            --arg p "$target" \
            --arg pw "$OPENLIST_PASSWORD" \
            '{
                path:$p,
                password:$pw,
                refresh:true,
                page:1,
                per_page:100
            }')"
}


# ============================================================
# WebDAV：检查目标
#
# 实际文件比较使用 OpenList refresh 后的 JSON。
# WebDAV 用来确认你实际使用的 WebDAV 目录没有失联。
# ============================================================

webdav_check() {

    local target="$1"
    local code

    code="$(
        curl -sS \
            --connect-timeout 15 \
            --max-time 30 \
            "${CURL_TLS_ARGS[@]}" \
            -u "$WEBDAV_USER:$WEBDAV_PASS" \
            -o /dev/null \
            -w '%{http_code}' \
            -X PROPFIND \
            -H 'Depth: 0' \
            -H 'Content-Type: application/xml' \
            "${WEBDAV_URL%/}${target}" 2>/dev/null
    )" || code="000"

    case "$code" in
        200|207)
            return 0
            ;;
        401|403)
            error "WebDAV认证/权限失效：$target HTTP=$code"
            return 1
            ;;
        *)
            error "WebDAV目标不可访问：$target HTTP=$code"
            return 1
            ;;
    esac
}


# ============================================================
# Quark：根据路径取得目标目录 FID
# ============================================================

get_target_fid() {

    local target="$1"
    local result

    result="$(
        quark_curl \
            -X POST \
            'https://drive-pc.quark.cn/1/clouddrive/file/info/path_list?pr=ucpro&fr=pc&uc_param_str=' \
            --data "$(jq -nc \
                --arg p "$target" \
                '{file_path:[$p],namespace:"0"}')"
    )"

    printf '%s' "$result" |
        jq -r '.data[0].fid // empty'
}


# ============================================================
# Quark：目标目录文件列表
# ============================================================

quark_target_list() {

    local target_fid="$1"
    local output="$2"

    local page=1
    local result
    local count
    local total

    : > "$output"

    while :; do

        result="$(
            quark_curl \
                -G \
                'https://drive-pc.quark.cn/1/clouddrive/file/sort' \
                --data-urlencode 'pr=ucpro' \
                --data-urlencode 'fr=pc' \
                --data-urlencode "pdir_fid=$target_fid" \
                --data-urlencode "_page=$page" \
                --data-urlencode '_size=100' \
                --data-urlencode '_fetch_total=1' \
                --data-urlencode '_fetch_sub_dirs=0' \
                --data-urlencode '_sort=file_name:asc' \
                --data-urlencode 'fetch_all_file=1' \
                --data-urlencode 'fetch_risk_file_name=1'
        )"

        printf '%s' "$result" |
            jq -r '
                .data.list[]?
                | select(.dir == false)
                | [
                    (.file_name // ""),
                    (.fid // ""),
                    (.size // 0)
                ]
                | @tsv
            ' >> "$output"

        count="$(printf '%s' "$result" | jq '.data.list | length')"
        total="$(printf '%s' "$result" | jq '.metadata._total // 0')"

        [ "$count" -eq 0 ] && break

        if [ "$total" -eq 0 ] ||
           [ $((page * 100)) -ge "$total" ]; then
            break
        fi

        page=$((page + 1))
    done
}


# ============================================================
# Quark：重命名
# ============================================================

quark_rename() {

    local fid="$1"
    local new_name="$2"

    local result

    result="$(
        quark_curl \
            -X POST \
            'https://drive-pc.quark.cn/1/clouddrive/file/rename?pr=ucpro&fr=pc&uc_param_str=' \
            --data "$(jq -nc \
                --arg f "$fid" \
                --arg n "$new_name" \
                '{fid:$f,file_name:$n}')"
    )"

    [ "$(printf '%s' "$result" | jq -r '.code // -1')" = "0" ]
}


# ============================================================
# 转存一组文件
# ============================================================

save_group() {

    local pwd_id="$1"
    local stoken="$2"
    local target_fid="$3"
    local group_file="$4"
    local share="$5"

    local fid_json
    local token_json
    local result
    local task_id
    local status
    local start

    fid_json="$(
        cut -f6 "$group_file" |
        jq -R -s '
            split("\n")
            | map(select(length > 0))
        '
    )"

    token_json="$(
        cut -f7 "$group_file" |
        jq -R -s '
            split("\n")
            | map(select(length > 0))
        '
    )"

    result="$(
        quark_curl \
            -X POST \
            'https://drive-pc.quark.cn/1/clouddrive/share/sharepage/save?pr=ucpro&fr=pc&uc_param_str=' \
            --data "$(jq -nc \
                --arg p "$pwd_id" \
                --arg s "$stoken" \
                --arg tf "$target_fid" \
                --argjson f "$fid_json" \
                --argjson t "$token_json" \
                '{
                    fid_list:$f,
                    fid_token_list:$t,
                    to_pdir_fid:$tf,
                    pwd_id:$p,
                    stoken:$s,
                    pdir_fid:"0",
                    scene:"link"
                }')"
    )"

    if [ "$(printf '%s' "$result" | jq -r '.code // -1')" != "0" ]; then

        error "转存请求失败：$share：$(printf '%s' "$result" | jq -r '.message // "unknown"')"

        return 1
    fi

    task_id="$(printf '%s' "$result" | jq -r '.data.task_id // empty')"

    if [ -z "$task_id" ]; then

        error "转存接口没有返回 task_id：$share"

        return 1
    fi

    log "转存提交：$share files=$(wc -l < "$group_file") task=$task_id"

    start="$(date +%s)"

    while :; do

        sleep "$QUARK_TASK_POLL"

        result="$(
            quark_curl \
                -G \
                'https://drive-pc.quark.cn/1/clouddrive/task' \
                --data-urlencode 'pr=ucpro' \
                --data-urlencode 'fr=pc' \
                --data-urlencode "task_id=$task_id" \
                --data-urlencode 'retry_index=0'
        )"

        status="$(printf '%s' "$result" | jq -r '.data.status // -1')"

        case "$status" in

            2)
                log "转存成功：$share task=$task_id"
                return 0
                ;;

            3)
                error "转存失败：$share task=$task_id：$(printf '%s' "$result" | jq -r '.message // .data.message // "unknown"')"
                return 1
                ;;

            0|1)
                ;;

            *)
                error "转存任务状态异常：$share task=$task_id status=$status"
                return 1
                ;;
        esac

        if [ $(( $(date +%s) - start )) -ge "$QUARK_TASK_TIMEOUT" ]; then

            error "转存超时：$share task=$task_id"

            return 1
        fi
    done
}


# ============================================================
# 单个任务
# ============================================================

process_task() {

    local task_name="$1"
    local target="$2"

    shift 2

    local shares=( "$@" )

    local task_dir="$TMP_ROOT/$(printf '%s' "$task_name" | md5sum | cut -d' ' -f1)"

    mkdir -p "$task_dir"

    log "TASK开始：$task_name target=$target"

    # --------------------------------------------------------
    # target 检查
    # --------------------------------------------------------

    if ! openlist_target_exists "$target"; then
        return
    fi

    if ! webdav_check "$target"; then
        return
    fi

    # --------------------------------------------------------
    # Quark 目标目录
    # --------------------------------------------------------

    local target_fid

    local quark_target="${target#/kuake}"

    target_fid="$(get_target_fid "$quark_target")"

    if [ -z "$target_fid" ]; then

        error "Quark目标目录不存在或无法取得FID：$target"

        return
    fi

    # --------------------------------------------------------
    # OpenList 强制刷新
    # --------------------------------------------------------

    local openlist_result

    openlist_result="$(openlist_refresh "$target")"

    if [ "$(printf '%s' "$openlist_result" | jq -r '.code // 0')" != "200" ]; then

        error "OpenList刷新失败：$target"

        return
    fi

    # --------------------------------------------------------
    # 当前已经存在的集数
    # --------------------------------------------------------

    local existing="$task_dir/existing"

    printf '%s' "$openlist_result" |
        jq -r '
            .data.content[]?
            | select(.is_dir == false)
            | .name
        ' |
    while IFS= read -r filename; do

        local key

        key="$(parse_episode "$filename" || true)"

        [ -n "$key" ] || continue

        printf '%s\t%s\n' "$key" "$filename" >> "$existing"

    done

    sort -u "$existing" -o "$existing" 2>/dev/null || true


    # --------------------------------------------------------
    # 扫描所有分享
    # --------------------------------------------------------

    local share
    local pwd_id
    local stoken
    local share_file
    local all_candidates="$task_dir/all_candidates"

    : > "$all_candidates"

    for share in "${shares[@]}"; do

        [ -n "$share" ] || continue

        pwd_id="$(get_pwd_id "$share")"

        if [ -z "$pwd_id" ]; then

            error "分享链接格式错误：$share"

            continue
        fi

        stoken="$(get_stoken "$share" "$pwd_id")"

        if [ -z "$stoken" ]; then
            continue
        fi

        share_file="$task_dir/share_${pwd_id}"

        : > "$share_file"

        if ! scan_share_dir \
            "$pwd_id" \
            "$stoken" \
            "0" \
            "" \
            "$share_file"; then

            error "分享递归扫描失败：$share"

            continue
        fi

        while IFS=$'\t' read -r key size fid token filename ext path; do

            [ -n "$key" ] || continue

            # 本地已有
            if awk -F '\t' -v k="$key" '$1 == k {found=1} END {exit !found}' "$existing" 2>/dev/null; then
                continue
            fi

            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$key" \
                "$size" \
                "$share" \
                "$pwd_id" \
                "$stoken" \
                "$fid" \
                "$token" \
                "$filename" \
                "$ext" >> "$all_candidates"

        done < "$share_file"

    done


    # --------------------------------------------------------
    # 同一集多个来源：
    # 只保留文件最大的那个
    # --------------------------------------------------------

    local selected="$task_dir/selected"

    awk -F '\t' '
        BEGIN {
            OFS="\t"
        }

        {
            key=$1
            size=$2

            if (!(key in best_size) || size > best_size[key]) {
                best_size[key]=size
                best[key]=$0
            }
        }

        END {
            for (key in best)
                print best[key]
        }
    ' "$all_candidates" |
    sort -t $'\t' -k1,1 > "$selected"


    if [ ! -s "$selected" ]; then

        log "TASK完成：$task_name：没有发现缺失集"

        return
    fi


    log "发现缺失集：$task_name：$(cut -f1 "$selected" | tr '\n' ' ')"

    # --------------------------------------------------------
    # 按分享来源分组
    # 每个分享一次批量提交
    # --------------------------------------------------------

    local share
    local group
    local group_id
    local first

    cut -f3 "$selected" | sort -u |
    while IFS= read -r share; do

        [ -n "$share" ] || continue

        group_id="$(printf '%s' "$share" | md5sum | cut -d' ' -f1)"
        group="$task_dir/group_$group_id"

        awk -F '\t' -v s="$share" '$3 == s {print}' "$selected" > "$group"

        first="$(head -n1 "$group")"

        local gpwd
        local gstoken

        gpwd="$(printf '%s' "$first" | cut -f4)"
        gstoken="$(printf '%s' "$first" | cut -f5)"

        if [ "$DRY_RUN" = "true" ]; then

            log "DRY RUN：$task_name：share=$share files=$(wc -l < "$group")"

            continue
        fi

        save_group \
            "$gpwd" \
            "$gstoken" \
            "$target_fid" \
            "$group" \
            "$share"

    done


    if [ "$DRY_RUN" = "true" ]; then

        log "TASK结束(DRY RUN)：$task_name"

        return
    fi


    # --------------------------------------------------------
    # Quark 转存完成后，刷新 OpenList
    # --------------------------------------------------------

    sleep "$OPENLIST_REFRESH_WAIT"

    local refreshed=false

    local i

    for ((i=1; i<=OPENLIST_REFRESH_RETRY; i++)); do

        openlist_result="$(openlist_refresh "$target")"

        if [ "$(printf '%s' "$openlist_result" | jq -r '.code // 0')" = "200" ]; then
            refreshed=true
            break
        fi

        sleep "$OPENLIST_REFRESH_INTERVAL"

    done

    if [ "$refreshed" != "true" ]; then

        error "转存完成后 OpenList 刷新失败：$target"

        return
    fi


    # --------------------------------------------------------
    # 重新读取 Quark 目标目录
    # 用于找刚刚转存的文件并重命名
    # --------------------------------------------------------

    local qtarget="$task_dir/quark_target"

    quark_target_list "$target_fid" "$qtarget"


    # --------------------------------------------------------
    # 每个缺失集：
    # 1. 确认已经出现
    # 2. 统一重命名
    # --------------------------------------------------------

    while IFS=$'\t' read -r key size share pwd_id stoken fid token filename ext; do

        local normalized="${key}.${ext}"
        local found_fid=""
        local found_name=""
        local found_size=""

        # 已经存在标准文件
        if printf '%s' "$openlist_result" |
            jq -e \
                --arg n "$normalized" \
                '.data.content[]?
                | select(.is_dir == false and .name == $n)' \
                >/dev/null 2>&1; then

            log "已有标准文件：$task_name/$normalized"

            continue
        fi


        # 在 Quark 目标目录里找：
        # 集数相同 + 文件大小相同
        while IFS=$'\t' read -r qname qfid qsize; do

            [ "$qsize" = "$size" ] || continue

            local qkey

            qkey="$(parse_episode "$qname" || true)"

            if [ "$qkey" = "$key" ]; then

                found_fid="$qfid"
                found_name="$qname"
                found_size="$qsize"

                break
            fi

        done < "$qtarget"


        if [ -z "$found_fid" ]; then

            warn "转存成功但目标目录暂时找不到：$task_name $key source=$share"

            continue
        fi


        if [ "$found_name" = "$normalized" ]; then

            log "ADD OK：$task_name：$normalized source=$share size=$size"

            continue
        fi


        if quark_rename "$found_fid" "$normalized"; then

            log "ADD OK：$task_name：$normalized source=$share size=$size"

        else

            error "ADD FAIL：$task_name：重命名失败：$found_name -> $normalized"

        fi

    done < "$selected"


    # --------------------------------------------------------
    # 最终再刷新一次
    # --------------------------------------------------------

    openlist_refresh "$target" >/dev/null 2>&1 || true

    log "TASK结束：$task_name"
}


# ============================================================
# 任务并行调度
# ============================================================

if [ ! -f "$TASKS" ]; then

    error "找不到 tasks 文件：$TASKS"

    exit 1
fi


PIDS=()
PID_NAMES=()


wait_one() {

    local pid="$1"
    local name="$2"

    if wait "$pid"; then
        log "并行任务退出：$name OK"
    else
        error "并行任务退出：$name FAIL"
    fi
}


while IFS= read -r line || [ -n "$line" ]; do

    # 去掉开头空格
    line="${line#"${line%%[![:space:]]*}"}"

    [ -z "$line" ] && continue
    [[ "$line" == \#* ]] && continue

    if [[ "$line" != TASK\|* ]]; then

        error "tasks格式错误：$line"

        continue
    fi


    IFS='|' read -r -a parts <<< "$line"

    if [ "${#parts[@]}" -lt 4 ]; then

        error "tasks字段不足：$line"

        continue
    fi


    task_name="${parts[1]}"
    target="${parts[2]}"

    shares=( "${parts[@]:3}" )


    if [ -z "$task_name" ]; then

        error "任务名称为空"

        continue
    fi


    if [ -z "$target" ]; then

        error "target为空：$task_name"

        continue
    fi


    process_task \
        "$task_name" \
        "$target" \
        "${shares[@]}" &


    PIDS+=( "$!" )
    PID_NAMES+=( "$task_name" )


    # 达到并行上限
    if [ "${#PIDS[@]}" -ge "$MAX_PARALLEL_TASKS" ]; then

        wait_one "${PIDS[0]}" "${PID_NAMES[0]}"

        PIDS=( "${PIDS[@]:1}" )
        PID_NAMES=( "${PID_NAMES[@]:1}" )

    fi

done < "$TASKS"


# 等待剩余任务

while [ "${#PIDS[@]}" -gt 0 ]; do

    wait_one "${PIDS[0]}" "${PID_NAMES[0]}"

    PIDS=( "${PIDS[@]:1}" )
    PID_NAMES=( "${PID_NAMES[@]:1}" )

done


log "全部任务处理结束"
