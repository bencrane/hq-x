// HTTP clients for calling hq-x from Trigger.dev tasks.
//
// `callHqx`    — POST to `/internal/*` routes, authed with
//                TRIGGER_SHARED_SECRET (verify_trigger_secret).
// `callHqxApi` — GET to `/api/v1/*` routes, authed with
//                HQ_X_SERVICE_TOKEN (verify_backend_x_token). These
//                are the same routes the hq-zone platform-api BFF calls;
//                the two surfaces rotate their secrets independently.
//
// Both shared-secret schemes — no JWT, no JWKS, no token caching.

const requireEnv = (name: string): string => {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} must be set in the Trigger.dev dashboard.`);
  }
  return value;
};

export interface CallHqxOptions {
  // Override the per-request fetch timeout. Defaults to 30s. The GTM
  // pipeline `/internal/gtm/*/run-step` endpoint blocks on the full
  // Anthropic round-trip, so it needs ~600s headroom.
  timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 30_000;

export async function callHqx<T = unknown>(
  path: string,
  body: unknown = {},
  options: CallHqxOptions = {},
): Promise<T> {
  const baseUrl = requireEnv("HQX_API_BASE_URL").replace(/\/$/, "");
  const secret = requireEnv("TRIGGER_SHARED_SECRET");
  const url = `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let resp: Response;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${secret}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timeout);
  }

  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(
      `hq-x ${path} failed: HTTP ${resp.status} — ${text.slice(0, 500)}`,
    );
  }
  return (text ? JSON.parse(text) : {}) as T;
}

export async function callHqxApi<T = unknown>(
  path: string,
  options: CallHqxOptions = {},
): Promise<T> {
  const baseUrl = requireEnv("HQX_API_BASE_URL").replace(/\/$/, "");
  const secret = requireEnv("HQ_X_SERVICE_TOKEN");
  const url = `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let resp: Response;
  try {
    resp = await fetch(url, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${secret}`,
        Accept: "application/json",
      },
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timeout);
  }

  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(
      `hq-x ${path} failed: HTTP ${resp.status} — ${text.slice(0, 500)}`,
    );
  }
  return (text ? JSON.parse(text) : {}) as T;
}
