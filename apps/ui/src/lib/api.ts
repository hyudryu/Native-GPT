import { useQuery } from "@tanstack/react-query";
import { authFetch } from "./auth";

export interface Health {
  status: "ok" | "degraded";
  uptime_seconds: number;
  rss_bytes: number;
}

export async function getHealth(): Promise<Health> {
  const res = await authFetch("/api/health");
  if (!res.ok) {
    throw new Error(`GET /api/health failed: ${res.status}`);
  }
  return (await res.json()) as Health;
}

/**
 * Server health. Deliberately low-key: cached for 30s, refetched on window
 * focus (so the status pill freshens when the user comes back), no polling.
 */
export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    refetchInterval: false,
  });
}
