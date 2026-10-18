// Guided case: the four-step path a first-time reviewer follows in about 90 seconds.
// Progress is recorded only when the real action happens (a completed agent run, an opened
// citation, a submitted review, a reviewer approval); nothing is simulated or pre-filled.
import { useSyncExternalStore } from "react";

export type GuideStep = "run" | "citation" | "submit" | "approve";

export const GUIDE_STEPS: { id: GuideStep; title: string; detail: string }[] = [
  { id: "run", title: "Run the agent", detail: "Ask about a synthetic readmission journey and watch real tool calls stream in." },
  { id: "citation", title: "Check a citation", detail: "Open a numbered citation to see the exact source record and version." },
  { id: "submit", title: "Submit for review", detail: "Send the verified answer to the hash-bound human review queue." },
  { id: "approve", title: "Approve as reviewer", detail: "Switch to the Demo Reviewer persona and record a decision." },
];

export const GUIDE_CASE = {
  patientId: "synthetic_p001",
  question: "Summarize this patient journey",
};

interface GuideState {
  done: GuideStep[];
  dismissed: boolean;
  /** The guided run, so "continue" can reopen it after navigating away. */
  runId?: string;
}

const KEY = "ct-guide";
const listeners = new Set<() => void>();
let state: GuideState = load();

function load(): GuideState {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(KEY) ?? "null") as Partial<GuideState> | null;
    if (parsed && Array.isArray(parsed.done)) {
      return { done: parsed.done.filter((s): s is GuideStep => GUIDE_STEPS.some((g) => g.id === s)), dismissed: Boolean(parsed.dismissed),
        runId: typeof parsed.runId === "string" ? parsed.runId : undefined };
    }
  } catch {
    // Storage unavailable or corrupt: start fresh.
  }
  return { done: [], dismissed: false };
}

function save(next: GuideState): void {
  state = next;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // Progress then lasts for this page load only.
  }
  listeners.forEach((listener) => listener());
}

export function markGuideStep(step: GuideStep, runId?: string): void {
  if (!state.done.includes(step)) save({ ...state, done: [...state.done, step], ...(runId ? { runId } : {}) });
}

export function dismissGuide(dismissed = true): void {
  save({ ...state, dismissed });
}

export function resetGuide(): void {
  save({ done: [], dismissed: false });
}

/** Re-read storage (tests and workspace resets). */
export function reloadGuide(): void {
  state = load();
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useGuide(): GuideState & { next: (typeof GUIDE_STEPS)[number] | null; complete: boolean } {
  const current = useSyncExternalStore(subscribe, () => state, () => state);
  const next = GUIDE_STEPS.find((s) => !current.done.includes(s.id)) ?? null;
  return { ...current, next, complete: next === null };
}
