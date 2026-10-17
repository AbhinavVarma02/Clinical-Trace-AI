# ADR 0006: Workspace frontend without new runtime dependencies

**Status:** accepted (2026-10-17)

**Context.** The preferred stack mentions Tailwind, a component library, TanStack Query, and Recharts. The repository already pins React 19, Vite 8, Vitest, and Testing Library for the SHAP explorer.

**Decision.** Build the workspace in the existing `frontend/` package as a second Vite entry (`vite.workspace.config.ts`, root `workspace/`), with hand-written CSS design tokens (light/dark), a small typed fetch hook instead of TanStack Query, hash routing, and inline SVG charts.

**Consequences.** No new npm dependencies or lockfile churn; 90 KB gzipped JS. More CSS and chart code is maintained in-repo. Playwright end-to-end tests are still to be added.
