import { useState } from "react";
import { Globe, LoaderCircle } from "lucide-react";
import Toggle from "../../components/Toggle";
import { useEnabledModels, modelOptionValue } from "../../lib/dataApi";
import {
  useBrowserComponent,
  useBrowserProfiles,
  useInstallBrowserComponent,
  useUninstallBrowserComponent,
} from "./browserApi";
import { useBrowserStore } from "./browserStore";
import BrowserInstallDialog from "./BrowserInstallDialog";

const COMING_SOON =
  "Coming soon — the server does not expose this preference yet.";

/**
 * Settings → Browser (spec §16). Only settings backed by server endpoints are
 * enabled today: component install/uninstall and profile info. Panel mode and
 * width persist via the panel itself; the remaining preferences are rendered
 * disabled until the server exposes them.
 */
export default function BrowserSection() {
  const component = useBrowserComponent();
  const profiles = useBrowserProfiles();
  const install = useInstallBrowserComponent();
  const uninstall = useUninstallBrowserComponent();
  const enabledModels = useEnabledModels();

  const profileId = useBrowserStore((s) => s.profileId);
  const processStatus = useBrowserStore((s) => s.processStatus);
  const installStatus = useBrowserStore((s) => s.installStatus);
  const installProgress = useBrowserStore((s) => s.installProgress);
  const keepHidden = useBrowserStore((s) => s.keepHiddenDuringAutomation);
  const setKeepHidden = useBrowserStore((s) => s.setKeepHiddenDuringAutomation);

  const [installOpen, setInstallOpen] = useState(false);
  // "Coming soon" scaffolding: the automation-model radios/select below are
  // permanently disabled (no server endpoint yet), so their values are plain
  // constants — they only seed the initial visual state and never change.
  const modelMode: string = "follow_conversation";
  const fixedModel: string = "";

  const info = component.data;
  const status = info?.status ?? installStatus;
  const installing =
    status === "downloading" || status === "verifying" || status === "extracting";
  const activeProfile = (profiles.data ?? []).find((p) => p.id === profileId);

  return (
    <section
      aria-labelledby="settings-browser"
      className="mt-6 rounded-2xl border border-border bg-surface-1 p-5 shadow-sm"
    >
      <div className="flex items-center gap-2">
        <Globe className="size-5 text-fg-subtle" aria-hidden />
        <h2 id="settings-browser" className="text-lg font-medium">
          Browser
        </h2>
      </div>

      <div className="mt-4 space-y-5">
        {/* ---- Runtime component ---- */}
        <div>
          <span className="mb-1 block text-sm font-medium text-fg-muted">
            Native GPT Browser component
          </span>
          <p className="text-xs text-fg-subtle">
            A dedicated Chromium runtime with Alibaba Page Agent support,
            installed as an optional component.
          </p>
          <dl className="mt-3 space-y-2 text-sm">
            <div className="flex items-center justify-between gap-4">
              <dt className="text-fg-muted">Status</dt>
              <dd className="flex items-center gap-2 text-fg">
                {installing && (
                  <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
                )}
                {status.replaceAll("_", " ")}
                {status === "downloading" && installProgress != null && (
                  <span className="text-xs text-fg-subtle">
                    {Math.round(installProgress * 100)}%
                  </span>
                )}
              </dd>
            </div>
            <div className="flex items-center justify-between gap-4">
              <dt className="text-fg-muted">Installed version</dt>
              <dd className="text-fg">{info?.installedVersion ?? "—"}</dd>
            </div>
            <div className="flex items-center justify-between gap-4">
              <dt className="text-fg-muted">Available version</dt>
              <dd className="text-fg">{info?.availableVersion ?? "—"}</dd>
            </div>
            <div className="flex items-center justify-between gap-4">
              <dt className="text-fg-muted">Browser process</dt>
              <dd className="text-fg">{processStatus}</dd>
            </div>
          </dl>
          <div className="mt-3 flex gap-2">
            {info?.installed ? (
              <button
                type="button"
                onClick={() => uninstall.mutate()}
                disabled={uninstall.isPending || processStatus === "running"}
                title={
                  processStatus === "running"
                    ? "Stop the browser before uninstalling"
                    : undefined
                }
                className="min-h-11 rounded-xl border border-border px-4 text-sm text-danger hover:bg-danger-subtle disabled:opacity-50"
              >
                Uninstall
              </button>
            ) : (
              <button
                type="button"
                onClick={() => setInstallOpen(true)}
                disabled={installing || install.isPending}
                className="min-h-11 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover disabled:opacity-50"
              >
                {status === "error" ? "Retry install" : "Install Browser"}
              </button>
            )}
          </div>
          {(install.isError || uninstall.isError) && (
            <p role="alert" className="mt-2 text-sm text-danger">
              {install.error?.message ?? uninstall.error?.message}
            </p>
          )}
        </div>

        {/* ---- Profile ---- */}
        <div>
          <span className="mb-1 block text-sm font-medium text-fg-muted">
            Profile
          </span>
          <dl className="space-y-2 text-sm">
            <div className="flex items-center justify-between gap-4">
              <dt className="text-fg-muted">Active profile</dt>
              <dd className="text-fg">
                {activeProfile ? activeProfile.name : profileId}
              </dd>
            </div>
          </dl>
          <p className="mt-2 text-xs text-fg-subtle">
            Multiple named profiles, profile reset, and browsing-data controls
            are coming soon.
          </p>
        </div>

        {/* ---- Behavior ---- */}
        <div className="space-y-3">
          <span className="block text-sm font-medium text-fg-muted">
            Behavior
          </span>
          <div className="flex items-center justify-between gap-4">
            <div>
              <span className="block text-sm text-fg">
                Keep browser hidden during automation
              </span>
              <span className="block text-xs text-fg-subtle">
                Agent tasks will not reopen the panel while this is on.
              </span>
            </div>
            <Toggle
              checked={keepHidden}
              onCheckedChange={setKeepHidden}
              label="Keep browser hidden during automation"
            />
          </div>
          {[
            {
              label: "Restore previous tabs on start",
              hint: COMING_SOON,
            },
            {
              label: "Keep browser running in background",
              hint: COMING_SOON,
            },
          ].map((row) => (
            <div
              key={row.label}
              className="flex items-center justify-between gap-4"
            >
              <div>
                <span className="block text-sm text-fg">{row.label}</span>
                <span className="block text-xs text-fg-subtle">{row.hint}</span>
              </div>
              <Toggle
                checked={false}
                onCheckedChange={() => {}}
                disabled
                label={row.label}
              />
            </div>
          ))}
        </div>

        {/* ---- Automation model (no server endpoint yet) ---- */}
        <div>
          <span className="mb-1 block text-sm font-medium text-fg-muted">
            Browser automation model
          </span>
          <div
            role="radiogroup"
            aria-label="Browser automation model"
            className="flex flex-col gap-2 sm:flex-row"
          >
            {[
              { value: "follow_conversation", label: "Follow conversation model" },
              { value: "fixed", label: "Fixed model" },
            ].map((opt) => (
              <button
                key={opt.value}
                type="button"
                role="radio"
                aria-checked={modelMode === opt.value}
                disabled
                title={COMING_SOON}
                className={`flex min-h-11 flex-1 cursor-not-allowed items-center gap-3 rounded-xl border px-3 text-left opacity-60 ${
                  modelMode === opt.value
                    ? "border-accent bg-accent-subtle"
                    : "border-border bg-surface-1"
                }`}
              >
                <span
                  className={`size-4 shrink-0 rounded-full border ${
                    modelMode === opt.value
                      ? "border-accent bg-accent"
                      : "border-border-strong"
                  }`}
                />
                <span className="text-sm text-fg">{opt.label}</span>
              </button>
            ))}
          </div>
          {modelMode === "fixed" && (
            <select
              aria-label="Fixed browser model"
              value={fixedModel}
              disabled
              className="mt-2 min-h-11 w-full rounded-xl border border-border bg-surface-1 px-3 text-sm text-fg opacity-60 no-focus-ring"
            >
              <option value="">Select a model</option>
              {enabledModels.data?.map((model) => (
                <option key={modelOptionValue(model)} value={modelOptionValue(model)}>
                  {model.provider_name} — {model.model_id}
                </option>
              ))}
            </select>
          )}
          <p className="mt-2 text-xs text-fg-subtle">{COMING_SOON}</p>
        </div>

        {/* ---- Remote access (no server endpoint yet) ---- */}
        <div className="flex items-center justify-between gap-4">
          <div>
            <span className="block text-sm text-fg">
              Allow remote clients to view the browser
            </span>
            <span className="block text-xs text-fg-subtle">
              Warning: the browser profile may contain logged-in sessions.
              Enable this only on trusted networks. {COMING_SOON}
            </span>
          </div>
          <Toggle
            checked={false}
            onCheckedChange={() => {}}
            disabled
            label="Allow remote clients to view the browser"
          />
        </div>
      </div>

      <BrowserInstallDialog open={installOpen} onOpenChange={setInstallOpen} />
    </section>
  );
}
