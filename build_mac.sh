#!/bin/bash
# ─────────────────────────────────────────────
#  AutoCut — Mac build script
#  Produces AutoCut.dmg (drag-to-Applications)
# ─────────────────────────────────────────────
set -e

echo "Installing dependencies..."
pip3.11 install -q -r requirements.txt pyinstaller

echo "Building AutoCut.app ..."
pyinstaller \
  --noconfirm \
  --onedir \
  --windowed \
  --collect-data tkinterdnd2 \
  --add-data "assets:assets" \
  --name "AutoCut" \
  main.py

echo "Creating DMG..."
# Make a temp folder for DMG contents
rm -rf /tmp/autocut_dmg
mkdir /tmp/autocut_dmg

# Copy app into it
ditto dist/AutoCut.app /tmp/autocut_dmg/AutoCut.app

# Create a symlink to Applications so user can drag-and-drop
ln -s /Applications /tmp/autocut_dmg/Applications

# Create the DMG
hdiutil create \
  -volname "AutoCut" \
  -srcfolder /tmp/autocut_dmg \
  -ov \
  -format UDZO \
  dist/AutoCut-Mac.dmg

rm -rf /tmp/autocut_dmg

echo ""
echo "✅ Done!  dist/AutoCut-Mac.dmg"
echo ""
echo "User just:"
echo "  1. Double-click AutoCut-Mac.dmg"
echo "  2. Drag AutoCut → Applications"
echo "  3. Open from Applications"
