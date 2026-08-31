#!/bin/zsh
set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "${0:A:h}/.."

# macOS keeps /usr/bin ahead of Homebrew in some shells. Select a supported
# interpreter explicitly instead of accidentally using Apple's Python 3.9.
meeting_python=""
for candidate in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.12 /usr/local/bin/python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3,11))' >/dev/null 2>&1; then
    meeting_python="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$meeting_python" ]]; then
  echo "Python 3.11+ is required. Install it with: brew install python@3.12"
  exit 1
fi
echo "Using $meeting_python ($($meeting_python --version 2>&1))"
"$meeting_python" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
mkdir -p data/meetings logs
[[ -f .env ]] || cp .env.example .env
command -v npm >/dev/null || { echo "Node.js is required. Install it with: brew install node"; exit 1; }
(cd frontend && npm install)
scripts/build_native_audio.sh
scripts/check_environment.sh || true
echo "Setup complete. Run ./scripts/start.sh"
