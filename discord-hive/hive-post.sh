#!/usr/bin/env bash
# hive-post.sh -- post a message to the hive channel.
#
#   hive-post.sh "a plain message"
#   echo "long body" | hive-post.sh
#   hive-post.sh --username "SA" -- "decision: hold, conf 0.4"
#   hive-post.sh --via webhook "force the webhook identity"
#   hive-post.sh --dry-run "no network, show payload"
#   hive-post.sh --check     "where does each identity actually land"
#
# Exit codes: 0 posted, 1 config/usage error, 2 network/HTTP error.
#
# Two identities, deliberately:
#   bot      POST /channels/<id>/messages. Needs the bot in the guild, but it
#            lands exactly where hive-watch.py is listening.
#   webhook  POST the Captain Hook URL. Works with no guild membership, but a
#            webhook is pinned to the channel it was created in. Run --check
#            after moving the hive between servers: a webhook posting into a
#            channel nobody reads fails silently.
#   auto     bot first, webhook as fallback (default).
#
# Notes
#   - Discord caps a message at 2000 chars; longer input is split on newline
#     boundaries and each chunk is posted separately.
#   - @everyone / @here pings are defanged. This script feeds on LLM output,
#     so an untrusted string must never be able to ping the whole guild.
#   - --username only affects the webhook path; a bot posts under its own name
#     and Discord ignores the field.
#   - Secrets never reach argv (visible in ps) and never reach the log: the bot
#     path uses urllib with the token passed through the environment.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hive-lib.sh
source "$HERE/hive-lib.sh"
hive_load

HIVE_LOG="$HERE/logs/post.log"
LIMIT=2000
USERNAME="qwen local agent"
DRY=0
CHECK=0
VIA="${HIVE_SEND_VIA:-auto}"
CONTENT=""

usage() { sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --file)      shift; CONTENT=$( [ "$1" = "-" ] && cat || cat -- "$1" ); shift ;;
        --username)  USERNAME="${2-}"; shift 2 ;;
        --via)       VIA="${2-}"; shift 2 ;;
        --check)     CHECK=1; shift ;;
        --dry-run)   DRY=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        --)          shift; CONTENT="$*"; break ;;
        -*)          echo "hive-post: unknown flag: $1" >&2; exit 1 ;;
        *)           CONTENT="$*"; break ;;
    esac
done

# ---------------------------------------------------------------- --check
# Read-only. Answers the question that bites after a server change: does the
# thing I post with reach the thing I listen on?
if [ "$CHECK" = 1 ]; then
    if hive_have_bot; then
        res=$(HIVE_BOT_TOKEN="$HIVE_BOT_TOKEN" HIVE_C="$HIVE_CHANNEL_ID" python3 - <<'PYCHK'
import json, os, urllib.request, urllib.error
url = "https://discord.com/api/v10/channels/%s/messages?limit=1" % os.environ["HIVE_C"]
req = urllib.request.Request(url, headers={
    "Authorization": "Bot " + os.environ["HIVE_BOT_TOKEN"],
    "User-Agent": "hive-node/0.1"})
try:
    with urllib.request.urlopen(req, timeout=20) as f:
        n = len(json.load(f))
    print("readable (%d message(s))" % n)
except urllib.error.HTTPError as e:
    print("HTTP %s" % e.code)
except OSError as e:
    print("%s" % type(e).__name__)
PYCHK
)
        echo "bot     channel $HIVE_CHANNEL_ID -> $res"
    else
        echo "bot     no token in $HIVE_BOT_FILE"
    fi

    if hive_have_webhook; then
        res=$(HIVE_WEBHOOK="$HIVE_WEBHOOK" python3 - <<'PYWH'
import json, os, urllib.request, urllib.error
url = os.environ["HIVE_WEBHOOK"]   # GET /webhooks/<id>/<token> needs the token
try:
    with urllib.request.urlopen(urllib.request.Request(url, headers={
            "User-Agent": "hive-node/0.1"}), timeout=20) as f:
        d = json.load(f)
    print("bound to channel %s (guild %s, name %r)"
          % (d.get("channel_id"), d.get("guild_id"), d.get("name")))
except urllib.error.HTTPError as e:
    print("HTTP %s" % e.code)
except OSError as e:
    print("%s" % type(e).__name__)
PYWH
)
        echo "webhook $res"
        case "$res" in
            *"bound to channel $HIVE_CHANNEL_ID"*) echo "        matches the hive channel" ;;
            *) echo "        MISMATCH: posts land somewhere hive-watch.py cannot see" ;;
        esac
    else
        echo "webhook no url in $HIVE_WEBHOOK_FILE"
    fi
    exit 0
fi

# No argument and stdin is not a terminal -> read stdin.
if [ -z "$CONTENT" ] && [ ! -t 0 ]; then CONTENT=$(cat); fi

if [ -z "${CONTENT//[[:space:]]/}" ]; then
    echo "hive-post: nothing to post" >&2
    exit 1
fi

case "$VIA" in
    auto|bot|webhook) ;;
    *) echo "hive-post: --via must be auto|bot|webhook" >&2; exit 1 ;;
esac
if [ "$VIA" != bot ] && ! hive_have_webhook; then
    [ "$VIA" = webhook ] && { echo "hive-post: no webhook in $HIVE_WEBHOOK_FILE" >&2; exit 1; }
fi
if [ "$VIA" = bot ] && ! hive_have_bot; then
    echo "hive-post: --via bot but no token in $HIVE_BOT_FILE" >&2; exit 1
fi

# Split into <=LIMIT chunks on newline boundaries; defang pings.
mapfile -d '' -t CHUNKS < <(HIVE_IN="$CONTENT" HIVE_LIMIT="$LIMIT" python3 - <<'PYCHUNK'
import os, sys
limit = int(os.environ["HIVE_LIMIT"])
text = os.environ["HIVE_IN"].strip("\n")
for tok in ("@everyone", "@here"):
    text = text.replace(tok, tok[:1] + "\u200b" + tok[1:])
while text:
    if len(text) <= limit:
        sys.stdout.write(text + "\0"); break
    cut = text.rfind("\n", 0, limit)
    if cut == -1:
        cut = limit                       # no newline anywhere: hard split
    elif cut < limit // 2 and len(text[cut + 1:]) > limit:
        cut = limit                       # newline too early to be useful
    sys.stdout.write(text[:cut].rstrip() + "\0")
    text = text[cut:].lstrip("\n")
PYCHUNK
)

json_payload() {
    HIVE_USER="$1" HIVE_BODY="$2" python3 - <<'PYJSON'
import json, os
print(json.dumps({
    "username": os.environ["HIVE_USER"],
    "content": os.environ["HIVE_BODY"],
    "allowed_mentions": {"parse": []},
}))
PYJSON
}

# Bot identity: urllib so the token stays in the environment, never in argv.
post_bot() {
    HIVE_BOT_TOKEN="$HIVE_BOT_TOKEN" HIVE_C="$HIVE_CHANNEL_ID" HIVE_BODY="$1" python3 - <<'PYBOT'
import json, os, time, urllib.error, urllib.request
tok, ch, body = (os.environ["HIVE_BOT_TOKEN"], os.environ["HIVE_C"], os.environ["HIVE_BODY"])
url = "https://discord.com/api/v10/channels/%s/messages" % ch
data = json.dumps({"content": body, "allowed_mentions": {"parse": []}}).encode()
for attempt in range(4):
    req = urllib.request.Request(url, method="POST", data=data, headers={
        "Authorization": "Bot " + tok,
        "Content-Type": "application/json",
        "User-Agent": "hive-node/0.1 (+https://github.com/OWNER/hive)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as f:
            print(json.load(f).get("id", ""))
        raise SystemExit(0)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        if e.code == 429 and attempt < 3:
            try:
                wait = min(float(json.loads(raw).get("retry_after", 2)), 60.0)
            except Exception:
                wait = 3.0
            print("hive-post: 429, retry in %.1fs" % wait, file=__import__("sys").stderr)
            time.sleep(wait)
            continue
        print("hive-post: bot HTTP %s %s" % (e.code, raw[:120]), file=__import__("sys").stderr)
        raise SystemExit(2)
    except OSError as e:
        print("hive-post: bot %s: %s" % (type(e).__name__, e), file=__import__("sys").stderr)
        raise SystemExit(2)
raise SystemExit(2)
PYBOT
}

post_webhook() {
    local body out http attempt=0 wait_s
    body=$(json_payload "$USERNAME" "$1") || return 1
    while :; do
        out=$(curl -sS -m 30 -w $'\n%{http_code}' -X POST "${HIVE_WEBHOOK}?wait=true" \
            -H 'Content-Type: application/json' \
            -H "User-Agent: hive-node/0.1 (+https://github.com/OWNER/hive)" \
            --data-binary "$body" 2>&1) || {
                echo "hive-post: curl failed: ${out##*$'\n'}" >&2; return 2; }
        http="${out##*$'\n'}"
        case "$http" in
            200|204)
                printf '%s\n' "${out%$'\n'*}" | python3 -c '
import json,sys
try: print(json.load(sys.stdin).get("id",""))
except Exception: print("")
'
                return 0 ;;
            429)
                attempt=$((attempt+1))
                [ "$attempt" -gt 3 ] && { echo "hive-post: rate-limited, giving up" >&2; return 2; }
                wait_s=$(printf '%s' "${out%$'\n'*}" | python3 -c '
import json,sys
try: print(int(float(json.load(sys.stdin).get("retry_after",2)))+1)
except Exception: print(3)
')
                echo "hive-post: 429, retry in ${wait_s}s" >&2
                sleep "$wait_s" ;;
            *)
                echo "hive-post: HTTP $http ${out%$'\n'*}" >&2
                return 2 ;;
        esac
    done
}

# Returns "<message id>|<bot|webhook>" on stdout; tries bot first under auto.
post_chunk() {
    local id
    if [ "$DRY" = 1 ]; then json_payload "$USERNAME" "$1"; return 0; fi

    if [ "$VIA" != webhook ] && hive_have_bot; then
        if id=$(post_bot "$1"); then printf '%s|bot\n' "${id:-}"; return 0; fi
        [ "$VIA" = bot ] && return 2
        echo "hive-post: bot path failed, falling back to webhook" >&2
    fi

    if [ "$VIA" != bot ] && hive_have_webhook; then
        if id=$(post_webhook "$1"); then printf '%s|webhook\n' "${id:-}"; return 0; fi
    fi
    return 2
}

rc=0
for c in "${CHUNKS[@]}"; do
    [ -z "$c" ] && continue
    if [ "$DRY" = 1 ]; then post_chunk "$c"; continue; fi
    line=$(post_chunk "$c") || { rc=2; continue; }
    mid="${line%|*}"; how="${line##*|}"
    if [ -n "$mid" ]; then
        echo "$(date -Iseconds) posted $mid via $how len=${#c}" >> "$HIVE_LOG"
        echo "posted $mid via $how (len ${#c})"
    else
        echo "posted via $how (len ${#c}, no id in response)"
    fi
done
exit $rc
