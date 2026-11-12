#!/bin/sh
# Double-click to open the PingClaude settings window (macOS).
cd "$(dirname "$0")" || exit 1
exec python3 pingclaude_gui.py
