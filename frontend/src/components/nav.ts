import type { LucideIcon } from "lucide-react";
import { Activity, Bot, BookOpen, Users, Database, FileBarChart, FileDown, FlaskConical, Gauge, GitCompareArrows, Home, Layers, ListChecks, ListTree, Settings as Cog, FileText } from "lucide-react";

/** SpecForge's tools share one library of parsed specs: converter, compare, coverage, and the MCP endpoint that serves the specs to coding agents. */
export type NavGroup = "home" | "convert" | "compare" | "coverage" | "agents" | "system";
/** `shared`: a coverage-group page the other tools use too (the spec library), so it stays open while the Coverage Tool is unreleased.
 *  `soon`: the placeholder shown instead of the Coverage Tool's pages to users who cannot use it yet. */
export interface NavItem { to: string; label: string; icon: LucideIcon; desc: string; group: NavGroup; adminOnly?: boolean; shared?: boolean; soon?: boolean }
export const NAV: NavItem[] = [
  { to: "/", label: "Home", icon: Home, desc: "The three tools: convert, compare and coverage", group: "home" },
  { to: "/convert", label: "To Markdown", icon: FileDown, desc: "Convert a PDF, DOC or DOCX into a clean Markdown file", group: "convert" },
  { to: "/compare", label: "Compare versions", icon: GitCompareArrows, desc: "Compare two spec versions and see what changed, with an AI summary", group: "compare" },
  { to: "/workflow", label: "Workflow", icon: ListChecks, desc: "Coverage tool: where things stand and what to do next", group: "coverage" },
  { to: "/specs", label: "Specs", icon: FileText, desc: "Upload and parse specification documents (the library the compare and coverage tools use)", group: "coverage", shared: true },
  { to: "/explorer", label: "Explorer", icon: BookOpen, desc: "Browse parsed sections, tables and requirements", group: "coverage", shared: true },
  { to: "/sets", label: "Sets", icon: Layers, desc: "Group spec versions, resolve cross-references", group: "coverage" },
  { to: "/catalog", label: "Catalog", icon: ListTree, desc: "Everything that can be covered", group: "coverage" },
  { to: "/cmddb", label: "Command DBs", icon: Database, desc: "Commands your test scripts submitted", group: "coverage" },
  { to: "/scenarios", label: "Scenarios", icon: FlaskConical, desc: "What the tests executed, one scenario per test case", group: "coverage" },
  { to: "/coverage", label: "Coverage", icon: Gauge, desc: "Run and inspect coverage", group: "coverage" },
  { to: "/reports", label: "Reports", icon: FileBarChart, desc: "Export Excel, HTML, CSV, JSON", group: "coverage" },
  // not "/mcp": that path is the endpoint agents connect to
  { to: "/agents", label: "MCP for agents", icon: Bot, desc: "Connect OpenCode, Roo Code or Claude Code to the parsed specs (MCP endpoint, setup guide, admin status)", group: "agents" },
  { to: "/activity", label: "Activity", icon: Activity, desc: "Live logs and jobs: what the system and AI agents are doing", group: "system" },
  { to: "/users", label: "Users", icon: Users, desc: "Approve access requests, add users, roles, passwords (admins)", group: "system", adminOnly: true },
  { to: "/settings", label: "Settings", icon: Cog, desc: "AI providers, models, parser, logging, theme", group: "system" },
];
/** Who can use the Coverage Tool: everyone once an admin released it; before that admins as an internal preview, and the
 *  others see it as coming soon. */
export type CoverageAccess = "released" | "preview" | "soon";
export const coverageAccess = (isAdmin: boolean, released: boolean): CoverageAccess => (released ? "released" : isAdmin ? "preview" : "soon");
/** The Coverage Tool's own pages (not the shared spec library): what "coming soon" stands in for. */
export const COVERAGE_PAGES = NAV.filter((n) => n.group === "coverage" && !n.shared).map((n) => n.to);
export const isCoveragePage = (path: string) => COVERAGE_PAGES.some((p) => path === p || path.startsWith(p + "/"));
export const COMING_SOON: NavItem = { to: "/workflow", label: "Coverage Tool", icon: Gauge, desc: "Measure which commands, fields and requirements your tests cover. Coming soon.", group: "coverage", soon: true };

/** The pages this user may open: admin-only pages (user management) only for an admin of a server with sign-in. While the
 *  Coverage Tool is coming soon its pages give way to one placeholder, and the spec library moves next to Spec Compare. */
export const navFor = (canManageUsers: boolean, coverage: CoverageAccess = "released"): NavItem[] => {
  const pages = NAV.filter((n) => canManageUsers || !n.adminOnly);
  if (coverage !== "soon") return pages;
  const out: NavItem[] = [];
  for (const n of pages) {
    if (n.group !== "coverage") out.push(n);
    else if (n.shared) out.push({ ...n, group: "compare" });
  }
  return [...out, COMING_SOON];
};
export const GROUPS: { key: NavGroup; title: string | null; blurb?: string }[] = [
  { key: "home", title: null },
  { key: "convert", title: "Spec Converter", blurb: "PDF / DOC / DOCX to Markdown" },
  { key: "compare", title: "Spec Compare", blurb: "What changed between two versions" },
  { key: "coverage", title: "Coverage Tool", blurb: "Measure test coverage of the specs" },
  { key: "agents", title: "Spec MCP", blurb: "Spec lookups for coding agents" },
];
