#!/bin/sh
# Installs the driveup command:
#   curl -fsSL https://raw.githubusercontent.com/nanj-dnd/driveup/main/install.sh | sh
set -e

app="$HOME/.driveup/app"
bin="$HOME/.local/bin"

# Prefer a current Python (e.g. Homebrew's) over macOS's built-in 3.9 when one is installed.
py=""
for v in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$v" >/dev/null 2>&1; then py="$v"; break; fi
done
if [ -z "$py" ]; then
  echo "driveup needs Python 3. On a Mac, run: xcode-select --install (or: brew install python)" >&2
  exit 1
fi

echo "Installing driveup..."
"$py" -m venv "$app"
"$app/bin/pip" install --quiet --upgrade pip
"$app/bin/pip" install --quiet --upgrade "git+https://github.com/nanj-dnd/driveup.git"
mkdir -p "$bin"
ln -sf "$app/bin/driveup" "$bin/driveup"

echo "Installed. Try: driveup add ~/Movies --to \"Backups/Movies\""
case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "First add it to your PATH: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
esac
