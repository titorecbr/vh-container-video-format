#!/bin/bash
# Open .vh files directly in VLC using the native VH demuxer plugin
# Usage: ./vlc-vh.sh <file.vh>

VLC_PLUGIN_PATH="$HOME/.local/lib/vlc/plugins" exec vlc "$@"
