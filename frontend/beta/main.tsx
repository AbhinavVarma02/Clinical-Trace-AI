import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

// Follow the OS colour scheme (no storage and no toggle in this slice).
document.documentElement.dataset.theme =
  typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

const root = document.getElementById("root");
if (root) {
  // Keep the builds isolated, including the module initializers and fixture data in their imports.
  const page = import.meta.env.VITE_BETA_CLIENT === "genuine-owner"
    ? import("./GenuineBetaApp").then(({ GenuineBetaApp }) => <GenuineBetaApp />)
    : import.meta.env.VITE_BETA_CLIENT === "published"
    ? import("./PublishedBetaApp").then(({ PublishedBetaApp }) => <PublishedBetaApp />)
    : Promise.all([import("./BetaApp"), import("./client"), import("./state")]).then(
      ([{ BetaApp }, { fixtureClient, httpClient }, { BetaProvider }]) => (
        <BetaProvider client={import.meta.env.VITE_BETA_CLIENT === "fixture" ? fixtureClient : httpClient}>
          <BetaApp />
        </BetaProvider>
      ),
    );
  void page.then((app) => createRoot(root).render(<StrictMode>{app}</StrictMode>));
}
