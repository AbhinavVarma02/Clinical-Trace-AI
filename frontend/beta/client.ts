// Beta client seam. This slice ships only the fixture implementation: it performs no network
// request, reads no cookie or storage, and holds no CSRF token. A later, separately approved
// slice can supply a same-origin implementation of this interface for the bootstrap reads
// (`GET /beta/auth/session`, `GET /beta/epic/patients`) and for status; no record route exists yet.
import {
  PATIENT_CHOICES,
  RECORDS,
  STATUSES,
  type FixtureRecord,
  type FixtureStatus,
  type PatientChoice,
} from "./fixtures";

export interface BetaClient {
  readonly kind: "fixture";
  listPatients(): Promise<PatientChoice[]>;
  getRecord(alias: string): Promise<FixtureRecord>;
  getStatus(alias: string): Promise<FixtureStatus>;
}

export class UnknownPatientError extends Error {
  constructor() {
    super("patient_unavailable");
  }
}

function lookup<T>(table: Record<string, T>, alias: string): T {
  // Own-property check: aliases come from UI state and must never reach prototype members.
  if (!Object.prototype.hasOwnProperty.call(table, alias)) throw new UnknownPatientError();
  return structuredClone(table[alias] as T);
}

export const fixtureClient: BetaClient = {
  kind: "fixture",
  listPatients: async () => structuredClone(PATIENT_CHOICES),
  getRecord: async (alias) => lookup(RECORDS, alias),
  getStatus: async (alias) => lookup(STATUSES, alias),
};
