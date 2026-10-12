import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { JobsTray } from "./components/jobs";
import DashboardPage from "./pages/Dashboard";
import SetsPage from "./pages/Sets";
import CatalogPage from "./pages/Catalog";
import CommandDBsPage from "./pages/CommandDBs";
import JobsPage from "./pages/Jobs";
import SettingsPage from "./pages/Settings";
import SpecsPage from "./pages/Specs";
import ExplorerPage from "./pages/Explorer";
import ScenariosPage from "./pages/Scenarios";
import CoveragePage from "./pages/Coverage";
import ReportsPage from "./pages/Reports";

const NAV = [["/dashboard", "Dashboard"], ["/specs", "Specs"], ["/explorer", "Explorer"], ["/sets", "Sets"], ["/catalog", "Catalog"], ["/scenarios", "Scenarios"],
  ["/cmddb", "Command DBs"], ["/coverage", "Coverage"], ["/reports", "Reports"], ["/jobs", "Jobs"], ["/settings", "Settings"]] as const;

export default function App() {
  return (
    <div className="shell">
      <nav className="nav" aria-label="Main">
        <div className="brand">SpecForge</div>
        {NAV.map(([to, label]) => <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "active" : "")}>{label}</NavLink>)}
        <div className="spacer" />
        <JobsTray />
      </nav>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/specs" element={<SpecsPage />} />
          <Route path="/explorer" element={<ExplorerPage />} />
          <Route path="/sets" element={<SetsPage />} />
          <Route path="/catalog" element={<CatalogPage />} />
          <Route path="/cmddb" element={<CommandDBsPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/scenarios" element={<ScenariosPage />} />
          <Route path="/coverage" element={<CoveragePage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  );
}
