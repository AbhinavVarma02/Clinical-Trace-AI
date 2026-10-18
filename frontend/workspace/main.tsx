import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AvailabilityProvider } from "./edge";
import { AppProvider } from "./state";
import "./styles.css";

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <AvailabilityProvider>
        <AppProvider>
          <App />
        </AppProvider>
      </AvailabilityProvider>
    </StrictMode>,
  );
}
