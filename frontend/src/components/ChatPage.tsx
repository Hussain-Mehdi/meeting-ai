/* Ask questions about the meetings. Answers stream from the local model over SSE; the
 * citations in an answer are clickable and open the meeting they came from. */
import {useEffect, useMemo, useRef, useState} from 'react';
import {motion} from 'motion/react';
import {MessageSquarePlus, Trash2, FileText, Clock, Globe, ExternalLink} from 'lucide-react';
import {api} from '../api';
import PromptComposer from './PromptComposer';
import SpeechBubble from './SpeechBubble';
import SearchingIndicator, {type Stage} from './SearchingIndicator';
import {cn} from '../lib/utils';

type Source = {n: number; meeting_id: string; meeting_title: string; date: string; kind: string; heading?: string; start?: number; excerpt: string};
type WebSource = {n: number; title: string; url: string; snippet: string; domain: string};
type Message = {id?: string; role: 'user' | 'assistant'; content: string; sources?: Source[]; web?: WebSource[]};
type Conversation = {id: string; title: string; updated_at: string; messages?: number};

const STARTERS = [
  'What should I do with predictions?',
  'What did we decide in the last meeting?',
  'What is still open from last week?',
];

const clock = (seconds?: number) => {
  const total = Math.max(0, Math.floor(seconds || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
};

/** Render [1]-style citations as buttons that jump to the matching source card.
 *  A citation keeps any punctuation that follows it on the same line, so an answer never
 *  wraps with a lone full stop at the start of the next line. */
function AnswerText({text, sources, web = [], onCite}: {text: string; sources: Source[]; web?: WebSource[]; onCite: (n: number) => void}) {
  const parts = useMemo(() => text.split(/(\[W?\d{1,2}\][.,;:!?)]*)/gi), [text]);
  return (
    <div className="whitespace-pre-wrap break-words">
      {parts.map((part, index) => {
        const webMatch = /^\[W(\d{1,2})\]([.,;:!?)]*)$/i.exec(part);
        if (webMatch) {
          const result = web.find(w => w.n === Number(webMatch[1]));
          if (!result) return <span key={index}>{part}</span>;
          return (
            <span key={index} className="whitespace-nowrap">
              <a href={result.url} target="_blank" rel="noreferrer noopener" title={`${result.title} — ${result.domain}`}
                className="mx-0.5 inline-flex h-4.5 items-center gap-0.5 rounded-full bg-inset px-1.5 align-[1px] text-[10.5px] font-semibold text-ink-2 hover:text-ink">
                <Globe size={9}/>{result.n}
              </a>
              {webMatch[2]}
            </span>
          );
        }
        const match = /^\[(\d{1,2})\]([.,;:!?)]*)$/.exec(part);
        const source = match && sources.find(s => s.n === Number(match[1]));
        if (!source) return <span key={index}>{part}</span>;
        return (
          <span key={index} className="whitespace-nowrap">
            <button type="button" onClick={() => onCite(source.n)}
              title={`${source.meeting_title} · ${source.date}`}
              className="mx-0.5 inline-flex h-4.5 min-w-4.5 items-center justify-center rounded-full bg-green-tint px-1 align-[1px] text-[10.5px] font-semibold text-green hover:opacity-80">
              {source.n}
            </button>
            {match[2]}
          </span>
        );
      })}
    </div>
  );
}

function WebCard({result}: {result: WebSource}) {
  return (
    <a href={result.url} target="_blank" rel="noreferrer noopener"
      className="block rounded-[12px] border border-line-soft bg-card p-2.5 transition-colors hover:bg-inset">
      <div className="flex items-center gap-2 text-[11.5px] text-ink-3">
        <span className="inline-flex size-4.5 shrink-0 items-center justify-center rounded-full bg-inset text-[9px] font-semibold text-ink-2">
          <Globe size={9}/>
        </span>
        <span className="truncate font-medium text-ink-2">{result.title}</span>
        <span className="ml-auto inline-flex shrink-0 items-center gap-1">{result.domain}<ExternalLink size={10}/></span>
      </div>
      <p className="mt-1.5 line-clamp-2 break-words text-[12px] leading-relaxed text-ink-3">{result.snippet}</p>
    </a>
  );
}

function SourceCard({source, open, highlighted}: {source: Source; open: (id: string) => void; highlighted: boolean}) {
  return (
    <div id={`source-${source.n}`}
      className={cn('rounded-[12px] border bg-card p-2.5 transition-colors',
        highlighted ? 'border-green bg-green-tint' : 'border-line-soft')}>
      <div className="flex items-center gap-2 text-[11.5px] text-ink-3">
        <span className="inline-flex size-4.5 shrink-0 items-center justify-center rounded-full bg-green-tint text-[10px] font-semibold text-green">{source.n}</span>
        <button type="button" onClick={() => open(source.meeting_id)} className="truncate font-medium text-ink-2 hover:underline">
          {source.meeting_title || 'Meeting'}
        </button>
        <span className="shrink-0">· {source.date}</span>
        {source.kind === 'transcript'
          ? <span className="ml-auto inline-flex shrink-0 items-center gap-1"><Clock size={11}/>{clock(source.start)}</span>
          : <span className="ml-auto inline-flex shrink-0 items-center gap-1 capitalize"><FileText size={11}/>{(source.heading || source.kind).replace(/_/g, ' ')}</span>}
      </div>
      <p className="mt-1.5 line-clamp-3 break-words text-[12px] leading-relaxed text-ink-3">{source.excerpt}</p>
    </div>
  );
}

export default function ChatPage({open}: {open: (id: string) => void}) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [streaming, setStreaming] = useState('');
  const [liveSources, setLiveSources] = useState<Source[]>([]);
  const [liveWeb, setLiveWeb] = useState<WebSource[]>([]);
  const [web, setWeb] = useState(false);
  const [webAvailable, setWebAvailable] = useState(false);
  const [webQuery, setWebQuery] = useState<string | null>(null);
  const [stage, setStage] = useState<Stage>('reading');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [highlight, setHighlight] = useState<number | null>(null);
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const loadConversations = () => api<Conversation[]>('/chat/conversations').then(setConversations).catch(() => {});
  useEffect(() => {loadConversations()}, []);
  useEffect(() => {api<any>("/settings").then((s: any) => setWebAvailable(!!s?.retrieval?.web?.available)).catch(() => {})}, []);
  useEffect(() => {bottom.current?.scrollIntoView({behavior: 'smooth', block: 'end'})}, [messages.length, streaming]);

  const openConversation = async (id: string) => {
    abort.current?.abort();
    setBusy(false); setStreaming(''); setLiveSources([]); setError('');
    const value = await api<{messages: Message[]}>('/chat/conversations/' + id);
    setConversationId(id);
    setMessages(value.messages || []);
  };

  const newChat = () => {
    abort.current?.abort();
    setBusy(false); setConversationId(null); setMessages([]); setStreaming(''); setLiveSources([]); setError('');
  };

  const removeConversation = async (id: string) => {
    await api('/chat/conversations/' + id, {method: 'DELETE'});
    if (id === conversationId) newChat();
    loadConversations();
  };

  const citeJump = (n: number) => {
    setHighlight(n);
    document.getElementById(`source-${n}`)?.scrollIntoView({behavior: 'smooth', block: 'center'});
    setTimeout(() => setHighlight(null), 1800);
  };

  const send = async (text: string) => {
    setError('');
    setMessages(current => [...current, {role: 'user', content: text}]);
    setStreaming(''); setLiveSources([]); setLiveWeb([]); setWebQuery(null);
    setStage(web ? 'searching' : 'reading'); setBusy(true);
    const controller = new AbortController();
    abort.current = controller;
    let answer = '';
    let sources: Source[] = [];
    let webSources: WebSource[] = [];
    let newId = conversationId;
    try {
      const response = await fetch('/api/chat', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, signal: controller.signal,
        body: JSON.stringify({message: text, conversation_id: conversationId, web}),
      });
      if (!response.ok || !response.body) {
        let detail = response.statusText;
        try {detail = (await response.json()).detail || detail} catch {}
        throw new Error(detail);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const frames = buffer.split('\n\n');
        buffer = frames.pop() || '';
        for (const frame of frames) {
          const event = /^event: (.+)$/m.exec(frame)?.[1];
          const raw = /^data: (.+)$/m.exec(frame)?.[1];
          if (!event || !raw) continue;
          const data = JSON.parse(raw);
          if (event === 'start') newId = data.conversation_id;
          else if (event === 'searching') {setStage('searching'); setWebQuery(data.query || null)}
          else if (event === 'sources') {
            sources = data.sources || []; setLiveSources(sources);
            webSources = data.web || []; setLiveWeb(webSources);
            setWebQuery(data.web_query || null);
            setStage('reading');
          }
          else if (event === 'token') {answer += data.text; setStage('writing'); setStreaming(answer)}
          else if (event === 'error') throw new Error(data.message);
          else if (event === 'done') {sources = data.sources || []; webSources = data.web || []}
        }
      }
      setMessages(current => [...current, {role: 'assistant', content: answer, sources, web: webSources}]);
      setConversationId(newId);
      loadConversations();
    } catch (exception: any) {
      if (exception?.name === 'AbortError') {
        if (answer.trim()) setMessages(current => [...current, {role: 'assistant', content: answer, sources, web: webSources}]);
      } else {
        setError(exception?.message || 'The assistant could not answer.');
      }
    } finally {
      setBusy(false); setStreaming(''); setLiveSources([]); setLiveWeb([]); abort.current = null;
    }
  };

  const empty = !messages.length && !streaming;
  return (
    /* header 68 + section padding 34 + main bottom padding 80 */
    <section className="bui flex h-[calc(100vh-182px)] min-h-125 flex-col">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="eyebrow">ASK</p>
          <h1>Chat with your meetings</h1>
          <p className="lede">Grounded in what was actually said. Answers cite the meeting they came from.</p>
        </div>
        <button type="button" onClick={newChat}
          className="mt-1 inline-flex shrink-0 items-center gap-2 rounded-full border border-line px-3 py-1.5 text-[12.5px] text-ink-2 hover:bg-inset">
          <MessageSquarePlus size={14}/>New chat
        </button>
      </div>

      {conversations.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-1.5">
          {conversations.slice(0, 8).map(conversation => (
            <span key={conversation.id}
              className={cn('group inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[12px] transition-colors',
                conversation.id === conversationId ? 'border-transparent bg-accent text-on-accent' : 'border-line-soft text-ink-2 hover:bg-inset')}>
              <button type="button" className="max-w-50 truncate" onClick={() => openConversation(conversation.id)}>{conversation.title}</button>
              <button type="button" aria-label={`Delete ${conversation.title}`} onClick={() => removeConversation(conversation.id)}
                className="opacity-0 transition-opacity group-hover:opacity-60 hover:!opacity-100"><Trash2 size={11}/></button>
            </span>
          ))}
        </div>
      )}

      <div className="mt-5 min-h-0 w-full flex-1 overflow-x-hidden overflow-y-auto pr-1">
        {empty ? (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <p className="max-w-md text-[14px] leading-relaxed text-ink-3">
              Ask about anything that was discussed. Meeting facts are quoted from your transcripts and cited;
              general questions are answered as general knowledge and labelled as such.
            </p>
            <div className="flex flex-wrap justify-center gap-2">
              {STARTERS.map(starter => (
                <button key={starter} type="button" onClick={() => send(starter)}
                  className="rounded-full border border-line-soft px-3 py-1.5 text-[12.5px] text-ink-2 hover:bg-inset">
                  {starter}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="flex w-full min-w-0 flex-col gap-4">
            {messages.map((message, index) => (
              <motion.div key={message.id || index} initial={{opacity: 0, y: 8}} animate={{opacity: 1, y: 0}}
                transition={{duration: 0.28, ease: [0.23, 1, 0.32, 1]}} className="flex w-full min-w-0 flex-col gap-2">
                <SpeechBubble side={message.role === 'user' ? 'right' : 'left'}>
                  {message.role === 'user'
                    ? <span className="whitespace-pre-wrap break-words">{message.content}</span>
                    : <AnswerText text={message.content} sources={message.sources || []} web={message.web || []} onCite={citeJump}/>}
                </SpeechBubble>
                {message.role === 'assistant' && !!message.web?.length && webQuery && index === messages.length - 1 && (
                  <p className="pl-1 text-[11px] text-ink-4">Searched the web for: “{webQuery}”</p>
                )}
                {message.role === 'assistant' && (!!message.sources?.length || !!message.web?.length) && (
                  <div className="grid min-w-0 gap-2 pr-10 sm:grid-cols-2 xl:grid-cols-3">
                    {(message.sources || []).map(source => (
                      <SourceCard key={'m' + source.n} source={source} open={open} highlighted={highlight === source.n}/>
                    ))}
                    {(message.web || []).map(result => <WebCard key={'w' + result.n} result={result}/>)}
                  </div>
                )}
              </motion.div>
            ))}
            {busy && (
              <div className="flex flex-col gap-2">
                {!streaming && (
                  <SearchingIndicator stage={stage} query={webQuery}
                    domains={liveWeb.map(result => result.domain).filter(Boolean)}/>
                )}
                {streaming && (
                  <SpeechBubble side="left">
                    <AnswerText text={streaming} sources={liveSources} web={liveWeb} onCite={citeJump}/>
                  </SpeechBubble>
                )}
              </div>
            )}
            <div ref={bottom}/>
          </div>
        )}
      </div>

      {error && <p className="mt-3 text-[12.5px] text-red" role="alert">{error}</p>}
      <div className="mt-3 pb-4">
        <PromptComposer onSend={send} busy={busy} onStop={() => abort.current?.abort()}
          web={web} setWeb={setWeb} webAvailable={webAvailable}/>
      </div>
    </section>
  );
}
