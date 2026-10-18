import { api } from "../api";
import { Badge, Card, EmptyState, ErrorState, Skeleton, Stat, formatDate, ms, pct } from "../components/ui";
import { useApi, useApp } from "../state";

export function OperationsPage() {
  const { persona, setPersona } = useApp();
  const ops = useApi(api.ops, "ops");
  const m = ops.data;
  return (
    <>
      <div className="page-header">
        <div>
          <h1 className="page-title">Operations</h1>
          <p className="page-subtitle">Live request, agent and tool timings from this server. Identifiers and timings only, never record text.</p>
        </div>
        <button type="button" className="btn sm" onClick={ops.reload}>Refresh</button>
      </div>
      {ops.error?.status === 403 && (
        <Card>
          <EmptyState
            title="Operations data is limited to the administrator persona"
            action={persona !== "admin.demo" && <button type="button" className="btn primary sm" onClick={() => setPersona("admin.demo")}>Switch to Demo Administrator</button>}
          >
            Administrators can see platform telemetry and audit events but cannot read patient records.
          </EmptyState>
        </Card>
      )}
      {ops.error && ops.error.status !== 403 && <Card><ErrorState error={ops.error} onRetry={ops.reload} /></Card>}
      {ops.loading && <Card><Skeleton lines={6} /></Card>}
      {m && (
        <>
          <div className="grid cols-4">
            <Stat label="Requests observed" value={m.window.requests} note={`as of ${formatDate(m.generated_at, true)}`} />
            <Stat label="Agent runs" value={m.window.agent_runs} note={`p50 ${ms(m.agent.p50_ms)} · p95 ${ms(m.agent.p95_ms)}`} />
            <Stat label="Inference provider" value={m.provider.provider} note={m.provider.model ? `${m.provider.model} · circuit ${m.provider.circuit}` : "deterministic mode"} />
            <Stat label="Rate limiting" value={`${m.rate_limit.agent_runs_per_minute}/min`} note={`agent runs · ${m.rate_limit.requests_per_minute}/min other · ${m.rate_limit.rejected_since_start} rejected`} />
          </div>
          <div className="grid cols-2">
            <Card title="Agent outcomes">
              <div className="row">
                {Object.entries(m.agent.outcomes).map(([outcome, count]) => <Badge key={outcome} tone="outline">{outcome}: {count}</Badge>)}
                {Object.keys(m.agent.outcomes).length === 0 && <span className="muted">No agent runs yet.</span>}
              </div>
              <p className="faint" style={{ fontSize: 12, marginTop: 10 }}>
                Retrieval index {m.retrieval.index_version} · {m.retrieval.chunks} chunks · cache {m.retrieval.cache_hits} hits / {m.retrieval.cache_misses} misses
              </p>
            </Card>
            <Card title="Graph node latency" flush>
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Node</th><th className="num">p50</th><th className="num">p95</th></tr></thead>
                  <tbody>{m.nodes.map((n) => <tr key={n.node}><td className="mono">{n.node}</td><td className="num mono">{ms(n.p50_ms)}</td><td className="num mono">{ms(n.p95_ms)}</td></tr>)}</tbody>
                </table>
              </div>
            </Card>
          </div>
          <Card title="Tool latency and errors" flush>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Tool</th><th className="num">Calls</th><th className="num">Errors</th><th className="num">p50</th><th className="num">p95</th></tr></thead>
                <tbody>
                  {m.tools.map((t) => (
                    <tr key={t.tool}><td className="mono">{t.tool}</td><td className="num">{t.calls}</td><td className="num">{t.errors}</td><td className="num mono">{ms(t.p50_ms)}</td><td className="num mono">{ms(t.p95_ms)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
            {m.tools.length === 0 && <EmptyState title="No tool calls recorded yet" />}
          </Card>
          <Card title="Routes" subtitle={m.note} flush>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Route template</th><th className="num">Requests</th><th className="num">5xx</th><th className="num">4xx</th><th className="num">p50</th><th className="num">p95</th></tr></thead>
                <tbody>
                  {m.routes.map((r) => (
                    <tr key={r.route}><td className="mono">{r.route}</td><td className="num">{r.count}</td><td className="num mono">{pct(r.error_rate)}</td><td className="num mono">{pct(r.client_error_rate)}</td><td className="num mono">{ms(r.p50_ms)}</td><td className="num mono">{ms(r.p95_ms)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </>
  );
}
