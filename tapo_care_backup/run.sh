#!/usr/bin/env bash
set -e

# Load s6 container environment variables if present
for s6_dir in /var/run/s6/container_environment /run/s6/container_environment; do
    if [ -d "$s6_dir" ]; then
        for env_file in "$s6_dir"/*; do
            if [ -f "$env_file" ]; then
                var_name=$(basename "$env_file")
                if [ -z "${!var_name}" ]; then
                    export "$var_name"="$(cat "$env_file")"
                fi
            fi
        done
    fi
done

CONFIG_PATH="/data/options.json"

if [ ! -f "$CONFIG_PATH" ]; then
    echo "[ERROR] $CONFIG_PATH not found! Make sure the add-on configuration is set."
    exit 1
fi

EMAIL=$(jq -r '.email // empty' "$CONFIG_PATH")
PASSWORD=$(jq -r '.password // empty' "$CONFIG_PATH")
DAYS=$(jq -r '.days // 1' "$CONFIG_PATH")
INTERVAL=$(jq -r '.interval_minutes // 15' "$CONFIG_PATH")
TO_MP4=$(jq -r '.to_mp4 // true' "$CONFIG_PATH")
GENERATE_THUMB=$(jq -r '.generate_thumb // true' "$CONFIG_PATH")
DATE_SUBFOLDERS=$(jq -r '.date_subfolders // true' "$CONFIG_PATH")
DEFAULT_PATH=$(jq -r '.default_backup_path // "/media/tapo_care"' "$CONFIG_PATH")
CAMERAS_JSON=$(jq -c '.cameras // []' "$CONFIG_PATH")

# Detect Home Assistant timezone:
TIMEZONE=""

# 1. Direct read from Home Assistant Core storage (/config/.storage/core.config)
if [ -f "/config/.storage/core.config" ]; then
    HA_STORAGE_TZ=$(jq -r '.data.time_zone // empty' /config/.storage/core.config 2>/dev/null || true)
    if [ -n "$HA_STORAGE_TZ" ] && [ "$HA_STORAGE_TZ" != "null" ]; then
        echo "[INFO] Detected timezone from Home Assistant storage: $HA_STORAGE_TZ"
        TIMEZONE="$HA_STORAGE_TZ"
    fi
fi

# 2. Check /config/configuration.yaml if present
if [ -z "$TIMEZONE" ] && [ -f "/config/configuration.yaml" ]; then
    HA_YAML_TZ=$(grep -E '^[[:space:]]*time_zone:' /config/configuration.yaml 2>/dev/null | awk -F: '{gsub(/[" \r\n]/,"",$2); print $2}' || true)
    if [ -n "$HA_YAML_TZ" ]; then
        echo "[INFO] Detected timezone from configuration.yaml: $HA_YAML_TZ"
        TIMEZONE="$HA_YAML_TZ"
    fi
fi

# 3. Query Home Assistant Core / Supervisor API using SUPERVISOR_TOKEN
if [ -z "$TIMEZONE" ] && [ -n "$SUPERVISOR_TOKEN" ]; then
    echo "[INFO] Fetching configured timezone from Home Assistant Core API..."
    HA_TZ=$(curl -s -f -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/core/api/config 2>/dev/null | jq -r '.time_zone // empty' 2>/dev/null || true)
    if [ -n "$HA_TZ" ] && [ "$HA_TZ" != "null" ]; then
        TIMEZONE="$HA_TZ"
    fi
    if [ -z "$TIMEZONE" ]; then
        SUPERVISOR_TZ=$(curl -s -f -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/info 2>/dev/null | jq -r '.data.timezone // empty' 2>/dev/null || true)
        if [ -n "$SUPERVISOR_TZ" ] && [ "$SUPERVISOR_TZ" != "null" ]; then
            TIMEZONE="$SUPERVISOR_TZ"
        fi
    fi
fi

# 2. Check environment variable TZ if provided and not UTC
if [ -z "$TIMEZONE" ] && [ -n "$TZ" ] && [ "$TZ" != "UTC" ]; then
    TIMEZONE="${TZ}"
fi

# 3. Check /etc/timezone or /etc/localtime
if [ -z "$TIMEZONE" ] && [ -f /etc/timezone ]; then
    ETC_TZ=$(cat /etc/timezone | tr -d ' \r\n')
    if [ -n "$ETC_TZ" ] && [ "$ETC_TZ" != "UTC" ]; then
        TIMEZONE="$ETC_TZ"
    fi
fi
if [ -z "$TIMEZONE" ] && [ -L /etc/localtime ]; then
    REAL_TZ=$(readlink -f /etc/localtime 2>/dev/null || true)
    if [[ "$REAL_TZ" == *"zoneinfo/"* ]]; then
        TIMEZONE="${REAL_TZ#*zoneinfo/}"
    fi
fi
if [ -z "$TIMEZONE" ]; then
    TIMEZONE=$(jq -r '.timezone // empty' "$CONFIG_PATH" 2>/dev/null || true)
fi
if [ -z "$TIMEZONE" ]; then
    TIMEZONE="${TZ:-UTC}"
fi

if [ -z "$EMAIL" ] || [ -z "$PASSWORD" ]; then
    echo "[ERROR] Tapo email and password must be configured in Add-on Configuration!"
    exit 1
fi

echo "===================================================="
echo " Starting Tapo Care Backup Add-on"
echo " Account:              $EMAIL"
echo " Days to sync:         $DAYS"
echo " Sync interval:        ${INTERVAL} minutes"
echo " Timezone (from HA):   $TIMEZONE"
echo " Convert to MP4:       $TO_MP4"
echo " Generate thumbnails:  $GENERATE_THUMB"
echo " Date subfolders:      $DATE_SUBFOLDERS"
echo " Default backup path:  $DEFAULT_PATH"
echo " Custom camera paths:  $CAMERAS_JSON"
echo "===================================================="

export TAPO_USERNAME="$EMAIL"
export TAPO_PASSWORD="$PASSWORD"
export TAPO_CARE_BACKUP_CONFIG_DIR="/data"

# Set system timezone inside the container so logs and date match Home Assistant
export TZ="$TIMEZONE"
if [ -f "/usr/share/zoneinfo/$TIMEZONE" ]; then
    ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime 2>/dev/null || true
    echo "$TIMEZONE" > /etc/timezone 2>/dev/null || true
fi

mkdir -p "$DEFAULT_PATH"

MP4_FLAG=""
if [ "$TO_MP4" = "false" ]; then
    MP4_FLAG="--no-to-mp4"
fi

THUMB_FLAG=""
if [ "$GENERATE_THUMB" = "false" ]; then
    THUMB_FLAG="--no-thumb"
fi

DATE_FLAG=""
if [ "$DATE_SUBFOLDERS" = "false" ]; then
    DATE_FLAG="--no-date-subfolders"
fi

# Initial login / session validation
echo "[INFO] Verifying session with Tapo Cloud..."
tapo-care-backup --config /data/session.json login || {
    echo "[WARN] Initial login attempt failed, will retry during download cycle."
}

# Trap signals for graceful shutdown
stop_addon() {
    echo "[INFO] Stopping Tapo Care Backup Add-on..."
    exit 0
}
trap stop_addon SIGTERM SIGINT

while true; do
    echo "[INFO] [$(date '+%Y-%m-%d %H:%M:%S')] Starting backup sync..."
    
    if ! tapo-care-backup --config /data/session.json download \
        --days "$DAYS" \
        --timezone "$TIMEZONE" \
        --path "$DEFAULT_PATH" \
        --camera-paths "$CAMERAS_JSON" \
        $MP4_FLAG \
        $THUMB_FLAG \
        $DATE_FLAG; then
        
        echo "[WARN] Download error encountered. Attempting to refresh login session..."
        tapo-care-backup --config /data/session.json login || true
    fi

    echo "[INFO] Sync cycle completed. Next sync in ${INTERVAL} minute(s)."
    sleep $((INTERVAL * 60)) &
    wait $!
done
