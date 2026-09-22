/* Adapted from Beautiful UI (MIT, https://www.beautifului.dev) — see NOTICE.md.
 * Upstream counts its own elapsed time from mount; here the label and the elapsed
 * clock come from real processing state, so a reload shows the true elapsed time. */
import {useEffect, useState} from 'react';

/* A chevron wavefront driving right across a 3x3 pixel grid. */
const CHEVRON = Array.from({length: 9}, (_, i) => ((i % 3) + Math.abs(Math.floor(i / 3) - 1)) * 90);

export function LoaderGrid({active = true}: {active?: boolean}) {
  return (
    <span aria-hidden className="grid shrink-0 grid-cols-[repeat(3,4px)] gap-[1.5px]">
      {CHEVRON.map((delay, index) => (
        <span key={index} className="size-[4px] rounded-[1px] bg-ink"
          style={{opacity: 0.15, animation: active ? `pixel-on 650ms ease-in-out ${delay}ms infinite` : 'none'}}/>
      ))}
    </span>
  );
}

function useElapsed(since?: string | null) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!since) return;
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(timer);
  }, [since]);
  if (!since) return null;
  const started = Date.parse(since.endsWith('Z') || /[+-]\d\d:\d\d$/.test(since) ? since : since + 'Z');
  if (Number.isNaN(started)) return null;
  const total = Math.max(0, (now - started) / 1000);
  return total < 60 ? `${total.toFixed(0)}s` : `${Math.floor(total / 60)}m ${String(Math.floor(total % 60)).padStart(2, '0')}s`;
}

export default function LoadingState({label, since, active = true}: {label: string; since?: string | null; active?: boolean}) {
  const elapsed = useElapsed(active ? since : null);
  return (
    <div role="status" className="bui flex items-center gap-2.5">
      <LoaderGrid active={active}/>
      <span className="bg-clip-text text-[13px] font-medium text-transparent"
        style={{backgroundImage: 'linear-gradient(90deg, var(--ink-3) 35%, var(--ink) 50%, var(--ink-3) 65%)',
                backgroundSize: '200% 100%', animation: active ? 'shimmer-text 1.4s linear infinite' : 'none',
                ...(active ? {} : {color: 'var(--ink-2)', WebkitTextFillColor: 'var(--ink-2)'})}}>{label}</span>
      {elapsed && <span className="font-mono text-[12px] tabular-nums text-ink-3">{elapsed}</span>}
    </div>
  );
}
