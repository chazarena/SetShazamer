#!/usr/bin/env bash
# Build SetShazamer.app with PyInstaller and install it into /Applications.
#
#   ./build_mac.sh               build + install
#   ./build_mac.sh --no-install  build only (result in ./dist)
#
# Needs Python 3.11+ with Tk 8.6 (python.org installer, or `brew install
# python@3.12 python-tk@3.12`). FFmpeg is found at runtime, not bundled.
set -euo pipefail

APP_NAME="SetShazamer"
BUNDLE_ID="com.setshazamer.app"
VERSION="1.2.0"

cd "$(dirname "$0")"
ROOT="$PWD"
BUILD_DIR="$ROOT/.build"
VENV="$BUILD_DIR/venv"
DIST="$ROOT/dist"
APP="$DIST/$APP_NAME.app"

INSTALL=1
for arg in "$@"; do
  case "$arg" in
    --no-install) INSTALL=0 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "build_mac.sh only runs on macOS."
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

find_python() {
  local candidate path
  for candidate in python3.13 python3.12 python3.11 python3 \
      /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
    path="$(command -v "$candidate" 2>/dev/null)" || continue
    if "$path" - <<'PY' >/dev/null 2>&1; then
import sys, tkinter
assert sys.version_info >= (3, 11)
assert tkinter.TkVersion >= 8.6
tkinter.Tcl()
PY
      echo "$path"
      return 0
    fi
  done
  return 1
}

PYTHON="$(find_python)" || die "No Python 3.11+ with a working Tk 8.6 found.
  Install one with:  brew install python@3.12 python-tk@3.12
  (or the python.org macOS installer), then re-run this script."
say "Using $("$PYTHON" -V) at $PYTHON"

say "Creating a clean virtual environment"
rm -rf "$VENV"
mkdir -p "$BUILD_DIR"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"

say "Running PyInstaller"
rm -rf "$DIST" "$BUILD_DIR/pyinstaller"
ICON_ARGS=()
[[ -f "$ROOT/assets/$APP_NAME.icns" ]] && ICON_ARGS=(--icon "$ROOT/assets/$APP_NAME.icns")
"$VENV/bin/pyinstaller" \
  --noconfirm --clean --log-level WARN \
  --windowed --onedir \
  --name "$APP_NAME" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --collect-all customtkinter \
  --collect-all shazamio \
  --collect-all shazamio_core \
  --collect-submodules aiohttp \
  --collect-all yt_dlp_ejs \
  --hidden-import darkdetect \
  --paths "$ROOT" \
  --distpath "$DIST" \
  --workpath "$BUILD_DIR/pyinstaller" \
  --specpath "$BUILD_DIR" \
  ${ICON_ARGS[@]+"${ICON_ARGS[@]}"} \
  "$ROOT/app.py"

[[ -d "$APP" ]] || die "PyInstaller finished but $APP is missing."

say "Writing Info.plist metadata"
PLIST="$APP/Contents/Info.plist"
pb() { /usr/libexec/PlistBuddy -c "$1" "$PLIST" 2>/dev/null || true; }
pb "Delete :CFBundleShortVersionString"
pb "Add :CFBundleShortVersionString string $VERSION"
pb "Delete :CFBundleVersion"
pb "Add :CFBundleVersion string $VERSION"
pb "Delete :NSHighResolutionCapable"
pb "Add :NSHighResolutionCapable bool true"
pb "Delete :LSMinimumSystemVersion"
pb "Add :LSMinimumSystemVersion string 11.0"
# Lets Finder offer SetShazamer under "Open With" for mixes.
pb "Delete :CFBundleDocumentTypes"
pb "Add :CFBundleDocumentTypes array"
pb "Add :CFBundleDocumentTypes:0 dict"
pb "Add :CFBundleDocumentTypes:0:CFBundleTypeName string Audio or Video Mix"
pb "Add :CFBundleDocumentTypes:0:CFBundleTypeRole string Viewer"
pb "Add :CFBundleDocumentTypes:0:LSHandlerRank string Alternate"
pb "Add :CFBundleDocumentTypes:0:LSItemContentTypes array"
i=0
for uti in public.mp3 public.mpeg-4 com.microsoft.waveform-audio com.apple.m4a-audio \
    org.xiph.flac org.matroska.mkv public.audio public.movie; do
  pb "Add :CFBundleDocumentTypes:0:LSItemContentTypes:$i string $uti"
  i=$((i + 1))
done

# Editing Info.plist breaks PyInstaller's signature; Apple Silicon refuses to
# launch an app whose signature doesn't verify, so re-sign ad hoc.
say "Signing (ad hoc)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

if [[ "$INSTALL" -eq 0 ]]; then
  say "Built $APP"
  exit 0
fi

TARGET_DIR="/Applications"
if [[ ! -w "$TARGET_DIR" ]]; then
  TARGET_DIR="$HOME/Applications"
  mkdir -p "$TARGET_DIR"
  say "/Applications isn't writable; installing to $TARGET_DIR instead"
fi
TARGET="$TARGET_DIR/$APP_NAME.app"

if pgrep -f "$TARGET/Contents/MacOS/$APP_NAME" >/dev/null 2>&1; then
  die "$APP_NAME is running. Quit it and run this script again."
fi
say "Installing to $TARGET"
rm -rf "$TARGET"
ditto "$APP" "$TARGET"
xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true

if ! command -v ffmpeg >/dev/null 2>&1; then
  printf '\n\033[1;33mNote:\033[0m FFmpeg is not installed. SetShazamer needs it:  brew install ffmpeg\n'
fi
say "Done. Launch $APP_NAME from Launchpad, Spotlight or $TARGET_DIR."
