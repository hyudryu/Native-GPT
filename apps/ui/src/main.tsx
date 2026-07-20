import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { registerSW } from "virtual:pwa-register";
import App from "./App";
import { initAuth } from "./lib/auth";
import { startSocket } from "./lib/ws";
import "./index.css";

// Service worker: update immediately on load; autoUpdate reloads when a new
// version is ready, so desktop webviews never serve a stale shell.
registerSW({ immediate: true });

// 1) Grab ?token= from the pairing URL, persist it, clean the address bar.
initAuth();
// 2) Open the WebSocket (with reconnect handling).
startSocket();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1 },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
