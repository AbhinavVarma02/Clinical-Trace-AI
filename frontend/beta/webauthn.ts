// Standard local-owner browser helpers. Historical cookie client stays unmounted; genuine entry uses only
// the cookie-free helper. No certificate bypass or credential storage.
// Binary encoding and WebAuthn operations use the browser's standard JSON/WebAuthn APIs exclusively.
type Options = { flow_id: string; publicKey: unknown };
type JsonCredential = PublicKeyCredential & { toJSON(): unknown };
type JsonAPI = typeof PublicKeyCredential & {
  parseCreationOptionsFromJSON(value: unknown): PublicKeyCredentialCreationOptions;
  parseRequestOptionsFromJSON(value: unknown): PublicKeyCredentialRequestOptions;
};

// Pinned owner origin. Historical bearer-cookie serving still lacks all-port isolation; genuine uses no cookies.
export const OWNER_ORIGIN = "https://owner.clinical-trace.localhost:8443";
export const OWNER_RP_ID = "owner.clinical-trace.localhost";

/** Shared standard-browser conversion/ceremony for the separately enabled genuine integration. */
export async function genuineOwnerCredential(options: unknown, register = false, signal?: AbortSignal): Promise<unknown> {
  if (!window.isSecureContext || window.location.origin !== OWNER_ORIGIN) throw new Error("Trusted owner HTTPS is required");
  if (signal?.aborted || document.visibilityState !== "visible" || !document.hasFocus()) throw new Error("Focused owner window is required");
  const api = PublicKeyCredential as JsonAPI;
  let credential: JsonCredential | null;
  if (register) {
    if (typeof api.parseCreationOptionsFromJSON !== "function") throw new Error("Standard WebAuthn JSON support is required");
    const publicKey = api.parseCreationOptionsFromJSON(options);
    if (publicKey.rp.id !== OWNER_RP_ID || publicKey.authenticatorSelection?.userVerification !== "required" || publicKey.authenticatorSelection.authenticatorAttachment !== "platform") throw new Error("Pinned platform UV policy is required");
    credential = await navigator.credentials.create({ publicKey, ...(signal ? { signal } : {}) }) as JsonCredential | null;
  } else {
    if (typeof api.parseRequestOptionsFromJSON !== "function") throw new Error("Standard WebAuthn JSON support is required");
    const publicKey = api.parseRequestOptionsFromJSON(options);
    if (publicKey.rpId !== OWNER_RP_ID || publicKey.userVerification !== "required") throw new Error("Pinned owner UV policy is required");
    credential = await navigator.credentials.get({ publicKey, ...(signal ? { signal } : {}) }) as JsonCredential | null;
  }
  if (signal?.aborted || document.visibilityState !== "visible" || !document.hasFocus() || window.location.origin !== OWNER_ORIGIN || credential?.type !== "public-key" || typeof credential.toJSON !== "function") throw new Error("Owner credential is required");
  return credential.toJSON();
}

export function localOwnerClient(origin = "https://localhost:8443") {
  function secure() {
    const url = new URL(origin);
    if (!window.isSecureContext || window.location.origin !== origin || url.protocol !== "https:" || (url.hostname !== "localhost" && origin !== OWNER_ORIGIN) || url.origin !== origin) {
      throw new Error("Trusted local HTTPS is required");
    }
  }

  async function request(path: string, body?: unknown, csrf?: string, grant?: string): Promise<unknown> {
    secure();
    const response = await fetch(`/owner/auth/${path}`, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin", mode: "same-origin", cache: "no-store", redirect: "error",
      headers: {
        "X-Owner-Client": "webauthn-v1", Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(csrf === undefined ? {} : { "X-CSRF-Token": csrf }),
        ...(grant === undefined ? {} : { "X-Enrollment-Grant": grant }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) throw new Error("Local owner authentication failed");
    return response.status === 204 ? undefined : response.json();
  }

  async function ceremony(purpose: "register" | "login" | "step-up", csrf?: string, grant?: string) {
    secure();
    const api = PublicKeyCredential as JsonAPI;
    if (typeof api.parseCreationOptionsFromJSON !== "function" || typeof api.parseRequestOptionsFromJSON !== "function") {
      throw new Error("Standard WebAuthn JSON support is required");
    }
    const options = await request(`${purpose}/options`, {}, csrf, grant) as Options;
    secure();
    let credential: JsonCredential | null;
    if (purpose === "register") {
      const publicKey = api.parseCreationOptionsFromJSON(options.publicKey);
      if (origin === OWNER_ORIGIN && publicKey.rp.id !== OWNER_RP_ID) throw new Error("Pinned owner RP is required");
      credential = await navigator.credentials.create({ publicKey }) as JsonCredential | null;
    } else {
      const publicKey = api.parseRequestOptionsFromJSON(options.publicKey);
      if (origin === OWNER_ORIGIN && publicKey.rpId !== OWNER_RP_ID) throw new Error("Pinned owner RP is required");
      credential = await navigator.credentials.get({ publicKey }) as JsonCredential | null;
    }
    if (credential?.type !== "public-key" || typeof credential.toJSON !== "function") {
      throw new Error("A WebAuthn credential is required");
    }
    return request(`${purpose}/finish`, { flow_id: options.flow_id, credential: credential.toJSON() }, csrf);
  }

  return {
    // Grant is supplied by the future approved local provisioner, never issued by an unauthenticated API.
    enroll: (grant: string) => ceremony("register", undefined, grant),
    signIn: () => ceremony("login"),
    stepUp: (csrf: string) => ceremony("step-up", csrf),
    session: () => request("session"),
    signOut: (csrf: string) => request("logout", {}, csrf),
    revoke: (csrf: string) => request("revoke", {}, csrf),
  };
}
