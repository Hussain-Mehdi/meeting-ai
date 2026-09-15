#!/bin/zsh
set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "${0:A:h}/.."
[[ -x .venv/bin/uvicorn ]] || { echo "Run ./scripts/setup.sh first."; exit 1; }
[[ -d frontend/node_modules ]] || { echo "Frontend dependencies are missing. Run ./scripts/setup.sh first."; exit 1; }
mkdir -p logs data/meetings
# --lan exposes the web app to other devices on your network (the backend stays on 127.0.0.1;
# the frontend proxies /api to it). Anyone on the network can then view meetings and start or
# stop recordings, so use it only on a network you trust.
lan=0; [[ "${1:-}" == "--lan" ]] && lan=1
frontend_args=(); [[ $lan == 1 ]] && frontend_args=(-- --host 0.0.0.0)
cleanup(){ kill $backend_pid $frontend_pid 2>/dev/null || true; }
trap cleanup EXIT INT TERM
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 & backend_pid=$!
( sleep 1; kill -0 $backend_pid 2>/dev/null ) || {
  echo "Backend failed to start. Run: .venv/bin/python -m uvicorn backend.main:app --port 8000"
  exit 1
}
(cd frontend && npm run dev "${frontend_args[@]}") & frontend_pid=$!
echo "========================================"
echo "Meeting AI"
echo "========================================"
echo "Backend: http://localhost:8000"
echo "Frontend: http://localhost:3000"
if [[ $lan == 1 ]]; then
  lan_ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "<this-mac-ip>")
  echo "Network:  http://${lan_ip}:3000   (other devices on this network)"
  echo "Note: macOS may ask to allow 'node' to accept incoming connections - click Allow."
fi
echo "Meet Detector: RUNNING"
echo "Status: READY"
echo "========================================"
wait
