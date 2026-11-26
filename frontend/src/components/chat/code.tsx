import { Fragment, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Check, Copy, Download, Eye, Code2, WrapText } from "lucide-react";
import { copyText } from "../../clipboard";
import { saveFile } from "../../download";

/** A message split into prose and fenced code. A fence that is still open (the answer is streaming) is a code block too. */
export type Segment = { kind: "text"; text: string } | { kind: "code"; lang: string; code: string; open: boolean };

export function splitFences(src: string): Segment[] {
  const out: Segment[] = [];
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  let text: string[] = [];
  let i = 0;
  const flush = () => { if (text.length) { out.push({ kind: "text", text: text.join("\n") }); text = []; } };
  while (i < lines.length) {
    const m = /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)[^\n]*$/.exec(lines[i]);
    if (!m) { text.push(lines[i++]); continue; }
    flush();
    const fence = m[1];
    const body: string[] = [];
    i++;
    let closed = false;
    while (i < lines.length) {
      if (lines[i].trim().startsWith(fence[0].repeat(fence.length)) && /^\s*[`~]+\s*$/.test(lines[i])) { closed = true; i++; break; }
      body.push(lines[i++]);
    }
    out.push({ kind: "code", lang: m[2].toLowerCase(), code: body.join("\n"), open: !closed });
  }
  flush();
  return out;
}

const EXT: Record<string, string> = { json: "json", html: "html", xml: "xml", yaml: "yaml", yml: "yml", python: "py", py: "py", c: "c", cpp: "cpp", "c++": "cpp", h: "h", js: "js", javascript: "js", ts: "ts", typescript: "ts", tsx: "tsx", bash: "sh", sh: "sh", shell: "sh", sql: "sql", rust: "rs", go: "go", java: "java", markdown: "md", md: "md", csv: "csv", css: "css", toml: "toml", ini: "ini", diff: "diff" };
const KEYWORDS: Record<string, string> = {
  py: "def class return if elif else for while in not and or is None True False import from as with try except finally raise lambda yield pass break continue async await self",
  c: "int char void unsigned signed long short struct union enum typedef static const volatile extern return if else for while do switch case break continue sizeof uint8_t uint16_t uint32_t uint64_t bool true false NULL #include #define",
  js: "const let var function return if else for while do switch case break continue new class extends import export from default async await try catch finally throw typeof instanceof true false null undefined this interface type enum",
  sh: "if then else fi for do done while case esac function echo export sudo cd ls cat grep nvme",
  sql: "select from where insert into values update set delete create table join left right inner on group by order limit and or not null as",
  go: "func package import return if else for range struct interface type var const go defer chan map true false nil",
  rs: "fn let mut pub struct enum impl trait use mod return if else for while loop match true false self Self",
  java: "public private protected class interface extends implements static final void int long boolean new return if else for while try catch throw true false null",
};
const ALIAS: Record<string, string> = { cpp: "c", h: "c", ts: "js", tsx: "js", java: "java", yml: "yaml", bash: "sh" };

function langKey(lang: string): string { const e = EXT[lang] ?? lang; return ALIAS[e] ?? e; }

/** A small tokenizer: strings, comments, numbers and keywords. Good enough to read a snippet; it never runs or injects anything. */
export function highlight(code: string, lang: string): ReactNode {
  const key = langKey(lang);
  if (code.length > 40_000) return code;
  const kw = new Set((KEYWORDS[key] ?? "").split(" ").filter(Boolean));
  const comment = key === "py" || key === "sh" || key === "yaml" || key === "toml" || key === "ini" ? "#[^\\n]*" : key === "sql" ? "--[^\\n]*" : "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/";
  const markup = key === "html" || key === "xml";
  const re = markup
    ? /(<!--[\s\S]*?-->)|(<\/?[A-Za-z][\w:-]*)|("[^"\n]*"|'[^'\n]*')|(\/?>)/g
    : new RegExp(`(${comment})|("(?:[^"\\\\\\n]|\\\\.)*"|'(?:[^'\\\\\\n]|\\\\.)*'|\`(?:[^\`\\\\]|\\\\.)*\`)|(\\b(?:0x[0-9a-fA-F_]+|\\d[\\d_]*\\.?\\d*)\\b)|(#?[A-Za-z_][\\w]*)`, "g");
  const out: ReactNode[] = [];
  let last = 0, n = 0;
  for (let m = re.exec(code); m; m = re.exec(code)) {
    if (m.index > last) out.push(code.slice(last, m.index));
    const [txt] = m;
    let cls = "";
    if (markup) cls = m[1] ? "tk-c" : m[2] ? "tk-k" : m[3] ? "tk-s" : "tk-k";
    else if (m[1]) cls = "tk-c";
    else if (m[2]) cls = key === "json" && /^\s*:/.test(code.slice(m.index + txt.length, m.index + txt.length + 3)) ? "tk-p" : "tk-s";
    else if (m[3]) cls = "tk-n";
    else if (m[4]) cls = kw.has(m[4]) || (key === "json" && /^(true|false|null)$/.test(m[4])) ? "tk-k" : "";
    out.push(cls ? <span key={n++} className={cls}>{txt}</span> : <Fragment key={n++}>{txt}</Fragment>);
    last = m.index + txt.length;
    if (txt.length === 0) re.lastIndex++;
  }
  if (last < code.length) out.push(code.slice(last));
  return out;
}

const pretty = (code: string) => { try { return JSON.stringify(JSON.parse(code), null, 2); } catch { return null; } };

/** A fenced block as an assistant shows it: language label, Copy, Download, wrap, JSON formatting and a sandboxed HTML preview. */
export function CodeBlock({ lang, code, streaming }: { lang: string; code: string; streaming?: boolean }) {
  const [copied, setCopied] = useState(false);
  const [wrap, setWrap] = useState(false);
  const [view, setView] = useState<"code" | "preview">("code");
  const [formatted, setFormatted] = useState(false);
  const isJson = langKey(lang) === "json";
  const shown = useMemo(() => (isJson && formatted ? pretty(code) ?? code : code), [code, isJson, formatted]);
  const canPreview = langKey(lang) === "html" && !streaming;
  const ext = EXT[lang] ?? (lang || "txt");
  const body = useMemo(() => highlight(shown, lang), [shown, lang]);
  const copy = () => copyText(shown).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }, () => undefined);
  return (
    <div className="cb">
      <div className="cb-head">
        <span className="cb-lang">{lang || "text"}</span>
        <span className="grow" />
        {isJson && !streaming && pretty(code) !== null && <button className="sm cb-btn" onClick={() => setFormatted((f) => !f)} aria-pressed={formatted}>{formatted ? "Raw" : "Format"}</button>}
        {canPreview && <button className="sm cb-btn" onClick={() => setView(view === "code" ? "preview" : "code")}>{view === "code" ? <><Eye size={12} /> Preview</> : <><Code2 size={12} /> Code</>}</button>}
        <button className="sm cb-btn" onClick={() => setWrap((w) => !w)} aria-pressed={wrap} aria-label="Wrap lines" title="Wrap lines"><WrapText size={12} /></button>
        <button className="sm cb-btn" onClick={() => saveFile(`snippet.${ext}`, shown)} aria-label="Download" title="Download"><Download size={12} /></button>
        <button className="sm cb-btn" onClick={() => void copy()} aria-label="Copy code">{copied ? <><Check size={12} /> Copied</> : <><Copy size={12} /> Copy</>}</button>
      </div>
      {view === "preview"
        ? <iframe className="cb-preview" title="HTML preview" sandbox="" srcDoc={code} />   // no scripts, no same-origin: it cannot touch SpecForge
        : <pre className={`cb-pre ${wrap ? "wrap" : ""}`}><code>{body}</code></pre>}
    </div>
  );
}
