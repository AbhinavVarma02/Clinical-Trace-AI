import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Clinical workspace (React + TypeScript). Built to dist-workspace/ and served by
// FastAPI at /app. Node/Vite are build tooling only; no Node server runs in production.
// Paths are relative to frontend/ (npm scripts run there); outDir is relative to root.
export default defineConfig({
  root: "workspace",
  plugins: [react()],
  base: "/app/",
  build: {
    outDir: "../dist-workspace",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 3002,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/fhir": "http://127.0.0.1:8000",
    },
  },
});
