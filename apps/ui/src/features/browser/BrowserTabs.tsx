import {
  ChevronDown,
  Globe,
  LoaderCircle,
  Maximize2,
  Minimize2,
  PanelRightClose,
  Plus,
  X,
} from "lucide-react";
import { useCloseBrowserTab, useCreateBrowserTab } from "./browserApi";
import { browserStream } from "./browserStream";
import {
  selectTaskActive,
  selectVisibleTabs,
  useBrowserStore,
} from "./browserStore";
import type { BrowserTab } from "./types";

const controlButton =
  "inline-flex min-h-9 min-w-9 shrink-0 items-center justify-center rounded-lg text-fg-muted transition-colors hover:bg-surface-2 hover:text-fg";

function TabFavicon({ tab }: { tab: BrowserTab }) {
  if (tab.loading) {
    return <LoaderCircle className="size-3.5 shrink-0 animate-spin text-fg-subtle" aria-hidden />;
  }
  if (tab.faviconUrl) {
    return (
      <img src={tab.faviconUrl} alt="" className="size-3.5 shrink-0 rounded-sm" />
    );
  }
  return <Globe className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />;
}

function closeDetails(element: HTMLElement) {
  const details = element.closest("details") as HTMLDetailsElement | null;
  if (details) details.open = false;
}

/**
 * Tab row (spec §2.4): favicon + title, close buttons, new-tab, dropdown for
 * many tabs, agent status dot, and the permanent Shrink/Expand/Hide controls
 * (spec §2.2).
 */
export default function BrowserTabs() {
  const tabs = useBrowserStore(selectVisibleTabs);
  const activeTabId = useBrowserStore((s) => s.activeTabId);
  const mode = useBrowserStore((s) => s.mode);
  const taskActive = useBrowserStore(selectTaskActive);
  const shrink = useBrowserStore((s) => s.shrink);
  const expand = useBrowserStore((s) => s.expand);
  const hide = useBrowserStore((s) => s.hide);

  const createTab = useCreateBrowserTab();
  const closeTab = useCloseBrowserTab();

  const activate = (id: string) => browserStream.sendTabActivate(id);

  const tabButton = (tab: BrowserTab, inMenu = false) => {
    const active = tab.id === activeTabId;
    return (
      <div
        key={tab.id}
        className={`group flex min-w-0 items-center gap-1 rounded-lg ${
          inMenu ? "w-full" : "max-w-40"
        } ${active ? "bg-surface-2" : "hover:bg-surface-2/60"} ${
          active && taskActive ? "ring-1 ring-accent" : ""
        }`}
      >
        <button
          type="button"
          onClick={(event) => {
            closeDetails(event.currentTarget);
            activate(tab.id);
          }}
          title={tab.title || tab.url}
          className="flex min-h-9 min-w-0 flex-1 items-center gap-1.5 rounded-lg px-2 text-left text-xs text-fg-muted hover:text-fg"
        >
          <TabFavicon tab={tab} />
          <span className="min-w-0 flex-1 truncate">
            {tab.title || tab.url || "New tab"}
          </span>
          {taskActive && active && (
            <span
              className="size-1.5 shrink-0 animate-pulse rounded-full bg-accent"
              title="Agent active on this tab"
              aria-hidden
            />
          )}
        </button>
        <button
          type="button"
          aria-label={`Close tab ${tab.title || tab.url}`}
          onClick={(event) => {
            event.stopPropagation();
            closeDetails(event.currentTarget);
            closeTab.mutate(tab.id);
          }}
          className="mr-1 hidden min-h-6 min-w-6 shrink-0 items-center justify-center rounded-md text-fg-subtle hover:bg-surface-3 hover:text-fg group-hover:inline-flex"
        >
          <X className="size-3" aria-hidden />
        </button>
      </div>
    );
  };

  const inlineTabs = tabs.length > 3 ? tabs.slice(0, 3) : tabs;
  const overflowTabs = tabs.length > 3 ? tabs.slice(3) : [];

  return (
    <div className="flex items-center gap-1 border-b border-border bg-surface-1 px-2 py-1">
      <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
        {inlineTabs.map((tab) => tabButton(tab))}
        {overflowTabs.length > 0 && (
          <details className="relative shrink-0">
            <summary
              aria-label="More tabs"
              className="flex min-h-9 min-w-9 cursor-pointer list-none items-center justify-center rounded-lg text-fg-muted hover:bg-surface-2 hover:text-fg [&::-webkit-details-marker]:hidden"
            >
              <ChevronDown className="size-4" aria-hidden />
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
              aria-label="More tabs"
              className="absolute left-0 top-10 z-30 w-56 rounded-xl border border-border bg-surface-3 p-1 shadow-lg"
            >
              {overflowTabs.map((tab) => tabButton(tab, true))}
            </div>
          </details>
        )}
        <button
          type="button"
          aria-label="New tab"
          onClick={() => createTab.mutate(undefined)}
          disabled={createTab.isPending}
          className={controlButton}
        >
          <Plus className="size-4" aria-hidden />
        </button>
      </div>

      <div className="flex shrink-0 items-center">
        <button
          type="button"
          aria-label={mode === "compact" ? "Restore previous width" : "Shrink browser panel"}
          title={mode === "compact" ? "Restore previous width" : "Shrink to compact"}
          onClick={shrink}
          className={controlButton}
        >
          <Minimize2 className="size-4" aria-hidden />
        </button>
        <button
          type="button"
          aria-label={mode === "focus" ? "Exit focus mode" : "Expand browser panel"}
          title={mode === "focus" ? "Exit focus mode" : "Expand / focus"}
          onClick={expand}
          className={controlButton}
        >
          <Maximize2 className="size-4" aria-hidden />
        </button>
        <button
          type="button"
          aria-label="Hide browser panel"
          title="Hide panel (browser keeps running)"
          onClick={hide}
          className={controlButton}
        >
          <PanelRightClose className="size-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}
