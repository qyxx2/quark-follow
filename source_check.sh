#!/bin/bash

set -u

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONFIG="$BASE_DIR/config.local"
DB="$BASE_DIR/resource.db"
LOG_DIR_DEFAULT="$BASE_DIR/logs"
LOG_DIR="$LOG_DIR_DEFAULT"
LOG_FILE="$LOG_DIR/source_check.log"
TMP_ROOT="/tmp/quark-follow-source-$$"
API_STATS_FILE="$LOG_DIR_DEFAULT/api_stats_live_source_check_$$.tsv"
API_STATS_PY="$BASE_DIR/docker/api_stats.py"

mkdir -p "$TMP_ROOT"

cleanup() {
    if [ -s "${API_STATS_FILE:-}" ] && [ -f "${API_STATS_PY:-}" ]; then
        python3 "$API_STATS_PY" --flush "$API_STATS_FILE" >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT INT TERM

# ============================================================
# 读取配置
# 与 addfile.sh 保持相同的配置调用方式
# ============================================================

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: 找不到 config.local: $CONFIG" >&2
    exit 1
fi

# shellcheck disable=SC1090
. "$CONFIG"

LOG_DIR="${LOG_DIR:-$LOG_DIR_DEFAULT}"
LOG_FILE="$LOG_DIR/source_check.log"
API_STATS_FILE="$LOG_DIR/api_stats_live_source_check_$$.tsv"

: "${QUARK_COOKIE:?config.local 中没有 QUARK_COOKIE}"

QUARK_API_DELAY="${QUARK_API_DELAY:-2}"
CURL_TLS_MAX="${CURL_TLS_MAX:-1.2}"

VIDEO_MIN_MB="${VIDEO_MIN_MB:-100}"
VIDEO_MAX_GB="${VIDEO_MAX_GB:-40}"
ARCHIVE_LOG_MB="${ARCHIVE_LOG_MB:-500}"
SHARE_DEAD_FAIL_COUNT="${SHARE_DEAD_FAIL_COUNT:-3}"

# 单次运行默认扫描所有非 dead 分享。
# 可通过命令行缩小范围，方便首次测试：
#   ./source_check.sh --share-id 1
#   ./source_check.sh --show-id 1

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$CONFIG" 2>/dev/null || true
chmod 600 "$LOG_FILE" 2>/dev/null || true
chmod 700 "$0" 2>/dev/null || true

# API 统计文件同样只保存时间、服务、接口名和失败标记，不保存 URL 参数、Cookie 或 Token。
touch "$API_STATS_FILE" 2>/dev/null || true
chmod 600 "$API_STATS_FILE" 2>/dev/null || true

# ============================================================
# 日志
# ============================================================

log() {
    printf '[%s] [PARSE] %s\n' \
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
# API 统计
# ============================================================

api_stats_record_quark() {
    local url="$1"
    local failed="$2"
    local path operation arg

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

# ============================================================
# 依赖检查
# ============================================================

for CMD in bash curl jq sed awk sort md5sum date sleep mktemp sqlite3; do
    if ! command -v "$CMD" >/dev/null 2>&1; then
        error "缺少命令：$CMD"
        echo "ERROR: 缺少命令 $CMD，详见 $LOG_FILE" >&2
        exit 1
    fi
done

if [ ! -f "$DB" ]; then
    error "找不到数据库：$DB"
    echo "ERROR: 找不到数据库 $DB" >&2
    exit 1
fi

# ============================================================
# SQLite 结构检查
# 数据库结构以数据库设计文档为唯一准则。
# 本脚本不创建、不修改表结构。
# ============================================================

sqlite_check_column() {
    local table="$1"
    local column="$2"

    sqlite3 "$DB" \
        "SELECT 1 FROM pragma_table_info('$table') WHERE name='$column' LIMIT 1;" \
        | grep -qx '1'
}

for table in shows shares share_files; do
    if ! sqlite3 "$DB" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='$table' LIMIT 1;" | grep -qx '1'; then
        error "数据库缺少表：$table"
        echo "ERROR: 数据库缺少表 $table" >&2
        exit 1
    fi
done

for spec in \
    "shows:id" \
    "shares:id" \
    "shares:show_id" \
    "shares:url" \
    "shares:seedhub_rank" \
    "shares:status" \
    "shares:fail_count" \
    "shares:last_check" \
    "shares:last_success" \
    "share_files:id" \
    "share_files:share_id" \
    "share_files:episode" \
    "share_files:filename" \
    "share_files:size" \
    "share_files:last_check"; do

    table="${spec%%:*}"
    column="${spec#*:}"

    if ! sqlite_check_column "$table" "$column"; then
        error "数据库表 $table 缺少字段：$column"
        echo "ERROR: $table 缺少字段 $column" >&2
        exit 1
    fi
done

# SQLite 并发等待
sqlite3 "$DB" 'PRAGMA busy_timeout=10000;' >/dev/null 2>&1 || true

# ============================================================
# curl TLS
# 与 addfile.sh 保持一致
# ============================================================

CURL_TLS_ARGS=()

if [ -n "$CURL_TLS_MAX" ]; then
    CURL_TLS_ARGS+=(--tls-max "$CURL_TLS_MAX")
fi

# ============================================================
# Quark 全局限速
# 本脚本当前不主动并行，但仍保留锁，避免以后扩展时破坏限速。
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
# 与 addfile.sh 使用相同 Cookie / Header / TLS 方式
# API 统计只在 curl 完成后追加事件；统计失败绝不影响真实请求。
# ============================================================

quark_curl() {
    quark_rate_limit

    local api_url=""
    local arg
    local result
    local rc

    for arg in "$@"; do
        case "$arg" in
            http://*|https://*)
                api_url="$arg"
                break
                ;;
        esac
    done

    result="$(curl -sS \
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

# ============================================================
# 提取 Quark 分享 ID
# ============================================================

get_pwd_id() {
    printf '%s\n' "$1" |
        sed -n 's#.*pan\.quark\.cn/s/\([^/#?]*\).*#\1#p'
}

# ============================================================
# stoken 临时缓存
# 同一次运行中，一个分享只请求一次。
# stoken 不写日志。
# ============================================================

STOKEN_CACHE="$TMP_ROOT/stoken.cache"

get_stoken() {
    local share="$1"
    local pwd_id="$2"

    local token result code message lock

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
        error "分享 stoken 获取失败：$share：$message"
        rmdir "$lock" 2>/dev/null || true
        return 1
    fi

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
# 解析剧集编号
# 与 addfile.sh 保持一致
# 输出：S01E01
# ============================================================

parse_episode() {
    local name="$1"
    local stem
    local season=1
    local episode

    stem="${name%.*}"
    stem="$(normalize_circled "$stem")"

    if [[ "$stem" =~ [Ss]([0-9]{1,2})[[:space:]_.-]*[Ee][Pp]?([0-9]{1,4}) ]]; then
        season=$((10#${BASH_REMATCH[1]}))
        episode=$((10#${BASH_REMATCH[2]}))
    elif [[ "$stem" =~ [Ee][Pp]?([0-9]{1,4}) ]]; then
        episode=$((10#${BASH_REMATCH[1]}))
    else
        if [[ "$stem" =~ 第[[:space:]]*([0-9]+|[零一二两三四五六七八九十百]+)[[:space:]]*季 ]]; then
            season="$(cn_number "${BASH_REMATCH[1]}")" || return 1
        fi

        if [[ "$stem" =~ 第[[:space:]]*([0-9]+|[零一二两三四五六七八九十百]+)[[:space:]]*集 ]]; then
            episode="$(cn_number "${BASH_REMATCH[1]}")" || return 1
        elif [[ "$stem" =~ ^[[:space:]]*([0-9]{1,3})[[:space:]_.-]*$ ]]; then
            episode=$((10#${BASH_REMATCH[1]}))
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
# 文件类型
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
# 输出 TSV：
# episode  size  fid  share_fid_token  filename  extension  relative_path
#
# fid/token/path 当前主要用于运行时验证和日志；
# 当前数据库设计只保存 episode/filename/size。
# ============================================================

scan_share_dir() {
    local pwd_id="$1"
    local stoken="$2"
    local pdir_fid="$3"
    local relative="$4"
    local output="$5"

    local page=1
    local result count total

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
        )" || {
            error "Quark 请求失败：share_id=$pwd_id relative=${relative:-/} page=$page"
            return 1
        }

        if ! printf '%s' "$result" | jq -e . >/dev/null 2>&1; then
            error "Quark 返回非 JSON：share_id=$pwd_id relative=${relative:-/} page=$page"
            return 1
        fi

        if [ "$(printf '%s' "$result" | jq -r '.code // -1')" != "0" ]; then
            error "分享目录读取失败：share_id=$pwd_id relative=${relative:-/} page=$page message=$(printf '%s' "$result" | jq -r '.message // "unknown"')"
            return 1
        fi

        while IFS=$'\t' read -r name fid share_token size is_dir; do
            [ -n "$name" ] || continue

            if [ "$is_dir" = "true" ]; then
                if ! scan_share_dir \
                    "$pwd_id" \
                    "$stoken" \
                    "$fid" \
                    "$relative$name/" \
                    "$output"; then
                    return 1
                fi
                continue
            fi

            if is_archive "$name"; then
                if [ "${size:-0}" -ge $((ARCHIVE_LOG_MB * 1024 * 1024)) ]; then
                    warn "疑似视频压缩包：share_id=$pwd_id path=$relative$name size=$size"
                fi
                continue
            fi

            is_video "$name" || continue

            if [ "${size:-0}" -lt $((VIDEO_MIN_MB * 1024 * 1024)) ]; then
                warn "过滤异常小视频：share_id=$pwd_id path=$relative$name size=$size"
                continue
            fi

            if [ "$VIDEO_MAX_GB" -gt 0 ] &&
               [ "${size:-0}" -gt $((VIDEO_MAX_GB * 1024 * 1024 * 1024)) ]; then
                warn "过滤异常大视频：share_id=$pwd_id path=$relative$name size=$size"
                continue
            fi

            local key ext
            ext="${name##*.}"
            key="$(parse_episode "$name" || true)"

            if ! [[ "${size:-0}" =~ ^[0-9]+$ ]]; then
                warn "文件大小不是有效整数，跳过缓存：share_id=$pwd_id path=$relative$name size=${size:-0}"
                continue
            fi

            if [ -n "$key" ]; then
                printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$key" \
                    "$size" \
                    "$fid" \
                    "$share_token" \
                    "$name" \
                    "${ext,,}" \
                    "$relative" >> "$output"
            else
                warn "无法解析集数，跳过缓存：share_id=$pwd_id path=$relative$name"
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

        if [ "$total" -eq 0 ] || [ $((page * 50)) -ge "$total" ]; then
            break
        fi

        page=$((page + 1))
    done
}

# ============================================================
# SQL 字符串安全转义
# ============================================================

sql_quote() {
    local value="$1"
    value="${value//\'/\'\'}"
    printf "'%s'" "$value"
}

# ============================================================
# 更新 shares 状态
# 失败不会直接删除 share；达到阈值才标记 dead。
# ============================================================

mark_share_success() {
    local share_id="$1"

    sqlite3 "$DB" <<SQL
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;
UPDATE shares
   SET status='valid',
       fail_count=0,
       last_check=CURRENT_TIMESTAMP,
       last_success=CURRENT_TIMESTAMP,
       updated_at=CURRENT_TIMESTAMP
 WHERE id=$(sql_quote "$share_id") AND COALESCE(status,'unknown')!='excluded';
COMMIT;
SQL
}

mark_share_failure() {
    local share_id="$1"
    local sql_file
    sql_file="$TMP_ROOT/fail-${share_id}.sql"

    cat > "$sql_file" <<SQL
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;
UPDATE shares
   SET fail_count=COALESCE(fail_count,0)+1,
       last_check=CURRENT_TIMESTAMP,
       updated_at=CURRENT_TIMESTAMP
 WHERE id=$(sql_quote "$share_id") AND COALESCE(status,'unknown')!='excluded';

UPDATE shares
   SET status='dead'
 WHERE id=$(sql_quote "$share_id")
   AND COALESCE(fail_count,0) >= $((SHARE_DEAD_FAIL_COUNT));
COMMIT;
SQL

    sqlite3 "$DB" < "$sql_file"
}

# ============================================================
# 成功后写入 share_files
#
# 关键安全原则：
#   1. 先完整扫描到临时文件
#   2. 扫描成功后才进入事务
#   3. 删除该 share 的旧缓存
#   4. 写入本次完整结果
#   5. 任意 API 扫描失败则旧缓存完全不动
#
# share_files 是缓存事实表，不在这里做“最佳资源”选择。
# 同一分享里同一集存在多个视频时，全部保留。
# ============================================================

replace_share_cache() {
    local share_id="$1"
    local scan_file="$2"
    local sql_file

    sql_file="$TMP_ROOT/write-${share_id}.sql"

    {
        printf 'PRAGMA busy_timeout=10000;\n'
        printf 'BEGIN IMMEDIATE;\n'
        printf 'DELETE FROM share_files WHERE share_id=%s;\n' "$(sql_quote "$share_id")"

        while IFS=$'\t' read -r episode size fid share_token filename ext relative; do
            [ -n "$episode" ] || continue

            printf 'INSERT INTO share_files (share_id, episode, filename, size, last_check) VALUES (%s,%s,%s,%s,%s);\n' \
                "$(sql_quote "$share_id")" \
                "$(sql_quote "$episode")" \
                "$(sql_quote "$filename")" \
                "${size:-0}" \
                'CURRENT_TIMESTAMP'
        done < "$scan_file"

        printf 'COMMIT;\n'
    } > "$sql_file"

    if ! sqlite3 "$DB" < "$sql_file"; then
        error "share_files 写入失败：share_id=$share_id"
        return 1
    fi

    return 0
}

# ============================================================
# 单个 share
# ============================================================

process_share() {
    local share_id="$1"
    local show_id="$2"
    local url="$3"
    local seedhub_rank="$4"
    local status="$5"

    local pwd_id
    local stoken
    local scan_file
    local before_count after_count episode_count

    status="$(sqlite3 "$DB" "SELECT COALESCE(status,'unknown') FROM shares WHERE id=$(sql_quote "$share_id") LIMIT 1;")"
    if [ "$status" = "excluded" ]; then
        log "SOURCE跳过已排除 Share：share_id=$share_id show_id=$show_id rank=$seedhub_rank url=$url"
        return 0
    fi

    log "SOURCE开始：share_id=$share_id show_id=$show_id rank=$seedhub_rank status=$status url=$url"

    pwd_id="$(get_pwd_id "$url")"

    if [ -z "$pwd_id" ]; then
        error "分享链接格式错误：share_id=$share_id url=$url"
        mark_share_failure "$share_id" || true
        return 1
    fi

    stoken="$(get_stoken "$url" "$pwd_id" || true)"

    if [ -z "$stoken" ]; then
        mark_share_failure "$share_id" || true
        return 1
    fi

    scan_file="$TMP_ROOT/share_${share_id}.tsv"
    : > "$scan_file"

    if ! scan_share_dir "$pwd_id" "$stoken" "0" "" "$scan_file"; then
        error "分享递归扫描失败，保留旧缓存：share_id=$share_id url=$url"
        mark_share_failure "$share_id" || true
        return 1
    fi

    # 先按数据库唯一键 (episode, filename) 去重，避免递归扫描出现同名文件时触发 UNIQUE constraint。
    # 对重复键保留更大的 size；数据库当前不保存 relative_path，因此同名文件只能保留一条事实。
    local dedup_file="$TMP_ROOT/share_${share_id}.dedup.tsv"
    awk -F '\t' 'BEGIN{OFS="\t"} {k=$1 SUBSEP $5; if (!(k in size) || $2 > size[k]) {size[k]=$2; line[k]=$0}} END{for(k in line) print line[k]}' "$scan_file" > "$dedup_file"
    sort -u "$dedup_file" -o "$dedup_file" 2>/dev/null || true
    mv "$dedup_file" "$scan_file"

    before_count="$(sqlite3 "$DB" "SELECT COUNT(*) FROM share_files WHERE share_id=$(sql_quote "$share_id");")"

    current_status="$(sqlite3 "$DB" "SELECT COALESCE(status,'unknown') FROM shares WHERE id=$(sql_quote "$share_id") LIMIT 1;")"
    if [ "$current_status" = "excluded" ]; then
        log "SOURCE跳过缓存写入：Share 在扫描过程中被排除：share_id=$share_id url=$url"
        return 0
    fi

    if ! replace_share_cache "$share_id" "$scan_file"; then
        mark_share_failure "$share_id" || true
        return 1
    fi

    after_count="$(sqlite3 "$DB" "SELECT COUNT(*) FROM share_files WHERE share_id=$(sql_quote "$share_id");")"
    episode_count="$(awk -F '\t' '{seen[$1]=1} END {print length(seen)+0}' "$scan_file")"

    mark_share_success "$share_id" || {
        error "shares 状态更新失败：share_id=$share_id"
        return 1
    }

    log "SOURCE成功：share_id=$share_id rank=$seedhub_rank old_cache=$before_count new_cache=$after_count episodes=$episode_count"
    return 0
}

# ============================================================
# 参数
# ============================================================

MODE="all"
TARGET_ID=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --share-id)
            [ "$#" -ge 2 ] || { echo "ERROR: --share-id 缺少参数" >&2; exit 2; }
            MODE="share"
            TARGET_ID="$2"
            shift 2
            ;;
        --show-id)
            [ "$#" -ge 2 ] || { echo "ERROR: --show-id 缺少参数" >&2; exit 2; }
            MODE="show"
            TARGET_ID="$2"
            shift 2
            ;;
        -h|--help)
            cat <<HELP
用法：
  ./source_check.sh                 扫描所有 status != dead 的分享
  ./source_check.sh --share-id 3   只扫描 shares.id=3
  ./source_check.sh --show-id 1    只扫描指定剧集的非 dead 分享

说明：
  - 成功扫描后才替换该 share 的 share_files 缓存
  - 扫描失败不会破坏旧缓存
  - 一次失败不会立即删除分享
  - 连续失败达到 SHARE_DEAD_FAIL_COUNT 后标记 dead
HELP
            exit 0
            ;;
        *)
            echo "ERROR: 未知参数：$1" >&2
            exit 2
            ;;
    esac
done

# ============================================================
# 读取待处理 shares
# 顺序按 SeedHub 原始 rank，保证首次测试容易观察。
# ============================================================

SHARE_LIST="$TMP_ROOT/share_list.tsv"

case "$MODE" in
    all)
        sqlite3 -tabs "$DB" <<'SQL' > "$SHARE_LIST"
SELECT id, show_id, url, seedhub_rank, status
  FROM shares
 WHERE COALESCE(status,'unknown') NOT IN ('dead','excluded')
 ORDER BY show_id, seedhub_rank, id;
SQL
        ;;

    share)
        sqlite3 -tabs "$DB" \
            "SELECT id, show_id, url, seedhub_rank, status FROM shares WHERE id=$(sql_quote "$TARGET_ID") AND COALESCE(status,'unknown')!='excluded' LIMIT 1;" \
            > "$SHARE_LIST"
        ;;

    show)
        sqlite3 -tabs "$DB" \
            "SELECT id, show_id, url, seedhub_rank, status FROM shares WHERE show_id=$(sql_quote "$TARGET_ID") AND COALESCE(status,'unknown') NOT IN ('dead','excluded') ORDER BY seedhub_rank, id;" \
            > "$SHARE_LIST"
        ;;
esac

if [ ! -s "$SHARE_LIST" ]; then
    log "没有需要扫描的 shares（排除项也不会扫描）：mode=$MODE target=${TARGET_ID:-all}"
    echo "没有需要扫描的 shares。"
    exit 0
fi

# ============================================================
# 主循环
# ============================================================

TOTAL=0
SUCCESS=0
FAIL=0

while IFS=$'\t' read -r share_id show_id url seedhub_rank status; do
    [ -n "$share_id" ] || continue
    TOTAL=$((TOTAL + 1))

    if process_share "$share_id" "$show_id" "$url" "$seedhub_rank" "$status"; then
        SUCCESS=$((SUCCESS + 1))
    else
        FAIL=$((FAIL + 1))
    fi
done < "$SHARE_LIST"

log "全部 source_check 结束：total=$TOTAL success=$SUCCESS fail=$FAIL（excluded 已跳过）"

printf '[PARSE] source_check 完成：total=%s success=%s fail=%s\n' "$TOTAL" "$SUCCESS" "$FAIL"

# 只要存在失败，返回非 0，方便 cron / 外部监控发现问题。
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi

exit 0
