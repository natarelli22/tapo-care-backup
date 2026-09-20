#!/usr/bin/env bash
set -e

CONFIG_PATH="/data/options.json"

if [ ! -f "$CONFIG_PATH" ]; then
    echo "[ERROR] $CONFIG_PATH not found! Make sure the add-on configuration is set."
    exit 1
fi

EMAIL=$(jq -r '.email // empty' "$CONFIG_PATH")
PASSWORD=$(jq -r '.password // empty' "$CONFIG_PATH")
DAYS=$(jq -r '.days // 1' "$CONFIG_PATH")
INTERVAL=$(jq -r '.interval_minutes // 15' "$CONFIG_PATH")
TIMEZONE=$(jq -r '.timezone // "America/Sao_Paulo"' "$CONFIG_PATH")
TO_MP4=$(jq -r '.to_mp4 // true' "$CONFIG_PATH")
GENERATE_THUMB=$(jq -r '.generate_thumb // true' "$CONFIG_PATH")
DATE_SUBFOLDERS=$(jq -r '.date_subfolders // true' "$CONFIG_PATH")
DEFAULT_PATH=$(jq -r '.default_backup_path // "/media/tapo_care"' "$CONFIG_PATH")
CAMERAS_JSON=$(jq -c '.cameras // []' "$CONFIG_PATH")

if [ -z "$EMAIL" ] || [ -z "$PASSWORD" ]; then
    echo "[ERROR] Tapo email and password must be configured in Add-on Configuration!"
    exit 1
fi

echo "===================================================="
echo " Starting Tapo Care Backup Add-on"
echo " Account:              $EMAIL"
echo " Days to sync:         $DAYS"
echo " Sync interval:        ${INTERVAL} minutes"
echo " Timezone:             $TIMEZONE"
echo " Convert to MP4:       $TO_MP4"
echo " Generate thumbnails:  $GENERATE_THUMB"
echo " Date subfolders:      $DATE_SUBFOLDERS"
echo " Default backup path:  $DEFAULT_PATH"
echo " Custom camera paths:  $CAMERAS_JSON"
echo "===================================================="

export TAPO_USERNAME="$EMAIL"
export TAPO_PASSWORD="$PASSWORD"
export TAPO_CARE_BACKUP_CONFIG_DIR="/data"

# Set system timezone inside the container so logs and date match the user's timezone
if [ -n "$TIMEZONE" ]; then
    export TZ="$TIMEZONE"
    if [ -f "/usr/share/zoneinfo/$TIMEZONE" ]; then
        ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime 2>/dev/null || true
        echo "$TIMEZONE" > /etc/timezone 2>/dev/null || true
    fi
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
