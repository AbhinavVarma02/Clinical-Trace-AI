// Mocked standard browser/transport contract only. No authenticator, browser, genuine evidence or provider.
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { completeOwnerEnrollment, genuineOwnerActions, genuineOwnerOperation, genuineOwnerRead, pendingOwnerEnrollment } from "../genuine";
import { OWNER_ORIGIN, OWNER_RP_ID } from "../webauthn";

const fetcher = vi.fn();
const get = vi.fn();
const create = vi.fn();
const signed = { type: "public-key", toJSON: () => ({ test: "INVENTED SOFTWARE CONTRACT" }) };
let result: Record<string, unknown>;
let now: number;
let windowEvents: EventTarget;
const source = "epic-fhir-sandbox-r4";
beforeEach(() => {
  now = 100000;
  vi.spyOn(Date, "now").mockImplementation(() => now);
  windowEvents = new EventTarget();
  const documentEvents = new EventTarget();
  vi.stubGlobal("window", { isSecureContext: true, location: { origin: OWNER_ORIGIN }, addEventListener: windowEvents.addEventListener.bind(windowEvents), removeEventListener: windowEvents.removeEventListener.bind(windowEvents) });
  vi.stubGlobal("document", { visibilityState: "visible", hasFocus: () => true, addEventListener: documentEvents.addEventListener.bind(documentEvents), removeEventListener: documentEvents.removeEventListener.bind(documentEvents) });
  vi.stubGlobal("navigator", { credentials: { get, create } });
  vi.stubGlobal("PublicKeyCredential", { parseCreationOptionsFromJSON: (value: unknown) => value, parseRequestOptionsFromJSON: (value: unknown) => value });
  vi.stubGlobal("fetch", fetcher);
  get.mockResolvedValue(signed); create.mockResolvedValue(signed);
  // Deliberate CONTRACT DOUBLE, not an API/provider acquired record and never evidence of genuine serving.
  result = { mode: "LIVE", source, data_origin: "epic_sandbox", alias: "approved-alpha", serving: "fresh", freshness: "fresh",
    completeness: "unconfirmed", publication: "disabled", record: { contract_double: true }, evidence_error: null,
    active: { generation_id: "a".repeat(32) }, provenance: { mode: "LIVE", source, generation_id: "a".repeat(32), synthetic_transport: false, manifest_sha256: "b".repeat(64), bundle_sha256: "c".repeat(64) } };
  fetcher.mockImplementation(async (path: string) => ({ ok: true, json: async () => path.endsWith("options")
    ? { flow_id: "f".repeat(43), publicKey: { rpId: OWNER_RP_ID, userVerification: "required" } }
    : { result: path.includes("verify") ? { authenticated: true } : result, server_time: 100, display_until: 130 } }));
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.clearAllMocks(); });

it("requires a new standard WebAuthn proof for every direct read, omitting cookies", async () => {
  await genuineOwnerRead("read"); await genuineOwnerRead("read");
  expect(get).toHaveBeenCalledTimes(2);
  expect(fetcher).toHaveBeenCalledTimes(4);
  for (const [path, init] of fetcher.mock.calls) {
    expect(path).toMatch(/^\/owner\/genuine\/read\/(options|finish)$/);
    expect(init).toMatchObject({ credentials: "omit", mode: "same-origin", cache: "no-store", redirect: "error", method: "POST" });
    expect(Object.keys(init.headers)).toEqual(["X-Owner-Client", "Accept", "Content-Type"]);
  }
});

it.each(["synthetic", "mode", "source", "generation", "hash", "serving"])("refuses the %s response contract", async (attack) => {
  if (attack === "synthetic") result.data_origin = "synthetic_live_fixture";
  if (attack === "mode") result.mode = "MOCK";
  if (attack === "source") result.source = "wrong";
  if (attack === "generation") (result.provenance as Record<string, unknown>).generation_id = "d".repeat(32);
  if (attack === "hash") (result.provenance as Record<string, unknown>).bundle_sha256 = "";
  if (attack === "serving") result.serving = "unavailable";
  await expect(genuineOwnerRead("read")).rejects.toThrow("Genuine record refused");
});

it.each(["insecure", "other-port", "cancelled", "no-json", "wrong-rp", "no-uv", "unfocused-after-Hello"])("fails closed for %s", async (attack) => {
  if (attack === "insecure") vi.stubGlobal("window", { isSecureContext: false, location: { origin: OWNER_ORIGIN } });
  if (attack === "other-port") vi.stubGlobal("window", { isSecureContext: true, location: { origin: OWNER_ORIGIN.replace(":8443", ":9443") } });
  if (attack === "cancelled") get.mockResolvedValue(null);
  if (attack === "no-json") vi.stubGlobal("PublicKeyCredential", {});
  if (attack === "wrong-rp" || attack === "no-uv") fetcher.mockResolvedValue({ ok: true, json: async () => ({ flow_id: "f".repeat(43), publicKey: { rpId: attack === "wrong-rp" ? "localhost" : OWNER_RP_ID, userVerification: attack === "no-uv" ? "preferred" : "required" } }) });
  if (attack === "unfocused-after-Hello") get.mockImplementation(async () => { vi.stubGlobal("document", { visibilityState: "visible", hasFocus: () => false }); return signed; });
  await expect(genuineOwnerRead("read")).rejects.toThrow();
  expect(fetcher.mock.calls.some(([path]) => String(path).endsWith("finish"))).toBe(false);
});

it.each(["latency", "unfocused-response", "hidden-response"])("rejects the %s result before admission", async (attack) => {
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation(async (...args) => {
    const value = await original(...args);
    if (String(args[0]).endsWith("finish")) {
      if (attack === "latency") now = 131000;
      else vi.stubGlobal("document", { visibilityState: attack === "hidden-response" ? "hidden" : "visible", hasFocus: () => attack !== "unfocused-response" });
    }
    return value;
  });
  await expect(genuineOwnerRead("read")).rejects.toThrow("Owner proof expired");
});

it("keeps enrollment grants internal, carrying only an unprivileged browser binding", async () => {
  fetcher.mockImplementation(async (path: string) => ({ ok: true, json: async () => path.endsWith("pending")
    ? { pending_id: "p".repeat(43), fingerprint: "a".repeat(64) }
    : path.endsWith("options") ? { flow_id: "f".repeat(43), publicKey: { rp: { id: OWNER_RP_ID }, authenticatorSelection: { authenticatorAttachment: "platform", userVerification: "required" } } }
    : { enrolled: true } }));
  const pending = await pendingOwnerEnrollment();
  await completeOwnerEnrollment(pending);
  expect(create).toHaveBeenCalledOnce();
  for (const [, init] of fetcher.mock.calls) { expect(init.credentials).toBe("omit"); expect(init.body).not.toMatch(/grant|csrf|session/); }
});

it.each(["clear", "blur-return"])("cancels delayed options before native Hello after %s", async (change) => {
  const controller = new AbortController();
  fetcher.mockImplementationOnce(async () => {
    if (change === "clear") controller.abort();
    else { windowEvents.dispatchEvent(new Event("blur")); windowEvents.dispatchEvent(new Event("focus")); }
    return { ok: true, json: async () => ({ flow_id: "f".repeat(43), publicKey: { rpId: OWNER_RP_ID, userVerification: "required" } }) };
  });
  await expect(genuineOwnerRead("read", controller.signal)).rejects.toThrow("cancelled");
  expect(get).not.toHaveBeenCalled();
});

it("rejects an in-flight finish across blur and return even when focused again", async () => {
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation(async (...args) => {
    const value = await original(...args);
    if (String(args[0]).endsWith("finish")) { windowEvents.dispatchEvent(new Event("blur")); windowEvents.dispatchEvent(new Event("focus")); }
    return value;
  });
  await expect(genuineOwnerRead("read")).rejects.toThrow("cancelled");
});

function candidate(operation: "acquire" | "promote") {
  const provenance = { attempt_id: "a".repeat(32), generation_id: "b".repeat(32), manifest_sha256: "c".repeat(64), bundle_sha256: "d".repeat(64) };
  result = { operation, alias: "approved", patient_identifier: "invented-contract", synthetic_transport: false, provenance,
    live_serving: "disabled", clinical_ai: "disabled", review: { mode: "LIVE", source, policy: "invented", completeness: "unconfirmed", retrieved_at: 100, resource_counts: { Patient: 1 }, unresolved_references: 0, note_bodies: "not_imported", operations: {},
      patient_display_name: "Invented contract patient", document_metadata_count: 0, http_outcomes: { Patient: [200] }, source_timestamps: {},
      mapping: { mapping_version: "invented", received: {}, mapped: {}, rejected: {}, field_status: {}, skipped_types: {}, dangling_references: 0 } } };
  return provenance;
}

it("requires a fresh proof for promotion and sends only the exact reviewed candidate binding", async () => {
  const provenance = candidate("promote");
  await expect(genuineOwnerOperation("promote", null)).rejects.toThrow("Review the exact candidate");
  expect(fetcher).not.toHaveBeenCalled();
  await expect(genuineOwnerOperation("promote", provenance)).resolves.toMatchObject({ result: { provenance } });
  expect(get).toHaveBeenCalledOnce();
  expect(fetcher).toHaveBeenNthCalledWith(1, "/owner/genuine/promote/options", expect.objectContaining({ credentials: "omit", body: JSON.stringify(provenance) }));
  expect(fetcher).toHaveBeenNthCalledWith(2, "/owner/genuine/promote/finish", expect.objectContaining({ credentials: "omit", body: JSON.stringify({ flow_id: "f".repeat(43), credential: signed.toJSON() }) }));
  result.provenance = { ...provenance, bundle_sha256: "e".repeat(64) };
  await expect(genuineOwnerOperation("promote", provenance)).rejects.toThrow("Genuine candidate binding changed");
});

it("admits a long acquisition receipt only within its same-host post-acquisition deadline", async () => {
  const provenance = candidate("acquire");
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation(async (...args) => {
    if (!String(args[0]).endsWith("finish")) return original(...args);
    now = 150000;
    return { ok: true, json: async () => ({ result, server_time: 145, display_until: 175 }) };
  });
  await expect(genuineOwnerOperation("acquire", null)).resolves.toMatchObject({ result: { provenance }, expiresAt: 175000 });
  fetcher.mockImplementation(async (...args) => String(args[0]).endsWith("finish")
    ? { ok: true, json: async () => ({ result, server_time: 110, display_until: 140 }) }
    : original(...args));
  await expect(genuineOwnerOperation("acquire", null)).rejects.toThrow("Owner proof expired");
});

it("discovers only the enabled coordinator actions without background authentication", async () => {
  fetcher.mockResolvedValueOnce({ ok: true, json: async () => ({ actions: ["review", "promote"] }) });
  await expect(genuineOwnerActions()).resolves.toEqual(["review", "promote"]);
  expect(fetcher).toHaveBeenCalledWith("/owner/genuine/operations", expect.objectContaining({ credentials: "omit", body: "{}" }));
  fetcher.mockResolvedValueOnce({ ok: true, json: async () => ({ actions: ["acquire", "promote"] }) });
  await expect(genuineOwnerActions()).rejects.toThrow("Owner actions refused");
  expect(get).not.toHaveBeenCalled();
});
