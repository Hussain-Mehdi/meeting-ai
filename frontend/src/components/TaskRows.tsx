/* Adapted from Beautiful UI (MIT, https://www.beautifului.dev) — see NOTICE.md.
 * Upstream drives badges and pills from a scripted demo timeline; here every row is a
 * real task from the database, the badge toggles the task, and the expanded detail shows
 * the verified evidence quote with a button that plays it from the recording. */
import {useState} from 'react';

export type Task = {
  id: string; task: string; owner?: string; status: 'open' | 'completed';
  priority?: 'high' | 'medium' | 'low'; deadline_original?: string; deadline_normalized?: string;
  evidence?: string; meeting_title?: string; confidence?: string;
  evidence_location?: {segment_id?: number; start?: number; end?: number; speaker?: string} | null;
};

const CheckIcon = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
);
const PlayIcon = (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden><path d="M8 5v14l11-7z"/></svg>
);

const clock = (seconds?: number) => {
  const total = Math.max(0, Math.floor(seconds || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
};

function StatusBadge({completed, onToggle, label}: {completed: boolean; onToggle: () => void; label: string}) {
  return (
    <span
      role="checkbox" aria-checked={completed} tabIndex={0} aria-label={label}
      onClick={event => {event.stopPropagation(); onToggle()}}
      onKeyDown={event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault(); event.stopPropagation(); onToggle()}}}
      className={`flex size-5.5 shrink-0 cursor-pointer items-center justify-center rounded-full border transition-colors ${
        completed ? 'border-green bg-green text-white' : 'border-line-soft bg-card text-transparent hover:border-ink-3'}`}
      style={completed ? {animation: 'pop-in 300ms cubic-bezier(0.23,1,0.32,1) both'} : undefined}>
      {CheckIcon}
    </span>
  );
}

export default function TaskRows({tasks, onToggle, play, showMeeting = false, emptyText = 'Nothing here yet.'}: {
  tasks: Task[];
  onToggle: (task: Task) => void;
  play?: (start: number, end?: number) => void;
  showMeeting?: boolean;
  emptyText?: string;
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (!tasks.length) return <p className="empty">{emptyText}</p>;
  return (
    <div className="bui flex w-full flex-col gap-2">
      {tasks.map((task, index) => {
        const expanded = !!open[task.id];
        const details: [string, string][] = [
          task.owner ? ['Owner', task.owner] : null,
          showMeeting && task.meeting_title ? ['Meeting', task.meeting_title] : null,
          task.deadline_original ? ['Due', task.deadline_normalized ? `${task.deadline_original} · ${task.deadline_normalized}` : task.deadline_original] : null,
          task.confidence ? ['Confidence', task.confidence] : null,
        ].filter(Boolean) as [string, string][];
        const location = task.evidence_location;
        return (
          <div key={task.id}
            className="self-stretch overflow-hidden border border-line-soft bg-card transition-[border-radius,background-color] duration-300 hover:bg-inset"
            style={{borderRadius: expanded ? 14 : 22, animation: `fade-up 450ms cubic-bezier(0.23,1,0.32,1) ${Math.min(index, 8) * 60}ms both`}}>
            <button type="button" aria-expanded={expanded} onClick={() => setOpen(current => ({...current, [task.id]: !expanded}))}
              className="flex min-h-11 w-full items-center gap-2.5 px-2.5 py-1.5 text-left">
              <span className="flex size-6 shrink-0 items-center justify-center">
                <StatusBadge completed={task.status === 'completed'} onToggle={() => onToggle(task)}
                  label={`Mark "${task.task}" as ${task.status === 'completed' ? 'open' : 'completed'}`}/>
              </span>
              <span className={`min-w-0 flex-1 text-[13px] font-medium ${task.status === 'completed' ? 'text-ink-3 line-through' : 'text-ink'}`}>
                {task.task}
              </span>
              {task.deadline_original &&
                <span className="hidden shrink-0 text-[12.5px] text-ink-2 tabular-nums sm:inline">{task.deadline_original}</span>}
              {task.priority &&
                <span className={`inline-flex h-5.5 shrink-0 items-center rounded-full px-2 text-[11.5px] font-medium capitalize ${
                  task.priority === 'high' ? 'bg-red-tint text-red' : task.status === 'completed' ? 'bg-green-tint text-green' : 'bg-inset text-ink-3'}`}>
                  {task.status === 'completed' ? 'Completed' : task.priority}
                </span>}
              <span aria-hidden className="-ml-1 flex size-7 shrink-0 items-center justify-center rounded-full text-ink-3">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
                  className="transition-transform duration-300" style={{transform: expanded ? 'rotate(180deg)' : 'rotate(0)'}}><path d="M6 9l6 6 6-6"/></svg>
              </span>
            </button>
            <div className="grid transition-[grid-template-rows,opacity] duration-300"
              style={{gridTemplateRows: expanded ? '1fr' : '0fr', opacity: expanded ? 1 : 0, transitionTimingFunction: 'cubic-bezier(0.23, 1, 0.32, 1)'}}>
              <div className="overflow-hidden">
                <div className="mb-2.5 grid grid-cols-[24px_1fr] gap-2.5 px-2.5">
                  <span aria-hidden className="mx-auto h-full w-px bg-line"/>
                  <div className="flex flex-col gap-1.5">
                    {details.map(([label, value], j) => (
                      <div key={label} className="flex items-center justify-between gap-3"
                        style={expanded ? {animation: `fade-up 300ms cubic-bezier(0.23,1,0.32,1) ${120 + j * 80}ms both`} : undefined}>
                        <span className="text-[12px] text-ink-2">{label}</span>
                        <span className="text-right font-mono text-[11.5px] text-ink-3">{value}</span>
                      </div>
                    ))}
                    {task.evidence && (
                      <div className="flex flex-wrap items-center gap-2 pt-0.5"
                        style={expanded ? {animation: `fade-up 300ms cubic-bezier(0.23,1,0.32,1) ${120 + details.length * 80}ms both`} : undefined}>
                        <span className="min-w-0 flex-1 text-[12px] leading-relaxed text-ink-2">Evidence: “{task.evidence}”</span>
                        {play && location && location.start != null && (
                          <button type="button" onClick={() => play(location.start!, location.end)}
                            className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-line-soft px-2 py-0.5 text-[11px] text-green hover:bg-green-tint">
                            {PlayIcon}{clock(location.start)}
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
