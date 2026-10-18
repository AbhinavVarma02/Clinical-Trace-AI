# UI polish: less reading, clearer results

Frontend-only simplification of the React workspace (`frontend/workspace/`). No backend, model, Worker or deployment change. **Not yet published**: the public site keeps serving release `7012e8e` until the owner approves a release.

## Principle

Less reading. More interaction. Clearer results. Essential trust and safety statements stay visible; engineering depth moves behind labelled disclosures.

## What changed

| Area | Before | After |
| --- | --- | --- |
| Overview first screen | Title, 3-line subtitle, guided-case card with long paragraph, warning banner, workspace note, four stat cards | Hero: "Understand patient records. See the evidence.", one supporting sentence, a single primary **Try a guided case** button, one-line safety note, four short steps |
| Overview body | Status cards, scenario cards with patient id and question, a six-card evidence grid, model card, patients list | One status line, four short scenario cards, four headline results, a visible calibration limit, **All measurements and limits** disclosure |
| Guided steps | Two-line descriptions | One short line each; progress is still recorded only by real actions |
| Agent | Long page intro; suggestions in a side card | One-line intro; suggested questions inside the empty state; **Sources** label before citations; first 5 claims shown with **Show all N verified claims** |
| Risk | Eight stacked cards | Probability, threshold, calibration (kept visible), SHAP, caveats (kept visible); benchmark metrics and mapped inputs behind **Model details** |
| Evidence / Evaluation | Dense diagnostics subtitles and run history always open | Short subtitles; diagnostics and run history behind disclosures |
| Banners and hints | Multi-sentence availability, quota and guide messages | One or two sentences each, same meaning |

## Kept on purpose

Synthetic-data and not-clinically-validated notice (hero and sidebar), calibration limit on the first screen, deterministic versus live-LLM labelling, claim verification counts, removed-claim lists, citations with source version, human review bound to the exact artifact hash, persona isolation and audit trail. Real agent execution is unchanged; nothing is mocked or precomputed.

## Accessibility and theming

- Native `<details>` disclosures (keyboard and screen-reader operable, visible focus).
- Dark theme: primary buttons now use a dark label (`--on-accent`); white on the dark accent measured 2.8:1.
- Light theme: `--text-faint` darkened to meet 4.5:1 for small text.
- Patients page gets a real page-level `<h1>`. Every page has exactly one `<h1>`, no unnamed controls and no horizontal overflow at 390 px, in both themes (scripted audit, 8 pages x 2 viewports x 2 themes).

## Measured text reduction (visible words, 1440 px, same API)

| Page | Before | After |
| --- | --- | --- |
| Overview | 614 | 236 (-62%) |
| Risk | 401 | 261 (-35%) |
| Evidence | 58 | 39 (-33%) |
| Agent (empty) | 170 | 137 (-19%) |
| Evaluation / Patients / Operations / Governance | 1,853 | 1,746 (-6%; mostly data tables, not explanatory copy) |

## Verification

- `npm run check`: TypeScript, 41 Vitest tests, both builds.
- Playwright: 30 passed (desktop, tablet, mobile): four guided scenarios with real streaming, citations, review approval, persona isolation, cold start.
- Screenshots (before and after, desktop, tablet, mobile, dark): [docs/images/ui-polish/](images/ui-polish/).

## Releasing

When approved: `cd deploy/cloudflare && npm run release -- --full` (see [DEPLOYMENT.md](DEPLOYMENT.md)). After release, update the live-demo wording in `README.md` line 9 (**Start guided case** is now **Try a guided case**).
