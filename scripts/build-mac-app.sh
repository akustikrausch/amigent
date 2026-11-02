#!/bin/bash
# Build Amigent.app for macOS, sign it with a Developer ID, notarize and staple it.
# The result goes to src/amigent/macos/Amigent.zip; `amigent-viewer --install`
# takes the app from there, so it carries the signature to every Mac.
#
# Needs: Xcode command line tools, a "Developer ID Application" certificate in the
# keychain and a notarytool keychain profile (AMIGENT_NOTARY_PROFILE, default
# fxchain-notary; make one with `xcrun notarytool store-credentials`).
# --no-notarize signs only.
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE="${AMIGENT_NOTARY_PROFILE:-fxchain-notary}"
NOTARIZE=1
[ "${1:-}" = "--no-notarize" ] && NOTARIZE=0

IDENTITY="${AMIGENT_SIGN_IDENTITY:-$(security find-identity -v -p codesigning \
  | sed -n 's/.*"\(Developer ID Application:[^"]*\)".*/\1/p' | head -1)}"
[ -n "$IDENTITY" ] || { echo "No 'Developer ID Application' certificate in the keychain."; exit 1; }
echo "Signing as: $IDENTITY"

PY="${PYTHON:-python3}"
[ -x .venv/bin/python3 ] && PY=.venv/bin/python3
OUT=build/mac
rm -rf "$OUT"; mkdir -p "$OUT"
PYTHONPATH=src "$PY" -m amigent.desktop --build-mac-app "$OUT" --sign "$IDENTITY" --universal
APP="$OUT/Amigent.app"
codesign --verify --strict --verbose=2 "$APP"
lipo -archs "$APP/Contents/MacOS/Amigent"

if [ "$NOTARIZE" = 1 ]; then
  ditto -c -k --keepParent "$APP" "$OUT/notarize.zip"
  xcrun notarytool submit "$OUT/notarize.zip" --keychain-profile "$PROFILE" --wait
  xcrun stapler staple "$APP"
  xcrun stapler validate "$APP"
  spctl --assess --type execute --verbose=2 "$APP"
fi

ditto -c -k --keepParent "$APP" src/amigent/macos/Amigent.zip
ls -l src/amigent/macos/Amigent.zip
echo "Done. amigent-viewer --install now uses this app."
