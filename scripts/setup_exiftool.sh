#!/bin/bash
set -e

EXIFTOOL_VERSION="13.58"
INSTALL_DIR="$HOME/bin"
TOOL_DIR="$INSTALL_DIR/Image-ExifTool-$EXIFTOOL_VERSION"

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

curl -L -o "exiftool.tar.gz" "https://sourceforge.net/projects/exiftool/files/Image-ExifTool-$EXIFTOOL_VERSION.tar.gz/download"
tar -xzf "exiftool.tar.gz"

ln -sf "$TOOL_DIR/exiftool" "$INSTALL_DIR/exiftool"

echo "ExifTool installed at: $INSTALL_DIR/exiftool"
echo "Add to shell config: export PATH=\"\$HOME/bin:\$PATH\""