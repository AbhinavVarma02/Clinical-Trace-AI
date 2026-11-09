import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { BetaApp } from "./BetaApp";
import { BetaProvider } from "./state";

// Follow the OS colour scheme (no storage and no toggle in this slice).
document.documentElement.dataset.theme =
  typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <BetaProvider>
        <BetaApp />
      </BetaProvider>
    </StrictMode>,
  );
}
