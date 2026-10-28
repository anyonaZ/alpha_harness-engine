#!/bin/sh
# Build Alpha Harness Lean from this checkout and install it as a systemd user service.
#
#   sh contrib/arch/install-lean.sh
#
# Needs: uv, Node 22+ with pnpm (sudo pacman -S uv nodejs pnpm). Run from the repository root.
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"

for tool in uv pnpm; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "Missing $tool. On Arch: sudo pacman -S uv nodejs pnpm" >&2
    exit 1
  }
done

if systemctl --user is-active --quiet alpha-harness 2>/dev/null; then
  echo "Stopping the running service first…"
  systemctl --user stop alpha-harness
fi

echo "Building the UI…"
(cd frontend && pnpm install --frozen-lockfile && pnpm build)

echo "Installing the backend with uv…"
uv tool install --force --python 3.14 --from ./backend alpha-harness

echo "Installing the systemd user service…"
mkdir -p "$HOME/.config/systemd/user"
cp contrib/systemd/alpha-harness.service "$HOME/.config/systemd/user/alpha-harness.service"
systemctl --user daemon-reload
systemctl --user enable --now alpha-harness

echo
echo "Alpha Harness Lean is running. Open http://localhost:8000"
echo "Change the Resource profile under Settings > Simulations."
