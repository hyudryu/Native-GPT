import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import {
  ArrowLeft,
  ArrowRight,
  Ellipsis,
  ExternalLink,
  Globe,
  LoaderCircle,
  Lock,
  RotateCw,
  Settings,
  Square,
  TriangleAlert,
} from "lucide-react";
import { navigationTarget, securityKind } from "./addressBar";
import {
  useNavigateBrowser,
  useStopBrowser,
} from "./browserApi";
import { browserStream } from "./browserStream";
import { selectInputBlocked, useBrowserStore } from "./browserStore";

const navButton =
  "inline-flex min-h-9 min-w-9 shrink-0 items-center justify-center rounded-lg text-fg-muted transition-colors hover:bg-surface-2 hover:text-fg disabled:cursor-not-allowed disabled:opacity-40";

function closeDetails(element: HTMLElement) {
  const details = element.closest("details") as HTMLDetailsElement | null;
  if (details) details.open = false;
}

function SecurityIcon({ url }: { url: string }) {
  const kind = securityKind(url);
  if (kind === "secure") {
    return <Lock className="size-3.5 shrink-0 text-fg-subtle" aria-label="Secure connection" />;
  }
  if (kind === "insecure") {
    return <TriangleAlert className="size-3.5 shrink-0 text-danger" aria-label="Not secure" />;
  }
  return <Globe className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />;
}

/**
 * Navigation row (spec §2.4): Back/Forward, Reload/Stop, address + search
 * field, security icon, open-external, and the browser menu.
 */
export default function BrowserToolbar() {
  const activeTabId = useBrowserStore((s) => s.activeTabId);
  const activeTab = useBrowserStore((s) =>
    s.tabs.find((t) => t.id === s.activeTabId),
  );
  const processStatus = useBrowserStore((s) => s.processStatus);
  const inputBlocked = useBrowserStore(selectInputBlocked);
  const addressFocusNonce = useBrowserStore((s) => s.addressFocusNonce);

  const navigate = useNavigateBrowser();
  const stopBrowser = useStopBrowser();

  const [editing, setEditing] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const running = processStatus === "running";
  const url = activeTab?.url ?? "";

  // Ctrl/Cmd+L anywhere in the viewport focuses the address field (spec §2.4).
  useEffect(() => {
    if (addressFocusNonce > 0) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [addressFocusNonce]);

  const submit = () => {
    const text = (editing ?? "").trim();
    setEditing(null);
    if (!text) return;
    const target = navigationTarget(text);
    if (!target) return;
    navigate.mutate({ url: target, tabId: activeTabId ?? undefined });
  };

  /** Reload/Stop are sent as key input — the server has no dedicated endpoint. */
  const sendKey = (key: string, code: string) => {
    browserStream.sendKey({ kind: "rawKeyDown", key, code, windowsVirtualKeyCode: key === "F5" ? 116 : 27 });
    browserStream.sendKey({ kind: "keyUp", key, code, windowsVirtualKeyCode: key === "F5" ? 116 : 27 });
  };

  const openExternal = () => {
    if (!url) return;
    window.open(url, "_blank", "noopener,noreferrer");
  };

  return (
    <div className="flex items-center gap-1 border-b border-border bg-surface-1 px-2 py-1">
      <button
        type="button"
        aria-label="Back"
        disabled
        title="Back — not supported by the server yet"
        className={navButton}
      >
        <ArrowLeft className="size-4" aria-hidden />
      </button>
      <button
        type="button"
        aria-label="Forward"
        disabled
        title="Forward — not supported by the server yet"
        className={navButton}
      >
        <ArrowRight className="size-4" aria-hidden />
      </button>
      {activeTab?.loading ? (
        <button
          type="button"
          aria-label="Stop loading"
          title="Stop loading"
          disabled={!running || inputBlocked}
          onClick={() => sendKey("Escape", "Escape")}
          className={navButton}
        >
          <Square className="size-3.5" aria-hidden />
        </button>
      ) : (
        <button
          type="button"
          aria-label="Reload"
          title="Reload"
          disabled={!running || inputBlocked}
          onClick={() => sendKey("F5", "F5")}
          className={navButton}
        >
          <RotateCw className="size-4" aria-hidden />
        </button>
      )}

      <div className="flex min-h-9 min-w-0 flex-1 items-center gap-1.5 rounded-xl border border-border bg-surface-0 px-2.5">
        <SecurityIcon url={url} />
        <input
          ref={inputRef}
          type="text"
          aria-label="Address or search"
          placeholder="Search or enter address"
          value={editing ?? url}
          onChange={(event) => setEditing(event.target.value)}
          onFocus={() => setEditing(url)}
          onBlur={() => setEditing(null)}
          onKeyDown={(event) => {
            if (event.key === "Enter") submit();
            if (event.key === "Escape") {
              setEditing(null);
              inputRef.current?.blur();
            }
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "l") {
              event.preventDefault();
              inputRef.current?.select();
            }
          }}
          className="min-h-8 min-w-0 flex-1 bg-transparent text-xs text-fg placeholder:text-fg-subtle focus:outline-none"
        />
        {activeTab?.loading && (
          <LoaderCircle className="size-3.5 shrink-0 animate-spin text-fg-subtle" aria-hidden />
        )}
      </div>

      <button
        type="button"
        aria-label="Open in external browser"
        title="Open in external browser"
        disabled={!url}
        onClick={openExternal}
        className={navButton}
      >
        <ExternalLink className="size-4" aria-hidden />
      </button>

      <details className="relative shrink-0">
        <summary
          aria-label="Browser menu"
          className="flex min-h-9 min-w-9 cursor-pointer list-none items-center justify-center rounded-lg text-fg-muted hover:bg-surface-2 hover:text-fg [&::-webkit-details-marker]:hidden"
        >
          <Ellipsis className="size-4" aria-hidden />
        </summary>
        <div
          className="fixed inset-0 z-20"
          onClick={(event) => {
            event.preventDefault();
            const details = (event.currentTarget as HTMLElement).closest("details");
            if (details) details.open = false;
          }}
        />
        <div
          role="menu"
          aria-label="Browser menu"
          className="absolute right-0 top-10 z-30 w-52 rounded-xl border border-border bg-surface-3 p-1 shadow-lg"
        >
          <button
            type="button"
            role="menuitem"
            disabled={!url}
            onClick={(event) => {
              closeDetails(event.currentTarget);
              openExternal();
            }}
            className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm text-fg-muted hover:bg-surface-2 hover:text-fg disabled:opacity-50"
          >
            <ExternalLink className="size-4" aria-hidden /> Open externally
          </button>
          <button
            type="button"
            role="menuitem"
            disabled={processStatus === "stopped" || stopBrowser.isPending}
            onClick={(event) => {
              closeDetails(event.currentTarget);
              stopBrowser.mutate();
            }}
            className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm text-fg-muted hover:bg-surface-2 hover:text-fg disabled:opacity-50"
          >
            <Square className="size-4" aria-hidden /> Stop browser
          </button>
          <Link
            to="/settings"
            role="menuitem"
            onClick={(event) => closeDetails(event.currentTarget)}
            className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm text-fg-muted hover:bg-surface-2 hover:text-fg"
          >
            <Settings className="size-4" aria-hidden /> Browser settings
          </Link>
        </div>
      </details>
    </div>
  );
}
