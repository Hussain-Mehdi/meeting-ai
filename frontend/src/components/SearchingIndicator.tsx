/* The "working" state of an answer, in the shape people now expect from an AI tool: the
 * stages it is going through, and the sites it is actually reading as circular logos.
 *
 * Favicons are loaded straight from each result's own domain — never through a third-party
 * favicon proxy, which would hand one company the list of everything you search. A domain
 * that blocks or fails falls back to its initial, so this works offline too. */
import {useState} from 'react';
import {Globe, Check} from 'lucide-react';
import {cn} from '../lib/utils';

export type Stage = 'searching' | 'reading' | 'writing';

/* A site's own favicon, with the domain's initial as a deliberate fallback — plenty of sites
 * serve no /favicon.ico, and a letter in a tinted circle reads as a logo rather than a gap. */
const TINTS = ['bg-green-tint text-green', 'bg-warn-bg text-warn', 'bg-red-tint text-red', 'bg-inset text-ink-2'];

function SiteAvatar({domain, index}: {domain: string; index: number}) {
  const [failed, setFailed] = useState(false);
  const tint = TINTS[[...domain].reduce((sum, ch) => sum + ch.charCodeAt(0), 0) % TINTS.length];
  return (
    <span
      title={domain}
      className={cn('relative -ml-1.5 flex size-6 shrink-0 items-center justify-center overflow-hidden rounded-full border border-line text-[9px] font-semibold first:ml-0',
        failed ? tint : 'bg-card text-ink-3')}
      style={{animation: `pop-in 320ms cubic-bezier(0.23,1,0.32,1) ${index * 70}ms both`, zIndex: 10 - index}}>
      {failed
        ? (domain.replace(/^www\./, '')[0] || '?').toUpperCase()
        : <img src={`https://${domain}/favicon.ico`} alt="" width={14} height={14}
            className="size-3.5 object-contain"
            onError={() => setFailed(true)}
            onLoad={event => {if (!(event.target as HTMLImageElement).naturalWidth) setFailed(true)}}/>}
    </span>
  );
}

export default function SearchingIndicator({stage, query, domains = []}: {
  stage: Stage;
  query?: string | null;
  domains?: string[];
}) {
  const steps: [Stage, string][] = [
    ['searching', query ? `Searching the web for “${query}”` : 'Searching the web'],
    ['reading', domains.length ? `Reading ${domains.length} source${domains.length === 1 ? '' : 's'}` : 'Reading your meetings'],
    ['writing', 'Writing the answer'],
  ];
  const current = steps.findIndex(([name]) => name === stage);

  return (
    <div className="bui flex flex-col gap-1.5 py-0.5" role="status" aria-live="polite">
      {steps.map(([name, label], index) => {
        const done = index < current;
        const active = index === current;
        if (index > current) return null;
        return (
          <div key={name} className={cn('flex items-center gap-2.5 text-[13px] transition-colors',
            done ? 'text-ink-4' : 'text-ink-2')}
            style={{animation: `fade-up 300ms cubic-bezier(0.23,1,0.32,1) ${index * 60}ms both`}}>
            <span className={cn('flex size-5 shrink-0 items-center justify-center rounded-full',
              done ? 'bg-green-tint text-green' : 'text-ink-3')}>
              {done ? <Check size={11}/> : <Globe size={12} className="animate-pulse"/>}
            </span>
            {active && !done ? (
              <span className="bg-clip-text text-transparent"
                style={{backgroundImage: 'linear-gradient(90deg, var(--ink-3) 35%, var(--ink) 50%, var(--ink-3) 65%)',
                        backgroundSize: '200% 100%', animation: 'shimmer-text 1.4s linear infinite'}}>
                {label}
              </span>
            ) : <span className="truncate">{label}</span>}
            {name === 'reading' && !!domains.length && (
              <span className="flex items-center pl-0.5">
                {domains.slice(0, 6).map((domain, i) => <SiteAvatar key={domain + i} domain={domain} index={i}/>)}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
