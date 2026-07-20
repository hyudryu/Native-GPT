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
}

export const DEFAULT_TIMEOUT_SECONDS = 15;
export const MAX_TIMEOUT_SECONDS = 300;

export interface EndpointFormErrors {
  name?: string;
  base_url?: string;
  timeout_seconds?: string;
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
} {
  const rawTimeout = values.timeout_seconds.trim();
  return {
    name: values.name.trim(),
    base_url: values.base_url.trim().replace(/\/+$/, ""),
    timeout_seconds:
      rawTimeout.length > 0 ? Number(rawTimeout) : DEFAULT_TIMEOUT_SECONDS,
  };
}
