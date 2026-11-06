#!/bin/sh
# Installs the driveup command:
#   curl -fsSL https://raw.githubusercontent.com/nanj-dnd/driveup/main/install.sh | sh
set -e

app="$HOME/.driveup/app"
bin="$HOME/.local/bin"

if ! command -v python3 >/dev/null 2>&1; then
  echo "driveup needs Python 3. On a Mac, run: xcode-select --install" >&2
  exit 1
fi

echo "Installing driveup..."
python3 -m venv "$app"
"$app/bin/pip" install --quiet --upgrade pip
"$app/bin/pip" install --quiet --upgrade "git+https://github.com/nanj-dnd/driveup.git"
mkdir -p "$bin"
ln -sf "$app/bin/driveup" "$bin/driveup"

echo "Installed. Try: driveup add ~/Movies --to \"Backups/Movies\""
case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "First add it to your PATH: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
esac
