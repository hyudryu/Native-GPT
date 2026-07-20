import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { VitePWA } from "vite-plugin-pwa";

// Backend the dev server proxies to (Rust host). Override with VITE_DEV_BACKEND.
const backend = process.env.VITE_DEV_BACKEND ?? "http://127.0.0.1:8787";
const backendWs = backend.replace(/^http/, "ws");

export default defineConfig({
  // Relative asset paths: the shell must work served from any port/path
  // (Tauri webview on 127.0.0.1, phone over Tailscale on 100.x.y.z).
  base: "./",
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["icons/*.png"],
      manifest: {
        name: "Native GPT",
        short_name: "Native GPT",
        description: "Native GPT — local-first AI chat",
        display: "standalone",
        orientation: "portrait",
        start_url: "./",
        scope: "./",
        theme_color: "#131315",
        background_color: "#faf9f7",
        icons: [
          {
            src: "icons/icon-192.png",
            sizes: "192x192",
            type: "image/png",
          },
          {
            src: "icons/icon-512.png",
            sizes: "512x512",
            type: "image/png",
          },
          {
            src: "icons/icon-maskable-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
      workbox: {
        // Cache the app shell ONLY — never /api responses or the WS upgrade.
        globPatterns: ["**/*.{js,css,html,png,svg,ico,webmanifest}"],
        navigateFallback: "index.html",
        navigateFallbackDenylist: [/^\/api\//, /^\/ws($|\?|\/)/],
        runtimeCaching: [],
        cleanupOutdatedCaches: true,
      },
    }),
  ],
  server: {
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/ws": { target: backendWs, ws: true, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
