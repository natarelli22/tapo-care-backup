# Tapo Care Backup Add-on

Local Home Assistant OS Add-on that periodically downloads event recordings from your Tapo Care cloud subscription, converts them automatically to browser-compatible `.mp4` format (with AAC audio and faststart streaming flags), and stores them in `/media/tapo_care`.

## Features
- **Cloud-based backup**: Downloads clips directly from TP-Link Tapo Care cloud servers without consuming camera CPU or requiring slow SD card pull.
- **Automatic MP4 Conversion**: Remuxes MPEG-TS video losslessly and converts 8000Hz audio to AAC so videos play instantly in all web browsers and Home Assistant apps.
- **Zero Duplicate Downloads**: Detects existing `.mp4` files and skips already downloaded clips.
- **Disk Cleanup**: Immediately removes raw `.ts` files after conversion to save space.
- **Automatic Timezone Sync**: Automatically discovers and follows Home Assistant's configured timezone, ensuring date boundaries and daily midnight rollovers match your local Home Assistant time.
- **Native HA Media Integration**: Seamlessly integrates with the rewritten `HomeAssistant-Tapo-Control` media source to show recordings in `Media -> Tapo: Recordings`.

## Installation
1. Copy the `tapo_care_backup_addon` folder into your Home Assistant `/addons/` directory.
2. In Home Assistant, go to **Settings > Add-ons > Add-on Store**.
3. Click the top-right three dots menu and select **Check for updates** (or **Reload**).
4. Locate **Tapo Care Backup** under **Local add-ons** and click **Install**.
5. Go to the **Configuration** tab, enter your Tapo credentials, and click **Save**.
6. Start the add-on and check the **Log** tab.
