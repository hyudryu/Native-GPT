/**
 * Pure validation for the endpoint add/edit form — unit-tested, no React.
 */

export interface EndpointFormValues {
  name: string;
  base_url: string;
  /** Raw input; empty means "keep stored key" when editing, "no key" when adding. */
  api_key: string;
  /** Editing only: clear the stored API key. */
  clear_key: boolean;
  /** Raw input (seconds); empty falls back to the default. */
  timeout_seconds: string;
  /** Raw JSON text; empty means no thinking-off override. */
  thinking_off_params: string;
  /** Raw JSON text; empty means no thinking-high override. */
  thinking_high_params: string;
}

export const DEFAULT_TIMEOUT_SECONDS = 15;
export const MAX_TIMEOUT_SECONDS = 300;

export interface EndpointFormErrors {
  name?: string;
  base_url?: string;
  timeout_seconds?: string;
  thinking_off_params?: string;
  thinking_high_params?: string;
}

/**
 * Validate a raw thinking-params textarea value. Empty is valid (no override);
 * otherwise the text must parse to a JSON object — the runtime merges it
 * verbatim into the chat-completions request, so arrays/scalars are rejected
 * client-side just like the server does.
 */
function validateThinkingParams(raw: string): string | undefined {
  const trimmed = raw.trim();
  if (trimmed.length === 0) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return "Must be valid JSON";
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return "Must be a JSON object";
  }
  return undefined;
}

/**
 * Parse a validated thinking-params textarea value. Returns undefined when
 * empty; callers must run validateEndpointForm first (this throws on bad JSON).
 */
export function parseThinkingParams(raw: string): Record<string, unknown> | undefined {
  const trimmed = raw.trim();
  if (trimmed.length === 0) return undefined;
  return JSON.parse(trimmed) as Record<string, unknown>;
}

/** Pretty-print a stored thinking-params JSON column for the edit form. */
export function formatThinkingParams(raw: string | null | undefined): string {
  if (!raw) return "";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    // Unparseable stored value — show it raw so the user can fix it.
    return raw;
  }
}

export function validateEndpointForm(
  values: EndpointFormValues,
): EndpointFormErrors {
  const errors: EndpointFormErrors = {};

  if (values.name.trim().length === 0) {
    errors.name = "Name is required";
  }

  const rawUrl = values.base_url.trim();
  if (rawUrl.length === 0) {
    errors.base_url = "Base URL is required";
  } else {
    let url: URL | null = null;
    try {
      url = new URL(rawUrl);
    } catch {
      url = null;
    }
    if (!url || (url.protocol !== "http:" && url.protocol !== "https:")) {
      errors.base_url = "Must be a valid http(s) URL, e.g. http://127.0.0.1:8080/v1";
    }
  }

  const rawTimeout = values.timeout_seconds.trim();
  if (rawTimeout.length > 0) {
    const timeout = Number(rawTimeout);
    if (
      !Number.isInteger(timeout) ||
      timeout < 1 ||
      timeout > MAX_TIMEOUT_SECONDS
    ) {
      errors.timeout_seconds = `Whole seconds, 1–${MAX_TIMEOUT_SECONDS}`;
    }
  }

  const thinkingOffError = validateThinkingParams(values.thinking_off_params);
  if (thinkingOffError) errors.thinking_off_params = thinkingOffError;
  const thinkingHighError = validateThinkingParams(values.thinking_high_params);
  if (thinkingHighError) errors.thinking_high_params = thinkingHighError;

  return errors;
}

export function hasErrors(errors: EndpointFormErrors): boolean {
  return Object.values(errors).some((e) => e !== undefined);
}

/** Normalize the validated form into the API payload. */
export function toEndpointPayload(values: EndpointFormValues): {
  name: string;
  base_url: string;
  timeout_seconds: number;
  /** Parsed object, or null to clear/leave unset (server maps null → unset on create). */
  thinking_off_params: Record<string, unknown> | null;
  thinking_high_params: Record<string, unknown> | null;
} {
  const rawTimeout = values.timeout_seconds.trim();
  return {
    name: values.name.trim(),
    base_url: values.base_url.trim().replace(/\/+$/, ""),
    timeout_seconds:
      rawTimeout.length > 0 ? Number(rawTimeout) : DEFAULT_TIMEOUT_SECONDS,
    thinking_off_params: parseThinkingParams(values.thinking_off_params) ?? null,
    thinking_high_params: parseThinkingParams(values.thinking_high_params) ?? null,
  };
}
