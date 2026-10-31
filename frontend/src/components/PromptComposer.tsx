/* Adapted from cult-ui's Prompt Composer (MIT, https://www.cult-ui.com) — see NOTICE.md.
 * Upstream renders a WebGL shader avatar via @paper-design/shaders-react and uses Base UI
 * primitives; both are dropped here, as is its animated loading line: progress belongs in the
 * conversation, not over the field you are typing in. What is kept is the composer's shape:
 * one field that grows with the text, a placeholder that cycles through example questions,
 * and send chrome that reflects what can be sent. The placeholder is the textarea's own,
 * rather than an overlay, so it can never drift out of alignment with the text. */
import {useEffect, useRef, useState} from 'react';
import {ArrowUp, Square, Globe} from 'lucide-react';
import {cn} from '../lib/utils';

const PLACEHOLDERS = [
  'What did we decide about predictions?',
  'What am I supposed to deliver this week?',
  'What did Jason ask for on the scoreboard?',
  'Summarise the last meeting in three lines',
];

export default function PromptComposer({onSend, busy, onStop, disabled, hint, web, setWeb, webAvailable}: {
  onSend: (value: string) => void;
  busy?: boolean;
  onStop?: () => void;
  disabled?: boolean;
  hint?: string;
  web?: boolean;
  setWeb?: (value: boolean) => void;
  webAvailable?: boolean;
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

  const ready = !!value.trim() && !disabled;
  return (
    <div className="bui w-full">
      {/* One row that grows with the text. The chrome sits inside the field rather than in a
          second row below it, so the composer stays about the height of a single line. */}
      <div className={cn(
        'group relative flex items-end gap-1.5 rounded-[20px] border border-line bg-card p-1.5',
        'shadow-[0_1px_2px_var(--shadow-soft),0_8px_24px_-18px_var(--shadow)]',
        'transition-[border-color,box-shadow] duration-200',
        'focus-within:border-border-focus focus-within:shadow-[0_0_0_3px_var(--focus-ring),0_8px_24px_-18px_var(--shadow)]',
        disabled && 'opacity-60')}>
        <textarea
          ref={area} value={value} rows={1} disabled={disabled}
          placeholder={PLACEHOLDERS[placeholder]}
          aria-label="Ask about your meetings"
          onChange={event => setValue(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) {event.preventDefault(); send()}
          }}
          className="block max-h-40 min-w-0 flex-1 px-2.5 py-2 text-[14.5px] leading-relaxed text-ink placeholder:text-ink-4"
        />

        {setWeb && (
          /* The only control in the app that lets anything leave this machine, so it carries a
             label rather than hiding behind an icon. */
          <button type="button" role="switch" aria-checked={!!web} disabled={!webAvailable}
            onClick={() => setWeb(!web)}
            title={webAvailable
              ? (web ? 'Web search is on — only the words you type are sent' : 'Also search the web')
              : 'Web search is unavailable'}
            className={cn(
              'flex h-9 shrink-0 items-center gap-1.5 rounded-full border px-3 text-[12.5px] font-medium transition-all duration-200',
              web ? 'border-green/40 bg-green-tint text-green'
                  : 'border-line-soft text-ink-3 hover:border-line hover:bg-inset hover:text-ink-2',
              !webAvailable && 'cursor-not-allowed opacity-40')}>
            <Globe size={14} className={cn('transition-transform duration-300', web && 'rotate-[18deg]')}/>
            <span className="hidden sm:inline">Web</span>
          </button>
        )}

        {busy ? (
          <button type="button" onClick={onStop} title="Stop generating"
            className="flex size-9 shrink-0 items-center justify-center rounded-full border border-line text-ink-2 transition-colors hover:bg-inset">
            <Square size={12} fill="currentColor"/>
          </button>
        ) : (
          <button type="button" onClick={send} disabled={!ready} title="Send"
            className={cn('flex size-9 shrink-0 items-center justify-center rounded-full transition-all duration-200',
              ready ? 'bg-accent text-on-accent hover:scale-105 active:scale-95'
                    : 'bg-inset text-ink-4')}>
            <ArrowUp size={17}/>
          </button>
        )}
      </div>

      {!value && (
        <p className="mt-2 px-3 text-[11px] text-ink-4">
          {web ? 'Web search is on: your message is sent to a search engine. Meeting content never is.'
               : (hint || 'Enter to send · Shift+Enter for a new line')}
        </p>
      )}
    </div>
  );
}
