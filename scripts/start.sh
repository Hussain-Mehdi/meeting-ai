#!/bin/zsh
set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "${0:A:h}/.."
[[ -x .venv/bin/uvicorn ]] || { echo "Run ./scripts/setup.sh first."; exit 1; }
[[ -d frontend/node_modules ]] || { echo "Frontend dependencies are missing. Run ./scripts/setup.sh first."; exit 1; }
mkdir -p logs data/meetings
cleanup(){ kill $backend_pid $frontend_pid 2>/dev/null || true; }
trap cleanup EXIT INT TERM
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 & backend_pid=$!
( sleep 1; kill -0 $backend_pid 2>/dev/null ) || {
  echo "Backend failed to start. Run: .venv/bin/python -m uvicorn backend.main:app --port 8000"
  exit 1
}
(cd frontend && npm run dev) & frontend_pid=$!
echo "========================================"
echo "Meeting AI"
echo "========================================"
echo "Backend: http://localhost:8000"
echo "Frontend: http://localhost:3000"
echo "Meet Detector: RUNNING"
echo "Status: READY"
echo "========================================"
wait
