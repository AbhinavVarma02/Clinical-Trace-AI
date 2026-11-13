import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { localOwnerClient, OWNER_ORIGIN, OWNER_RP_ID } from "../webauthn";

const ORIGIN = "https://localhost:8443";
const create = vi.fn();
const get = vi.fn();
const parseCreation = vi.fn((value) => value);
const parseRequest = vi.fn((value) => value);
const fetcher = vi.fn();
const signed = { id: "explicitly-synthetic-test-credential", type: "public-key" };

beforeEach(() => {
  vi.stubGlobal("window", { isSecureContext: true, location: { origin: ORIGIN } });
  vi.stubGlobal("navigator", { credentials: { create, get } });
  vi.stubGlobal("PublicKeyCredential", { parseCreationOptionsFromJSON: parseCreation, parseRequestOptionsFromJSON: parseRequest });
  vi.stubGlobal("fetch", fetcher);
  create.mockResolvedValue({ type: "public-key", toJSON: () => signed });
  get.mockResolvedValue({ type: "public-key", toJSON: () => signed });
  fetcher.mockImplementation(async (path: string) => ({
    ok: true, status: 200,
    json: async () => path.endsWith("options")
      ? { flow_id: "synthetic-flow", publicKey: { challenge: "synthetic-challenge", userVerification: "required" } }
      : { authenticated: true },
  }));
});

afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

describe("optional local WebAuthn client", () => {
  it("uses native registration JSON APIs and a provisioner grant", async () => {
    await localOwnerClient().enroll("synthetic-out-of-band-grant");
    expect(parseCreation).toHaveBeenCalledOnce();
    expect(create).toHaveBeenCalledWith({ publicKey: { challenge: "synthetic-challenge", userVerification: "required" } });
    expect(get).not.toHaveBeenCalled();
    expect(fetcher.mock.calls[0]?.[1].headers["X-Enrollment-Grant"]).toBe("synthetic-out-of-band-grant");
    expect(JSON.parse(fetcher.mock.calls[1]?.[1].body)).toEqual({ flow_id: "synthetic-flow", credential: signed });
  });

  it.each(["login", "step-up"])("uses native assertion JSON APIs for %s", async (purpose) => {
    const client = localOwnerClient();
    await (purpose === "login" ? client.signIn() : client.stepUp("synthetic-csrf"));
    expect(parseRequest).toHaveBeenCalledOnce();
    expect(get).toHaveBeenCalledOnce();
    expect(create).not.toHaveBeenCalled();
    for (const [path, init] of fetcher.mock.calls) {
      expect(path).toMatch(/^\/owner\/auth\//);
      expect(init).toMatchObject({ credentials: "same-origin", mode: "same-origin", cache: "no-store", redirect: "error" });
      if (purpose === "step-up") expect(init.headers["X-CSRF-Token"]).toBe("synthetic-csrf");
    }
  });

  it.each(["insecure", "wrong-origin", "http"])("refuses %s before touching an authenticator or network", async (attack) => {
    vi.stubGlobal("window", { isSecureContext: attack !== "insecure", location: { origin: attack === "wrong-origin" ? "https://other.invalid" : ORIGIN } });
    const client = localOwnerClient(attack === "http" ? "http://localhost:8443" : ORIGIN);
    await expect(client.signIn()).rejects.toThrow("Trusted local HTTPS");
    expect(fetcher).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
  });

  it("fails closed when the browser has no standard JSON support", async () => {
    vi.stubGlobal("PublicKeyCredential", {});
    await expect(localOwnerClient().signIn()).rejects.toThrow("Standard WebAuthn JSON");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not submit cancelled credentials", async () => {
    get.mockResolvedValue(null);
    await expect(localOwnerClient().signIn()).rejects.toThrow("WebAuthn credential");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("returns no invented session after a server refusal", async () => {
    fetcher.mockResolvedValue({ ok: false, status: 401 });
    await expect(localOwnerClient().signIn()).rejects.toThrow("authentication failed");
    expect(get).not.toHaveBeenCalled();
  });

  it("uses CSRF for sign-out and handles an empty response", async () => {
    fetcher.mockResolvedValue({ ok: true, status: 204 });
    expect(await localOwnerClient().signOut("synthetic-csrf")).toBeUndefined();
    expect(fetcher.mock.calls[0]?.[1].headers["X-CSRF-Token"]).toBe("synthetic-csrf");
    expect(get).not.toHaveBeenCalled();
  });
});

describe("dedicated offline owner contract (mocked browser only)", () => {
  beforeEach(() => {
    vi.stubGlobal("window", { isSecureContext: true, location: { origin: OWNER_ORIGIN } });
  });

  it("uses the exact dedicated RP for native registration and assertion", async () => {
    fetcher.mockImplementation(async (path: string) => ({
      ok: true, status: 200,
      json: async () => path.endsWith("options") ? {
        flow_id: "synthetic-flow",
        publicKey: path.includes("register") ? { rp: { id: OWNER_RP_ID } } : { rpId: OWNER_RP_ID },
      } : { authenticated: true },
    }));
    await localOwnerClient(OWNER_ORIGIN).enroll("synthetic-console-grant");
    await localOwnerClient(OWNER_ORIGIN).signIn();
    expect(create).toHaveBeenCalledWith({ publicKey: { rp: { id: OWNER_RP_ID } } });
    expect(get).toHaveBeenCalledWith({ publicKey: { rpId: OWNER_RP_ID } });
    expect(fetcher).toHaveBeenCalledTimes(4);
  });

  it.each(["register", "login"])("rejects a different RP before native %s", async (purpose) => {
    fetcher.mockResolvedValue({ ok: true, status: 200, json: async () => ({
      flow_id: "synthetic-flow", publicKey: { rp: { id: "localhost" }, rpId: "localhost" },
    }) });
    const client = localOwnerClient(OWNER_ORIGIN);
    await expect(purpose === "register" ? client.enroll("synthetic-grant") : client.signIn()).rejects.toThrow("Pinned owner RP");
    expect(create).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each([
    "https://owner.clinical-trace.localhost:9443", "https://owner.clinical-trace.localhost",
    "http://owner.clinical-trace.localhost:8443", "https://other.clinical-trace.localhost:8443",
    "https://owner.clinical-trace.localhost:8443/",
  ])("rejects noncontract origin %s before I/O", async (origin) => {
    vi.stubGlobal("window", { isSecureContext: true, location: { origin } });
    await expect(localOwnerClient(origin).signIn()).rejects.toThrow("Trusted local HTTPS");
    expect(fetcher).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
  });
});
