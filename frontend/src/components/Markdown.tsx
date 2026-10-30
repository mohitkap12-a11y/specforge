import { Fragment, memo, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

/* A small Markdown renderer for the converter's own output (GitHub-flavoured: front matter, headings, paragraphs, lists,
 * quotes, fenced code, pipe tables, **bold**, *italic*, `code`, links, <br> in cells). It builds React elements and never
 * injects HTML, so a converted spec cannot run script in the page. Search highlights every match in the text it renders. */

export type Inline =
  | { t: "text"; v: string } | { t: "code"; v: string } | { t: "br" } | { t: "sup"; v: string }
  | { t: "b" | "i" | "u" | "sp" | "sb"; c: Inline[] } | { t: "a"; href: string; c: Inline[] };
type Item = { depth: number; marker: string; c: Inline[] };
export type Block =
  | { t: "h"; level: number; c: Inline[] } | { t: "p"; c: Inline[] } | { t: "quote"; c: Inline[] }
  | { t: "list"; items: Item[] } | { t: "code"; v: string } | { t: "raw"; v: string } | { t: "hr" } | { t: "image" }
  | { t: "front"; entries: [string, string][] }
  | { t: "table"; align: ("left" | "center" | "right" | undefined)[]; head: Inline[][]; rows: Inline[][][] };

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", "#39": "'" };

/** Where the emphasis opened at `from` closes ("*" / "**" / "_" / "__"), or -1; escapes and the other width are skipped. */
function closing(s: string, from: number, ch: string, double: boolean): number {
  for (let j = from + 1; j < s.length; j++) {
    if (s[j] === "\\") { j++; continue; }
    if (s[j] !== ch) continue;
    const run = s[j + 1] === ch ? 2 : 1;
    if ((run === 2) === double && s[j - 1] !== " " && !(ch === "_" && /\w/.test(s[j + run] ?? ""))) return j;
    j += run - 1;
  }
  return -1;
}

/** Inline markup -> tokens. Unbalanced markers stay literal text. */
export function parseInline(s: string): Inline[] {
  const out: Inline[] = [];
  let buf = "";
  const flush = () => { if (buf) { out.push({ t: "text", v: buf }); buf = ""; } };
  let i = 0;
  while (i < s.length) {
    const ch = s[i];
    if (ch === "\\" && i + 1 < s.length && /[!-/:-@[-`{-~]/.test(s[i + 1])) { buf += s[i + 1]; i += 2; continue; }
    if (ch === "`") {
      let n = 0;
      while (s[i + n] === "`") n++;
      const fence = "`".repeat(n);
      const end = s.indexOf(fence, i + n);
      if (end > 0) { flush(); out.push({ t: "code", v: s.slice(i + n, end).replace(/^ (.*) $/, "$1") }); i = end + n; continue; }
      buf += fence; i += n; continue;
    }
    // "_" only marks emphasis at a word boundary: SQ_ID and __FOO__BAR stay as written
    if ((ch === "*" || ch === "_") && !(ch === "_" && /\w/.test(s[i - 1] ?? "")) && s[i + 1] !== " ") {
      const triple = ch.repeat(3);
      const end3 = s.startsWith(triple, i) ? s.indexOf(triple, i + 3) : -1;
      if (end3 > i + 3) { flush(); out.push({ t: "b", c: [{ t: "i", c: parseInline(s.slice(i + 3, end3)) }] }); i = end3 + 3; continue; }
      const double = s[i + 1] === ch;
      const end = closing(s, i + (double ? 2 : 1), ch, double);
      if (end > 0) {
        flush();
        out.push({ t: double ? "b" : "i", c: parseInline(s.slice(i + (double ? 2 : 1), end)) });
        i = end + (double ? 2 : 1);
        continue;
      }
    }
    if (ch === "<") {
      const br = /^<br\s*\/?>/i.exec(s.slice(i, i + 6));
      if (br) { flush(); out.push({ t: "br" }); i += br[0].length; continue; }
      if (s.startsWith("<!--", i)) { const end = s.indexOf("-->", i); if (end > 0) { i = end + 3; continue; } }
      // the few tags the built-in converter writes (<sup>®</sup>, <sub>, <u>) become elements; any other tag stays text
      const tag = /^<(sup|sub|u)>/i.exec(s.slice(i, i + 5));
      const end = tag ? s.toLowerCase().indexOf(`</${tag[1].toLowerCase()}>`, i) : -1;
      if (tag && end > 0) {
        flush();
        out.push({ t: ({ sup: "sp", sub: "sb", u: "u" } as const)[tag[1].toLowerCase() as "sup" | "sub" | "u"], c: parseInline(s.slice(i + tag[0].length, end)) });
        i = end + tag[1].length + 3;
        continue;
      }
    }
    if (ch === "[") {
      const fn = /^\[\^([^\]\s]+)\]/.exec(s.slice(i));
      if (fn) { flush(); out.push({ t: "sup", v: fn[1] }); i += fn[0].length; continue; }
      const link = /^\[((?:\\.|[^\]\\])*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/.exec(s.slice(i));
      if (link) { flush(); out.push({ t: "a", href: link[2], c: parseInline(link[1]) }); i += link[0].length; continue; }
    }
    if (ch === "&") {
      const ent = /^&(#\d+|#x[0-9a-f]+|[a-z]+);/i.exec(s.slice(i, i + 10));
      if (ent) {
        const k = ent[1].toLowerCase();
        const v = ENTITIES[k] ?? (k.startsWith("#x") ? String.fromCodePoint(parseInt(k.slice(2), 16)) : k.startsWith("#") ? String.fromCodePoint(parseInt(k.slice(1), 10)) : undefined);
        if (v !== undefined) { buf += v; i += ent[0].length; continue; }
      }
    }
    buf += ch;
    i++;
  }
  flush();
  return out;
}

const splitRow = (line: string): string[] => {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  const cells: string[] = [];
  let cur = "";
  for (let i = 0; i < s.length; i++) {
    if (s[i] === "\\" && s[i + 1] === "|") { cur += "|"; i++; continue; }
    if (s[i] === "|") { cells.push(cur.trim()); cur = ""; continue; }
    cur += s[i];
  }
  cells.push(cur.trim());
  return cells;
};
const SEP = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;
const HEADING = /^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const FENCE = /^\s*(`{3,}|~{3,})/;
const HR = /^\s*([-*_])(\s*\1){2,}\s*$/;

/** Markdown text -> blocks. */
export function parseMarkdown(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out: Block[] = [];
  let i = 0;
  if (lines[0] === "---") {
    const end = lines.indexOf("---", 1);
    if (end > 0) {
      out.push({ t: "front", entries: lines.slice(1, end).map((l) => { const k = l.indexOf(":"); return [l.slice(0, k).trim(), l.slice(k + 1).trim().replace(/^"(.*)"$/, "$1")] as [string, string]; }).filter(([k]) => k) });
      i = end + 1;
    }
  }
  let para: string[] = [];
  const endPara = () => { if (para.length) { out.push({ t: "p", c: parseInline(para.join(" ")) }); para = []; } };
  while (i < lines.length) {
    const ln = lines[i];
    if (!ln.trim()) { endPara(); i++; continue; }
    const fence = FENCE.exec(ln);
    if (fence) {
      endPara();
      const close = fence[1][0].repeat(fence[1].length);
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(close)) body.push(lines[i++]);
      i++;
      out.push({ t: "code", v: body.join("\n") });
      continue;
    }
    const h = HEADING.exec(ln);
    if (h) { endPara(); out.push({ t: "h", level: h[1].length, c: parseInline(h[2]) }); i++; continue; }
    if (ln.trim() === "<!-- image -->") { endPara(); out.push({ t: "image" }); i++; continue; }
    if (ln.trimStart().startsWith("|") && i + 1 < lines.length && SEP.test(lines[i + 1])) {
      endPara();
      const head = splitRow(ln);
      const align = splitRow(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : undefined));
      i += 2;
      const rows: Inline[][][] = [];
      while (i < lines.length && lines[i].trimStart().startsWith("|")) rows.push(splitRow(lines[i++]).map(parseInline));
      out.push({ t: "table", align, head: head.map(parseInline), rows });
      continue;
    }
    if (HR.test(ln)) { endPara(); out.push({ t: "hr" }); i++; continue; }
    if (ln.trimStart().startsWith(">")) {
      endPara();
      const body: string[] = [];
      while (i < lines.length && lines[i].trimStart().startsWith(">")) body.push(lines[i++].trimStart().replace(/^>\s?/, ""));
      out.push({ t: "quote", c: parseInline(body.join(" ")) });
      continue;
    }
    const it = ITEM.exec(ln);
    if (it) {
      endPara();
      const items: Item[] = [];
      let m: RegExpExecArray | null;
      while (i < lines.length && (m = ITEM.exec(lines[i]))) {
        let body = m[3];
        i++;
        // a continuation line is indented and is not itself an item
        while (i < lines.length && lines[i].trim() && /^\s{2,}/.test(lines[i]) && !ITEM.test(lines[i])) body += " " + lines[i++].trim();
        items.push({ depth: Math.min(6, Math.floor(m[1].replace(/\t/g, "    ").length / 2)), marker: /\d/.test(m[2]) ? m[2] : "•", c: parseInline(body) });
      }
      out.push({ t: "list", items });
      continue;
    }
    para.push(ln.trim());
    i++;
  }
  endPara();
  return out;
}

/** The raw source split into chunks of lines, so it renders and searches like the formatted view. */
export function sourceBlocks(text: string, size = 200): Block[] {
  const lines = text.split("\n");
  const out: Block[] = [];
  for (let i = 0; i < lines.length; i += size) out.push({ t: "raw", v: lines.slice(i, i + size).join("\n") + (i + size < lines.length ? "\n" : "") });
  return out;
}

// ----------------------------------------------------------------- search

const inlineTexts = (c: Inline[], acc: string[]) => {
  for (const x of c) {
    if (x.t === "text" || x.t === "code" || x.t === "sup") acc.push(x.v);
    else if (x.t !== "br") inlineTexts(x.c, acc);
  }
  return acc;
};
/** Every text run of a block, in the order it is rendered: matches are counted per run, as they are highlighted. */
function blockTexts(b: Block): string[] {
  switch (b.t) {
    case "h": case "p": case "quote": return inlineTexts(b.c, []);
    case "list": return b.items.flatMap((x) => inlineTexts(x.c, []));
    case "code": case "raw": return [b.v];
    case "front": return b.entries.flatMap(([k, v]) => [k, v]);
    case "table": return [...b.head, ...b.rows.flat()].flatMap((c) => inlineTexts(c, []));
    default: return [];
  }
}
export const blockText = (b: Block) => blockTexts(b).join(" ");

function countIn(s: string, q: string): number {
  if (!q) return 0;
  const hay = s.toLowerCase();
  let n = 0;
  for (let i = hay.indexOf(q); i >= 0; i = hay.indexOf(q, i + q.length)) n++;
  return n;
}

/** Hands out global hit numbers while one block renders. */
type Hits = { q: string; next: number; cur: number };

/** Text with its matches marked. A plain function, not a component: hit numbers are handed out in the one render of BlockView. */
function hl(v: string, h: Hits | null): ReactNode {
  if (!h || !h.q) return v;
  const lower = v.toLowerCase();
  const parts: ReactNode[] = [];
  let from = 0;
  for (let i = lower.indexOf(h.q); i >= 0; i = lower.indexOf(h.q, i + h.q.length)) {
    if (i > from) parts.push(v.slice(from, i));
    const n = h.next++;
    parts.push(<mark key={i} className={n === h.cur ? "hit cur" : "hit"} data-hit={n}>{v.slice(i, i + h.q.length)}</mark>);
    from = i + h.q.length;
  }
  if (!parts.length) return v;
  if (from < v.length) parts.push(v.slice(from));
  return <>{parts}</>;
}

const safeHref = (href: string) => (/^(https?:|mailto:|#)/i.test(href) ? href : undefined);

function inl(c: Inline[], h: Hits | null): ReactNode {
  return c.map((x, k) => {
    switch (x.t) {
      case "text": return <Fragment key={k}>{hl(x.v, h)}</Fragment>;
      case "code": return <code key={k}>{hl(x.v, h)}</code>;
      case "br": return <br key={k} />;
      case "sup": return <sup key={k}>{hl(x.v, h)}</sup>;
      case "b": return <strong key={k}>{inl(x.c, h)}</strong>;
      case "i": return <em key={k}>{inl(x.c, h)}</em>;
      case "u": return <u key={k}>{inl(x.c, h)}</u>;
      case "sp": return <sup key={k}>{inl(x.c, h)}</sup>;
      case "sb": return <sub key={k}>{inl(x.c, h)}</sub>;
      case "a": return <a key={k} href={safeHref(x.href)} target="_blank" rel="noreferrer noopener">{inl(x.c, h)}</a>;
    }
  });
}

const BlockView = memo(function BlockView({ b, q, base, cur }: { b: Block; q: string; base: number; cur: number }) {
  const h: Hits | null = q ? { q, next: base, cur } : null;
  switch (b.t) {
    case "h": { const H = `h${b.level}` as "h1"; return <H>{inl(b.c, h)}</H>; }
    case "p": return <p>{inl(b.c, h)}</p>;
    case "quote": return <blockquote>{inl(b.c, h)}</blockquote>;
    case "hr": return <hr />;
    case "image": return <div className="md-image muted small">[image not included]</div>;
    case "code": return <pre className="md-code"><code>{hl(b.v, h)}</code></pre>;
    case "raw": return <pre className="md-raw">{hl(b.v, h)}</pre>;
    case "front":
      return (
        <dl className="md-front">
          {b.entries.map(([k, v]) => <div key={k}><dt>{hl(k, h)}</dt><dd>{hl(v, h)}</dd></div>)}
        </dl>
      );
    case "list":
      return (
        <ul className="md-list">
          {b.items.map((x, k) => <li key={k} style={{ marginLeft: x.depth * 20 }}><span className="md-marker">{x.marker}</span><span>{inl(x.c, h)}</span></li>)}
        </ul>
      );
    case "table":
      return (
        <div className="md-table">
          <table>
            <thead><tr>{b.head.map((c, k) => <th key={k} style={{ textAlign: b.align[k] }}>{inl(c, h)}</th>)}</tr></thead>
            <tbody>{b.rows.map((r, ri) => <tr key={ri}>{r.map((c, k) => <td key={k} style={{ textAlign: b.align[k] }}>{inl(c, h)}</td>)}</tr>)}</tbody>
          </table>
        </div>
      );
  }
});

const PAGE = 300;   // blocks rendered at a time; more are added as the reader scrolls (a large spec has tens of thousands)

/** Renders blocks with search highlights; `current` is the hit to scroll to. `jump` scrolls to a block (the outline). */
export function MarkdownView({ blocks, query, current, jump }: { blocks: Block[]; query: string; current: number; jump?: { block: number; n: number } }) {
  const q = query.toLowerCase();
  const counts = useMemo(() => blocks.map((b) => (q ? blockTexts(b).reduce((n, s) => n + countIn(s, q), 0) : 0)), [blocks, q]);
  const bases = useMemo(() => { let n = 0; return counts.map((c) => { const b = n; n += c; return b; }); }, [counts]);
  const [limit, setLimit] = useState(PAGE);
  const box = useRef<HTMLDivElement>(null);
  const more = useRef<HTMLDivElement>(null);
  useEffect(() => setLimit(PAGE), [blocks]);
  // the block that holds the current hit (or the outline target) must be rendered before it can be scrolled to
  const hitBlock = useMemo(() => {
    if (!q || current < 0) return -1;
    let lo = 0, hi = bases.length - 1, ans = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (bases[mid] <= current) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
    while (ans >= 0 && counts[ans] === 0) ans--;
    return ans;
  }, [bases, counts, current, q]);
  const need = Math.max(hitBlock, jump?.block ?? -1);
  useEffect(() => { if (need >= limit) setLimit(need + PAGE); }, [need, limit]);
  // scroll once per new hit / outline click, as soon as its block is rendered (not again when scrolling loads more)
  const shown = useRef({ hit: "", jump: 0 });
  useEffect(() => {
    const key = `${q}#${current}#${blocks.length}`;
    if (current < 0 || !q || shown.current.hit === key) return;
    const el = box.current?.querySelector(`[data-hit="${current}"]`);
    if (el) { el.scrollIntoView({ block: "center" }); shown.current.hit = key; }
  }, [current, q, limit, blocks]);
  useEffect(() => {
    if (!jump || shown.current.jump === jump.n) return;
    const el = box.current?.querySelector(`[data-block="${jump.block}"]`);
    if (el) { el.scrollIntoView({ block: "start" }); shown.current.jump = jump.n; }
  }, [jump, limit]);
  useEffect(() => {
    const el = more.current;
    if (!el) return;
    const io = new IntersectionObserver((e) => { if (e[0]?.isIntersecting) setLimit((l) => l + PAGE); }, { rootMargin: "800px" });
    io.observe(el);
    return () => io.disconnect();
  }, [limit, blocks]);
  return (
    <div className="md" ref={box}>
      {blocks.slice(0, limit).map((b, k) => {
        const c = counts[k];
        return <div key={k} data-block={k}><BlockView b={b} q={c ? q : ""} base={bases[k]} cur={c && current >= bases[k] && current < bases[k] + c ? current : -1} /></div>;
      })}
      {limit < blocks.length && <div ref={more} className="muted small md-more">Loading more…</div>}
    </div>
  );
}

/** Total number of matches of `query` in the blocks, counted the way MarkdownView highlights them. */
export function countHits(blocks: Block[], query: string): number {
  const q = query.toLowerCase();
  return q ? blocks.reduce((n, b) => n + blockTexts(b).reduce((m, s) => m + countIn(s, q), 0), 0) : 0;
}
