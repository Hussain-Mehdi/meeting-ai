/* Adapted from cult-ui's Speech Bubble (MIT, https://www.cult-ui.com) — see NOTICE.md.
 * Upstream is a single oversized marketing bubble with a fixed tail. Here it is a chat
 * bubble with a side, sized for running text, and its colours come from the app's tokens. */
import {cn} from '../lib/utils';

export function SpeechBubble({side = 'left', className, children}: {
  side?: 'left' | 'right';
  className?: string;
  children: React.ReactNode;
}) {
  const mine = side === 'right';
  return (
    <div data-slot="speech-bubble" className={cn('relative flex', mine ? 'justify-end pl-10' : 'justify-start pr-10')}>
      <div
        data-slot="speech-bubble-body"
        className={cn(
          'relative max-w-[min(42rem,100%)] rounded-[18px] px-4 py-2.5 text-[14px] leading-relaxed',
          mine ? 'bg-accent text-on-accent' : 'border border-line bg-card text-ink',
          className,
        )}>
        {children}
        <svg
          aria-hidden="true" focusable="false" viewBox="0 0 53 45" height="14" width="16"
          className={cn('absolute bottom-0 h-3.5', mine ? '-right-[7px]' : '-left-[7px] -scale-x-100')}>
          <path
            className={mine ? 'fill-accent' : 'fill-card'}
            d="M44 0H0V0C0 24.8528 20.1472 45 45 45H53V45C47.1396 36.535 44 26.4842 44 16.1886V0Z"/>
        </svg>
      </div>
    </div>
  );
}

export default SpeechBubble;
