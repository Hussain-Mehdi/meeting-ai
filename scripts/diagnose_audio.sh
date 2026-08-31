#!/bin/zsh
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "${0:A:h}/.."

echo "========================================"
echo "Meeting AI Audio Diagnostic"
echo "========================================"
echo
echo "1. AVFoundation audio devices"
ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 | sed -n '/AVFoundation audio devices:/,$p' | sed -n '1,30p'

echo
echo "2. BlackHole discovery"
.venv/bin/python -m backend.audio.devices

echo
echo "3. Current CoreAudio devices"
system_profiler SPAudioDataType 2>/dev/null | sed -n '1,220p'

tone_file="$(mktemp -t meeting-ai-tone).wav"
cleanup() { rm -f "$tone_file"; }
trap cleanup EXIT INT TERM

echo
echo "4. Playing a test tone through the CURRENT macOS output while recording BlackHole"
ffmpeg -hide_banner -loglevel error -f lavfi -i "sine=frequency=880:duration=3" -ar 48000 -ac 2 -y "$tone_file"
.venv/bin/python -m backend.audio.recorder --test --seconds 5 & recorder_pid=$!
sleep 1
afplay "$tone_file"
wait "$recorder_pid"

echo
echo "5. Result"
levels="$(ffmpeg -hide_banner -i data/meetings/audio-test.wav -af volumedetect -f null - 2>&1 | grep -E 'mean_volume|max_volume')"
echo "$levels"
peak="$(echo "$levels" | sed -n 's/.*max_volume: \(-*[0-9.]*\) dB.*/\1/p')"
if [[ -n "$peak" ]] && awk "BEGIN { exit !($peak > -60) }"; then
  echo "PASS: BlackHole received the system test tone."
else
  echo "FAIL: BlackHole did not receive the tone."
  echo "Select Multi-Output Device in System Settings > Sound > Output, then rerun this script."
fi

