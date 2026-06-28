import { useEffect, useState } from 'react';
import { getDashboardOverview, type DashboardCandidate, type DashboardOverview } from './api/dashboard';

const navItems = [
  ['Overview', 'Dashboard'],
  ['Radar', 'Markets'],
  ['Positions', 'Portfolio'],
  ['Strategy AI', 'AI Insight'],
  ['Settings', 'Control'],
] as const;

const metricLabels: Array<[keyof DashboardOverview['metrics'], string]> = [
  ['top50_count', 'Top50'],
  ['ai_candidate_count', 'AI Candidates'],
  ['actionable_count', 'Actionable'],
  ['average_score', 'Avg Score'],
  ['dynamic_stream_count', 'Dynamic Stream'],
  ['active_coin_count', 'Active Coins'],
  ['fund_ready_count', 'Funding Ready'],
  ['fake_high_count', 'High Fake Risk'],
];

function directionClass(direction: string) {
  if (direction === 'SHORT') return 'short';
  if (direction === 'LONG') return 'long';
  return 'neutral';
}

function actionClass(action: string) {
  if (action === 'OPEN_LONG') return 'long';
  if (action === 'OPEN_SHORT') return 'short';
  return 'neutral';
}

function CandidateRow({ candidate }: { candidate: DashboardCandidate }) {
  return (
    <div className="candidate-row">
      <div>
        <strong>{candidate.base_asset || candidate.symbol}</strong>
        <span>{candidate.symbol}</span>
      </div>
      <b className={directionClass(candidate.direction)}>{candidate.direction}</b>
      <small>{candidate.regime || 'structure'} / {candidate.phase || 'watch'}</small>
      <em>{candidate.score} / 100</em>
      <i className={actionClass(candidate.action)}>{candidate.action || 'WAIT'}</i>
    </div>
  );
}

function LoadingDashboard() {
  return (
    <section className="panel-grid">
      <article className="wide-panel">
        <span>Loading</span>
        <strong>Connecting to v2 API</strong>
        <p>Reading the preserved backend state without triggering a new radar scan.</p>
      </article>
    </section>
  );
}

function Dashboard({ overview }: { overview: DashboardOverview }) {
  const direction = overview.direction;

  return (
    <>
      <section className="dashboard-layout">
        <article className="hero-status">
          <div className="panel-title">
            <b>Market State</b>
            <span>AI Insight</span>
          </div>
          <div className="state-word">
            {overview.state.code}
            <i />
          </div>
          <p>{overview.state.text}</p>
          <div className="status-bars">
            <div>
              <span>AI candidates</span>
              <b>{overview.metrics.ai_candidate_count}</b>
              <i style={{ '--w': `${Math.min(100, overview.metrics.ai_candidate_count * 12)}%` } as React.CSSProperties} />
            </div>
            <div>
              <span>Actionable</span>
              <b>{overview.metrics.actionable_count}</b>
              <i style={{ '--w': `${Math.min(100, overview.metrics.actionable_count * 20)}%` } as React.CSSProperties} />
            </div>
            <div>
              <span>Average score</span>
              <b>{overview.metrics.average_score}</b>
              <i style={{ '--w': `${Math.min(100, overview.metrics.average_score)}%` } as React.CSSProperties} />
            </div>
          </div>
        </article>

        <article>
          <div className="panel-title">
            <b>Global Scan</b>
            <span>Radar Summary</span>
          </div>
          <div className="metrics-grid">
            {metricLabels.map(([key, label]) => (
              <div className="metric-card" key={key}>
                <span>{label}</span>
                <b>{overview.metrics[key]}</b>
              </div>
            ))}
          </div>
        </article>

        <article>
          <div className="panel-title">
            <b>Direction Bias</b>
            <span>Top50 Bias</span>
          </div>
          <div className="direction-grid">
            <div>
              <span>LONG</span>
              <b className="long">{direction.long}</b>
              <small>{direction.long_pct}%</small>
            </div>
            <div>
              <span>SHORT</span>
              <b className="short">{direction.short}</b>
              <small>{direction.short_pct}%</small>
            </div>
            <div>
              <span>NEUTRAL</span>
              <b>{direction.neutral}</b>
              <small>{direction.neutral_pct}%</small>
            </div>
          </div>
          <div className="bias-bar" aria-label="Direction distribution">
            <i className="long" style={{ '--w': `${direction.long_pct}%` } as React.CSSProperties} />
            <i className="short" style={{ '--w': `${direction.short_pct}%` } as React.CSSProperties} />
            <i className="neutral" style={{ '--w': `${direction.neutral_pct}%` } as React.CSSProperties} />
          </div>
        </article>

        <article>
          <div className="panel-title">
            <b>Latest AI Candidates</b>
            <span>Top Confirmed</span>
          </div>
          <div className="candidate-list">
            {overview.candidates.length ? (
              overview.candidates.map((candidate) => (
                <CandidateRow candidate={candidate} key={`${candidate.symbol}-${candidate.action}`} />
              ))
            ) : (
              <p className="empty-state">No confirmed candidate is active. The system is observing.</p>
            )}
          </div>
        </article>
      </section>

      <section className="ops-strip">
        <div>
          <span>Last scan</span>
          <b>{overview.scan.last_scan_time || '--'}</b>
        </div>
        <div>
          <span>Market heat</span>
          <b>{overview.scan.market_heat}</b>
        </div>
        <div>
          <span>Alerts</span>
          <b>{overview.scan.alert_count}</b>
        </div>
        <div>
          <span>Live trading</span>
          <b className="short">OFF</b>
        </div>
      </section>
    </>
  );
}

export function App() {
  const [overview, setOverview] = useState<DashboardOverview | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    getDashboardOverview()
      .then((data) => {
        if (!active) return;
        setOverview(data);
        setError('');
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark">AI</div>
          <div>
            <b>AI Radar</b>
            <span>Preserved Rebuild</span>
          </div>
        </div>
        <nav>
          {navItems.map(([label, caption], index) => (
            <button className={`nav-item ${index === 0 ? 'active' : ''}`} key={label} type="button">
              <span>{label}</span>
              <small>{caption}</small>
            </button>
          ))}
        </nav>
        <div className="sidebar-status">
          <b>Paper</b>
          <span>Real OFF</span>
        </div>
      </aside>
      <main className="workspace">
        <header className="workspace-header">
          <div>
            <p>Monitor Control</p>
            <h1>AI Radar Control Center</h1>
          </div>
          <span className={error ? 'status error' : 'status online'}>
            {error || 'v2 API online'}
          </span>
        </header>
        {overview ? <Dashboard overview={overview} /> : <LoadingDashboard />}
      </main>
    </div>
  );
}
