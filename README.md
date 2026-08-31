# Meeting AI

Meeting AI is a local-first macOS meeting memory and action assistant. Its primary recorder uses Apple's native ScreenCaptureKit to capture system audio and the microphone without changing the Mac's output device or disabling volume keys. BlackHole remains an optional fallback. Recording begins only after explicit approval; faster-whisper, Ollama, SQLite, and the UI all run locally.
> A private, local-first macOS meeting recorder that captures system and microphone audio, creates timestamped transcripts with Whisper, and turns meetings into structured reports and actionable tasks with a local Ollama model.

[Complete technical documentation](PROJECT_DOCUMENTATION.md) · [Repository](https://github.com/Hussain-Mehdi/meeting-ai)

## Why Meeting AI?

Meeting AI is designed for people who want useful meeting memory without sending sensitive conversations to a cloud transcription or AI provider.

It records system audio and your microphone into separate local files, transcribes them on your Mac, extracts evidence-backed meeting intelligence, and provides a clean interface for reviewing meetings and tracking tasks.

All processing remains local:

- no cloud AI or transcription;
- no telemetry or analytics;
- no remote application backend;
- no recording without explicit user approval;
- no deletion of source recordings when processing fails.

## Key features

### Native macOS audio capture

- Captures system and microphone audio with Apple's ScreenCaptureKit.
- Keeps normal MacBook output selected, so volume keys continue working.
- Saves system and microphone tracks separately to prevent clock-mixing distortion.
- Creates a mono listening preview after recording.
- Preserves original tracks if preview generation fails.
- Falls back to BlackHole 2ch and ffmpeg when the native helper is unavailable.
- Discovers BlackHole dynamically instead of relying on a fixed device index.
- Measures the saved signal and stops processing when the recording is definite silence.

### Local transcription

- Uses `faster-whisper` locally on the Mac.
- Defaults to the high-accuracy `large-v3` model.
- Supports English, Urdu, Hindi, and mixed-language meetings.
- Can translate mixed speech into a consistent English transcript.
- Preserves timestamps and audio-source labels.
- Uses voice activity detection and hallucination filters.
- Removes repeated Whisper loops and nearby acoustic duplicates.
- Produces readable text and structured JSON transcripts.
- Calculates transcript quality before AI analysis.

### Evidence-first meeting intelligence

- Uses local Ollama; the default model is `qwen3:14b`.
- Splits long transcripts at natural line boundaries.
- Extracts evidence candidates before writing the final report.
- Generates a concise executive summary.
- Identifies goals and major topics.
- Keeps only confirmed decisions.
- Finds people explicitly mentioned in the conversation.
- Extracts tasks only when ownership or commitment is supported.
- Preserves exact task evidence from the transcript.
- Extracts and normalizes explicit deadlines.
- Produces practical next steps.
- Never infers attendees from spoken names or source labels.
- Uses deterministic generation settings and structured JSON schemas.
- Validates final output with Pydantic.
- Repairs common local-model shape errors before validation.
- Intentionally excludes an **Open Questions** section from the final report and UI.

### Safe failure recovery

- Keeps recordings when Whisper or Ollama fails.
- Keeps transcripts when report generation fails.
- Retries Ollama directly from a saved transcript without rerunning Whisper.
- Can rerun Whisper from original WAV tracks when the transcript is poor.
- Allows completed meetings to be analyzed again.
- Writes `recovery.json` with the last safe checkpoint.
- Archives validated reports in `analysis-attempts/`.
- Marks interrupted jobs as retryable after restart.
- Prevents a new recording while another meeting is processing.

### Meeting and task interface

- Dashboard with current status and percentage progress.
- Local meeting library and detailed meeting reports.
- My Tasks page with open and completed tasks.
- Task completion controls.
- Search across titles, summaries, transcripts, tasks, and people.
- Settings showing models, storage, audio status, and privacy.
- Native notifications for detection, recording, completion, and failure.
- Explicit **Analyze saved transcript again** and **Re-transcribe recording** actions.

### Google Meet assistance

- Detects open `meet.google.com` tabs in Google Chrome.
- Shows a native notification when a Meet tab is found.
- Requires manual approval for every recording.
- Stops an active recording when the detected tab disappears.
- Always provides a manual Stop button.
