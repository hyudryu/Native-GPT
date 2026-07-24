import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { authFetch } from "../../lib/auth";
import type {
  BrowserComponentInfo,
  BrowserPanelMode,
  BrowserPanelPrefs,
  BrowserProfile,
  BrowserState,
  BrowserTab,
  PermissionScope,
} from "./types";

/**
 * REST client + react-query hooks for the host's browser API (spec §9.2).
 * Mirrors lib/remoteHosts.ts conventions. Live fields (tabs, task, frames)
 * arrive over the browser stream and update the zustand store; these hooks
 * cover the initial fetch and user-triggered mutations only — the stream is
 * the source of truth after connect.
 */

export class ApiError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await authFetch(path, { ...init, headers });
  if (!res.ok) {
    let code = `http_${res.status}`;
    let message = `Request failed with status ${res.status}`;
    try {
      const body = (await res.json()) as {
        error?: { code?: string; message?: string };
      };
      if (body.error?.code) code = body.error.code;
      if (body.error?.message) message = body.error.message;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(code, message, res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---- plain API functions ----

export function getBrowserComponent(): Promise<BrowserComponentInfo> {
  return apiFetch<BrowserComponentInfo>("/api/browser/component");
}

export function installBrowserComponent(): Promise<{
  status: string;
  progress: number | null;
}> {
  return apiFetch("/api/browser/component/install", { method: "POST" });
}

export function uninstallBrowserComponent(): Promise<{ status: string }> {
  return apiFetch("/api/browser/component", { method: "DELETE" });
}

export async function listBrowserProfiles(): Promise<BrowserProfile[]> {
  const data = await apiFetch<{ profiles: BrowserProfile[] }>(
    "/api/browser/profiles",
  );
  return data.profiles;
}

export function getBrowserState(): Promise<BrowserState> {
  return apiFetch<BrowserState>("/api/browser/state");
}

export function startBrowser(profileId?: string): Promise<BrowserState> {
  return apiFetch<BrowserState>("/api/browser/start", {
    method: "POST",
    body: JSON.stringify(profileId ? { profileId } : {}),
  });
}

export function stopBrowser(): Promise<{ processStatus: string }> {
  return apiFetch("/api/browser/stop", { method: "POST" });
}

export function navigateBrowser(input: {
  url: string;
  tabId?: string;
}): Promise<{ navigated: string }> {
  return apiFetch("/api/browser/navigate", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function createBrowserTab(url?: string): Promise<BrowserTab> {
  return apiFetch<BrowserTab>("/api/browser/tabs", {
    method: "POST",
    body: JSON.stringify(url ? { url } : {}),
  });
}

export function closeBrowserTab(id: string): Promise<void> {
  return apiFetch<void>(`/api/browser/tabs/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function updateBrowserPanel(input: {
  mode?: BrowserPanelMode;
  width?: number;
  containerWidth?: number;
}): Promise<BrowserPanelPrefs> {
  return apiFetch<BrowserPanelPrefs>("/api/browser/panel", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function stopBrowserTask(
  id: string,
): Promise<{ taskId: string; status: string }> {
  return apiFetch(`/api/browser/task/${encodeURIComponent(id)}/stop`, {
    method: "POST",
  });
}

export function takeOverBrowserTask(id: string): Promise<{
  taskId: string;
  status: string;
  manualControlEnabled: boolean;
}> {
  return apiFetch(`/api/browser/task/${encodeURIComponent(id)}/take-over`, {
    method: "POST",
  });
}

export function resolveBrowserApproval(
  id: string,
  input: { allow: boolean; scope?: PermissionScope },
): Promise<{ id: string; resolved: boolean; allowed: boolean }> {
  return apiFetch(`/api/browser/approvals/${encodeURIComponent(id)}/resolve`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

// ---- react-query hooks ----

const browserComponentKey = ["browser", "component"] as const;
const browserProfilesKey = ["browser", "profiles"] as const;
const browserStateKey = ["browser", "state"] as const;

export function useBrowserComponent() {
  return useQuery({
    queryKey: browserComponentKey,
    queryFn: getBrowserComponent,
    staleTime: 15_000,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "downloading" ||
        status === "verifying" ||
        status === "extracting"
        ? 1_000
        : false;
    },
  });
}

export function useInstallBrowserComponent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: installBrowserComponent,
    onSettled: () => qc.invalidateQueries({ queryKey: browserComponentKey }),
  });
}

export function useUninstallBrowserComponent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: uninstallBrowserComponent,
    onSettled: () => qc.invalidateQueries({ queryKey: browserComponentKey }),
  });
}

export function useBrowserProfiles() {
  return useQuery({
    queryKey: browserProfilesKey,
    queryFn: listBrowserProfiles,
    staleTime: 30_000,
  });
}

/**
 * Initial state fetch. After the stream connects it owns live updates —
 * do not refetch this on a timer.
 */
export function useBrowserState() {
  return useQuery({
    queryKey: browserStateKey,
    queryFn: getBrowserState,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}

export function useStartBrowser() {
  return useMutation({ mutationFn: startBrowser });
}

export function useStopBrowser() {
  return useMutation({ mutationFn: stopBrowser });
}

export function useNavigateBrowser() {
  return useMutation({ mutationFn: navigateBrowser });
}

export function useCreateBrowserTab() {
  return useMutation({ mutationFn: createBrowserTab });
}

export function useCloseBrowserTab() {
  return useMutation({ mutationFn: closeBrowserTab });
}

export function useStopBrowserTask() {
  return useMutation({ mutationFn: stopBrowserTask });
}

export function useTakeOverBrowserTask() {
  return useMutation({ mutationFn: takeOverBrowserTask });
}

export function useResolveBrowserApproval() {
  return useMutation({
    mutationFn: ({ id, allow, scope }: { id: string; allow: boolean; scope?: PermissionScope }) =>
      resolveBrowserApproval(id, { allow, scope }),
  });
}
