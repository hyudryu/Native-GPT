import { HashRouter, Route, Routes } from "react-router";
import AppShell from "./layout/AppShell";
import ChatPage from "./pages/ChatPage";
import SettingsPage from "./pages/SettingsPage";
import NotFoundPage from "./pages/NotFoundPage";
import { useDataChangedSync } from "./lib/useDataChangedSync";
import AnalyticsPage from "./pages/AnalyticsPage";
import BrainPage from "./pages/BrainPage";
import KnowledgeDumpPage from "./pages/KnowledgeDumpPage";
import ToolsPage from "./pages/ToolsPage";
import UpdatesPage from "./pages/UpdatesPage";

export default function App() {
  useDataChangedSync();

  return (
    <HashRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<ChatPage />} />
          <Route path="conversations/:conversationId" element={<ChatPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="apps/analytics" element={<AnalyticsPage />} />
          <Route path="apps/brain" element={<BrainPage />} />
          <Route path="apps/knowledge-dump" element={<KnowledgeDumpPage />} />
          <Route path="apps/tools" element={<ToolsPage />} />
          <Route path="apps/updates" element={<UpdatesPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </HashRouter>
  );
}
