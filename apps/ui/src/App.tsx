import { HashRouter, Route, Routes } from "react-router";
import AppShell from "./layout/AppShell";
import ChatPage from "./pages/ChatPage";
import SettingsPage from "./pages/SettingsPage";
import NotFoundPage from "./pages/NotFoundPage";
import { useDataChangedSync } from "./lib/useDataChangedSync";

export default function App() {
  useDataChangedSync();

  return (
    <HashRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<ChatPage />} />
          <Route path="conversations/:conversationId" element={<ChatPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </HashRouter>
  );
}
