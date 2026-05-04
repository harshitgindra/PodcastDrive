# macOS Scheduling with launchd

A guide for running scripts automatically on macOS using `launchd` Launch Agents.

---

## Table of Contents

1. [Path Independence](#1-path-independence)
2. [Create the Launch Agent (.plist)](#2-create-the-launch-agent-plist)
3. [Manage the Service](#3-manage-the-service)
4. [Troubleshooting](#4-troubleshooting)

---

## 1. Path Independence

`launchd` runs in a **minimal environment** — it does not inherit your shell's `PATH`. Any tool installed via Homebrew (`python3`, `ffmpeg`, `yt-dlp`, etc.) will not be found unless you explicitly provide its full path.

**Option A — Export PATH at the top of your script:**

```bash
#!/bin/bash
set -euo pipefail

# Ensures the script can find Homebrew and system binaries
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
```

**Option B — Resolve each tool to its full path explicitly (more robust):**

```bash
# Find python3 across common install locations
PYTHON3=""
for _p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$_p" ] && PYTHON3="$_p" && break
done
[ -z "$PYTHON3" ] && echo "ERROR: python3 not found" && exit 1

# Find ffmpeg across common install locations
FFMPEG=""
for _p in /opt/homebrew/bin/ffmpeg /usr/local/bin/ffmpeg /usr/bin/ffmpeg; do
    [ -x "$_p" ] && FFMPEG="$_p" && break
done
[ -z "$FFMPEG" ] && echo "ERROR: ffmpeg not found" && exit 1
```

> **Tip:** Option B is preferred because it fails fast with a clear error message if a tool is missing, rather than silently using the wrong binary.

---

## 2. Create the Launch Agent (.plist)

macOS uses **Property List (`.plist`)** files to define background services. Place your file in `~/Library/LaunchAgents/` using a reverse-DNS naming convention (e.g., `com.yourname.myscript.plist`).

**Template:**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>

    <!-- Unique identifier for this agent -->
    <key>Label</key>
    <string>com.yourname.myscript</string>

    <!-- Command to run. Use caffeinate -i to prevent sleep during execution. -->
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/caffeinate</string>
        <string>-i</string>
        <string>/absolute/path/to/your/script.sh</string>
    </array>

    <!-- Working directory for the script -->
    <key>WorkingDirectory</key>
    <string>/absolute/path/to/your/project_folder</string>

    <!-- Schedule: runs daily at 06:30 -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>06</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>

    <!-- Redirect stdout and stderr to log files -->
    <key>StandardOutPath</key>
    <string>/absolute/path/to/your/project_folder/logs/launchd.stdout</string>
    <key>StandardErrorPath</key>
    <string>/absolute/path/to/your/project_folder/logs/launchd.stderr</string>

</dict>
</plist>
```

> **Note:** The `logs/` directory must exist before launchd runs the script. Create it manually or have your script create it with `mkdir -p`.

---

## 3. Manage the Service

Use these Terminal commands to control your scheduled task:

| Action | Command |
|--------|---------|
| **Load** (register & enable) | `launchctl load ~/Library/LaunchAgents/com.yourname.myscript.plist` |
| **Unload** (unregister) | `launchctl unload ~/Library/LaunchAgents/com.yourname.myscript.plist` |
| **Force run now** | `launchctl start com.yourname.myscript` |
| **Check status / exit code** | `launchctl list \| grep com.yourname.myscript` |

> **After editing the `.plist`**, always unload then reload for changes to take effect:
> ```bash
> launchctl unload ~/Library/LaunchAgents/com.yourname.myscript.plist
> launchctl load  ~/Library/LaunchAgents/com.yourname.myscript.plist
> ```

---

## 4. Troubleshooting

### Exit Code 1 or 127 — "Command Not Found"

If `launchctl list` shows exit code `1` or `127`, the script couldn't find a binary.

- **Fix:** Use absolute paths for all tools (see [Section 1](#1-path-independence)).
- **Check logs:** `cat /path/to/logs/launchd.stderr`

---

### Full Disk Access

macOS protects user folders (Desktop, Documents, Downloads, etc.). A script may get "Permission Denied" even if it has `chmod +x`.

- **Symptoms:** `Permission denied` errors in logs despite correct file permissions.
- **Fix:** Go to **System Settings → Privacy & Security → Full Disk Access** and add `/bin/bash` (or your terminal app).

---

### Network Failures When Mac is Asleep

If your script requires internet access but the Mac goes to sleep before it finishes:

- **Fix:** Enable **"Wake for network access"** in **System Settings → Energy Saver**.
- **Fix:** Wrap your script with `caffeinate -i` in the `.plist` (already included in the template above) to prevent sleep during execution.

---

### Relative vs. Absolute Paths

`launchd` has no concept of a current working directory unless you set `WorkingDirectory` in the `.plist`.

| | Example |
|---|---|
| ❌ Wrong | `python3 main.py` |
| ✅ Right | `/opt/homebrew/bin/python3 /Users/yourname/project/main.py` |
