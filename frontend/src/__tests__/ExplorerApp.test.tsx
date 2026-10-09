import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ExplorerApp } from "../components/ExplorerApp";
import { SAMPLE_EXPLORER_DATA } from "../fixtures";
import type { ReadyExplorerData } from "../types";

function barNames(): string[] {
  const chart = screen.getByRole("list", { name: "SHAP feature contributions" });
  return within(chart)
    .getAllByRole("button")
    .map((button) => button.querySelector(".bar-name")?.textContent ?? "");
}

describe("ExplorerApp", () => {
  it("shows probability, backend label, threshold, and the disclaimer", () => {
    render(<ExplorerApp data={SAMPLE_EXPLORER_DATA} />);
    expect(screen.getByTestId("probability")).toHaveTextContent("41.2%");
    expect(screen.getByTestId("risk-label")).toHaveTextContent("High");
    expect(screen.getByTestId("threshold")).toHaveTextContent("0.30");
    expect(screen.getByText("Dashboard tier: High")).toBeInTheDocument();
    expect(screen.getByTestId("disclaimer")).toHaveTextContent(SAMPLE_EXPLORER_DATA.disclaimer);
  });

  it("renders one bar per contribution, strongest first, with the strongest inspected by default", () => {
    render(<ExplorerApp data={SAMPLE_EXPLORER_DATA} />);
    expect(screen.getAllByTestId("contribution-bar")).toHaveLength(5);
    expect(barNames()[0]).toBe("Inpatient visits");
    expect(screen.getByTestId("inspector")).toHaveTextContent("Inpatient visits");
    expect(screen.getByTestId("inspector-contribution")).toHaveTextContent("+0.2312");
  });

  it("re-sorts and filters when the controls are used", async () => {
    const user = userEvent.setup();
    render(<ExplorerApp data={SAMPLE_EXPLORER_DATA} />);

    await user.click(screen.getByRole("button", { name: "A–Z" }));
    expect(barNames()).toEqual([
      "Age bracket",
      "Inpatient visits",
      "Insulin status: Up",
      "Lab procedures",
      "Number of diagnoses",
    ]);
    expect(screen.getByRole("button", { name: "A–Z" })).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByRole("button", { name: "Lowers first" }));
    expect(barNames()[0]).toBe("Lab procedures");

    await user.click(screen.getByRole("button", { name: "Lowers risk" }));
    expect(barNames()).toEqual(["Lab procedures", "Insulin status: Up"]);
  });

  it("inspects a feature when its bar is clicked", async () => {
    const user = userEvent.setup();
    render(<ExplorerApp data={SAMPLE_EXPLORER_DATA} />);
    await user.click(screen.getByRole("button", { name: /^Lab procedures:/ }));
    const inspector = screen.getByTestId("inspector");
    expect(inspector).toHaveTextContent("Lab procedures");
    expect(inspector).toHaveTextContent("Lowers model risk score");
    expect(inspector).toHaveTextContent("#4 of 5");
    expect(screen.getByTestId("inspector-contribution")).toHaveTextContent("−0.0356");
  });

  it("shows a low backend label below the threshold", () => {
    const low: ReadyExplorerData = { ...SAMPLE_EXPLORER_DATA, probability: 0.12, riskLabel: "low", displayRiskLabel: "Low" };
    render(<ExplorerApp data={low} />);
    expect(screen.getByTestId("risk-label")).toHaveTextContent("Low");
    expect(screen.getByText("Below the threshold")).toBeInTheDocument();
  });

  it("handles a prediction with no contributions", () => {
    render(<ExplorerApp data={{ ...SAMPLE_EXPLORER_DATA, features: [] }} />);
    expect(screen.getByTestId("no-features")).toBeInTheDocument();
    expect(screen.queryAllByTestId("contribution-bar")).toHaveLength(0);
    expect(screen.getByTestId("disclaimer")).toBeInTheDocument();
  });

  it("renders the empty state and still shows the disclaimer", () => {
    render(<ExplorerApp data={{ status: "empty", disclaimer: "This is decision-support only and not medical advice." }} />);
    expect(screen.getByTestId("empty-state")).toHaveTextContent("Awaiting prediction");
    expect(screen.getByTestId("disclaimer")).toHaveTextContent("not medical advice");
  });

  it("keeps the inspected feature when a new result still contains it", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ExplorerApp data={SAMPLE_EXPLORER_DATA} />);
    await user.click(screen.getByRole("button", { name: /^Age bracket:/ }));
    rerender(<ExplorerApp data={{ ...SAMPLE_EXPLORER_DATA, features: [...SAMPLE_EXPLORER_DATA.features] }} />);
    expect(screen.getByTestId("inspector")).toHaveTextContent("Age bracket");

    rerender(<ExplorerApp data={{ ...SAMPLE_EXPLORER_DATA, features: [{ feature: "Emergency visits", contribution: 0.05 }] }} />);
    expect(screen.getByTestId("inspector")).toHaveTextContent("Emergency visits");
  });
});
