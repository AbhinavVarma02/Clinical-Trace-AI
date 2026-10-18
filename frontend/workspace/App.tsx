import type { ComponentType } from "react";
import { Shell } from "./components/Shell";
import { AgentPage } from "./pages/Agent";
import { EvaluationPage } from "./pages/Evaluation";
import { EvidencePage } from "./pages/Evidence";
import { GovernancePage } from "./pages/Governance";
import { OperationsPage } from "./pages/Operations";
import { OverviewPage } from "./pages/Overview";
import { PatientsPage } from "./pages/Patients";
import { RiskPage } from "./pages/Risk";
import { useApp, type RouteName } from "./state";

const PAGES: Record<RouteName, ComponentType> = {
  overview: OverviewPage,
  patients: PatientsPage,
  agent: AgentPage,
  risk: RiskPage,
  evidence: EvidencePage,
  evaluation: EvaluationPage,
  operations: OperationsPage,
  governance: GovernancePage,
};

export function App() {
  const { route, persona, patientId } = useApp();
  const Page = PAGES[route.name];
  return (
    <Shell>
      <Page key={route.name === "agent" ? `${persona}:${patientId}` : persona} />
    </Shell>
  );
}
