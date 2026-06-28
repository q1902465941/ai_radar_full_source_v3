import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { getDashboardOverview, type DashboardCandidate, type DashboardOverview } from './api/dashboard';
import {
  getLatestRadarScan,
  startRadarScan,
  type LatestRadarScanResponse,
  type RadarCandidate,
} from './api/radar';
import { getTaskStatus, type BackgroundTask } from './api/tasks';

type ViewId = 'overview' | 'radar' | 'positions' | 'strategy' | 'settings';

const navItems: Array<{ id: ViewId; label: string; caption: string }> = [
  { id: 'overview', label: 'Overview', caption: 'Dashboard' },
  { id: 'radar', label: 'Radar', caption: 'Markets' },
  { id: 'positions', label: 'Positions', caption: 'Portfolio' },
  { id: 'strategy', label: 'Strategy AI', caption: 'AI Insight' },
  { id: 'settings', label: 'Settings', caption: 'Control' },
];

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

function formatNumber(value: unknown, digits = 2) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return '--';
  return number.toFixed(digits);
}

function textValue(value: unknown, fallback = '') {
  return typeof value === 'string' && value ? value : fallback;
}

function structureText(candidate: RadarCandidate, key: string, fallback = '--') {
  const structure = candidate.market_structure || {};
  const value = structure[key];
  return textValue(value, fallback);
}

function isTaskActive(task: BackgroundTask | null) {
  return task?.state === 'pending' || task?.state === 'running';
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
              <i style={{ '--w': `${Math.min(100, overview.metrics.ai_candidate_count * 12)}%` } as CSSProperties} />
            </div>
            <div>
              <span>Actionable</span>
              <b>{overview.metrics.actionable_count}</b>
              <i style={{ '--w': `${Math.min(100, overview.metrics.actionable_count * 20)}%` } as CSSProperties} />
            </div>
            <div>
              <span>Average score</span>
              <b>{overview.metrics.average_score}</b>
              <i style={{ '--w': `${Math.min(100, overview.metrics.average_score)}%` } as CSSProperties} />
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
            <i className="long" style={{ '--w': `${direction.long_pct}%` } as CSSProperties} />
            <i className="short" style={{ '--w': `${direction.short_pct}%` } as CSSProperties} />
            <i className="neutral" style={{ '--w': `${direction.neutral_pct}%` } as CSSProperties} />
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

function RadarWorkspace() {
  const [latest, setLatest] = useState<LatestRadarScanResponse | null>(null);
  const [task, setTask] = useState<BackgroundTask | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const candidates = latest?.candidates || [];
  const scan = latest?.scan || null;
  const aiCandidates = useMemo(() => candidates.filter((candidate) => candidate.ai_candidate), [candidates]);

  const loadLatest = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getLatestRadarScan();
      setLatest(data);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadLatest();
  }, [loadLatest]);

  useEffect(() => {
    const activeTask = task;
    if (!activeTask || !isTaskActive(activeTask)) return undefined;
    let cancelled = false;
    let timer = 0;

    const pollTask = async () => {
      try {
        const nextTask = await getTaskStatus(activeTask.task_id);
        if (cancelled) return;
        setTask(nextTask);
        if (nextTask.state === 'succeeded') {
          await loadLatest();
          return;
        }
        if (nextTask.state === 'failed') {
          setError(nextTask.error || 'radar scan failed');
          return;
        }
        timer = window.setTimeout(pollTask, 1400);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    };

    timer = window.setTimeout(pollTask, 800);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [loadLatest, task]);

  const runScan = async () => {
    setError('');
    try {
      const response = await startRadarScan({ force_refresh: true });
      setTask(response.task);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <section className="radar-view">
      <article className="radar-control-panel">
        <div>
          <span>Radar Scan</span>
          <h2>Scan Evidence Matrix</h2>
          <p>Scan is evidence, not an order command. Slow work runs through v2 background tasks.</p>
        </div>
        <div className="radar-actions">
          <button className="primary-action" disabled={isTaskActive(task)} onClick={runScan} type="button">
            {isTaskActive(task) ? 'Scanning...' : 'Run Scan'}
          </button>
          <button className="secondary-action" onClick={loadLatest} type="button">
            Refresh Cache
          </button>
        </div>
      </article>

      <section className="radar-summary-grid">
        <div>
          <span>Task</span>
          <b>{task?.state || 'idle'}</b>
          <small>{task?.task_id || 'No active task'}</small>
        </div>
        <div>
          <span>Scan ID</span>
          <b>{scan?.scan_id || '--'}</b>
          <small>{scan?.state || (loading ? 'loading' : 'no cached scan')}</small>
        </div>
        <div>
          <span>Top50</span>
          <b>{scan?.top50_count ?? candidates.length}</b>
          <small>{aiCandidates.length} AI candidates</small>
        </div>
        <div>
          <span>Market Heat</span>
          <b>{scan?.market_heat ?? 0}</b>
          <small>{scan?.duration_ms ? `${scan.duration_ms}ms` : 'duration pending'}</small>
        </div>
      </section>

      {error ? <div className="radar-error">{error}</div> : null}

      <section className="radar-candidate-panel">
        <div className="panel-title">
          <b>AI Candidate Queue</b>
          <span>Confirmed candidates only</span>
        </div>
        <div className="radar-candidate-grid">
          {(aiCandidates.length ? aiCandidates : candidates.slice(0, 4)).map((candidate) => (
            <RadarCandidateCard candidate={candidate} key={`${candidate.scan_id}-${candidate.symbol}-${candidate.rank}`} />
          ))}
          {!candidates.length ? <p className="empty-state">No persisted radar result yet. Run a scan to populate the matrix.</p> : null}
        </div>
      </section>

      <section className="radar-table-panel">
        <div className="panel-title">
          <b>Evidence Rows</b>
          <span>{candidates.length} persisted rows</span>
        </div>
        <div className="radar-table">
          <div className="radar-table-head">
            <span>#</span>
            <span>Symbol</span>
            <span>Price</span>
            <span>Side</span>
            <span>Score</span>
            <span>Action</span>
            <span>Structure</span>
            <span>Risk</span>
            <span>Evidence</span>
          </div>
          <div className="radar-table-body">
            {candidates.map((candidate) => (
              <RadarEvidenceRow candidate={candidate} key={`${candidate.scan_id}-${candidate.symbol}-${candidate.rank}`} />
            ))}
            {!candidates.length ? <div className="radar-empty">No cached evidence rows.</div> : null}
          </div>
        </div>
      </section>
    </section>
  );
}

function RadarCandidateCard({ candidate }: { candidate: RadarCandidate }) {
  const action = structureText(candidate, 'action', 'WAIT');
  return (
    <div className="radar-candidate-card">
      <div>
        <b>{candidate.base_asset || candidate.symbol}</b>
        <span>{candidate.symbol}</span>
      </div>
      <strong className={directionClass(candidate.direction)}>{candidate.direction}</strong>
      <div className="score-line">
        <i style={{ '--w': `${Math.min(100, Number(candidate.score || 0))}%` } as CSSProperties} />
      </div>
      <small>{formatNumber(candidate.score, 1)} / 100</small>
      <em className={actionClass(action)}>{action}</em>
    </div>
  );
}

function RadarEvidenceRow({ candidate }: { candidate: RadarCandidate }) {
  const action = structureText(candidate, 'action', 'WAIT');
  const regime = structureText(candidate, 'regime', 'structure');
  const phase = structureText(candidate, 'phase', 'watch');
  return (
    <div className="radar-table-row">
      <span>#{candidate.rank || '--'}</span>
      <strong>
        {candidate.base_asset || candidate.symbol}
        <small>{candidate.symbol}</small>
      </strong>
      <span>{formatNumber(candidate.price, Number(candidate.price || 0) > 10 ? 2 : 5)}</span>
      <b className={directionClass(candidate.direction)}>{candidate.direction}</b>
      <span>{formatNumber(candidate.score, 1)}</span>
      <em className={actionClass(action)}>{action}</em>
      <span>{regime} / {phase}</span>
      <span>{candidate.fake_breakout_risk || '--'}</span>
      <span>
        5m {formatNumber(candidate.change_5m, 2)}% | OI {formatNumber(candidate.oi_change, 2)}% | Fund {candidate.fund_confirm_count ?? 0}/{candidate.fund_confirm_total ?? 0}
      </span>
    </div>
  );
}

function PlaceholderView({ title }: { title: string }) {
  return (
    <section className="panel-grid">
      <article className="wide-panel">
        <span>Migration Queue</span>
        <strong>{title}</strong>
        <p>This module is preserved in the legacy backend while its React view is migrated behind v2 APIs.</p>
      </article>
    </section>
  );
}

export function App() {
  const [activeView, setActiveView] = useState<ViewId>('overview');
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

  const activeTitle = navItems.find((item) => item.id === activeView)?.label || 'Overview';

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
          {navItems.map((item) => (
            <button
              className={`nav-item ${activeView === item.id ? 'active' : ''}`}
              key={item.id}
              onClick={() => setActiveView(item.id)}
              type="button"
            >
              <span>{item.label}</span>
              <small>{item.caption}</small>
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
            <h1>{activeView === 'overview' ? 'AI Radar Control Center' : activeTitle}</h1>
          </div>
          <span className={error ? 'status error' : 'status online'}>
            {error || 'v2 API online'}
          </span>
        </header>
        {activeView === 'overview' && (overview ? <Dashboard overview={overview} /> : <LoadingDashboard />)}
        {activeView === 'radar' && <RadarWorkspace />}
        {activeView === 'positions' && <PlaceholderView title="Positions" />}
        {activeView === 'strategy' && <PlaceholderView title="Strategy AI" />}
        {activeView === 'settings' && <PlaceholderView title="Settings" />}
      </main>
    </div>
  );
}
