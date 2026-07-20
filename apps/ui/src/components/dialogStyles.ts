/**
 * Shared class strings for Base UI dialogs: centered card on desktop,
 * bottom sheet on small screens (<640px).
 */
export const dialogBackdropCls =
  "fixed inset-0 z-40 bg-black/40 transition-opacity duration-200 data-[ending-style]:opacity-0 data-[starting-style]:opacity-0";

export const dialogPopupCls =
  "fixed z-50 bg-surface-3 shadow-lg outline-none transition-opacity duration-200 data-[ending-style]:opacity-0 data-[starting-style]:opacity-0 " +
  "max-sm:inset-x-0 max-sm:bottom-0 max-sm:max-h-[85dvh] max-sm:overflow-y-auto max-sm:rounded-t-2xl " +
  "sm:left-1/2 sm:top-1/2 sm:max-h-[85dvh] sm:w-full sm:max-w-md sm:-translate-x-1/2 sm:-translate-y-1/2 sm:overflow-y-auto sm:rounded-2xl";
