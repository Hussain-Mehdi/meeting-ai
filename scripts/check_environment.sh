#!/bin/zsh
set +e
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
echo "========================================"
echo "Meeting AI Environment Check"
echo "========================================"
missing=0
check(){ if eval "$2" >/dev/null 2>&1; then echo "✓ $1"; else echo "✗ $1 — $3"; missing=1; fi }
meeting_python=""
for candidate in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.12 /usr/local/bin/python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3,11))' >/dev/null 2>&1; then
    meeting_python="$(command -v "$candidate")"
    break
  fi
done
check "macOS" '[[ "$(uname -s)" == Darwin ]]' "This MVP requires macOS."
check "Apple Silicon" '[[ "$(uname -m)" == arm64 ]]' "Run on an Apple Silicon Mac."
check "Python 3.11+ (${meeting_python:-not found})" '[[ -n "$meeting_python" ]]' "Install with: brew install python@3.12"
check "ffmpeg" 'command -v ffmpeg' "Install with: brew install ffmpeg"
check "Chrome" '[[ -d "/Applications/Google Chrome.app" ]]' "Install Google Chrome in /Applications."
check "Ollama" 'command -v ollama' "Install from https://ollama.com/download"
check "Ollama running" 'curl -fsS --max-time 2 "${OLLAMA_HOST:-http://localhost:11434}/api/tags"' "Run: ollama serve"
model="${OLLAMA_MODEL:-llama3.1:8b}"
check "Ollama model ($model)" 'curl -fsS --max-time 2 "${OLLAMA_HOST:-http://localhost:11434}/api/tags" | grep -q "${model%%:*}"' "Run: ollama pull $model"
check "Native ScreenCaptureKit helper" '[[ -x bin/meeting-audio-capture ]]' "Run: ./scripts/build_native_audio.sh"
if ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 | grep -qi blackhole; then
  echo "✓ BlackHole 2ch fallback"
else
  echo "○ BlackHole fallback unavailable (native capture will be used)"
fi
check "Node.js 18+" 'node -p "Number(process.versions.node.split('\''.'\'')[0]) >= 18" | grep -q true' "Install with: brew install node"
echo
if (( missing )); then echo "Environment needs attention. Apply the fixes above."; exit 1; else echo "Environment ready."; fi
