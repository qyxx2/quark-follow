#!/bin/bash
# ============================================================
# Quark 自动资源维护系统
# replace.sh
#
# 职责：
#   只执行 resource_check.sh 已经写入 replace_queue 的 pending 替换任务。
#
# 安全原则：
#   1. 只处理 pending。
#   2. 新文件必须先成功转存并在 Quark 目标目录确认存在。
#   3. 新文件确认成功后，才删除旧文件。
#   4. 新文件转存/确认失败时，尽量恢复旧文件。
#   5. 成功/失败状态写回 replace_queue。
#   6. 不自行决定“是否值得替换”。
#
# 接口：
#   无命令行参数。
#   同目录 config.local / resource.db。
# ============================================================

set -u

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONFIG="$BASE_DIR/config.local"
DB="$BASE_DIR/resource.db"
TMP_ROOT="/tmp/quark-follow-replace-$$"
LOCK_DIR="$BASE_DIR/replace.lock"
LOG_DIR_DEFAULT="$BASE_DIR/logs"
LOG_DIR="$LOG_DIR_DEFAULT"
LOG_FILE="$LOG_DIR/replace.log"
API_STATS_FILE="$LOG_DIR_DEFAULT/api_stats_live_replace_$$.tsv"
API_STATS_PY="$BASE_DIR/docker/api_stats.py"

mkdir -p "$TMP_ROOT"

cleanup() {
    if [ -s "${API_STATS_FILE:-}" ] && [ -f "${API_STATS_PY:-}" ]; then
        python3 "$API_STATS_PY" --flush "$API_STATS_FILE" >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_ROOT"
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: 找不到 config.local：$CONFIG" >&2
    exit 2
fi
. "$CONFIG"

LOG_DIR="${LOG_DIR:-$LOG_DIR_DEFAULT}"
LOG_FILE="$LOG_DIR/replace.log"
API_STATS_FILE="$LOG_DIR/api_stats_live_replace_$$.tsv"

: "${QUARK_COOKIE:?config.local 中没有 QUARK_COOKIE}"
: "${OPENLIST_URL:?config.local 中没有 OPENLIST_URL}"
: "${OPENLIST_TOKEN:?config.local 中没有 OPENLIST_TOKEN}"
: "${WEBDAV_URL:?config.local 中没有 WEBDAV_URL}"
: "${WEBDAV_USER:?config.local 中没有 WEBDAV_USER}"
: "${WEBDAV_PASS:?config.local 中没有 WEBDAV_PASS}"

CURL_TLS_MAX="${CURL_TLS_MAX:-1.2}"
QUARK_API_DELAY="${QUARK_API_DELAY:-2}"
QUARK_TASK_POLL="${QUARK_TASK_POLL:-8}"
QUARK_TASK_TIMEOUT="${QUARK_TASK_TIMEOUT:-7200}"
OPENLIST_REFRESH_WAIT="${OPENLIST_REFRESH_WAIT:-3}"
OPENLIST_REFRESH_RETRY="${OPENLIST_REFRESH_RETRY:-5}"
OPENLIST_REFRESH_INTERVAL="${OPENLIST_REFRESH_INTERVAL:-3}"
REPLACE_VERIFY_INTERVAL="${REPLACE_VERIFY_INTERVAL:-10}"
REPLACE_MAX_RETRIES="${REPLACE_MAX_RETRIES:-1}"
OPENLIST_PASSWORD="${OPENLIST_PASSWORD:-}"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
touch "$API_STATS_FILE" 2>/dev/null || true
chmod 600 "$CONFIG" 2>/dev/null || true
chmod 600 "$LOG_FILE" 2>/dev/null || true
chmod 600 "$API_STATS_FILE" 2>/dev/null || true
chmod 700 "$0" 2>/dev/null || true

if [ "$#" -ne 0 ]; then
    echo "ERROR: replace.sh 不接受命令行参数。" >&2
    exit 2
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "ERROR: replace.sh 正在运行：$LOCK_DIR" >&2
    exit 1
fi

for CMD in bash curl jq awk sed grep sort cut tr date sleep sqlite3 md5sum basename; do
    command -v "$CMD" >/dev/null 2>&1 || {
        echo "ERROR: 缺少命令：$CMD" >&2
        exit 2
    }
done

sqlite_table_exists() {
    sqlite3 "$DB" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='$1' LIMIT 1;" |
        grep -qx 1
}

for table in shows shares share_files webdav_files replace_queue; do
    sqlite_table_exists "$table" || {
        echo "ERROR: 数据库缺少表：$table" >&2
        exit 2
    }
done

log() {
    printf '[%s] [REPLACE] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"
}
info() {
    printf '[REPLACE] INFO: %s\n' "$*"
    log "$*"
}
warn() {
    printf '[REPLACE] WARN: %s\n' "$*" >&2
    log "WARN: $*"
}
error() {
    printf '[REPLACE] ERROR: %s\n' "$*" >&2
    log "ERROR: $*"
}

sql_quote() {
    local value="${1:-}"
    value="${value//\'/\'\'}"
    printf "'%s'" "$value"
}

CURL_TLS_ARGS=()
[ -n "$CURL_TLS_MAX" ] && CURL_TLS_ARGS+=(--tls-max "$CURL_TLS_MAX")

RATE_FILE="$BASE_DIR/.quark-api-last"
RATE_LOCK="$BASE_DIR/.quark-api-rate.lock"

quark_rate_limit() {
    local now last delta
    while ! mkdir "$RATE_LOCK" 2>/dev/null; do sleep 0.1; done
    now="$(date +%s)"
    last="$(cat "$RATE_FILE" 2>/dev/null || echo 0)"
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
    delta=$((now-last))
    if [ "$delta" -lt "$QUARK_API_DELAY" ]; then
        sleep "$((QUARK_API_DELAY-delta))"
    fi
    date +%s > "$RATE_FILE"
    rmdir "$RATE_LOCK" 2>/dev/null || true
}

# ============================================================
# Quark API 统计
# 只记录时间、接口名和 curl 传输层是否失败，不记录 Cookie / Token / URL 参数。
# ============================================================

api_stats_record_quark() {
    local url="$1"
    local failed="$2"
    local path operation

    operation="unknown"
    case "$url" in
        https://drive-pc.quark.cn/1/clouddrive/*)
            path="${url#https://drive-pc.quark.cn/1/clouddrive/}"
            path="${path%%\?*}"
            [ -n "$path" ] && operation="$path"
            ;;
    esac

    printf '%s\tquark\t%s\t%s\n' \
        "$(date +%s)" "$operation" "$failed" >> "$API_STATS_FILE" 2>/dev/null || true
}

quark_curl() {
    quark_rate_limit

    local api_url=""
    local arg result rc

    for arg in "$@"; do
        case "$arg" in
            http://*|https://*)
                api_url="$arg"
                break
                ;;
        esac
    done

    result="$(
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
    )"
    rc=$?

    if [ "$rc" -eq 0 ]; then
        api_stats_record_quark "$api_url" 0
    else
        api_stats_record_quark "$api_url" 1
    fi

    printf '%s' "$result"
    return "$rc"
}

openlist_curl() {
    curl -sS \
        --connect-timeout 20 \
        --max-time 60 \
        "${CURL_TLS_ARGS[@]}" \
        -H "Authorization: $OPENLIST_TOKEN" \
        -H 'Content-Type: application/json' \
        "$@"
}

get_pwd_id() {
    printf '%s\n' "$1" |
        sed -n 's#.*pan\.quark\.cn/s/\([^/#?]*\).*#\1#p'
}

STOKEN_CACHE="$TMP_ROOT/stoken.cache"

get_stoken() {
    local share="$1" pwd_id="$2" token result code message lock

    token="$(awk -F '\t' -v id="$pwd_id" '$1==id{print $2;exit}' "$STOKEN_CACHE" 2>/dev/null || true)"
    if [ -n "$token" ]; then
        printf '%s' "$token"
        return 0
    fi

    lock="$TMP_ROOT/stoken-$pwd_id.lock"
    while ! mkdir "$lock" 2>/dev/null; do sleep 0.2; done

    token="$(awk -F '\t' -v id="$pwd_id" '$1==id{print $2;exit}' "$STOKEN_CACHE" 2>/dev/null || true)"
    if [ -n "$token" ]; then
        rmdir "$lock" 2>/dev/null || true
        printf '%s' "$token"
        return 0
    fi

    result="$(
        quark_curl -X POST \
            'https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc&uc_param_str=' \
            --data "$(jq -nc --arg p "$pwd_id" '{pwd_id:$p,passcode:""}')"
    )" || result=''

    code="$(printf '%s' "$result" | jq -r '.code // -1' 2>/dev/null || echo -1)"
    token="$(printf '%s' "$result" | jq -r '.data.stoken // empty' 2>/dev/null || true)"
    message="$(printf '%s' "$result" | jq -r '.message // "unknown"' 2>/dev/null || echo unknown)"

    if [ "$code" != "0" ] || [ -z "$token" ]; then
        rmdir "$lock" 2>/dev/null || true
        error "获取 stoken 失败：$share：$message"
        return 1
    fi

    printf '%s\t%s\n' "$pwd_id" "$token" >> "$STOKEN_CACHE"
    rmdir "$lock" 2>/dev/null || true
    printf '%s' "$token"
}

# 输出：
# filename<TAB>fid<TAB>share_fid_token<TAB>size<TAB>is_dir<TAB>relative_path
#
# 这里重新扫描源 Share 是因为当前数据库 schema 只保存
# episode / filename / size，没有保存 Quark fid/token。
scan_share_dir() {
    local pwd_id="$1" stoken="$2" pdir_fid="$3" relative="$4" output="$5"
    local page=1 result count total name fid share_token size is_dir

    while :; do
        result="$(
            quark_curl -G \
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
        )" || return 1

        [ "$(printf '%s' "$result" | jq -r '.code // -1')" = "0" ] || return 1

        while IFS=$'\t' read -r name fid share_token size is_dir; do
            [ -n "$name" ] || continue

            if [ "$is_dir" = "true" ]; then
                scan_share_dir \
                    "$pwd_id" "$stoken" "$fid" "$relative$name/" "$output" || return 1
            else
                printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$name" "$fid" "$share_token" "${size:-0}" "$is_dir" "$relative$name" >> "$output"
            fi
        done < <(
            printf '%s' "$result" |
            jq -r '.data.list[]? |
                [(.file_name//""),(.fid//""),(.share_fid_token//""),(.size//0),(.dir//false)] | @tsv'
        )

        count="$(printf '%s' "$result" | jq '.data.list | length')"
        total="$(printf '%s' "$result" | jq '.metadata._total // 0')"

        [ "$count" -eq 0 ] && break
        if [ "$total" -eq 0 ] || [ $((page*50)) -ge "$total" ]; then
            break
        fi
        page=$((page+1))
    done
}

quark_target_list() {
    local target_fid="$1" output="$2"
    local page=1 result count total

    : > "$output"

    while :; do
        result="$(
            quark_curl -G \
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
        )" || return 1

        [ "$(printf '%s' "$result" | jq -r '.code // -1')" = "0" ] || return 1

        printf '%s' "$result" |
            jq -r '.data.list[]? |
                select((.dir//false)==false) |
                [(.file_name//""),(.fid//""),(.size//0)] | @tsv' >> "$output"

        count="$(printf '%s' "$result" | jq '.data.list | length')"
        total="$(printf '%s' "$result" | jq '.metadata._total // 0')"

        [ "$count" -eq 0 ] && break
        if [ "$total" -eq 0 ] || [ $((page*100)) -ge "$total" ]; then
            break
        fi
        page=$((page+1))
    done
}

get_target_fid() {
    local target="$1" result

    result="$(
        quark_curl -X POST \
            'https://drive-pc.quark.cn/1/clouddrive/file/info/path_list?pr=ucpro&fr=pc&uc_param_str=' \
            --data "$(jq -nc --arg p "$target" '{file_path:[$p],namespace:"0"}')"
    )" || return 1

    printf '%s' "$result" | jq -r '.data[0].fid // empty'
}

quark_rename() {
    local fid="$1" new_name="$2" result

    result="$(
        quark_curl -X POST \
            'https://drive-pc.quark.cn/1/clouddrive/file/rename?pr=ucpro&fr=pc&uc_param_str=' \
            --data "$(jq -nc --arg f "$fid" --arg n "$new_name" \
                '{fid:$f,file_name:$n}')"
    )" || return 1

    [ "$(printf '%s' "$result" | jq -r '.code // -1')" = "0" ]
}

quark_delete() {
    local fid="$1" result

    result="$(
        quark_curl -X POST \
            'https://drive-pc.quark.cn/1/clouddrive/file/delete?pr=ucpro&fr=pc&uc_param_str=' \
            --data "$(jq -nc --arg f "$fid" \
                '{action_type:1,filelist:[$f]}')"
    )" || return 1

    [ "$(printf '%s' "$result" | jq -r '.code // -1')" = "0" ]
}

save_one() {
    local pwd_id="$1" stoken="$2" target_fid="$3"
    local fid="$4" token="$5" share="$6"
    local result task_id status start

    result="$(
        quark_curl -X POST \
            'https://drive-pc.quark.cn/1/clouddrive/share/sharepage/save?pr=ucpro&fr=pc&uc_param_str=' \
            --data "$(jq -nc \
                --arg p "$pwd_id" \
                --arg s "$stoken" \
                --arg tf "$target_fid" \
                --arg f "$fid" \
                --arg t "$token" \
                '{
                    fid_list:[$f],
                    fid_token_list:[$t],
                    to_pdir_fid:$tf,
                    pwd_id:$p,
                    stoken:$s,
                    pdir_fid:"0",
                    scene:"link"
                }')"
    )" || return 1

    [ "$(printf '%s' "$result" | jq -r '.code // -1')" = "0" ] || {
        error "转存提交失败：$share：$(printf '%s' "$result" | jq -r '.message // "unknown"')"
        return 1
    }

    task_id="$(printf '%s' "$result" | jq -r '.data.task_id // empty')"
    [ -n "$task_id" ] || {
        error "转存接口没有返回 task_id：$share"
        return 1
    }

    info "转存提交：share=$share fid=$fid task=$task_id"

    start="$(date +%s)"
    while :; do
        sleep "$QUARK_TASK_POLL"

        result="$(
            quark_curl -G \
                'https://drive-pc.quark.cn/1/clouddrive/task' \
                --data-urlencode 'pr=ucpro' \
                --data-urlencode 'fr=pc' \
                --data-urlencode "task_id=$task_id" \
                --data-urlencode 'retry_index=0'
        )" || return 1

        status="$(printf '%s' "$result" | jq -r '.data.status // -1')"

        case "$status" in
            2)
                info "转存成功：task=$task_id"
                return 0
                ;;
            3)
                error "转存失败：task=$task_id：$(printf '%s' "$result" | jq -r '.message // .data.message // "unknown"')"
                return 1
                ;;
            0|1)
                ;;
            *)
                error "转存任务状态异常：task=$task_id status=$status"
                return 1
                ;;
        esac

        if [ $(( $(date +%s)-start )) -ge "$QUARK_TASK_TIMEOUT" ]; then
            error "转存超时：task=$task_id"
            return 1
        fi
    done
}

openlist_refresh() {
    local target="$1"

    openlist_curl -X POST "$OPENLIST_URL/api/fs/list" \
        --data "$(jq -nc --arg p "$target" --arg pw "$OPENLIST_PASSWORD" \
            '{path:$p,password:$pw,refresh:true,page:1,per_page:100}')"
}

webdav_exists() {
    local path="$1" code

    code="$(
        curl -sS \
            --connect-timeout 20 \
            --max-time 60 \
            "${CURL_TLS_ARGS[@]}" \
            -u "$WEBDAV_USER:$WEBDAV_PASS" \
            -o /dev/null \
            -w '%{http_code}' \
            -X PROPFIND \
            -H 'Depth:0' \
            -H 'Content-Type: application/xml' \
            "${WEBDAV_URL%/}${path}" 2>/dev/null
    )" || code=000

    case "$code" in
        200|207) return 0 ;;
        *) return 1 ;;
    esac
}

wait_openlist_refresh() {
    local target="$1" i result

    sleep "$OPENLIST_REFRESH_WAIT"

    for ((i=1; i<=OPENLIST_REFRESH_RETRY; i++)); do
        result="$(openlist_refresh "$target")" || result=''

        if [ "$(printf '%s' "$result" | jq -r '.code // 0' 2>/dev/null)" = "200" ]; then
            return 0
        fi

        sleep "$OPENLIST_REFRESH_INTERVAL"
    done

    return 1
}

episode_from_name() {
    local name="$1" stem
    stem="${name%.*}"

    if [[ "$stem" =~ [Ss]([0-9]{1,2})[._[:space:]-]*[Ee]([0-9]{1,4}) ]]; then
        printf 'S%02dE%02d' \
            "$((10#${BASH_REMATCH[1]}))" \
            "$((10#${BASH_REMATCH[2]}))"
        return 0
    fi

    if [[ "$stem" =~ [Ee]pisode[[:space:]_.-]*([0-9]{1,4}) ]]; then
        printf 'S01E%02d' "$((10#${BASH_REMATCH[1]}))"
        return 0
    fi

    if [[ "$stem" =~ 第[[:space:]]*([0-9]{1,4})[[:space:]]*集 ]]; then
        printf 'S01E%02d' "$((10#${BASH_REMATCH[1]}))"
        return 0
    fi

    if [[ "$stem" =~ ^[[:space:]]*([0-9]{1,4})[[:space:]_.-]*$ ]]; then
        printf 'S01E%02d' "$((10#${BASH_REMATCH[1]}))"
        return 0
    fi

    return 1
}

queue_status() {
    local id="$1" status="$2" error_text="${3:-}"

    sqlite3 "$DB" \
        "UPDATE replace_queue
         SET status=$(sql_quote "$status"),
             finished_at=CASE
                 WHEN '$status' IN ('success','failed') THEN CURRENT_TIMESTAMP
                 ELSE finished_at
             END,
             error=$(sql_quote "$error_text")
         WHERE id=$(sql_quote "$id");"
}

process_queue_item() {
    local qid="$1" show_id="$2" episode="$3" source_share_id="$4"
    local source_file="$5" old_filename="$6" old_size="$7" new_size="$8"

    local target target_quark share_url source_pwd_id
    local target_fid target_list old_fid old_size_actual
    local source_tmp stoken source_line
    local source_fid source_token source_name source_size source_ext desired_name
    local backup_name backup_fid saved_fid saved_name saved_size
    local found i qname qfid qsize

    target="$(sqlite3 "$DB" \
        "SELECT webdav_path FROM shows WHERE id=$(sql_quote "$show_id");")"

    [ -n "$target" ] || {
        error "queue=$qid show_id=$show_id 没有 webdav_path"
        return 1
    }

    # 队列内容来自 resource_check.sh；在进行任何远端操作前仍要确认关键
    # 数值和集数格式，避免手工改库或损坏记录导致错误替换。
    [[ "$episode" =~ ^S[0-9]{2}E[0-9]{2,4}$ ]] || {
        error "queue=$qid 集数格式无效：$episode"
        return 1
    }
    [[ "$old_size" =~ ^[0-9]+$ ]] && [[ "$new_size" =~ ^[0-9]+$ ]] || {
        error "queue=$qid 文件大小无效：old=${old_size:-empty} new=${new_size:-empty}"
        return 1
    }

    case "$target" in
        /kuake|/kuake/*) ;;
        *)
            error "queue=$qid 非法 WebDAV 路径：$target"
            return 1
            ;;
    esac

    target_quark="${target#/kuake}"

    share_url="$(sqlite3 "$DB" \
        "SELECT url FROM shares WHERE id=$(sql_quote "$source_share_id");")"

    [ -n "$share_url" ] || {
        error "queue=$qid source_share_id=$source_share_id 找不到 Share"
        return 1
    }

    source_pwd_id="$(get_pwd_id "$share_url")"
    [ -n "$source_pwd_id" ] || {
        error "queue=$qid 无法从 Share URL 提取 pwd_id：$share_url"
        return 1
    }

    info "替换开始：queue=$qid show=$show_id episode=$episode old=$old_filename old_size=$old_size new_size=$new_size source=$share_url"

    target_fid="$(get_target_fid "$target_quark")"
    [ -n "$target_fid" ] || {
        error "无法取得 Quark 目标目录 FID：$target_quark"
        return 1
    }

    target_list="$TMP_ROOT/target-$qid-before.tsv"
    quark_target_list "$target_fid" "$target_list" || {
        error "无法读取 Quark 目标目录：$target_quark"
        return 1
    }

    # 优先精确匹配 queue 记录的旧文件名。
    old_fid="$(awk -F '\t' -v n="$old_filename" '$1==n{print $2;exit}' "$target_list")"

    # 如果文件被外部程序改过名，再用集数+原记录大小做保守回退。
    if [ -z "$old_fid" ]; then
        old_fid="$(awk -F '\t' -v e="$episode" -v s="$old_size" '
            {
                x=$1
                sub(/\.[^.]*$/,"",x)
                if (x==e && ($3+0)==(s+0)) {
                    print $2
                    exit
                }
            }' "$target_list")"
    fi

    [ -n "$old_fid" ] || {
        error "找不到待替换旧文件：$target/$old_filename"
        return 1
    }

    old_size_actual="$(awk -F '\t' -v f="$old_fid" '$2==f{print $3;exit}' "$target_list")"
    [[ "$old_size_actual" =~ ^[0-9]+$ ]] || old_size_actual="$old_size"

    [ "$old_size_actual" -gt 0 ] || {
        error "旧文件大小无效：$old_filename size=$old_size_actual"
        return 1
    }

    # resource_check.sh 入队时以 old_filename + old_size 标识待替换文件。
    # 执行前若该快照已变化，不能再沿用旧的收益判断；交给下一轮重新扫描并
    # 生成新队列，避免覆盖外部刚更新的同名文件。
    [ "$old_size_actual" = "$old_size" ] || {
        error "待替换旧文件已变化，拒绝使用过期队列：$old_filename queued=$old_size actual=$old_size_actual"
        return 1
    }

    # replace_queue 已经由 resource_check 判定过，这里只做执行层防篡改保护。
    [ "$new_size" -gt "$old_size_actual" ] || {
        error "队列中的新文件不大于旧文件，拒绝执行：old=$old_size_actual new=$new_size"
        return 1
    }

    # 重新扫描源 Share，拿到当前有效 fid / share_fid_token。
    source_tmp="$TMP_ROOT/source-$qid.tsv"
    : > "$source_tmp"

    stoken="$(get_stoken "$share_url" "$source_pwd_id")" || return 1

    scan_share_dir "$source_pwd_id" "$stoken" "0" "/" "$source_tmp" || {
        error "重新扫描源 Share 失败：$share_url"
        return 1
    }

    # source_check.sh 写入队列的是文件名而不是递归路径。分享中可能有
    # 同名文件，因此同时匹配入队时的大小，不能仅取扫描结果中的第一个。
    # 大小不一致意味着缓存已经过期，保守地让本次任务失败而非替换错文件。
    source_line="$(awk -F '\t' -v n="$source_file" -v s="$new_size" '$1==n && $4==s && ($4+0)>0{print;exit}' "$source_tmp")"

    if [ -z "$source_line" ]; then
        local source_base
        source_base="$(basename -- "$source_file")"
        source_line="$(awk -F '\t' -v n="$source_base" -v s="$new_size" '$1==n && $4==s && ($4+0)>0{print;exit}' "$source_tmp")"
    fi

    [ -n "$source_line" ] || {
        error "源 Share 找不到文件：$source_file"
        return 1
    }

    IFS=$'\t' read -r source_name source_fid source_token source_size _ source_rel <<< "$source_line"

    [[ "$source_size" =~ ^[0-9]+$ ]] || source_size="$new_size"

    # 防止 queue 记录过期后，源文件已经被替换/变小。
    [ "$source_size" -gt "$old_size_actual" ] || {
        error "实时源文件已不满足替换要求：$source_name size=$source_size old=$old_size_actual"
        return 1
    }

    source_ext="${source_name##*.}"
    source_ext="$(printf '%s' "$source_ext" | tr '[:upper:]' '[:lower:]')"
    [ -n "$source_ext" ] || source_ext='mkv'

    desired_name="${episode}.${source_ext}"

    # --------------------------------------------------------
    # 第一步：保护旧文件
    #
    # 必须先改名，而不是先删除。
    # 如果后面的转存失败，可以恢复。
    # --------------------------------------------------------
    backup_name=".replace-backup-${qid}-$(date +%s)-${old_filename}"

    if ! quark_rename "$old_fid" "$backup_name"; then
        error "旧文件重命名为备份失败：$old_filename -> $backup_name"
        return 1
    fi

    backup_fid="$old_fid"
    info "旧文件已临时保护：$old_filename -> $backup_name"

    # --------------------------------------------------------
    # 第二步：转存新文件
    # --------------------------------------------------------
    if ! save_one \
        "$source_pwd_id" "$stoken" "$target_fid" \
        "$source_fid" "$source_token" "$share_url"; then

        warn "新文件转存失败，尝试恢复旧文件"

        if quark_rename "$backup_fid" "$old_filename"; then
            info "旧文件恢复成功：$old_filename"
        else
            warn "!!! 旧文件恢复失败：$backup_name"
        fi

        return 1
    fi

    # --------------------------------------------------------
    # 第三步：确认新文件真正出现在 Quark 目标目录
    # 在这一步之前绝不删除旧文件。
    # --------------------------------------------------------
    sleep "$OPENLIST_REFRESH_WAIT"

    found=false

    for ((i=1; i<=REPLACE_MAX_RETRIES+1; i++)); do
        target_list="$TMP_ROOT/target-$qid-after-save-$i.tsv"

        if quark_target_list "$target_fid" "$target_list"; then
            while IFS=$'\t' read -r qname qfid qsize; do
                [ -n "$qname" ] || continue

                if [ "$qname" = "$source_name" ] &&
                   [ "$qsize" = "$source_size" ]; then
                    saved_fid="$qfid"
                    saved_name="$qname"
                    saved_size="$qsize"
                    found=true
                    break
                fi
            done < "$target_list"
        fi

        [ "$found" = true ] && break
        sleep "$REPLACE_VERIFY_INTERVAL"
    done

    if [ "$found" != true ]; then
        error "新文件转存后无法在目标目录确认：$source_name size=$source_size"

        if quark_rename "$backup_fid" "$old_filename"; then
            info "旧文件恢复成功：$old_filename"
        else
            warn "!!! 新文件未确认且旧文件恢复失败：$backup_name"
        fi

        return 1
    fi

    # --------------------------------------------------------
    # 第四步：统一命名
    #
    # 与 addfile.sh 保持一致：
    #   S01E01.mkv
    #   S01E02.mp4
    # --------------------------------------------------------
    if [ "$saved_name" != "$desired_name" ]; then
        if ! quark_rename "$saved_fid" "$desired_name"; then
            error "新文件重命名失败：$saved_name -> $desired_name"

            # 新文件已经存在但没有完成标准化。
            # 此时恢复旧文件，不删除新文件，避免数据丢失。
            if quark_rename "$backup_fid" "$old_filename"; then
                info "旧文件恢复成功：$old_filename"
            else
                warn "!!! 旧文件恢复失败：$backup_name"
            fi

            return 1
        fi

        saved_name="$desired_name"
    fi

    # --------------------------------------------------------
    # 第五步：再次确认标准文件存在且大小正确
    # --------------------------------------------------------
    target_list="$TMP_ROOT/target-$qid-final.tsv"

    quark_target_list "$target_fid" "$target_list" || {
        error "新文件重命名后无法读取目标目录"

        if quark_rename "$backup_fid" "$old_filename"; then
            info "旧文件恢复成功：$old_filename"
        else
            warn "!!! 旧文件恢复失败：$backup_name"
        fi

        return 1
    }

    saved_fid="$(awk -F '\t' -v n="$desired_name" '$1==n{print $2;exit}' "$target_list")"
    saved_size="$(awk -F '\t' -v n="$desired_name" '$1==n{print $3;exit}' "$target_list")"

    if [ -z "$saved_fid" ] || [ "$saved_size" -lt "$source_size" ]; then
        error "最终新文件验证失败：$desired_name size=${saved_size:-0} expected=$source_size"

        if quark_rename "$backup_fid" "$old_filename"; then
            info "旧文件恢复成功：$old_filename"
        else
            warn "!!! 旧文件恢复失败：$backup_name"
        fi

        return 1
    fi

    # --------------------------------------------------------
    # 第六步：现在才删除旧文件
    # --------------------------------------------------------
    if ! quark_delete "$backup_fid"; then
        error "新文件已成功，但删除旧文件备份失败：$backup_name"
        # 不标记 success；这样下一次运行仍能看到 failed 状态。
        return 1
    fi

    # 最后刷新 OpenList，并确认 WebDAV 目标仍可访问。
    wait_openlist_refresh "$target" || warn "OpenList 刷新失败，继续检查 WebDAV"

    if ! webdav_exists "$target"; then
        warn "WebDAV 目标目录暂时不可访问，但 Quark 替换动作已经完成"
    fi

    info "替换成功：queue=$qid episode=$episode $old_filename($old_size_actual) -> $desired_name($source_size)"
    return 0
}

# ============================================================
# 主流程
# ============================================================

if ! sqlite3 "$DB" "SELECT 1;" >/dev/null 2>&1; then
    error "无法打开数据库：$DB"
    exit 2
fi

mapfile -t QUEUE_ROWS < <(
    sqlite3 -tabs "$DB" "
        SELECT
            id,
            show_id,
            episode,
            source_share_id,
            source_file,
            old_filename,
            old_size,
            new_size
        FROM replace_queue
        WHERE status='pending'
        ORDER BY id;
    "
)

if [ "${#QUEUE_ROWS[@]}" -eq 0 ]; then
    info "当前没有 pending 替换任务"
    exit 0
fi

overall_rc=0

for row in "${QUEUE_ROWS[@]}"; do
    IFS=$'\t' read -r \
        qid show_id episode source_share_id source_file \
        old_filename old_size new_size <<< "$row"

    [ -n "$qid" ] || continue

    # 抢占式更新状态。
    changed="$(
        sqlite3 "$DB" "
            UPDATE replace_queue
            SET status='running', error=NULL
            WHERE id=$(sql_quote "$qid") AND status='pending';
            SELECT changes();
        "
    )"

    [ "$changed" = "1" ] || continue

    if process_queue_item \
        "$qid" "$show_id" "$episode" "$source_share_id" \
        "$source_file" "$old_filename" "$old_size" "$new_size"; then

        queue_status "$qid" success ''

    else

        queue_status "$qid" failed \
            "replace.sh 执行失败，请查看 replace.log"

        overall_rc=1
    fi
done

exit "$overall_rc"
