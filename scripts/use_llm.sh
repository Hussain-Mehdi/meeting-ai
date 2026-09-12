#!/bin/zsh
# Download an Ollama model and make Meeting AI use it.
#   ./scripts/use_llm.sh qwen3.8:27b        # switch to Qwen3.8 27B
#   ./scripts/use_llm.sh qwen3:14b          # switch back
set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "${0:A:h}/.."

model="${1:-}"
[[ -n "$model" ]] || { echo "Usage: ./scripts/use_llm.sh <ollama-model-tag>   e.g. qwen3.8:27b"; exit 1; }
command -v ollama >/dev/null || { echo "Ollama is not installed. Install it from https://ollama.com first."; exit 1; }
curl -s -m 5 http://localhost:11434/api/tags >/dev/null || { echo "Ollama is not running. Start the Ollama app, then re-run this script."; exit 1; }

echo "==> Downloading $model (this can be large; qwen3.8:27b is about 18 GB)"
ollama pull "$model"

echo "==> Pointing Meeting AI at $model"
[[ -f .env ]] || cp .env.example .env
if grep -q '^OLLAMA_MODEL=' .env; then
  sed -i '' "s|^OLLAMA_MODEL=.*|OLLAMA_MODEL=$model|" .env
else
  echo "OLLAMA_MODEL=$model" >> .env
fi
grep -q '^OLLAMA_NUM_CTX=' .env || echo "OLLAMA_NUM_CTX=32768" >> .env

echo "==> Warming the model up and checking where it runs"
ollama run "$model" "Reply with the single word OK." >/dev/null 2>&1 || true
ollama ps

ram_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
size_gb=$(ollama list | awk -v m="$model" '$1==m {print $3}')
echo
echo "Model: $model (${size_gb:-?} on disk) · Mac RAM: ${ram_gb} GB"
echo "In the table above, PROCESSOR must read '100% GPU'. A 'CPU/GPU' split means the model"
echo "does not fit in GPU memory and analysis will be very slow; switch back with:"
echo "    ./scripts/use_llm.sh qwen3:14b"
echo
echo "Now restart Meeting AI (Ctrl-C in its terminal, then ./scripts/start.sh)."
echo "To compare: open a processed meeting and press 'Analyze saved transcript again'."
