import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { JobToasts } from "./components/jobs";
import { useToast } from "./components/Toasts";
import OverviewPage from "./pages/Overview";
import SetsPage from "./pages/Sets";
import CatalogPage from "./pages/Catalog";
import CommandDBsPage from "./pages/CommandDBs";
import ActivityPage from "./pages/Activity";
import SettingsPage from "./pages/Settings";
import SpecsPage from "./pages/Specs";
import ExplorerPage from "./pages/Explorer";
import ScenariosPage from "./pages/Scenarios";
import CoveragePage from "./pages/Coverage";
import ReportsPage from "./pages/Reports";

export default function App() {
  const { push } = useToast();
  return (
    <Shell>
      <JobToasts push={push} />
      <Routes>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/dashboard" element={<Navigate to="/" replace />} />
        <Route path="/specs" element={<SpecsPage />} />
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
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}
