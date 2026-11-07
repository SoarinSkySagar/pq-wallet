#!/usr/bin/env bash
# Render the Leafguard logo and cover image with headless Chrome (+ ImageMagick for the smaller sizes).
set -euo pipefail
cd "$(dirname "$0")"
CHROME="${CHROME:-$(ls -d "$HOME"/.cache/puppeteer/chrome/linux-*/chrome-linux64/chrome 2>/dev/null | tail -1)}"
[ -x "$CHROME" ] || CHROME="$(command -v google-chrome || command -v chromium)"
shot() { "$CHROME" --headless=new --no-sandbox --hide-scrollbars --force-device-scale-factor=1 "$@" 2>/dev/null; }
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
printf '<html><body style="margin:0;background:transparent"><img src="file://%s/logo.svg" width="1024" height="1024" style="display:block"></body></html>' "$PWD" > "$tmp/logo.html"
shot --default-background-color=00000000 --window-size=1024,1024 --screenshot="$PWD/logo-1024.png" "file://$tmp/logo.html"
convert logo-1024.png -resize 512x512 logo-512.png
shot --window-size=1920,1080 --virtual-time-budget=2000 --screenshot="$PWD/cover-1920x1080.png" "file://$PWD/cover.html"
convert cover-1920x1080.png -resize 1200x675^ -gravity center -extent 1200x630 cover-1200x630.png
ls -la *.png
