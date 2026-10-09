import { describe, expect, it, vi } from "vitest";

import { COMPONENT_API_VERSION, notifyReady, readRenderMessage, setFrameHeight } from "../lib/streamlit";

describe("Streamlit component bridge", () => {
  it("announces readiness with the v1 component API version", () => {
    const target = { postMessage: vi.fn() };
    notifyReady(target);
    expect(target.postMessage).toHaveBeenCalledWith(
      { isStreamlitMessage: true, type: "streamlit:componentReady", apiVersion: COMPONENT_API_VERSION },
      "*",
    );
  });

  it("reports an integer frame height", () => {
    const target = { postMessage: vi.fn() };
    setFrameHeight(412.3, target);
    expect(target.postMessage).toHaveBeenCalledWith(
      { isStreamlitMessage: true, type: "streamlit:setFrameHeight", height: 413 },
      "*",
    );
  });

  it("parses render messages and ignores everything else", () => {
    expect(readRenderMessage({ type: "streamlit:render", args: { explorer: { status: "empty" } } })).toEqual({
      args: { explorer: { status: "empty" } },
      disabled: false,
    });
    expect(readRenderMessage({ type: "streamlit:render" })).toEqual({ args: {}, disabled: false });
    expect(readRenderMessage({ type: "other" })).toBeNull();
    expect(readRenderMessage("streamlit:render")).toBeNull();
    expect(readRenderMessage(null)).toBeNull();
  });
});
