import { useEffect, useState } from "react";

import { notifyReady, readRenderMessage } from "../lib/streamlit";

/**
 * Subscribe to Streamlit render messages and return the latest args.
 * Returns null until the first render message arrives.
 */
export function useStreamlitArgs(): Record<string, unknown> | null {
  const [args, setArgs] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const payload = readRenderMessage(event.data);
      if (payload) setArgs(payload.args);
    };
    window.addEventListener("message", onMessage);
    notifyReady();
    return () => window.removeEventListener("message", onMessage);
  }, []);

  return args;
}
