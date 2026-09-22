/* Adapted from Beautiful UI (MIT, https://www.beautifului.dev) — see NOTICE.md.
 * Upstream is a demo table with a scripted agent cursor; this version lists real
 * meetings, filters them by status, and hands the row click back to the app. */
import {useMemo, useState} from 'react';

export type MeetingRecord = {
  id: string; title: string; started_at: string; duration_seconds?: number;
  status: string; platform?: string | null;
};

const STATUS_TONE: Record<string, string> = {
  completed: 'bg-green-tint text-green',
  transcribed: 'bg-amber-tint text-amber',
  failed: 'bg-red-tint text-red',
};

const FILTERS: [string, string][] = [['all', 'All'], ['completed', 'Completed'], ['transcribed', 'Needs review'], ['failed', 'Needs attention']];

const fmtDate = (iso: string) => {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : new Intl.DateTimeFormat(undefined, {day: 'numeric', month: 'short', year: 'numeric'}).format(date);
};
const fmtTime = (iso: string) => {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(undefined, {hour: 'numeric', minute: '2-digit'}).format(date);
};

export default function RecordsTable({meetings, open, remove}: {
  meetings: MeetingRecord[];
  open: (id: string) => void;
  remove?: (id: string) => Promise<void>;
}) {
  const [filter, setFilter] = useState('all');
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState('');
  const counts = useMemo(() => meetings.reduce<Record<string, number>>((acc, m) => ({...acc, [m.status]: (acc[m.status] || 0) + 1}), {}), [meetings]);
  const rows = filter === 'all' ? meetings : meetings.filter(m => m.status === filter);
  return (
    <div className="bui flex w-full flex-col gap-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {FILTERS.map(([value, label]) => {
          const count = value === 'all' ? meetings.length : counts[value] || 0;
          if (value !== 'all' && !count) return null;
          return (
            <button key={value} type="button" onClick={() => setFilter(value)} aria-pressed={filter === value}
              className={`inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-[12px] transition-colors ${
                filter === value ? 'border-transparent bg-accent text-on-accent' : 'border-line-soft text-ink-2 hover:bg-inset'}`}>
              {label}<span className="tabular-nums opacity-60">{count}</span>
            </button>
          );
        })}
      </div>
      <div className="overflow-hidden rounded-[14px] border border-line-soft bg-card">
        {rows.map((meeting, index) => (
          <div key={meeting.id} className="group relative flex items-center border-b border-line-soft last:border-0 hover:bg-inset"
            style={{animation: `fade-up 380ms cubic-bezier(0.23,1,0.32,1) ${Math.min(index, 10) * 40}ms both`}}>
          <button type="button" onClick={() => open(meeting.id)}
            className="flex min-w-0 flex-1 items-center gap-3 px-3 py-2.5 text-left">
            <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">{meeting.title}</span>
            <span className="hidden shrink-0 text-[12.5px] text-ink-2 tabular-nums sm:inline">{fmtDate(meeting.started_at)}</span>
            <span className="hidden w-12 shrink-0 text-right text-[12.5px] text-ink-3 tabular-nums sm:inline">{fmtTime(meeting.started_at)}</span>
            <span className="hidden w-10 shrink-0 text-right text-[12.5px] text-ink-3 tabular-nums sm:inline">{Math.round((meeting.duration_seconds || 0) / 60)}m</span>
            <span className={`inline-flex h-5.5 shrink-0 items-center rounded-full px-2 text-[11px] font-medium capitalize ${STATUS_TONE[meeting.status] || 'bg-inset text-ink-3'}`}>
              {meeting.status === 'transcribed' ? 'Review' : meeting.status}
            </span>
          </button>
          {remove && (confirming === meeting.id ? (
            <span className="flex shrink-0 items-center gap-1.5 pr-3 text-[11.5px] text-ink-2">
              Delete?
              <button type="button" className="rounded-full bg-red-solid px-2 py-0.5 text-[11px] font-medium text-white"
                onClick={async () => {try {setError(''); await remove(meeting.id)} catch (e: any) {setError(e.message || 'Delete failed')} finally {setConfirming(null)}}}>Yes</button>
              <button type="button" className="rounded-full border border-line-soft px-2 py-0.5 text-[11px]" onClick={() => setConfirming(null)}>No</button>
            </span>
          ) : (
            <button type="button" aria-label={`Delete ${meeting.title}`} title="Delete meeting"
              className="mr-2 flex size-7 shrink-0 items-center justify-center rounded-full text-ink-4 opacity-0 transition-opacity hover:bg-red-tint hover:text-red focus-visible:opacity-100 group-hover:opacity-100"
              onClick={() => setConfirming(meeting.id)}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>
            </button>
          ))}
          </div>
        ))}
        {!rows.length && <p className="px-3 py-6 text-center text-[13px] text-ink-3">No meetings match this filter.</p>}
      </div>
      {error && <p className="text-[12px] text-red" role="alert">{error}</p>}
    </div>
  );
}
