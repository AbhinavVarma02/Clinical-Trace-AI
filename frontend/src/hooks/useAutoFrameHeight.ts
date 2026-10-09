import { useEffect } from "react";

import { setFrameHeight } from "../lib/streamlit";

/** Keep the Streamlit iframe height in sync with the rendered content. */
export function useAutoFrameHeight(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    const report = () => setFrameHeight(document.documentElement.scrollHeight);
    report();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(report);
    observer.observe(document.body);
    return () => observer.disconnect();
  }, [enabled]);
}
