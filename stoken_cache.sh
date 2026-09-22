#!/bin/bash

# Persistent Quark stoken cache shared by source_check.sh, addfile.sh and replace.sh.
# The cache stores pwd_id, update time and stoken. stoken values are never written to logs.

STOKEN_CACHE_FILE="${STOKEN_CACHE_FILE:-$BASE_DIR/stoken_cache.tsv}"
STOKEN_CACHE_LOCK="${STOKEN_CACHE_FILE}.lock"

stoken_cache_init() {
    local dir
    dir="$(dirname -- "$STOKEN_CACHE_FILE")"
    mkdir -p "$dir" || return 1
    if [ ! -f "$STOKEN_CACHE_FILE" ]; then
        : > "$STOKEN_CACHE_FILE" || return 1
    fi
    chmod 600 "$STOKEN_CACHE_FILE" 2>/dev/null || true
    return 0
}

stoken_cache_log() {
    if declare -F log >/dev/null 2>&1; then
        log "$*"
    fi
}

stoken_cache_acquire() {
    local owner_pid

    while ! mkdir "$STOKEN_CACHE_LOCK" 2>/dev/null; do
        owner_pid="$(cat "$STOKEN_CACHE_LOCK/pid" 2>/dev/null || true)"

        # A process can die between creating the lock directory and writing pid.
        # Such an incomplete lock is safe to remove after observing the missing owner.
        if [ -z "$owner_pid" ]; then
            rm -rf "$STOKEN_CACHE_LOCK" 2>/dev/null || true
            continue
        fi

        if [[ "$owner_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$owner_pid" 2>/dev/null; then
            rm -rf "$STOKEN_CACHE_LOCK" 2>/dev/null || true
            continue
        fi

        sleep 0.2
    done

    printf '%s\n' "$$" > "$STOKEN_CACHE_LOCK/pid"
}

stoken_cache_release() {
    rm -f "$STOKEN_CACHE_LOCK/pid" 2>/dev/null || true
    rmdir "$STOKEN_CACHE_LOCK" 2>/dev/null || true
}

stoken_cache_get() {
    local pwd_id="$1"

    awk -F '\t' -v id="$pwd_id" '
        $1 == id { token=$3 }
        END { if (token != "") print token }
    ' "$STOKEN_CACHE_FILE" 2>/dev/null
}

stoken_cache_store() {
    local pwd_id="$1"
    local token="$2"
    local tmp

    stoken_cache_acquire
    tmp="${STOKEN_CACHE_FILE}.tmp.$$"

    if awk -F '\t' -v id="$pwd_id" 'BEGIN{OFS="\t"} $1 != id {print}' \
        "$STOKEN_CACHE_FILE" > "$tmp"; then
        printf '%s\t%s\t%s\n' "$pwd_id" "$(date +%s)" "$token" >> "$tmp"
        chmod 600 "$tmp" 2>/dev/null || true
        if mv -f "$tmp" "$STOKEN_CACHE_FILE"; then
            chmod 600 "$STOKEN_CACHE_FILE" 2>/dev/null || true
            stoken_cache_release
            return 0
        fi
    fi

    rm -f "$tmp" 2>/dev/null || true
    stoken_cache_release
    return 1
}

stoken_cache_invalidate() {
    local pwd_id="$1"
    local tmp

    stoken_cache_acquire
    tmp="${STOKEN_CACHE_FILE}.tmp.$$"

    if awk -F '\t' -v id="$pwd_id" 'BEGIN{OFS="\t"} $1 != id {print}' \
        "$STOKEN_CACHE_FILE" > "$tmp"; then
        chmod 600 "$tmp" 2>/dev/null || true
        if mv -f "$tmp" "$STOKEN_CACHE_FILE"; then
            chmod 600 "$STOKEN_CACHE_FILE" 2>/dev/null || true
            stoken_cache_release
            return 0
        fi
    fi

    rm -f "$tmp" 2>/dev/null || true
    stoken_cache_release
    return 1
}

# Return success only when the API response clearly indicates an invalid token.
# Ordinary network failures or unrelated API errors deliberately return false.
stoken_response_is_invalid() {
    local result="$1"
    local code message combined

    if ! printf '%s' "$result" | jq -e . >/dev/null 2>&1; then
        return 1
    fi

    code="$(printf '%s' "$result" | jq -r '.code // -1' 2>/dev/null || echo -1)"
    [ "$code" != "0" ] || return 1

    message="$(printf '%s' "$result" | jq -r '[(.message // ""),(.data.message // ""),(.error.message // "")] | map(select(type == "string")) | join(" ")' 2>/dev/null || true)"
    combined="$(printf '%s %s' "$code" "$message" | tr '[:upper:]' '[:lower:]')"

    case "$code" in
        401|403)
            return 0
            ;;
    esac

    if printf '%s' "$combined" | grep -Eiq '(stoken|token|令牌|访问令牌).*(invalid|expired|expire|unauthori[sz]ed|forbidden|失效|过期|无效|错误|失败|拒绝)|((invalid|expired|expire|unauthori[sz]ed|forbidden|失效|过期|无效|错误|失败|拒绝).*(stoken|token|令牌|访问令牌))'; then
        return 0
    fi

    return 1
}

get_stoken() {
    local share="${1:-}"
    local pwd_id="${2:-}"
    local token result code message

    [ -n "$pwd_id" ] || {
        stoken_cache_log "stoken 获取跳过：缺少 pwd_id"
        return 1
    }

    if ! stoken_cache_init; then
        stoken_cache_log "stoken 缓存初始化失败：$STOKEN_CACHE_FILE"
        return 1
    fi

    token="$(stoken_cache_get "$pwd_id")"
    if [ -n "$token" ]; then
        stoken_cache_log "stoken 缓存命中：pwd_id=$pwd_id"
        printf '%s' "$token"
        return 0
    fi

    stoken_cache_acquire

    token="$(stoken_cache_get "$pwd_id")"
    if [ -n "$token" ]; then
        stoken_cache_release
        stoken_cache_log "stoken 缓存命中：pwd_id=$pwd_id"
        printf '%s' "$token"
        return 0
    fi

    if ! result="$(
        quark_curl \
            -X POST \
            'https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc&uc_param_str=' \
            --data "$(jq -nc --arg p "$pwd_id" '{pwd_id:$p,passcode:""}')"
    )"; then
        stoken_cache_release
        if [ -n "$share" ]; then
            stoken_cache_log "分享 stoken 获取请求失败：$share"
        else
            stoken_cache_log "分享 stoken 获取请求失败：pwd_id=$pwd_id"
        fi
        return 1
    fi

    code="$(printf '%s' "$result" | jq -r '.code // -1' 2>/dev/null || echo -1)"
    token="$(printf '%s' "$result" | jq -r '.data.stoken // empty' 2>/dev/null || true)"
    message="$(printf '%s' "$result" | jq -r '.message // "unknown"' 2>/dev/null || echo unknown)"

    if [ "$code" != "0" ] || [ -z "$token" ]; then
        stoken_cache_release
        if [ -n "$share" ]; then
            stoken_cache_log "分享 stoken 获取失败：$share：$message"
        else
            stoken_cache_log "分享 stoken 获取失败：pwd_id=$pwd_id：$message"
        fi
        return 1
    fi

    tmp="${STOKEN_CACHE_FILE}.tmp.$$"
    if awk -F '\t' -v id="$pwd_id" 'BEGIN{OFS="\t"} $1 != id {print}' \
        "$STOKEN_CACHE_FILE" > "$tmp"; then
        printf '%s\t%s\t%s\n' "$pwd_id" "$(date +%s)" "$token" >> "$tmp"
        chmod 600 "$tmp" 2>/dev/null || true
        if ! mv -f "$tmp" "$STOKEN_CACHE_FILE"; then
            rm -f "$tmp" 2>/dev/null || true
            stoken_cache_release
            stoken_cache_log "stoken 缓存写入失败：pwd_id=$pwd_id"
            printf '%s' "$token"
            return 0
        fi
        chmod 600 "$STOKEN_CACHE_FILE" 2>/dev/null || true
    else
        rm -f "$tmp" 2>/dev/null || true
        stoken_cache_release
        stoken_cache_log "stoken 缓存写入失败：pwd_id=$pwd_id"
        printf '%s' "$token"
        return 0
    fi

    stoken_cache_release
    stoken_cache_log "stoken 已持久缓存：pwd_id=$pwd_id"
    printf '%s' "$token"
}

# Execute one Quark request using the caller's existing quark_rate_limit,
# Cookie/TLS settings and API statistics hook. The function emits only response body.
stoken_cache_quark_http() {
    local api_url=""
    local arg result rc

    quark_rate_limit

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

stoken_args_set_token() {
    local new_token="$1"
    shift
    local -a args=("$@")
    local i value

    for i in "${!args[@]}"; do
        value="${args[$i]}"

        case "$value" in
            stoken=*)
                args[$i]="stoken=$new_token"
                ;;
            *'"stoken":'*)
                if printf '%s' "$value" | jq -e . >/dev/null 2>&1; then
                    args[$i]="$(printf '%s' "$value" | jq -c --arg s "$new_token" '.stoken=$s')"
                fi
                ;;
        esac
    done

    printf '%s\0' "${args[@]}"
}

quark_curl_with_stoken_refresh() {
    local api_url=""
    local arg
    local pwd_id=""
    local token=""
    local tokenized=0
    local result
    local rc
    local invalid=0
    local -a request_args=("$@")
    local -a refreshed_args

    for arg in "${request_args[@]}"; do
        case "$arg" in
            http://*|https://*)
                api_url="$arg"
                ;;
            pwd_id=*)
                pwd_id="${arg#pwd_id=}"
                ;;
            *)
                if [[ "$arg" == *'"pwd_id":'* ]] && printf '%s' "$arg" | jq -e . >/dev/null 2>&1; then
                    pwd_id="$(printf '%s' "$arg" | jq -r '.pwd_id // empty' 2>/dev/null || true)"
                fi
                ;;
        esac
    done

    case "$api_url" in
        https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail*|https://drive-pc.quark.cn/1/clouddrive/share/sharepage/save*)
            tokenized=1
            ;;
    esac

    if [ "$tokenized" -eq 1 ] && [ -n "$pwd_id" ]; then
        token="$(stoken_cache_get "$pwd_id" 2>/dev/null || true)"
        if [ -n "$token" ]; then
            mapfile -d '' -t refreshed_args < <(stoken_args_set_token "$token" "${request_args[@]}")
            request_args=("${refreshed_args[@]}")
        fi
    fi

    result="$(stoken_cache_quark_http "${request_args[@]}")"
    rc=$?

    if [ "$rc" -ne 0 ] || [ "$tokenized" -eq 0 ] || [ -z "$pwd_id" ]; then
        printf '%s' "$result"
        return "$rc"
    fi

    if ! stoken_response_is_invalid "$result"; then
        printf '%s' "$result"
        return "$rc"
    fi

    invalid=1
    stoken_cache_log "检测到 stoken 失效，刷新后重试：pwd_id=$pwd_id"
    stoken_cache_invalidate "$pwd_id" || true

    token="$(get_stoken "" "$pwd_id")" || {
        printf '%s' "$result"
        return "$rc"
    }

    mapfile -d '' -t refreshed_args < <(stoken_args_set_token "$token" "${request_args[@]}")
    request_args=("${refreshed_args[@]}")

    result="$(stoken_cache_quark_http "${request_args[@]}")"
    rc=$?

    if [ "$rc" -eq 0 ] && stoken_response_is_invalid "$result"; then
        stoken_cache_log "刷新后的 stoken 仍然无效：pwd_id=$pwd_id"
        stoken_cache_invalidate "$pwd_id" || true
    fi

    printf '%s' "$result"
    return "$rc"
}
