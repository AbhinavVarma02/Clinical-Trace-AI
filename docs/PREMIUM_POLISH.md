# Premium visual polish

Frontend-only visual refinement of the React workspace (`frontend/workspace/`). **Released 2026-10-18 (owner-approved)** as edge release `716ea3b`, Worker version `aa8e284c-f0b3-4649-9c7f-adb61d6de175`; one live guided case verified it (see [CLOUDFLARE_ACCOUNT_MIGRATION.md](CLOUDFLARE_ACCOUNT_MIGRATION.md#premium-ui-release-716ea3b-2026-10-18-owner-approved)). No backend, API, data contract, quota, Hugging Face or evaluation change.

## Direction: "clinical ink"

The previous look was a competent but generic SaaS dashboard: a stock blue accent, a gradient "CT" logo, every block in an identical shadowed card, uppercase micro-labels and coloured pills everywhere. The new direction is quieter:

- **Palette.** Warm paper neutrals (`#f6f5f2` page, white surfaces, hairline `#e6e3dd` borders), ink text (`#1a1917`) and one petrol accent (`#0f5f73`). Colour carries meaning only: green verified, amber caution, red refused or raised risk. Gradients removed (logo, hero, meters).
- **Typography.** Inter (already loaded, no new fonts) at 600 weight for headings with tightened tracking, sentence-case labels instead of uppercase micro-labels, tabular numerals for all figures, JetBrains Mono kept for identifiers and citations.
- **Structure.** Hairline borders with near-flat elevation, consistent 12 px card radius, 8 px controls, 32 px desktop gutters, a 1280 px content measure.
- **Identity.** A small record-trace glyph (pulse ending in a cited point) replaces the "CT" gradient tile.

## What changed

| Area | Before | After |
| --- | --- | --- |
| Daily usage indicator | Full-width blue info box on every page | Quiet hairline row: a small ring gauge (visitor remaining ÷ per-visitor limit, from the live status) + the same sentences. Ring turns amber at ≤20% left. Blocked, live-AI-exhausted and waking states keep their warning/info banners and wording |
| Shell | Tinted active nav, gradient logo, plain page label in the top bar | Raised white active nav item with accent icon, trace glyph, `Workspace / Page` breadcrumb, disclaimer footer with icon |
| Overview hero | Stacked card with radial gradient; steps in a row underneath | Two-column composition: pitch, CTA and safety note on the left; the four guided steps as a vertical progress rail on the right (stacks on tablet/phone). Second headline line set in a softer tone |
| Status line, scenarios | Loose spans; generic cards | Dot-separated status line; scenario cards with accent hover |
| Measured results | Four boxed tiles inside a card | One ruled band of four figures; the calibration limit as an amber-ruled note |
| Agent answer | Each claim boxed; uppercase "SOURCES"; blue question bubble that looked like a button | Claims as a ruled list; quiet "Sources" label with outlined mono citation tags; tinted question bubble; sticky composer with soft elevation; side-panel prompts as a tidy list |
| Tables | Uppercase headers | Sentence-case headers on a sunken band; model names no longer upper-cased |
| Tabs | Accent text with inset underline | Ink text, full-width accent underline, count pills |
| Patients | Accent-coloured encounter dots similar to labs | Encounters as ink anchors; active patient marked with an accent edge |
| Risk | Gradient meter | Solid meter (accent below threshold, red above) with a ring-separated threshold mark |
| Evaluation | Retrieval table clipped in a half-width card; 40+ failed cases stretched the page to 4,300 px | Retrieval table full width; failed cases in a keyboard-scrollable panel (all cases still listed) |
| Operations | "Agent outcomes" card stretched to the neighbouring table height | Two-column grids align to the top |
| Dark theme | Blue-black | Warm neutral dark with the same petrol accent; dark label on primary buttons |

## Wording removed or simplified

The copy was already cut by the earlier polish (Overview -62%), so changes are targeted:

- Hero: "Explore synthetic clinical cases with an AI agent that explains its findings and cites its sources." → "Ask an AI agent about synthetic patient records. It answers only with claims its cited sources support." (states the actual guarantee).
- Excluded claims: two sentences → "Drafted by the model but not supported by the cited records as written, so left out of the answer. Each is listed with the verifier's reason."
- Workspace note: "Private workspace … Only this browser can see your runs. No sign-in." → "Private workspace … · visible only in this browser · no sign-in".
- Fewer shouting labels: uppercase stat, table and citation labels became sentence case.

Kept verbatim: synthetic-data and not-for-medical-use notices, the usage sentences ("23 of 25 daily agent-run attempts left for your connection. Deployment-wide: 298 remaining. Resets at 00:00 UTC. Visitors sharing a connection share its allowance."), calibration limits, deterministic/live/fallback labels, verification counts, excluded-claim reasons, review hash binding, evaluation caveats and all error/recovery messages.

## Preserved

All eight sections, routing, guided-case flow, persona switching, streaming progress, citations and evidence drawers, review submission/approval, risk, evaluation, operations, governance, and every loading/error/limit state. No API call, TypeScript type, hook, state or quota logic changed. Markup changes are presentational: a brand SVG, a breadcrumb span, the usage ring and text spans, CSS class hooks (`composer`, `answer-footer`, `limit-note`, `scroll-panel`), a stacked layout for the evaluation tables, and two `theme-color` meta tags. Strings asserted by the existing unit and browser tests are unchanged.

## Verification (what was and was not run)

- Run: `npm run typecheck` (covers `workspace/`) and a workspace Vite build to a scratch directory. Both passed.
- Visual inspection: a local preview with the backend in deterministic mode (`CLINICAL_TRACE_LLM_PROVIDER=none`, scratch copy of the local DB), a **static** `/edge/status` sample, and every agent-execution POST refused by the preview server. Stored runs and reviews were opened read-only.
- Scripted checks at 375 px in both themes on all eight pages: no horizontal overflow, top bar fits with the longest persona, one `<h1>` per page, no unnamed controls. Token contrast measured ≥ 4.5:1 for every text/background pair in both themes.
- **Not run (deliberately):** Vitest, Playwright, Python tests, evaluations, guided cases, or anything against production. Zero inference calls and zero agent executions. This is not a full regression validation.

## Screenshots

[images/premium-polish/](images/premium-polish/): `live-*` from the public site after release (read-only, reusing the single guided run), and `before-*` / `after-*` pairs for desktop (1440) and mobile (390), light and dark: overview, agent answer, empty agent, evidence drawer, patients, risk, evidence explorer, evaluation, operations, governance.
