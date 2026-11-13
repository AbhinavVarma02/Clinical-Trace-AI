import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Epic Beta browser entry, with a separately selected genuine owner build. Separate from Stable:
// base /beta/app/, output dist-beta/, and a Vite manifest (the Beta static seam reads
// .vite/manifest.json). No remote fonts or scripts, so it fits the planned `'self'` CSP.
export default defineConfig({
  root: "beta",
  plugins: [react()],
  base: "/beta/app/",
  build: {
    outDir: "../dist-beta",
    emptyOutDir: true,
    manifest: true,
    sourcemap: false,
  },
  server: { port: 3003, strictPort: true },
});
