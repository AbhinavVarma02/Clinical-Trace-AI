/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Node.js/Vite are build tooling only. `npm run build` emits static files into
// dist/, which Streamlit serves itself via components.declare_component(path=...).
// No Node server runs in production.
export default defineConfig({
  plugins: [react()],
  // Streamlit serves the component under /component/<name>/, so asset URLs
  // must be relative to index.html rather than absolute.
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 3001,
    strictPort: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/setupTests.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
