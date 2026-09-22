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

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: 找不到 config.local: $CONFIG" >&2
    exit 1
fi

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
PENDING_RECHECK_HOURS="${PENDING_RECHECK_HOURS:-24}"
PENDING_MAX_CHECKS="${PENDING_MAX_CHECKS:-3}"

[[ "$PENDING_RECHECK_HOURS" =~ ^[0-9]+$ ]] || { echo "ERROR: PENDING_RECHECK_HOURS 必须是非负整数" >&2; exit 2; }
[[ "$PENDING_MAX_CHECKS" =~ ^[0-9]+$ ]] && [ "$PENDING_MAX_CHECKS" -ge 1 ] || { echo "ERROR: PENDING_MAX_CHECKS 必须 >= 1" >&2; exit 2; }

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$CONFIG" 2>/dev/null || true
chmod 600 "$LOG_FILE" 2>/dev/null || true
chmod 700 "$0" 2>/dev/null || true

touch "$API_STATS_FILE" 2>/dev/null || true
chmod 600 "$API_STATS_FILE" 2>/dev/null || true

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

sqlite3 "$DB" 'PRAGMA busy_timeout=10000;' >/dev/null 2>&1 || true

sqlite3 "$DB" <<'SQL' >/dev/null
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS share_blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER NOT NULL,
    pwd_id TEXT NOT NULL,
    url TEXT NOT NULL,
    seedhub_entry_url TEXT,
    reason TEXT NOT NULL DEFAULT 'empty_after_3_scans',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (show_id) REFERENCES shows(id) ON DELETE CASCADE,
    UNIQUE(show_id, pwd_id)
);
CREATE INDEX IF NOT EXISTS idx_share_blacklist_show_pwd ON share_blacklist(show_id,pwd_id);
SQL

changed_pool=0
for spec in \
    "shows:share_scan_rank" \
    "shows:discovery_cursor" \
    "shares:seedhub_entry_url" \
    "shares:pool_type" \
    "shares:pending_probe_count" \
    "shares:used_count" \
    "shares:last_used_at"; do
    table="${spec%%:*}"
    column="${spec#*:}"
    if ! sqlite_check_column "$table" "$column"; then
        case "$table:$column" in
            shows:share_scan_rank) definition="INTEGER DEFAULT 0" ;;
            shows:discovery_cursor) definition="INTEGER NOT NULL DEFAULT 20" ;;
            shares:seedhub_entry_url) definition="TEXT" ;;
            shares:pool_type) definition="TEXT NOT NULL DEFAULT 'front20'" ;;
            shares:pending_probe_count) definition="INTEGER NOT NULL DEFAULT 0" ;;
            shares:used_count) definition="INTEGER NOT NULL DEFAULT 0" ;;
            shares:last_used_at) definition="TEXT" ;;
        esac
        sqlite3 "$DB" "ALTER TABLE $table ADD COLUMN $column $definition;" || exit 1
        [ "$table:$column" = "shares:pool_type" ] && changed_pool=1
    fi
done

# 仅在迁移第一次添加 pool_type 时按旧 rank 初始化；以后绝不覆盖动态 pool_type。
if [ "$changed_pool" -eq 1 ]; then
    sqlite3 "$DB" "UPDATE shares SET pool_type=CASE WHEN COALESCE(seedhub_rank,999999) BETWEEN 1 AND 20 THEN 'front20' ELSE 'overflow' END;" || exit 1
fi

# 新字段迁移完成后再验证，避免旧数据库在迁移之前被提前拒绝。
for spec in \
    "shares:seedhub_entry_url" \
    "shares:pool_type" \
    "shares:pending_probe_count" \
    "shares:used_count" \
    "shares:last_used_at"; do
    table="${spec%%:*}"
    column="${spec#*:}"
    if ! sqlite_check_column "$table" "$column"; then
        error "数据库表 $table 缺少字段：$column"
        echo "ERROR: $table 缺少字段 $column" >&2
        exit 1
    fi
done

sqlite3 "$DB" "CREATE INDEX IF NOT EXISTS idx_shares_show_pool_rank ON shares(show_id,pool_type,seedhub_rank); CREATE INDEX IF NOT EXISTS idx_shares_show_entry ON shares(show_id,seedhub_entry_url);" || exit 1

# 旧 valid + 0文件记录转成 pending，避免历史数据继续触发“0文件优先重扫”。
sqlite3 "$DB" <<'SQL'
PRAGMA busy_timeout=10000;
UPDATE shares
   SET status='pending',
       pending_probe_count=CASE WHEN COALESCE(pending_probe_count,0)<1 THEN 1 ELSE pending_probe_count END,
       fail_count=0,
       last_success=NULL,
       updated_at=CURRENT_TIMESTAMP
 WHERE status='valid'
   AND COALESCE(pending_probe_count,0)=0
   AND last_success IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM share_files sf WHERE sf.share_id=shares.id);
SQL


CURL_TLS_ARGS=()

if [ -n "$CURL_TLS_MAX" ]; then
    CURL_TLS_ARGS+=(--tls-max "$CURL_TLS_MAX")
fi

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
# 持久化 stoken 缓存
#
# stoken_cache.sh 负责跨进程/跨运行缓存，并在明确判断 stoken 失效时
# 自动刷新一次当前请求。普通网络失败不会使缓存失效。
# ============================================================
STOKEN_CACHE_FILE="${STOKEN_CACHE_FILE:-$BASE_DIR/stoken_cache.tsv}"
STOKEN_CACHE_HELPER="$BASE_DIR/stoken_cache.sh"

if [ ! -f "$STOKEN_CACHE_HELPER" ]; then
    error "缺少 stoken_cache.sh：$STOKEN_CACHE_HELPER"
    exit 2
fi

# shellcheck disable=SC1090
. "$STOKEN_CACHE_HELPER"

quark_curl() {
    quark_curl_with_stoken_refresh "$@"
}
get_pwd_id() {
    printf '%s\n' "$1" |
        sed -n 's#.*pan\.quark\.cn/s/\([^/#?]*\).*#\1#p'
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
                jq -r '.data.list[]? | [(.file_name // ""),(.fid // ""),(.share_fid_token // ""),(.size // 0),(.dir // false)] | @tsv'
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
sql_quote() {
    local value="$1"
    value="${value//\'/\'\'}"
    printf "'%s'" "$value"
}

mark_share_success() {
    local share_id="$1"

    sqlite3 "$DB" <<SQL
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;
UPDATE shares
   SET status='valid',
       pending_probe_count=0,
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
    local current_status
    current_status="$(sqlite3 "$DB" "SELECT COALESCE(status,'unknown') FROM shares WHERE id=$(sql_quote "$share_id") LIMIT 1;")"

    # Pending 的失败不是“第几次空目录”；不消耗三次机会，也不进入 dead。
    if [ "$current_status" = "pending" ]; then
        sqlite3 "$DB" "UPDATE shares SET last_check=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=$(sql_quote "$share_id");"
        return 0
    fi

    local sql_file="$TMP_ROOT/fail-${share_id}.sql"
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

mark_share_empty() {
    local share_id="$1"
    local show_id="$2"
    local url="$3"
    local pwd_id="$4"
    local seedhub_entry_url
    local seedhub_rank
    local probe_count
    local current_status
    local pending_replace

    current_status="$(sqlite3 "$DB" "SELECT COALESCE(status,'unknown') FROM shares WHERE id=$(sql_quote "$share_id") LIMIT 1;")"
    [ "$current_status" != "excluded" ] || return 0

    seedhub_entry_url="$(sqlite3 "$DB" "SELECT COALESCE(seedhub_entry_url,'') FROM shares WHERE id=$(sql_quote "$share_id") LIMIT 1;")"
    seedhub_rank="$(sqlite3 "$DB" "SELECT COALESCE(seedhub_rank,'') FROM shares WHERE id=$(sql_quote "$share_id") LIMIT 1;")"
    probe_count="$(sqlite3 "$DB" "SELECT COALESCE(pending_probe_count,0)+1 FROM shares WHERE id=$(sql_quote "$share_id") LIMIT 1;")"
    [[ "$probe_count" =~ ^[0-9]+$ ]] || probe_count=1

    if [ "$probe_count" -ge "$PENDING_MAX_CHECKS" ]; then
        pending_replace="$(sqlite3 "$DB" "SELECT COUNT(*) FROM replace_queue WHERE source_share_id=$(sql_quote "$share_id") AND status='running';")"
        if [ "${pending_replace:-0}" -gt 0 ]; then
            # 极端并发情况下不删除正在执行替换任务依赖的 Share；下一轮仍允许再次判断。
            sqlite3 "$DB" <<SQL
PRAGMA busy_timeout=10000;
UPDATE shares SET status='pending', pending_probe_count=CASE WHEN $PENDING_MAX_CHECKS>1 THEN $((PENDING_MAX_CHECKS-1)) ELSE 0 END, last_check=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=$(sql_quote "$share_id");
SQL
            warn "空 Share 已达到最大尝试，但存在 running replace，暂不删除：share_id=$share_id"
            return 0
        fi

        if ! sqlite3 "$DB" <<SQL
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;
INSERT INTO share_blacklist(show_id,pwd_id,url,seedhub_entry_url,reason,created_at)
VALUES($(sql_quote "$show_id"),$(sql_quote "$pwd_id"),$(sql_quote "$url"),$(sql_quote "$seedhub_entry_url"),'empty_after_3_scans',CURRENT_TIMESTAMP)
ON CONFLICT(show_id,pwd_id) DO UPDATE SET
    url=excluded.url,
    seedhub_entry_url=excluded.seedhub_entry_url,
    reason=excluded.reason,
    created_at=CURRENT_TIMESTAMP;
DELETE FROM share_files WHERE share_id=$(sql_quote "$share_id");
DELETE FROM shares WHERE id=$(sql_quote "$share_id") AND show_id=$(sql_quote "$show_id");
COMMIT;
SQL
        then
            log "SOURCE淘汰空 Share：share_id=$share_id show_id=$show_id rank=${seedhub_rank:-} url=$url 已达到 ${PENDING_MAX_CHECKS} 次空目录扫描，加入黑名单"
            return 0
        fi
        error "写入空 Share 黑名单或删除记录失败：share_id=$share_id url=$url"
        return 1
    fi

    if ! sqlite3 "$DB" <<SQL
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;
DELETE FROM share_files WHERE share_id=$(sql_quote "$share_id");
UPDATE shares
   SET status='pending',
       pending_probe_count=$probe_count,
       fail_count=0,
       last_check=CURRENT_TIMESTAMP,
       last_success=NULL,
       updated_at=CURRENT_TIMESTAMP
 WHERE id=$(sql_quote "$share_id") AND show_id=$(sql_quote "$show_id");
COMMIT;
SQL
    then
        log "SOURCE空 Share 转待定：share_id=$share_id show_id=$show_id probe=$probe_count/$PENDING_MAX_CHECKS next_after=${PENDING_RECHECK_HOURS}h url=$url"
        return 0
    fi
    error "更新 Pending Share 状态失败：share_id=$share_id"
    return 1
}

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

    if sqlite3 "$DB" "SELECT 1 FROM share_blacklist WHERE show_id=$(sql_quote "$show_id") AND pwd_id=$(sql_quote "$pwd_id") LIMIT 1;" | grep -qx 1; then
        log "SOURCE跳过黑名单 Share：share_id=$share_id show_id=$show_id url=$url"
        return 0
    fi

    stoken="$(get_stoken "$url" "$pwd_id" || true)"

    if [ -z "$stoken" ]; then
        mark_share_failure "$share_id" || true
        return 1
    fi

    scan_file="$TMP_ROOT/share_${share_id}.tsv"
    : > "$scan_file"

    if ! scan_share_dir "$pwd_id" "$stoken" "0" "" "$scan_file" "$url"; then
        error "分享递归扫描失败，保留旧缓存：share_id=$share_id url=$url"
        mark_share_failure "$share_id" || true
        return 1
    fi

    local dedup_file="$TMP_ROOT/share_${share_id}.dedup.tsv"
    awk -F '\t' 'BEGIN{OFS="\t"} {k=$1 SUBSEP $5; if (!(k in size) || $2 > size[k]) {size[k]=$2; line[k]=$0}} END {for(k in line) print line[k]}' "$scan_file" > "$dedup_file"
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

    if [ "$episode_count" -eq 0 ]; then
        mark_share_empty "$share_id" "$show_id" "$url" "$pwd_id"
        log "SOURCE扫描成功但无有效剧集：share_id=$share_id rank=$seedhub_rank files=0"
        return 0
    fi

    mark_share_success "$share_id" || {
        error "shares 状态更新失败：share_id=$share_id"
        return 1
    }

    log "SOURCE成功：share_id=$share_id rank=$seedhub_rank old_cache=$before_count new_cache=$after_count episodes=$episode_count"
    return 0
}

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

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi

exit 0
