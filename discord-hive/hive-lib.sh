#!/usr/bin/env bash
# hive-lib.sh -- shared config for the hive scripts.
#
# Source this, do not execute it. It exposes:
#   HIVE_WEBHOOK     full webhook URL (send path, works today)
#   HIVE_WEBHOOK_ID  webhook id, parsed out of the URL (used to filter own posts)
#   HIVE_BOT_TOKEN   bot token (receive path, needs the bot in the guild)
#   HIVE_APP_ID      application id
#   HIVE_CHANNEL_ID  target text channel
#
# Credentials live in ~/.config/discord/ (mode 600) in a `key "value"` format.
# Never echo these values; never let them reach a log file.

HIVE_CONF_DIR="${HIVE_CONF_DIR:-$HOME/.config/discord}"
HIVE_WEBHOOK_FILE="$HIVE_CONF_DIR/webhook"
HIVE_BOT_FILE="$HIVE_CONF_DIR/discord"

# Hive's own channel. Overridable via env or ~/.config/discord/hive.json.
HIVE_CHANNEL_ID="${HIVE_CHANNEL_ID:-2000000000000000003}"   # PLACEHOLDER - set real id in hive-config.sh
HIVE_GUILD_ID="${HIVE_GUILD_ID:-2000000000000000004}"

# Read `key "value"` (quotes optional, one per line) from a config file.
_hive_conf_get() {
    local file="$1" key="$2"
    [ -r "$file" ] || return 1
    sed -n "s/^${key}[[:space:]]*\"\{0,1\}\([^\"]*\)\"\{0,1\}[[:space:]]*\$/\1/p" "$file" \
        | head -1 | tr -d '[:space:]'
}

hive_load() {
    HIVE_WEBHOOK=$(_hive_conf_get "$HIVE_WEBHOOK_FILE" url || true)
    HIVE_BOT_TOKEN=$(_hive_conf_get "$HIVE_BOT_FILE" token || true)
    HIVE_APP_ID=$(_hive_conf_get "$HIVE_BOT_FILE" ID || true)

    # A webhook URL is /api/webhooks/<webhook_id>/<token>
    if [ -n "$HIVE_WEBHOOK" ]; then
        HIVE_WEBHOOK_ID=$(printf '%s' "$HIVE_WEBHOOK" \
            | sed -n 's#.*/webhooks/\([0-9]\{6,\}\)/.*#\1#p')
    else
        HIVE_WEBHOOK_ID=""
    fi

    # Cross-check: the first segment of a bot token is base64 of the app id.
    # If ID and token drift apart this catches it before an API call does.
    if [ -n "$HIVE_BOT_TOKEN" ]; then
        local embedded
        embedded=$(python3 - "$HIVE_BOT_TOKEN" <<'PY' 2>/dev/null
import base64, sys
p = sys.argv[1].split('.')[0]
p += '=' * (-len(p) % 4)
print(base64.b64decode(p).decode('ascii', 'replace'))
PY
)
        if [ -n "$embedded" ] && [ -n "$HIVE_APP_ID" ] && [ "$embedded" != "$HIVE_APP_ID" ]; then
            echo "hive: WARNING token belongs to app $embedded but config ID is $HIVE_APP_ID" >&2
        fi
    fi
}

hive_have_webhook() { [ -n "$HIVE_WEBHOOK" ]; }
hive_have_bot()     { [ -n "$HIVE_BOT_TOKEN" ]; }
