# Third-party UI components

## cult-ui

`PromptComposer.tsx` and `SpeechBubble.tsx` are adapted from **cult-ui**
(https://www.cult-ui.com), MIT licensed. Changes made here:

- The Prompt Composer drops its WebGL shader avatar (`@paper-design/shaders-react`) and its
  Base UI primitives, and uses the textarea's own placeholder instead of an overlay, so the
  placeholder cannot drift out of alignment with the text. Its shape, cycling placeholder,
  loading line and send chrome are kept.
- The Speech Bubble becomes a chat bubble with a side instead of a single marketing bubble.
- Both resolve their colours through this app's light/dark tokens.

`motion`, `clsx` and `tailwind-merge` were added for these; `@base-ui/react` and
`@paper-design/shaders-react` deliberately were not.



`TaskRows.tsx`, `LoadingState.tsx`, and `RecordsTable.tsx` are adapted from
**Beautiful UI** (https://www.beautifului.dev), MIT licensed:

```
MIT License

Copyright (c) 2026 Shane Levine

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The upstream components are demo pieces: they animate through scripted states on a
marketing page. The versions here keep the layout, motion grammar, and Tailwind
classes but drive every state from real Meeting AI data, and their colours resolve
through this app's own light/dark tokens (see `src/tailwind.css`).
