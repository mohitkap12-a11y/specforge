import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { JobToasts } from "./components/jobs";
import { useToast } from "./components/Toasts";
import HomePage from "./pages/Home";
import OverviewPage from "./pages/Overview";
import ComparePage from "./pages/Compare";
import SetsPage from "./pages/Sets";
import CatalogPage from "./pages/Catalog";
import CommandDBsPage from "./pages/CommandDBs";
import ActivityPage from "./pages/Activity";
import SettingsPage from "./pages/Settings";
import SpecsPage from "./pages/Specs";
import ConvertPage from "./pages/Convert";
import ExplorerPage from "./pages/Explorer";
import ScenariosPage from "./pages/Scenarios";
import CoveragePage from "./pages/Coverage";
import ReportsPage from "./pages/Reports";
import UsersPage from "./pages/Users";
import AccountPage from "./pages/Account";
import AgentsPage from "./pages/Agents";
import { useAuth } from "./components/auth";
import ComingSoonPage from "./pages/ComingSoon";
import type { ReactNode } from "react";

export default function App() {
  const { push } = useToast();
  const { canManageUsers, coverage } = useAuth();
  // the Coverage Tool's pages: a "coming soon" page until an admin releases it (admins use it meanwhile)
  const cov = (page: ReactNode) => (coverage === "soon" ? <ComingSoonPage /> : page);
  return (
    <Shell>
      <JobToasts push={push} />
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/workflow" element={cov(<OverviewPage />)} />
        <Route path="/compare" element={<ComparePage />} />
        <Route path="/dashboard" element={<Navigate to="/" replace />} />
        <Route path="/specs" element={<SpecsPage />} />
        <Route path="/convert" element={<ConvertPage />} />
        <Route path="/explorer" element={<ExplorerPage />} />
        <Route path="/sets" element={cov(<SetsPage />)} />
        <Route path="/catalog" element={cov(<CatalogPage />)} />
        <Route path="/scenarios" element={cov(<ScenariosPage />)} />
        <Route path="/cmddb" element={cov(<CommandDBsPage />)} />
        <Route path="/coverage" element={cov(<CoveragePage />)} />
        <Route path="/reports" element={cov(<ReportsPage />)} />
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/activity" element={<ActivityPage />} />
        <Route path="/jobs" element={<Navigate to="/activity?tab=jobs" replace />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/users" element={canManageUsers ? <UsersPage /> : <Navigate to="/" replace />} />
        <Route path="/account" element={<AccountPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}
