/* Adapted from cult-ui's Prompt Composer (MIT, https://www.cult-ui.com) — see NOTICE.md.
 * Upstream renders a WebGL shader avatar via @paper-design/shaders-react and uses Base UI
 * primitives; both are dropped here, as is its animated loading line: progress belongs in the
 * conversation, not over the field you are typing in. What is kept is the composer's shape:
 * one field that grows with the text, a placeholder that cycles through example questions,
 * and send chrome that reflects what can be sent. The placeholder is the textarea's own,
 * rather than an overlay, so it can never drift out of alignment with the text. */
import {useEffect, useRef, useState} from 'react';
import {ArrowUp, Square} from 'lucide-react';
import {cn} from '../lib/utils';

const PLACEHOLDERS = [
  'What did we decide about predictions?',
  'What am I supposed to deliver this week?',
  'What did Jason ask for on the scoreboard?',
  'Summarise the last meeting in three lines',
];

export default function PromptComposer({onSend, busy, onStop, disabled, hint}: {
  onSend: (value: string) => void;
  busy?: boolean;
  onStop?: () => void;
  disabled?: boolean;
  hint?: string;
}) {
  const [value, setValue] = useState('');
  const [placeholder, setPlaceholder] = useState(0);
  const area = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (value || busy) return;
    const timer = setInterval(() => setPlaceholder(current => (current + 1) % PLACEHOLDERS.length), 4200);
    return () => clearInterval(timer);
  }, [value, busy]);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [value]);

  const send = () => {
    const text = value.trim();
    if (!text || busy || disabled) return;
    setValue('');
    onSend(text);
  };

  return (
    <div className="bui w-full">
      <div className={cn('relative flex flex-col overflow-hidden rounded-[18px] border border-line bg-card',
        disabled && 'opacity-60')}>
        <textarea
          ref={area} value={value} rows={1} disabled={disabled}
          placeholder={PLACEHOLDERS[placeholder]}
          aria-label="Ask about your meetings"
          onChange={event => setValue(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) {event.preventDefault(); send()}
          }}
          className="block max-h-45 w-full px-4 pt-3.5 pb-1 text-[14px] leading-relaxed text-ink placeholder:text-ink-4"
        />
        <div className="flex items-center justify-between gap-3 px-2.5 pt-1 pb-2.5">
          <span className="truncate pl-1.5 text-[11.5px] text-ink-4">
            {hint || 'Enter to send · Shift+Enter for a new line'}
          </span>
          {busy ? (
            <button type="button" onClick={onStop} title="Stop generating"
              className="flex size-8 shrink-0 items-center justify-center rounded-full border border-line text-ink-2 hover:bg-inset">
              <Square size={12} fill="currentColor"/>
            </button>
          ) : (
            <button type="button" onClick={send} disabled={!value.trim() || disabled} title="Send"
              className={cn('flex size-8 shrink-0 items-center justify-center rounded-full transition-colors',
                value.trim() && !disabled ? 'bg-accent text-on-accent hover:opacity-90' : 'bg-inset text-ink-4')}>
              <ArrowUp size={16}/>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
