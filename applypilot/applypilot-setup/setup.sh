#!/usr/bin/env bash
# ApplyPilot setup installer for Jane Doe
# Copies the staged config files into ~/.applypilot/ where ApplyPilot expects them.
set -euo pipefail

DEST="$HOME/.applypilot"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing ApplyPilot config to: $DEST"
mkdir -p "$DEST"

# Don't clobber an existing .env (it may already hold your real key)
if [ -f "$DEST/.env" ]; then
  echo "  • .env already exists — leaving it untouched (edit it manually if needed)."
else
  cp "$SRC/.env" "$DEST/.env"
  echo "  • .env installed"
fi

cp "$SRC/profile.json" "$DEST/profile.json";   echo "  • profile.json installed"
cp "$SRC/searches.yaml" "$DEST/searches.yaml"; echo "  • searches.yaml installed"
cp "$SRC/resume.txt"   "$DEST/resume.txt";     echo "  • resume.txt installed"
cp "$SRC/resume.pdf"   "$DEST/resume.pdf";     echo "  • resume.pdf installed"

echo ""
if grep -q "PASTE_YOUR_OPENAI_API_KEY_HERE" "$DEST/.env" 2>/dev/null; then
  echo "⚠️  Add your OpenAI API key:  open $DEST/.env  and replace PASTE_YOUR_OPENAI_API_KEY_HERE"
fi
echo "Done. Next:  applypilot doctor"
