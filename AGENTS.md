# Meeting AI contributor rules

- Keep all meeting data and AI processing local. Never add cloud AI, telemetry, or analytics.
- The single exception is web search, which is off by default and opted into per message. When it
  is on, the only thing that may leave the machine is the user's own typed words: the current
  message, plus their own earlier question in the same conversation when the current one depends
  on it. Meeting transcripts, summaries, tasks, names and retrieved passages must never appear in
  an outgoing request, nothing the model wrote may be added to a query, the exact string sent is
  always shown back to the user, and no model inference ever happens off-device.
- Never start recording without explicit user approval and never bypass macOS permissions.
- Preserve recordings after every failure.
- Never log secrets, raw audio, or complete transcripts.
- Never hallucinate meeting facts, task ownership, deadlines, attendees, or decisions.
- Mentioned people are not automatically attendees. Summaries must remain high-level.
- Prefer a tested end-to-end path over elaborate architecture. Do not over-engineer.
- Run relevant tests before declaring work complete.

