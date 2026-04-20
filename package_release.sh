#!/bin/bash
# ─────────────────────────────────────────────
#  AutoCut — Package final release zip
#  Run on Mac after both builds are ready
# ─────────────────────────────────────────────
set -e

if [ ! -d "release/Mac/AutoCut.app" ]; then
  echo "❌ Mac build missing. Run build_mac.sh first."
  exit 1
fi

if [ ! -d "release/Windows/AutoCut" ]; then
  echo "❌ Windows build missing. Run build_windows.bat on a Windows PC first,"
  echo "   then copy the release/Windows folder here."
  exit 1
fi

echo "Writing README..."
cat > release/README.txt << 'EOF'
AutoCut
=======

Choose the folder for your operating system:

  Mac/
    → Open the Mac folder
    → Right-click AutoCut.app → Open  (first time only)
    → Click Open in the security dialog

  Windows/
    → Open the Windows folder
    → Open the AutoCut folder
    → Double-click AutoCut.exe

EOF

echo "Creating AutoCut.zip..."
rm -f AutoCut.zip
ditto -c -k --sequesterRsrc --keepParent release AutoCut.zip

echo ""
echo "✅ Done!  AutoCut.zip is ready to share."
echo "   Users open it and pick Mac or Windows folder."
