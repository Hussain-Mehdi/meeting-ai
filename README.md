# Meeting AI

Meeting AI is a local-first macOS meeting memory and action assistant. Its primary recorder uses Apple's native ScreenCaptureKit to capture system audio and the microphone without changing the Mac's output device or disabling volume keys. BlackHole remains an optional fallback. Recording begins only after explicit approval; faster-whisper, Ollama, SQLite, and the UI all run locally.

All audio, transcripts, summaries, and tasks remain on this Mac. There is no authentication, telemetry, cloud backend, or cloud AI.

## Architecture

`Chrome detection → explicit Start → native macOS system+microphone WAVs → faster-whisper → chunked Ollama analysis → Pydantic validation → SQLite → FastAPI → React`

The FastAPI event loop stays responsive: transcription and Ollama inference run in worker threads. Recordings are retained if either stage fails. The app has manual Start/Stop controls even when browser detection or automatic end detection is unavailable.

## Requirements

- macOS on Apple Silicon
- Python 3.11 or newer
- Node.js 18 or newer
- ffmpeg, Ollama, Chrome, and BlackHole 2ch
- A Multi-Output Device containing MacBook Pro Speakers and BlackHole 2ch
- Ollama model `llama3.1:8b` (configurable)

Check the machine without changing it:

```bash
cd meeting-ai
./scripts/check_environment.sh
```

Each failed check prints its fix. On a Homebrew machine, missing runtimes can usually be installed with `brew install python@3.12 node`. Do not reinstall BlackHole if Audio MIDI Setup already shows it; first grant microphone access to Terminal and retry the check.

## Installation

```bash
cd meeting-ai
chmod +x scripts/*.sh
./scripts/setup.sh
```

The setup creates `.venv`, installs Python and frontend packages, creates local data/log directories, copies `.env.example` to `.env`, and runs the environment check.

Configure `.env` if needed:

```dotenv
USER_NAME=Hussain
USER_ALIASES=Hussain,Husain
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen3:14b
WHISPER_MODEL=large-v3
WHISPER_LANGUAGE=
WHISPER_TASK=translate
DATABASE_PATH=data/meetings.db
RECORDINGS_PATH=data/meetings
```

Prepare Ollama in another terminal:

```bash
ollama serve
ollama pull llama3.1:8b
```

The first Whisper transcription downloads the configured `large-v3` model. This is the only model download; transcription remains local afterward. The default `translate` task converts Urdu/Hindi/English meetings into a consistent English transcript for more reliable meeting intelligence while retaining timestamps and source labels. Set `WHISPER_TASK=transcribe` to preserve the spoken languages instead.

## Native audio and BlackHole fallback

The setup script builds `bin/meeting-audio-capture`, a small local ScreenCaptureKit helper. On first use, macOS asks for **Screen & System Audio Recording** and **Microphone** permission. Grant both to Terminal (or the host app launching Meeting AI), fully restart it, and keep **MacBook Pro Speakers** selected normally. Volume keys continue working.

If the native helper is unavailable, Meeting AI falls back to BlackHole. Only that fallback requires a Multi-Output Device containing MacBook Pro Speakers and BlackHole 2ch. Meeting AI dynamically looks up BlackHole's AVFoundation index; it never relies on a fixed number.

Grant the terminal/app these macOS permissions when prompted:

- Privacy & Security → Microphone (BlackHole capture)
- Privacy & Security → Automation → Google Chrome (tab detection)
- Accessibility or Screen & System Audio Recording only if macOS asks for them

The app does not bypass denied permissions.

## Run

```bash
./scripts/start.sh
```

Open [http://localhost:3000](http://localhost:3000). The API is at [http://localhost:8000](http://localhost:8000), and health is `/api/health`.

When a Meet tab is detected, a native notification directs you to Meeting AI. Detection means a Meet page is open—not necessarily that someone has joined—because Chrome does not expose a stable, permission-free active-call flag. Recording never starts until you press **Start recording**. Closing/navigating away from the only Meet tab stops an active recording; **Stop recording** is always available as a reliable fallback.

## Audio test

Select the Multi-Output Device, play audio, then run:

```bash
.venv/bin/python -m backend.audio.devices
.venv/bin/python -m backend.audio.recorder --test --seconds 5
```

The second command writes `data/meetings/audio-test.wav` and prints its duration, size, and measured signal level. It mixes BlackHole system audio with the MacBook microphone, so speak during the test while system audio plays. If BlackHole is missing, confirm it appears in Audio MIDI Setup, select the Multi-Output Device, and grant Terminal microphone permission. Do not proceed to a real meeting until `has_audible_audio` is true and the WAV contains both expected sources.

For the cleanest mixed recording, use headphones during meetings. With laptop speakers, the physical microphone can hear the same remote audio already captured by BlackHole; the delayed duplicate may sound echoey or phasey even though transcription remains usable. The recorder attenuates and filters the microphone path, but headphones eliminate this acoustic duplication at the source.

## Tests

```bash
.venv/bin/pytest -q
cd frontend && npm run build
```

Tests cover configuration-sensitive startup, state transitions, persistence, deadline normalization, chunking/schema validation, and API health. Real hardware/model acceptance remains a manual test because it requires macOS permissions, live audio, a running local Ollama model, and a Meet session.

## Storage and logs

Each meeting is stored under `data/meetings/<meeting-id>/`. `recording-system.wav` and `recording-microphone.wav` preserve the two native audio sources separately, while `recording.wav` is a convenient listening preview. Whisper transcribes the clean source tracks independently and merges their timestamped segments into `transcript.txt` and `transcript.json`; `analysis.json` stores the current validated intelligence and `analysis-attempts/` retains validated analysis attempts. A processing exception writes `recovery.json` with the last safe checkpoint. Relational meeting intelligence and task completion live in `data/meetings.db`. Restarting does not clear any of these artifacts. Operational logs rotate in `logs/app.log` and intentionally omit full transcripts and audio.

## Troubleshooting

- **Python 3.11+ required:** install `python@3.12`, ensure Homebrew is ahead of `/usr/bin`, and rerun setup.
- **Node not found:** `brew install node`, open a new terminal, and rerun setup.
- **Ollama is not running:** run `ollama serve`.
- **Configured model is not installed:** run `ollama pull llama3.1:8b` or change `OLLAMA_MODEL`.
- **BlackHole was not detected:** grant microphone permission, confirm BlackHole 2ch in Audio MIDI Setup, then run the device CLI.
- **Chrome detection does not work:** allow Terminal/Meeting AI to automate Chrome. Manual recording remains fully usable.
- **Processing failed:** open the meeting and choose **Analyze saved transcript again** to skip Whisper, or **Re-transcribe recording** to rebuild the transcript from the original WAV tracks. `POST /api/meetings/{id}/retry` provides the same recovery path programmatically.
- **Whisper is slow:** use `WHISPER_MODEL=base` for faster, less accurate local transcription.

## Known MVP limitations

- Meet detection treats a `meet.google.com` tab as the best local signal and may notify before an actual call starts.
- Automatic stop assumes the Meet tab is closed or navigated away; manual Stop is the dependable fallback.
- Attendees stay empty because Chrome does not reliably expose the participant list without brittle UI automation.
- Speaker diarization is intentionally omitted. Transcript segments use neutral `Speaker`; no identities are invented.
- Native notification action buttons are not used; approval happens in the app.
- First-run Whisper model acquisition needs internet access. Meeting processing itself is local.

Future improvements can add reliable on-device diarization, richer recording-level meters, and browser-extension-based active-call signals without changing the local-first model.
