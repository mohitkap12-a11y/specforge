import type { LucideIcon } from "lucide-react";
import { Activity, BookOpen, Database, FileBarChart, FlaskConical, Gauge, Home, Layers, ListTree, Settings as Cog, FileText, FileDown } from "lucide-react";

export interface NavItem { to: string; label: string; icon: LucideIcon; desc: string; group: "top" | "specs" | "test" | "system" }
export const NAV: NavItem[] = [
  { to: "/", label: "Overview", icon: Home, desc: "Where things stand and what to do next", group: "top" },
  { to: "/specs", label: "Specs", icon: FileText, desc: "Upload and parse specification documents", group: "specs" },
  { to: "/convert", label: "To Markdown", icon: FileDown, desc: "Convert a PDF, DOC or DOCX into a clean Markdown file", group: "specs" },
  { to: "/explorer", label: "Explorer", icon: BookOpen, desc: "Browse parsed sections, tables and requirements", group: "specs" },
  { to: "/sets", label: "Sets", icon: Layers, desc: "Group spec versions, resolve cross-references", group: "specs" },
  { to: "/catalog", label: "Catalog", icon: ListTree, desc: "Everything that can be covered", group: "specs" },
  { to: "/cmddb", label: "Command DBs", icon: Database, desc: "Commands your test scripts submitted", group: "test" },
  { to: "/scenarios", label: "Scenarios", icon: FlaskConical, desc: "What the tests executed, one scenario per test case", group: "test" },
  { to: "/coverage", label: "Coverage", icon: Gauge, desc: "Run and inspect coverage", group: "test" },
  { to: "/reports", label: "Reports", icon: FileBarChart, desc: "Export Excel, HTML, CSV, JSON", group: "test" },
  { to: "/activity", label: "Activity", icon: Activity, desc: "Live logs and jobs: what the system and AI agents are doing", group: "system" },
  { to: "/settings", label: "Settings", icon: Cog, desc: "AI providers, models, parser, logging, theme", group: "system" },
];
export const GROUPS: { key: NavItem["group"]; title: string | null }[] = [
  { key: "top", title: null }, { key: "specs", title: "Specifications" }, { key: "test", title: "Testing" },
];
