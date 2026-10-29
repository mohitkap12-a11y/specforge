import type { LucideIcon } from "lucide-react";
import { Activity, BookOpen, Users, Database, FileBarChart, FileDown, FlaskConical, Gauge, GitCompareArrows, Home, Layers, ListChecks, ListTree, Settings as Cog, FileText } from "lucide-react";

/** SpecForge is three separate tools that share one library of parsed specs. */
export type NavGroup = "home" | "convert" | "compare" | "coverage" | "system";
export interface NavItem { to: string; label: string; icon: LucideIcon; desc: string; group: NavGroup; adminOnly?: boolean }
export const NAV: NavItem[] = [
  { to: "/", label: "Home", icon: Home, desc: "The three tools: convert, compare and coverage", group: "home" },
  { to: "/convert", label: "To Markdown", icon: FileDown, desc: "Convert a PDF, DOC or DOCX into a clean Markdown file", group: "convert" },
  { to: "/compare", label: "Compare versions", icon: GitCompareArrows, desc: "Compare two spec versions and see what changed, with an AI summary", group: "compare" },
  { to: "/workflow", label: "Workflow", icon: ListChecks, desc: "Coverage tool: where things stand and what to do next", group: "coverage" },
  { to: "/specs", label: "Specs", icon: FileText, desc: "Upload and parse specification documents (the library the compare and coverage tools use)", group: "coverage" },
  { to: "/explorer", label: "Explorer", icon: BookOpen, desc: "Browse parsed sections, tables and requirements", group: "coverage" },
  { to: "/sets", label: "Sets", icon: Layers, desc: "Group spec versions, resolve cross-references", group: "coverage" },
  { to: "/catalog", label: "Catalog", icon: ListTree, desc: "Everything that can be covered", group: "coverage" },
  { to: "/cmddb", label: "Command DBs", icon: Database, desc: "Commands your test scripts submitted", group: "coverage" },
  { to: "/scenarios", label: "Scenarios", icon: FlaskConical, desc: "What the tests executed, one scenario per test case", group: "coverage" },
  { to: "/coverage", label: "Coverage", icon: Gauge, desc: "Run and inspect coverage", group: "coverage" },
  { to: "/reports", label: "Reports", icon: FileBarChart, desc: "Export Excel, HTML, CSV, JSON", group: "coverage" },
  { to: "/activity", label: "Activity", icon: Activity, desc: "Live logs and jobs: what the system and AI agents are doing", group: "system" },
  { to: "/users", label: "Users", icon: Users, desc: "Approve access requests, add users, roles, passwords (admins)", group: "system", adminOnly: true },
  { to: "/settings", label: "Settings", icon: Cog, desc: "AI providers, models, parser, logging, theme", group: "system" },
];
/** The pages this user may open: admin-only pages (user management) only for an admin of a server with sign-in. */
export const navFor = (canManageUsers: boolean) => NAV.filter((n) => canManageUsers || !n.adminOnly);
export const GROUPS: { key: NavGroup; title: string | null; blurb?: string }[] = [
  { key: "home", title: null },
  { key: "convert", title: "Spec Converter", blurb: "PDF / DOC / DOCX to Markdown" },
  { key: "compare", title: "Spec Compare", blurb: "What changed between two versions" },
  { key: "coverage", title: "Coverage Tool", blurb: "Measure test coverage of the specs" },
];
