import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ExplorerApp } from "./components/ExplorerApp";
import { SAMPLE_EXPLORER_DATA } from "./fixtures";
import { useAutoFrameHeight } from "./hooks/useAutoFrameHeight";
import { useStreamlitArgs } from "./hooks/useStreamlitArgs";
import { parseExplorerData } from "./lib/contributions";
import { isEmbedded } from "./lib/streamlit";
import "./styles.css";

/** Inside Streamlit: render whatever Python sends in the `explorer` kwarg. */
function StreamlitRoot() {
  const args = useStreamlitArgs();
  useAutoFrameHeight(args !== null);
  if (args === null) return null;
  return <ExplorerApp data={parseExplorerData(args.explorer)} />;
}

const container = document.getElementById("root");
if (container) {
  createRoot(container).render(
    <StrictMode>
      {isEmbedded() ? <StreamlitRoot /> : <ExplorerApp data={SAMPLE_EXPLORER_DATA} />}
    </StrictMode>,
  );
}
