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
import { useAuth } from "./components/auth";

export default function App() {
  const { push } = useToast();
  const { isAdmin } = useAuth();
  return (
    <Shell>
      <JobToasts push={push} />
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/workflow" element={<OverviewPage />} />
        <Route path="/compare" element={<ComparePage />} />
        <Route path="/dashboard" element={<Navigate to="/" replace />} />
        <Route path="/specs" element={<SpecsPage />} />
        <Route path="/convert" element={<ConvertPage />} />
        <Route path="/explorer" element={<ExplorerPage />} />
        <Route path="/sets" element={<SetsPage />} />
        <Route path="/catalog" element={<CatalogPage />} />
        <Route path="/scenarios" element={<ScenariosPage />} />
        <Route path="/cmddb" element={<CommandDBsPage />} />
        <Route path="/coverage" element={<CoveragePage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/activity" element={<ActivityPage />} />
        <Route path="/jobs" element={<Navigate to="/activity?tab=jobs" replace />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/users" element={isAdmin ? <UsersPage /> : <Navigate to="/" replace />} />
        <Route path="/account" element={<AccountPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}
