import { Link2 } from "lucide-react";
import { getToken } from "../lib/auth";
import EndpointsSection from "../features/endpoints/EndpointsSection";
import RemoteHostsSection from "../features/remote-hosts/RemoteHostsSection";
import AppearanceSection from "../features/settings/AppearanceSection";

function maskToken(token: string | null): string {
  if (!token) return "Not paired";
  if (token.length <= 8) return "••••";
  return `••••${token.slice(-4)}`;
}

export default function SettingsPage() {
  const token = getToken();

  return (
    <div className="h-full min-h-0 overflow-y-auto overscroll-contain">
      <div className="mx-auto w-full max-w-2xl px-4 py-8 sm:px-6">
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>

        <EndpointsSection />

        <RemoteHostsSection />

        <AppearanceSection />

        <section
          aria-labelledby="network-pairing"
          className="mt-6 rounded-2xl border border-border bg-surface-1 p-5 shadow-sm"
        >
          <div className="flex items-center gap-2">
            <Link2 className="size-5 text-fg-subtle" aria-hidden />
            <h2 id="network-pairing" className="text-lg font-medium">
              Network &amp; Pairing
            </h2>
          </div>

          <dl className="mt-4 space-y-3 text-sm">
            <div className="flex items-center justify-between gap-4">
              <dt className="text-fg-muted">Access token</dt>
              <dd className="font-mono text-fg">{maskToken(token)}</dd>
            </div>
          </dl>

          <p className="mt-4 text-sm text-fg-muted">
            Pairing controls land here soon: show a QR code with the Tailscale URL
            and token, rotate tokens, and manage paired devices.
          </p>
        </section>
      </div>
    </div>
  );
}
