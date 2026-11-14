#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${HOME}/.local/share/applications"
DESKTOP_FILE="${APP_DIR}/project-atlas.desktop"
mkdir -p "$APP_DIR"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Project Atlas
Comment=Cyber-Physical Intelligence Platform
Exec=${PROJECT_DIR}/tools/atlas_start.sh
Icon=applications-science
Terminal=false
Categories=Science;Development;Network;
StartupNotify=true
EOF

chmod +x "$PROJECT_DIR/tools/atlas_start.sh" "$PROJECT_DIR/tools/atlas_stop.sh"
chmod 644 "$DESKTOP_FILE"
update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
printf 'Project Atlas launcher installed at %s\n' "$DESKTOP_FILE"
