import { memo, useMemo } from "react";
import { MarkdownView, parseMarkdown } from "../Markdown";
import { CodeBlock, splitFences } from "./code";

/** An assistant message: Markdown prose (headings, lists, tables, links) with rich code blocks. Safe while streaming: an open
 *  fence renders as a code block that grows. Nothing is injected as HTML. */
export const ChatMarkdown = memo(function ChatMarkdown({ text, streaming }: { text: string; streaming?: boolean }) {
  const segs = useMemo(() => splitFences(text), [text]);
  return (
    <div className="chat-md">
      {segs.map((s, i) => s.kind === "code"
        ? <CodeBlock key={i} lang={s.lang} code={s.code} streaming={streaming && s.open} />
        : <Prose key={i} text={s.text} />)}
    </div>
  );
});

const Prose = memo(function Prose({ text }: { text: string }) {
  const blocks = useMemo(() => parseMarkdown(text.replace(/^\n+|\n+$/g, "")), [text]);
  return blocks.length ? <MarkdownView blocks={blocks} query="" current={-1} /> : null;
});
