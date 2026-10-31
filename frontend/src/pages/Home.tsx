import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Bot, FileDown, Gauge, GitCompareArrows, Sparkles } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../components/auth";

/** `soon`: not released to this user yet (a badge and no way in); `badge`: a note for admins, e.g. an internal preview. */
function Tool({ icon: Icon, tone, name, tagline, points, to, cta, links, foot, soon, badge }: {
  icon: LucideIcon; tone: string; name: string; tagline: string; points: string[]; to: string; cta: string; links?: { to: string; label: string }[]; foot?: ReactNode;
  soon?: boolean; badge?: ReactNode;
}) {
  return (
    <section className={`tool ${tone} ${soon ? "is-soon" : ""}`} aria-labelledby={`tool-${tone}`}>
      <div className="tool-ic"><Icon size={22} /></div>
      <h2 id={`tool-${tone}`}>{name} {soon && <span className="badge violet">Coming soon</span>}{badge}</h2>
      <p className="tool-tag">{tagline}</p>
      <ul>{points.map((p) => <li key={p}>{p}</li>)}</ul>
      <div className="row" style={{ marginTop: "auto" }}>
        {soon ? <button className="primary" disabled>Coming soon</button> : <Link to={to}><button className="primary">{cta} <ArrowRight size={15} /></button></Link>}
        {!soon && links?.map((l) => <Link key={l.to} to={l.to} className="small">{l.label}</Link>)}
      </div>
      {foot && <div className="tool-foot muted small">{foot}</div>}
    </section>
  );
}

/** The landing page: SpecForge is separate tools on one spec library; each card says what it does and what it needs. */
export default function HomePage() {
  const { coverage } = useAuth();
  const specs = useQuery({ queryKey: ["specs"], queryFn: api.specs });
  const comps = useQuery({ queryKey: ["comparisons"], queryFn: api.comparisons });
  const parsed = (specs.data ?? []).filter((d) => d.runs.some((r) => r.status === "done")).length;
  return (
    <>
      <section className="hero">
        <span className="badge violet"><Sparkles size={12} /> Local-first · works offline</span>
        <h1>Tools for NVMe specifications.</h1>
        <p>Turn a spec into Markdown, see exactly what a new revision changed, measure what your tests cover, and let your coding agent look the specs up. Each tool stands on its own; they share the library of specs you upload once.</p>
      </section>
      <div className="tools">
        <Tool icon={FileDown} tone="convert" name="Spec Converter" tagline="Any PDF, DOC or DOCX into a clean Markdown file."
          points={["Keeps headings, lists, tables, code, links and footnotes", "Drops page headers, footers and table-of-contents noise", "Upload, preview, copy or download; nothing is stored"]}
          to="/convert" cta="Convert a document" links={[{ to: "/convert", label: "Also available as `specforge to-md`" }]} />
        <Tool icon={GitCompareArrows} tone="compare" name="Spec Compare" tagline="Pick two versions, for example 2.3 and 2.4, and see what changed."
          points={["Finds new, removed and modified sections, tables and requirements, even when sections are renumbered", "Flags changes that can break implementations: a shall becoming a should, a field removed, a reserved bit defined", "Optional AI summary of what the new version brings"]}
          to="/compare" cta="Compare two versions" links={[{ to: "/specs", label: "Manage spec versions" }]}
          foot={<>{parsed} parsed spec(s) in the library · {(comps.data ?? []).length >= 100 ? "100+" : (comps.data ?? []).length} saved comparison(s)</>} />
        <Tool icon={Gauge} tone="coverage" name="Coverage Tool" tagline="Which commands, fields and requirements do your tests actually cover?"
          points={["Builds the list of everything that can be covered from the specs", "Derives one scenario per executed test from your command log", "Covered / partial / not covered with evidence; Excel, HTML, CSV, JSON reports"]}
          to="/workflow" cta="Open the coverage workflow" links={[{ to: "/specs", label: "Specs" }, { to: "/coverage", label: "Coverage" }, { to: "/reports", label: "Reports" }]}
          soon={coverage === "soon"}
          badge={coverage === "preview" && <Link to="/settings#rollout" className="badge warn" title="Only admins can use it until it is released in Settings">Admin preview</Link>}
          foot={coverage === "soon" ? <>Being tried out internally; it appears here once it is released.</> : undefined} />
        <Tool icon={Bot} tone="agents" name="Spec MCP" tagline="Your coding agent asks the parsed specs instead of guessing."
          points={["One MCP endpoint for OpenCode, Roo Code and Claude Code; no account or key", "Command Dwords and fields, data structures, sections and figures, with page citations", "What changed between versions, computed once on the server"]}
          to="/agents" cta="Connect your agent" foot={<>{parsed} parsed spec(s) served</>} />
      </div>
      <p className="muted small" style={{ marginTop: 18 }}>Code decides what exists, what changed and what is covered; AI only describes and drafts, and never changes a verdict. Every parse and AI call is visible in <Link to="/activity">Activity</Link>.</p>
    </>
  );
}
