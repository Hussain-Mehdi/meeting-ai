/* Renders an assistant answer: the Markdown the model writes, with citation chips kept as
 * first-class elements inside the text.
 *
 * Written rather than pulled in, because the hard part here is not Markdown — it is
 * interleaving `[1]` and `[W1]` chips with formatted text, which a general Markdown library
 * would hand back as plain strings for us to re-parse anyway. The grammar covered is the one
 * the model actually produces: bold, italic, inline code, links, fenced code, headings,
 * bullet and numbered lists, and block quotes. Anything unrecognised renders as written, so a
 * half-finished token mid-stream is never worse than the raw text.
 */
import {type ReactNode} from 'react';

export type InlineRenderer = (token: string, key: string) => ReactNode | null;

const INLINE = /(\[[^\]]+\]\(https?:\/\/[^)\s]+\)|\*\*[^*]+\*\*|__[^_]+__|\*[^*\n]+\*|`[^`]+`|\[W?\d{1,2}\][.,;:!?)]*)/gi;

function inline(text: string, keyBase: string, citation?: InlineRenderer): ReactNode[] {
  return text.split(INLINE).filter(part => part !== '' && part !== undefined).map((part, index) => {
    const key = `${keyBase}-${index}`;
    const cited = citation?.(part, key);
    if (cited) return cited;

    const link = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/.exec(part);
    if (link) {
      return (
        <a key={key} href={link[2]} target="_blank" rel="noreferrer noopener"
          className="underline underline-offset-2 hover:opacity-80">{link[1]}</a>
      );
    }
    if (/^\*\*[^*]+\*\*$/.test(part) || /^__[^_]+__$/.test(part)) {
      return <strong key={key} className="font-semibold">{part.slice(2, -2)}</strong>;
    }
    if (/^\*[^*\n]+\*$/.test(part)) return <em key={key}>{part.slice(1, -1)}</em>;
    if (/^`[^`]+`$/.test(part)) {
      return (
        <code key={key} className="rounded bg-inset px-1 py-0.5 font-mono text-[12.5px] text-ink">
          {part.slice(1, -1)}
        </code>
      );
    }
    return <span key={key}>{part}</span>;
  });
}

type Block =
  | {kind: 'p' | 'quote'; lines: string[]}
  | {kind: 'h'; level: number; text: string}
  | {kind: 'ul' | 'ol'; items: string[]}
  | {kind: 'code'; lines: string[]};

function parse(text: string): Block[] {
  const blocks: Block[] = [];
  const lines = (text || '').split('\n');
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (/^\s*```/.test(line)) {                      // fenced code, closing fence optional mid-stream
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) body.push(lines[index++]);
      index += 1;
      blocks.push({kind: 'code', lines: body});
      continue;
    }
    if (!line.trim()) {index += 1; continue}

    const heading = /^\s*(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({kind: 'h', level: heading[1].length, text: heading[2]});
      index += 1;
      continue;
    }
    if (/^\s*[-*•]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*[-*•]\s+/.test(lines[index])) {
        items.push(lines[index++].replace(/^\s*[-*•]\s+/, ''));
      }
      blocks.push({kind: 'ul', items});
      continue;
    }
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        items.push(lines[index++].replace(/^\s*\d+[.)]\s+/, ''));
      }
      blocks.push({kind: 'ol', items});
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const body: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        body.push(lines[index++].replace(/^\s*>\s?/, ''));
      }
      blocks.push({kind: 'quote', lines: body});
      continue;
    }

    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim()
           && !/^\s*(```|#{1,6}\s|[-*•]\s|\d+[.)]\s|>)/.test(lines[index])) {
      paragraph.push(lines[index++]);
    }
    blocks.push({kind: 'p', lines: paragraph});
  }
  return blocks;
}

export default function RichText({text, citation}: {text: string; citation?: InlineRenderer}) {
  const blocks = parse(text);
  return (
    <div className="flex flex-col gap-2 break-words">
      {blocks.map((block, index) => {
        const key = `b${index}`;
        switch (block.kind) {
          case 'h':
            return (
              <p key={key} className={block.level <= 2 ? 'text-[15px] font-semibold' : 'font-semibold'}>
                {inline(block.text, key, citation)}
              </p>
            );
          case 'ul':
            return (
              <ul key={key} className="flex list-disc flex-col gap-1 pl-5 marker:text-ink-4">
                {block.items.map((item, i) => <li key={i}>{inline(item, `${key}-${i}`, citation)}</li>)}
              </ul>
            );
          case 'ol':
            return (
              <ol key={key} className="flex list-decimal flex-col gap-1 pl-5 marker:text-ink-4">
                {block.items.map((item, i) => <li key={i}>{inline(item, `${key}-${i}`, citation)}</li>)}
              </ol>
            );
          case 'code':
            return (
              <pre key={key} className="overflow-x-auto rounded-[10px] bg-inset p-2.5 font-mono text-[12.5px] leading-relaxed text-ink">
                <code>{block.lines.join('\n')}</code>
              </pre>
            );
          case 'quote':
            return (
              <blockquote key={key} className="border-l-2 border-line-soft pl-3 text-ink-2">
                {inline(block.lines.join(' '), key, citation)}
              </blockquote>
            );
          default:
            return <p key={key}>{inline(block.lines.join('\n'), key, citation)}</p>;
        }
      })}
    </div>
  );
}
