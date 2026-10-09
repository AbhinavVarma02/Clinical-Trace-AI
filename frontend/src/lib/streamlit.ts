/**
 * Minimal typed bridge for Streamlit's custom-component (components.v1) iframe
 * protocol. Python mounts the component with
 * ``components.declare_component(path=frontend/dist)``; Streamlit then talks to
 * this iframe with window.postMessage:
 *
 *   iframe -> Streamlit: streamlit:componentReady, streamlit:setFrameHeight
 *   Streamlit -> iframe: streamlit:render  (carries the Python kwargs as args)
 *
 * The explorer is display-only, so it never sends a component value back and
 * never triggers a Streamlit rerun.
 */

export const COMPONENT_API_VERSION = 1;

export const MessageType = {
  componentReady: "streamlit:componentReady",
  setFrameHeight: "streamlit:setFrameHeight",
  render: "streamlit:render",
} as const;

export interface RenderPayload {
  args: Record<string, unknown>;
  disabled: boolean;
}

type Target = Pick<Window, "postMessage">;

function send(target: Target, type: string, data: Record<string, unknown> = {}): void {
  target.postMessage({ isStreamlitMessage: true, type, ...data }, "*");
}

export function notifyReady(target: Target = window.parent): void {
  send(target, MessageType.componentReady, { apiVersion: COMPONENT_API_VERSION });
}

export function setFrameHeight(height: number, target: Target = window.parent): void {
  send(target, MessageType.setFrameHeight, { height: Math.ceil(height) });
}

/** Extract a render payload from a message event, or null if it is not one. */
export function readRenderMessage(data: unknown): RenderPayload | null {
  if (typeof data !== "object" || data === null) return null;
  const message = data as Record<string, unknown>;
  if (message.type !== MessageType.render) return null;
  const args = message.args;
  return {
    args: typeof args === "object" && args !== null ? (args as Record<string, unknown>) : {},
    disabled: Boolean(message.disabled),
  };
}

/** True when the page is running inside Streamlit's component iframe. */
export function isEmbedded(): boolean {
  try {
    return window.parent !== window;
  } catch {
    return true;
  }
}
