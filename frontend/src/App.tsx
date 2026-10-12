import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { JobsTray } from "./components/jobs";
import SpecsPage from "./pages/Specs";
import ExplorerPage from "./pages/Explorer";
import ScenariosPage from "./pages/Scenarios";
import CoveragePage from "./pages/Coverage";
import ReportsPage from "./pages/Reports";

const NAV = [["/specs", "Specs"], ["/explorer", "Explorer"], ["/scenarios", "Scenarios"], ["/coverage", "Coverage"], ["/reports", "Reports"]] as const;

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
          <Route path="/" element={<Navigate to="/specs" replace />} />
          <Route path="/specs" element={<SpecsPage />} />
          <Route path="/explorer" element={<ExplorerPage />} />
          <Route path="/scenarios" element={<ScenariosPage />} />
          <Route path="/coverage" element={<CoveragePage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="*" element={<Navigate to="/specs" replace />} />
        </Routes>
      </main>
    </div>
  );
}
