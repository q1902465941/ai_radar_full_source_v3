function fmt(n, d = 2) {
  if (n === null || n === undefined || n === '') return '--';
  const value = Number(n);
  if (!Number.isFinite(value)) return '--';
  return value.toFixed(d);
}

function clampPct(n) {
  const value = Number(n);
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function ts(ms) {
  if (!ms) return '--';
  return new Date(ms).toLocaleString();
}

function cls(v) {
  return Number(v) >= 0 ? 'up' : 'down';
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function sideText(side) {
  if (side === 'SHORT') return '做空';
  if (side === 'LONG') return '做多';
  return '中性';
}

function priceStatus(p) {
  const source = p.price_source || 'unknown';
  const age = Number(p.price_age_seconds || 0);
  const stale = p.price_stale ? '<span class="down">STALE</span>' : '<span class="up">LIVE</span>';
  const bidAsk = Number(p.price_bid || 0) > 0 && Number(p.price_ask || 0) > 0
    ? `<br><small>bid ${fmt(p.price_bid, 5)} / ask ${fmt(p.price_ask, 5)}</small>`
    : '';
  const error = p.price_error ? `<br><small class="down">${p.price_error}</small>` : '';
  return `<small>${stale} ${source} ${fmt(age, 1)}s</small>${bidAsk}${error}`;
}

async function j(url, opts) {
  const nextOpts = opts || {};
  const headers = new Headers(nextOpts.headers || {});
  const apiToken = localStorage.getItem('api_token') || '';
  if (apiToken && !headers.has('X-API-Token')) headers.set('X-API-Token', apiToken);
  const r = await fetch(url, { ...nextOpts, headers });
  const text = await r.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!r.ok) throw new Error(data.error || data.detail || data.raw || `HTTP ${r.status}`);
  return data;
}

let radarRefreshTimer = 0;

function clearScheduledRadarRefresh() {
  if (!radarRefreshTimer || typeof window === 'undefined') return;
  window.clearTimeout(radarRefreshTimer);
  radarRefreshTimer = 0;
}

function scheduleRadarRefresh(delayMs = 2500) {
  if (typeof window === 'undefined' || window.PAGE !== 'radar' || radarRefreshTimer) return;
  radarRefreshTimer = window.setTimeout(async () => {
    radarRefreshTimer = 0;
    try {
      await refreshRadar();
    } catch (err) {
      setScanStatus(`扫描状态刷新失败：${err.message || err}`, false);
    }
  }, delayMs);
}

function radarScanStillRunning(data) {
  const status = data && data.scan_status ? data.scan_status : {};
  const rows = data && Array.isArray(data.top50) ? data.top50 : [];
  return rows.length === 0 && (
    status.in_progress ||
    data.error === 'radar_scan_running_no_cache' ||
    data.error === 'radar_scan_warming_up'
  );
}

function showSettingsOut(value) {
  const el = document.getElementById('settingsOut');
  if (!el) return;
  el.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function clearRecoveredAccountError(account) {
  const el = document.getElementById('settingsOut');
  if (!el || account.error || account.error_message) return;
  const text = el.textContent || '';
  if (text.includes('Binance') && (text.includes('账户') || text.includes('account') || text.includes('接口'))) {
    el.textContent = '';
  }
}

function errorMessage(err) {
  return err && err.message ? err.message : String(err);
}

const buttonActionLocks = new Set();

function resolveActionButton(source) {
  if (!source) return null;
  if (source.submitter && source.submitter.closest) return source.submitter.closest('button');
  if (source.currentTarget && source.currentTarget.closest) return source.currentTarget.closest('button');
  if (source.target && source.target.closest) return source.target.closest('button');
  if (source.closest) return source.closest('button');
  return null;
}

function setButtonBusy(btn, busy, busyLabel) {
  if (!btn) return;
  if (!btn.dataset.label) btn.dataset.label = (btn.textContent || '').trim();
  btn.disabled = !!busy;
  btn.setAttribute('aria-busy', busy ? 'true' : 'false');
  btn.textContent = busy ? (busyLabel || btn.dataset.busyLabel || '执行中...') : (btn.dataset.label || '');
}

function setActionButtonsBusy(selector, busy, busyLabel) {
  document.querySelectorAll(selector).forEach((btn) => setButtonBusy(btn, busy, busyLabel));
}

function confirmButtonAction(btn) {
  if (!btn || !btn.dataset.confirm) return true;
  const messages = {
    'start-auto-loop': '确认开启自动交易循环？',
    'stop-auto-loop': '确认关闭自动交易循环？',
    'manual-close-position': '确认手动平仓？',
    'clear-api-token': '确认清除当前浏览器保存的 API Token？',
  };
  return window.confirm(messages[btn.dataset.confirm] || '确认执行这个操作？');
}

async function withButtonBusy(source, action, options = {}) {
  const btn = resolveActionButton(source);
  const lockKey = options.lockKey || (btn ? (btn.dataset.action || btn.id || btn.dataset.label || btn.textContent || '').trim() : '');
  if (lockKey && buttonActionLocks.has(lockKey)) return null;
  if (!confirmButtonAction(btn)) return null;
  if (lockKey) buttonActionLocks.add(lockKey);
  setButtonBusy(btn, true, options.busyLabel);
  try {
    return await action(btn);
  } finally {
    setButtonBusy(btn, false);
    if (lockKey) buttonActionLocks.delete(lockKey);
  }
}

function refreshApiTokenStatus() {
  const el = document.getElementById('apiTokenStatus');
  if (!el) return;
  el.textContent = localStorage.getItem('api_token') ? '已保存' : '未保存';
}

function saveApiToken(event) {
  if (event && event.currentTarget) {
    if (event.preventDefault) event.preventDefault();
    return withButtonBusy(event, () => saveApiToken(), { lockKey: 'save-api-token' });
  }
  if (event) event.preventDefault();
  const input = document.getElementById('apiTokenInput');
  const token = input ? input.value.trim() : '';
  if (!token) {
    showSettingsOut('API Token 不能为空');
    return;
  }
  localStorage.setItem('api_token', token);
  if (input) input.value = '';
  refreshApiTokenStatus();
  showSettingsOut('API Token 已保存到当前浏览器');
}

function clearApiToken(event) {
  if (event && event.currentTarget) {
    return withButtonBusy(event, () => clearApiToken(), { lockKey: 'clear-api-token' });
  }
  localStorage.removeItem('api_token');
  refreshApiTokenStatus();
  showSettingsOut('API Token 已清除');
}

function activateNav() {
  document.querySelectorAll('.nav').forEach((a) => {
    if (location.pathname.startsWith(a.dataset.path)) a.classList.add('active');
  });
}

async function refreshTop() {
  const bar = document.getElementById('topbar');
  if (!bar) return;
  const s = await j('/api/state');
  const majors = s.major || [];
  const market = s.market_data || {};
  const sourceParts = [market.public_source || s.market_data_source || 'online', market.refresh_source].filter(Boolean);
  const sourceText = sourceParts.join(' / ');
  const marketHealth = market.degraded
    ? `error=${market.error || 'degraded'}`
    : (market.warning ? `warning=${market.warning}` : 'health=ok');
  const marketDetail = [
    `mode=${market.mode || '--'}`,
    `top50=${market.top50_count ?? s.top50_count ?? '--'}`,
    `snapshots=${market.snapshot_count ?? '--'}`,
    `active=${market.active_coin_count ?? '--'}`,
    `dynamic=${market.dynamic_stream_count ?? '--'}`,
    marketHealth,
  ].join(' | ');
  const qualityText = market.degraded
    ? 'DEGRADED'
    : `${market.warning ? 'WARN ' : ''}Top50 ${market.top50_count ?? s.top50_count ?? '--'}`;
  bar.innerHTML = `${majors.map((x) => {
    const hasPrice = Number(x.price || 0) > 0;
    const changeValue = finiteNumber(x.change);
    const changeLabel = x.change_label ? `${x.change_label} ` : '';
    const changeText = changeValue === null ? '--' : `${changeLabel}${changeValue >= 0 ? '+' : ''}${fmt(changeValue, 2)}%`;
    const titleParts = [
      x.source ? `price=${x.source}` : '',
      x.change_source ? `change=${x.change_source}` : '',
      finiteNumber(x.price_age_seconds) !== null ? `age=${fmt(x.price_age_seconds, 1)}s` : '',
    ].filter(Boolean).join(' | ');
    return `
    <div class="ticker" title="${escapeHtml(titleParts)}">
      <b>${escapeHtml(x.label || x.symbol || '--')}</b>
      <div class="price">${hasPrice ? fmt(x.price, x.price > 10 ? 2 : 5) : '--'}</div>
      <span class="${hasPrice && changeValue !== null ? cls(changeValue) : ''}">${hasPrice ? escapeHtml(changeText) : '--'}</span>
    </div>
  `;
  }).join('')}
  <div class="status-card"><span>最后扫描</span><b>${s.last_scan_time || '--'}</b></div>
  <div class="status-card"><span>市场热度</span><b>${fmt(s.market_heat, 0)}</b></div>
  <div class="status-card"><span>警报状态</span><b>${s.alert_count || 0}</b></div>
  <div class="status-card"><span>电报推送</span><b>正常</b></div>
  <div class="status-card" title="${escapeHtml(marketDetail)}"><span>Market Source</span><b>${escapeHtml(sourceText || 'online')}</b></div>
  <div class="status-card" title="${escapeHtml(marketDetail)}"><span>Market Data</span><b>${escapeHtml(qualityText)}</b></div>`;
}

function setScanStatus(message, busy = false) {
  const el = document.getElementById('scanStatus');
  if (el) el.textContent = message || '';
  document.querySelectorAll('[data-action="scan-now"]').forEach((btn) => {
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    btn.disabled = busy;
    btn.setAttribute('aria-busy', busy ? 'true' : 'false');
    btn.textContent = busy ? '扫描中...' : btn.dataset.label;
  });
}

async function scanNow(event) {
  if (event && event.currentTarget) {
    return withButtonBusy(event, () => scanNow(), { lockKey: 'scan-now', busyLabel: '扫描中...' });
  }
  const started = Date.now();
  setScanStatus('正在拉取 Binance 因子并重新计算扫描目标池...', true);
  try {
    const out = await j('/api/radar/scan-now', { method: 'POST' });
    if (window.PAGE === 'radar') await refreshRadar();
    if (window.PAGE === 'radar') await refreshSystemReadiness();
    const settingsOut = document.getElementById('settingsOut');
    if (settingsOut) settingsOut.textContent = JSON.stringify(out, null, 2);
    if (out.error === 'radar_scan_already_running' || out.error === 'radar_scan_still_running') {
      setScanStatus('扫描仍在运行，完成后自动刷新结果...', false);
      scheduleRadarRefresh(2000);
      return out;
    }
    if (!out.ok) throw new Error(out.error || 'radar_scan_failed');
    setScanStatus(`已刷新 ${out.count || 0} 条 · ${out.last_scan_time || '--'} · ${((Date.now() - started) / 1000).toFixed(1)}s`, false);
    clearScheduledRadarRefresh();
    return out;
  } catch (err) {
    const msg = `刷新失败：${err.message || err}`;
    setScanStatus(msg, false);
    const settingsOut = document.getElementById('settingsOut');
    if (settingsOut) settingsOut.textContent = msg;
  }
}

function bars(hist) {
  if (!hist || !hist.length) return '';
  const max = Math.max(...hist, 1);
  return `<div class="bars">${hist.slice(-8).map((v) => `<i class="bar" style="height:${Math.max(3, v / max * 22)}px"></i>`).join('')}</div>`;
}

function px(n) {
  const value = Number(n);
  if (!Number.isFinite(value) || value <= 0) return '--';
  return fmt(value, value >= 10 ? 2 : 5);
}

function structureOf(c) {
  return c && c.market_structure && typeof c.market_structure === 'object' ? c.market_structure : {};
}

function structureMetricsOf(c) {
  const features = c && c.score_features && typeof c.score_features === 'object' ? c.score_features : {};
  const metrics = features.structure_metrics && typeof features.structure_metrics === 'object' ? features.structure_metrics : {};
  return metrics;
}

function universalModelOf(c) {
  const features = c && c.score_features && typeof c.score_features === 'object' ? c.score_features : {};
  return features.universal_anomaly_model && typeof features.universal_anomaly_model === 'object' ? features.universal_anomaly_model : {};
}

function mapText(value, dict, fallback = '--') {
  const key = String(value || '');
  return dict[key] || key || fallback;
}

function regimeText(value) {
  return mapText(value, {
    trend_continuation: '趋势延续',
    breakout: '突破',
    pullback: '回踩',
    range_or_chop: '震荡/乱',
    fake_breakout: '假突破',
    exhaustion: '过热衰竭',
  });
}

function phaseText(value) {
  return mapText(value, {
    observation: '观察',
    building: '构建中',
    confirming: '确认中',
    actionable: '可做',
    overheated: '过热',
    invalid: '无效',
  });
}

function actionText(value) {
  return mapText(value, {
    OPEN_LONG: '开多',
    OPEN_SHORT: '开空',
    WAIT: '等待',
  });
}

function reasonText(value) {
  return mapText(value, {
    fake_breakout_high: '假突破高风险',
    fake_breakout_not_low: '假突破不干净',
    wick_noise_extreme: '极端插针',
    wick_too_high: '插针太重',
    fund_confirm_below_3: '资金确认不够',
    direction_confirmations_low: '方向确认不够',
    no_trade_structure: '结构不适合进',
    chase_displacement_high: '短线拉太猛，不能追',
    short_term_move_overextended: '短线过度延伸',
    short_term_anomaly_absent: '当前短线异动不足',
    direction_neutral_or_price_invalid: '方向或价格无效',
  }, value);
}

function structureBadgeClass(action) {
  if (action === 'OPEN_SHORT') return 'red';
  if (action === 'OPEN_LONG') return 'green';
  return 'wait';
}

function entryZoneText(s) {
  if (!s || Number(s.entry_zone_low || 0) <= 0 || Number(s.entry_zone_high || 0) <= 0) return '--';
  return `${px(s.entry_zone_low)} - ${px(s.entry_zone_high)}`;
}

function stopTargetText(s) {
  if (!s || Number(s.stop_loss || 0) <= 0) return '--';
  return `SL ${px(s.stop_loss)} / T1 ${px(s.tp1)} / T2 ${px(s.tp2)}`;
}

function reasonsText(s) {
  const reasons = Array.isArray(s.no_trade_reasons) ? s.no_trade_reasons : [];
  return reasons.length ? reasons.slice(0, 3).map(reasonText).join('，') : '结构干净';
}

function evidenceText(s) {
  const evidence = Array.isArray(s.evidence) ? s.evidence : [];
  const selected = evidence.filter((x) => /current_wick_ratio|max_wick_ratio_14|range_position|breakout_up|breakout_down|prev_high_20|prev_low_20|universal_direction/.test(String(x)));
  return selected.length ? selected.slice(0, 6).join(' / ') : '--';
}

function universalText(c) {
  const model = universalModelOf(c);
  const p = model.probabilities && typeof model.probabilities === 'object' ? model.probabilities : {};
  const direction = model.direction || '--';
  return `AI ${direction} L${fmt((p.LONG || 0) * 100, 0)} S${fmt((p.SHORT || 0) * 100, 0)} N${fmt((p.NEUTRAL || 0) * 100, 0)}`;
}

function radarSummary(data) {
  const rows = data.top50 || [];
  const active = data.active_coins || {};
  const stream = data.dynamic_stream || {};
  const confirmed = data.top5_confirmed || data.top4 || [];
  const longCount = rows.filter((x) => x.direction === 'LONG').length;
  const shortCount = rows.filter((x) => x.direction === 'SHORT').length;
  const neutralCount = Math.max(0, rows.length - longCount - shortCount);
  const aiCount = rows.filter((x) => x.ai_candidate).length;
  const actionable = rows.filter((x) => {
    const action = structureOf(x).action;
    return action === 'OPEN_LONG' || action === 'OPEN_SHORT';
  }).length;
  const waitCount = rows.filter((x) => structureOf(x).action === 'WAIT').length;
  const fakeHigh = rows.filter((x) => x.fake_breakout_risk === 'HIGH').length;
  const fundReady = rows.filter((x) => Number(x.fund_confirm_count || 0) >= 3).length;
  const avgScore = rows.length ? rows.reduce((sum, x) => sum + Number(x.score || 0), 0) / rows.length : 0;
  const state = actionable > 0 ? 'WATCH' : aiCount > 0 ? 'FILTERING' : 'NEUTRAL';
  const stateText = actionable > 0
    ? '存在可执行结构，仍需通过风控和交易成本检查。'
    : aiCount > 0
      ? '已有 AI 候选，当前主要在过滤假突破、插针和资金确认。'
      : '市场暂无明确方向，优先保持观察，等待更干净的候选。';
  return {
    rows,
    active,
    stream,
    confirmed,
    longCount,
    shortCount,
    neutralCount,
    aiCount,
    actionable,
    waitCount,
    fakeHigh,
    fundReady,
    avgScore,
    state,
    stateText,
  };
}

function renderRadarGlance(data) {
  const el = document.getElementById('radarGlance');
  if (!el) return;
  const summary = radarSummary(data);
  const total = Math.max(summary.rows.length, 1);
  const longPct = Math.round(summary.longCount / total * 100);
  const shortPct = Math.round(summary.shortCount / total * 100);
  const neutralPct = Math.max(0, 100 - longPct - shortPct);
  const riskPct = Math.round(summary.fakeHigh / total * 100);
  const confirmedPct = Math.round(summary.fundReady / total * 100);
  el.innerHTML = `
    <article class="glance-card primary">
      <span>扫描目标池</span>
      <b>${summary.rows.length}<em>/50</em></b>
      <div class="meter"><i style="--w:${clampPct(summary.rows.length * 2)}%"></i></div>
      <small>按后端真实返回展开</small>
    </article>
    <article class="glance-card">
      <span>AI 候选</span>
      <b>${summary.aiCount}</b>
      <div class="meter green"><i style="--w:${clampPct(summary.aiCount * 20)}%"></i></div>
      <small>确认队列 ${summary.confirmed.length}</small>
    </article>
    <article class="glance-card direction">
      <span>方向</span>
      <b>${summary.longCount}<em>L</em> / ${summary.shortCount}<em>S</em></b>
      <div class="signal-bars compact">
        <i class="long" style="--w:${longPct}%"></i>
        <i class="short" style="--w:${shortPct}%"></i>
        <i class="neutral" style="--w:${neutralPct}%"></i>
      </div>
      <small>中性 ${summary.neutralCount}</small>
    </article>
    <article class="glance-card">
      <span>平均评分</span>
      <b>${fmt(summary.avgScore, 0)}</b>
      <div class="meter"><i style="--w:${clampPct(summary.avgScore)}%"></i></div>
      <small>可执行 ${summary.actionable}</small>
    </article>
    <article class="glance-card">
      <span>过滤风险</span>
      <b>${summary.fakeHigh}</b>
      <div class="dual-meter">
        <i class="ok" style="--w:${clampPct(confirmedPct)}%"></i>
        <i class="bad" style="--w:${clampPct(riskPct)}%"></i>
      </div>
      <small>资金确认 ${summary.fundReady}</small>
    </article>
  `;
}

function refreshActiveRadarBox(data) {
  const el = document.getElementById('activeRadarBox');
  if (!el) return;
  const summary = radarSummary(data);
  const total = Math.max(summary.rows.length, 1);
  const longPct = Math.round(summary.longCount / total * 100);
  const shortPct = Math.round(summary.shortCount / total * 100);
  const neutralPct = Math.max(0, 100 - longPct - shortPct);
  el.innerHTML = `
    <div class="active-radar-metric market-state-card">
      <span>市场状态</span>
      <b>${summary.state}</b>
      <em>${summary.stateText}</em>
      <div class="status-line"><i></i> AI 自动扫描中 · 目标池 ${summary.rows.length}/50 · 活跃 ${summary.active.active_count || 0}</div>
    </div>
    <div class="active-radar-metric"><span>AI 候选</span><b>${summary.aiCount}</b></div>
    <div class="active-radar-metric"><span>可执行结构</span><b>${summary.actionable}</b></div>
    <div class="active-radar-metric"><span>平均评分</span><b>${fmt(summary.avgScore, 0)}</b></div>
    <div class="active-radar-list signal-distribution">
      <span>方向分布</span>
      <b>多 ${summary.longCount} · 空 ${summary.shortCount} · 中性 ${summary.neutralCount}</b>
      <div class="signal-bars">
        <i class="long" style="--w:${longPct}%"></i>
        <i class="short" style="--w:${shortPct}%"></i>
        <i class="neutral" style="--w:${neutralPct}%"></i>
      </div>
    </div>
  `;
}

function renderRadarSidePanels(data) {
  const summary = radarSummary(data);
  const decisionEl = document.getElementById('decisionPanel');
  const guardEl = document.getElementById('guardPanel');
  const watchEl = document.getElementById('watchPanel');
  const selected = summary.confirmed[0] || summary.rows[0] || null;
  if (decisionEl) {
    if (!selected) {
      decisionEl.innerHTML = '<div class="panel-title compact"><b>AI 决策面板</b><span>无候选</span></div><div class="rail-empty">等待下一轮扫描。</div>';
    } else {
      const s = structureOf(selected);
      decisionEl.innerHTML = `
        <div class="panel-title compact"><b>AI 决策面板</b><span>${selected.symbol || '--'}</span></div>
        <div class="decision-hero">
          <span class="badge ${structureBadgeClass(s.action)}">${actionText(s.action)}</span>
          <h2>${selected.base_asset || selected.symbol || '--'}</h2>
          <b>${fmt(selected.score, 0)} / 100</b>
          <small>${regimeText(s.regime)} · ${phaseText(s.phase)} · ${universalText(selected)}</small>
        </div>
        <dl class="decision-lines">
          <div><dt>方向</dt><dd>${sideText(selected.direction)}</dd></div>
          <div><dt>入场区</dt><dd>${entryZoneText(s)}</dd></div>
          <div><dt>止损/目标</dt><dd>${stopTargetText(s)}</dd></div>
          <div><dt>不能做原因</dt><dd>${reasonsText(s)}</dd></div>
        </dl>
      `;
    }
  }
  if (guardEl) {
    guardEl.innerHTML = `
      <div class="panel-title compact"><b>候选过滤</b><span>真实交易 OFF</span></div>
      <div class="risk-stack">
        <div><span>资金确认 ≥ 3</span><b>${summary.fundReady}</b></div>
        <div><span>假突破高风险</span><b class="${summary.fakeHigh ? 'down' : 'up'}">${summary.fakeHigh}</b></div>
        <div><span>等待结构</span><b>${summary.waitCount}</b></div>
        <div><span>安全状态</span><b class="down">Real OFF</b></div>
      </div>
      <p class="muted fine-print">雷达中心只展示候选证据；下单仍需策略验证、成本检查、风险预算和人工确认。</p>
    `;
  }
  if (watchEl) {
    const symbols = Array.isArray(summary.active.active_symbols) ? summary.active.active_symbols.slice(0, 28) : [];
    const recent = Array.isArray(summary.active.recent_removed) ? summary.active.recent_removed.slice(0, 5) : [];
    watchEl.innerHTML = `
      <div class="panel-title compact"><b>扫描范围</b><span>${summary.active.active_count || 0} active</span></div>
      <div class="watch-tags">${symbols.length ? symbols.map((x) => `<span>${escapeHtml(x)}</span>`).join('') : '<em>暂无活跃币种</em>'}</div>
      <div class="removed-list">
        <b>最近移除</b>
        ${recent.length ? recent.map((x) => `<span>${escapeHtml(x.symbol)} · ${escapeHtml(x.reason)}</span>`).join('') : '<span>--</span>'}
      </div>
    `;
  }
}

async function refreshDashboardOverview() {
  await refreshTop();
  const data = await j('/api/radar');
  const summary = radarSummary(data);
  const marketEl = document.getElementById('overviewMarketState');
  const metricsEl = document.getElementById('overviewMetrics');
  const directionEl = document.getElementById('overviewDirection');
  const candidatesEl = document.getElementById('overviewCandidates');
  const total = Math.max(summary.rows.length, 1);
  const longPct = Math.round(summary.longCount / total * 100);
  const shortPct = Math.round(summary.shortCount / total * 100);
  const neutralPct = Math.max(0, 100 - longPct - shortPct);

  if (marketEl) {
    marketEl.innerHTML = `
      <div class="panel-title compact"><b>市场状态</b><span>AI Insight</span></div>
      <div class="state-word">${summary.state} <i></i></div>
      <p class="muted">${summary.stateText}</p>
      <div class="status-bars">
        <div><span>AI 候选</span><b>${summary.aiCount}</b><i style="--w:${Math.min(100, summary.aiCount * 12)}%"></i></div>
        <div><span>可执行结构</span><b>${summary.actionable}</b><i style="--w:${Math.min(100, summary.actionable * 20)}%"></i></div>
        <div><span>平均评分</span><b>${fmt(summary.avgScore, 0)}</b><i style="--w:${Math.min(100, summary.avgScore)}%"></i></div>
      </div>
    `;
  }

  if (metricsEl) {
    metricsEl.innerHTML = [
      ['扫描标的', `${summary.rows.length}/50`],
      ['AI 候选', summary.aiCount],
      ['可执行结构', summary.actionable],
      ['平均评分', fmt(summary.avgScore, 0)],
      ['动态流', summary.stream.active_count || 0],
      ['活跃币种', summary.active.active_count || 0],
      ['资金确认≥3', summary.fundReady],
      ['高风险假突破', summary.fakeHigh],
    ].map((x) => `<div class="summary-card"><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');
  }

  if (directionEl) {
    directionEl.innerHTML = `
      <div class="direction-numbers">
        <div><span>LONG</span><b class="up">${summary.longCount}</b><small>${longPct}%</small></div>
        <div><span>SHORT</span><b class="down">${summary.shortCount}</b><small>${shortPct}%</small></div>
        <div><span>NEUTRAL</span><b>${summary.neutralCount}</b><small>${neutralPct}%</small></div>
      </div>
      <div class="signal-bars overview-bars">
        <i class="long" style="--w:${longPct}%"></i>
        <i class="short" style="--w:${shortPct}%"></i>
        <i class="neutral" style="--w:${neutralPct}%"></i>
      </div>
      <p class="muted fine-print">方向分布用于判断市场整体偏向，不代表可以直接开仓。</p>
    `;
  }

  if (candidatesEl) {
    const candidates = (summary.confirmed.length ? summary.confirmed : summary.rows.slice(0, 4)).slice(0, 4);
    candidatesEl.innerHTML = candidates.length ? candidates.map((c) => {
      const s = structureOf(c);
      return `
        <div class="overview-candidate">
          <b>${escapeHtml(c.base_asset || c.symbol || '--')}</b>
          <span class="badge ${c.direction === 'SHORT' ? 'red' : 'green'}">${sideText(c.direction)}</span>
          <small>${regimeText(s.regime)} · ${phaseText(s.phase)}</small>
          <em>${fmt(c.score, 0)} / 100</em>
        </div>
      `;
    }).join('') : '<p class="muted fine-print">当前没有候选。</p>';
  }
  renderRadarSidePanels(data);
}

async function refreshRadar() {
  await refreshTop();
  const data = await j('/api/radar');
  renderRadarGlance(data);
  const rows = data.top50 || [];
  const actualCount = rows.length;
  if (radarScanStillRunning(data)) {
    setScanStatus('扫描正在运行，完成后自动刷新结果...', false);
    scheduleRadarRefresh(2500);
  } else {
    clearScheduledRadarRefresh();
  }
  const meta = document.getElementById('radarEvidenceMeta');
  if (meta) {
    meta.textContent = `实际 ${actualCount} / 目标50 · 全部展开`;
  }
  const cand = document.getElementById('candidates');
  if (cand) {
    const topConfirmed = data.top5_confirmed || data.top4 || [];
    cand.innerHTML = topConfirmed.length ? topConfirmed.map((c) => {
      const s = structureOf(c);
      return `
      <div class="candidate candidate-wide">
        <div class="candidate-topline">
          <div>
            <h3>${escapeHtml(c.base_asset || c.symbol || '--')}</h3>
            <small>${escapeHtml(c.symbol || '')}</small>
          </div>
          <b>${fmt(c.price, c.price > 10 ? 2 : 5)}</b>
        </div>
        <div class="candidate-signal"><i style="--w:${clampPct(c.score)}%"></i><span>${fmt(c.score, 0)} / 100</span></div>
        <div class="candidate-badges">
          <span class="badge ${c.direction === 'SHORT' ? 'red' : 'green'}">${sideText(c.direction)}</span>
          <span class="badge ${structureBadgeClass(s.action)}">${actionText(s.action)}</span>
          <span class="badge ${c.ai_candidate ? 'green' : ''}">${c.ai_candidate ? 'AI候选' : '观察'}</span>
          <span class="badge green">确认${c.fund_confirm_count}</span>
        </div>
        <div class="candidate-lane">
          <div><span>结构</span><b>${regimeText(s.regime)}</b><small>${phaseText(s.phase)} · ${universalText(c)}</small></div>
          <div><span>入场</span><b>${entryZoneText(s)}</b><small>${c.change_5m >= 0 ? '+' : ''}${fmt(c.change_5m, 2)}% / ${c.change_15m >= 0 ? '+' : ''}${fmt(c.change_15m, 2)}%</small></div>
          <div><span>SL / T</span><b>${stopTargetText(s)}</b><small>${reasonsText(s)}</small></div>
        </div>
        <p>${c.trigger_mode} · 量能 ${fmt(c.volume_spike, 2)}x · OI ${c.oi_change >= 0 ? '+' : ''}${fmt(c.oi_change, 2)}% · 资金确认 ${c.fund_confirm_count}/${c.fund_confirm_total || 3}</p>
      </div>
    `;
    }).join('') : '<div class="candidate candidate-wide"><div class="candidate-topline"><div><h3>暂无确认候选</h3><small>WAIT</small></div><b>--</b></div><div class="candidate-badges"><span class="badge">观察</span></div><p>当前扫描目标池没有达到生产确认门槛的异动候选，先不展示低确认度标的。</p></div>';
  }

  const compactRows = document.getElementById('radarCompactRows');
  if (!compactRows) return;
  compactRows.innerHTML = rows.length ? rows.map((c) => {
    const s = structureOf(c);
    const metrics = structureMetricsOf(c);
    const fakeRisk = c.fake_breakout_risk === 'LOW' ? '低假突' : c.fake_breakout_risk === 'MEDIUM' ? '中假突' : '高假突';
    const wick = metrics.current_wick_ratio ?? c.wick_ratio;
    const maxWick = metrics.max_wick_ratio_14 ?? c.wick_ratio;
    const sideClass = c.direction === 'SHORT' ? 'red' : c.direction === 'LONG' ? 'green' : 'wait';
    const sideRgb = c.direction === 'SHORT' ? '255, 104, 122' : c.direction === 'LONG' ? '105, 231, 155' : '123, 130, 255';
    const heatPct = clampPct(50 + Number(c.heat_slope || 0) * 6);
    return `
    <article class="radar-compact-row" style="--side-rgb:${sideRgb};--score:${clampPct(c.score)}%;--heat:${heatPct}%">
      <div class="rank">#${c.rank || '--'}</div>
      <div class="symbol compact-symbol"><b>${escapeHtml(c.base_asset || c.symbol || '--')}</b><span>${escapeHtml(c.symbol || '')}</span></div>
      <div><b>${fmt(c.price, c.price > 10 ? 2 : 5)}</b><span>price</span></div>
      <div><span class="badge ${sideClass}">${sideText(c.direction)}</span></div>
      <div class="score-cell"><b>${fmt(c.score, 0)}</b><div class="score-gauge"><i></i></div></div>
      <div><span class="badge ${structureBadgeClass(s.action)}">${actionText(s.action)}</span><small>${escapeHtml(s.bias || c.direction || '--')}</small></div>
      <div class="clip"><b>${regimeText(s.regime)} · ${phaseText(s.phase)}</b><span>${escapeHtml(s.setup || c.trigger_mode || '--')}</span></div>
      <div class="clip"><b>${entryZoneText(s)}</b><span>理想 ${px(s.ideal_entry_price)}</span></div>
      <div class="clip risk-cell"><b>${stopTargetText(s)}</b><span>${reasonsText(s)} · RR ${fmt(s.risk_reward_r || 0, 2)}R</span></div>
      <div class="evidence-chips">
        <span class="${cls(c.change_5m)}">5m ${c.change_5m >= 0 ? '+' : ''}${fmt(c.change_5m, 2)}%</span>
        <span class="${cls(c.change_15m)}">15m ${c.change_15m >= 0 ? '+' : ''}${fmt(c.change_15m, 2)}%</span>
        <span>量 ${fmt(c.volume_spike, 2)}x</span>
        <span class="${cls(c.oi_change)}">OI ${c.oi_change >= 0 ? '+' : ''}${fmt(c.oi_change, 2)}%</span>
        <span>${fakeRisk}</span>
        <span>资 ${c.fund_confirm_count}/${c.fund_confirm_total || 3}</span>
        <span>ATR ${fmt(c.atr_pct, 2)}%</span>
        <span>影 ${fmt(wick, 2)}/${fmt(maxWick, 2)}</span>
        <span class="${cls(c.funding_rate)}">费 ${c.funding_rate >= 0 ? '+' : ''}${fmt(c.funding_rate * 100, 4)}%</span>
        <span>SM ${fmt(c.sm_position, 1)}%</span>
        <span class="${cls(c.heat_slope)}">热 ${c.heat_slope >= 0 ? '+' : ''}${fmt(c.heat_slope, 1)}</span>
      </div>
    </article>
  `;
  }).join('') : '<div class="radar-empty">当前后端没有返回扫描结果。</div>';
}

function yesNo(value) {
  return value ? 'ON' : 'OFF';
}

function severityClass(severity) {
  const value = String(severity || '').toUpperCase();
  if (value.includes('BLOCK')) return 'red';
  if (value === 'WARN' || value === 'WAIT') return 'wait';
  return 'green';
}

function blockerLine(blocker) {
  if (!blocker) return '';
  return `
    <div class="readiness-blocker">
      <span class="badge ${severityClass(blocker.severity)}">${escapeHtml(blocker.severity || '--')}</span>
      <b>${escapeHtml(blocker.code || '--')}</b>
      <small>${escapeHtml(blocker.source || '--')}</small>
      <p>${escapeHtml(blocker.message || '')}</p>
      <em>${escapeHtml(blocker.action || '')}</em>
    </div>
  `;
}

function graduationSummary(progress) {
  const p = progress || {};
  const real = finiteNumber(p.real_closed_samples_with_radar);
  const minimum = finiteNumber(p.minimum_real_closed_samples);
  const missing = finiteNumber(p.missing_real_closed_samples);
  const trust = p.trust_level || '--';
  if (real !== null && minimum !== null) {
    return `real ${fmt(real, 0)}/${fmt(minimum, 0)} / missing ${fmt(missing || 0, 0)} / ${trust}`;
  }
  return `${p.production_grade ? 'PRODUCTION' : 'PENDING'} / ${trust}`;
}

function codexGenerationSummary(codex) {
  const c = codex || {};
  const state = c.ready_for_generation ? 'READY' : 'BLOCKED';
  const enforcement = c.entry_enforced ? 'ENFORCED' : `NOT_ENFORCED:${c.entry_enforcement_reason || '--'}`;
  const auth = c.auth_required ? (c.auth_source || 'auth_missing') : 'auth_not_required';
  const reason = c.ready_for_generation
    ? `${c.last_status || 'idle'} / ${auth}`
    : (c.availability_reason || c.last_error || (c.command_found ? 'not_ready' : 'codex_command_missing'));
  return `${state} / ${enforcement} / ${reason}`;
}

function renderSystemReadiness(data) {
  const summary = document.getElementById('systemReadinessSummary');
  const blockersEl = document.getElementById('systemReadinessBlockers');
  const actionsEl = document.getElementById('systemReadinessActions');
  if (!summary && !blockersEl && !actionsEl) return;
  const market = data.market_data || {};
  const wait = data.wait || {};
  const paper = data.paper_learning || {};
  const graduation = paper.graduation_progress || {};
  const live = data.live_enablement || {};
  const codex = data.codex || {};
  const ws = data.websocket || {};
  const ticker = ws.ticker || {};
  const dynamic = ws.dynamic || {};
  const database = data.database || {};
  const marketHealth = market.degraded ? 'DEGRADED' : (market.warning ? 'WARN' : 'OK');
  const cards = [
    ['Overall', `${data.status || '--'} / blockers ${(data.blockers || []).length}`],
    ['Market', `${market.refresh_source || '--'} / snapshots ${market.effective_snapshot_count ?? '--'} / ${marketHealth}`],
    ['Radar Scan', `${market.scan && market.scan.in_progress ? 'RUNNING' : 'IDLE'} / top50 ${market.scan ? market.scan.top50_count ?? '--' : '--'}`],
    ['WAIT', `${wait.status || '--'} / candidates ${(wait.candidate_symbols || []).length}`],
    ['Paper Loop', `${yesNo(paper.auto_loop_enabled)} / ${paper.candidate_mode || '--'}`],
    ['Graduation', graduationSummary(graduation)],
    ['Live Stage', `${live.current_stage || '--'} / live ${yesNo(live.switches && live.switches.live_trading_enabled)}`],
    ['Codex', codexGenerationSummary(codex)],
    ['WS', `ticker ${ticker.running ? 'ON' : 'OFF'} / dyn ${dynamic.active_count ?? 0}`],
    ['DB', `${database.ok ? 'OK' : 'BAD'} / radar ${(database.tables || {}).radar_snapshots ?? '--'}`],
  ];
  if (summary) {
    summary.innerHTML = cards.map((x) => `<div class="summary-card readiness-card"><span>${x[0]}</span><b>${escapeHtml(x[1])}</b></div>`).join('');
  }
  if (blockersEl) {
    const blockers = (data.blockers || []).slice(0, 12);
    blockersEl.innerHTML = blockers.length
      ? blockers.map(blockerLine).join('')
      : '<div class="readiness-empty">No active blockers.</div>';
  }
  if (actionsEl) {
    const actions = (data.next_actions || []).slice(0, 6);
    actionsEl.innerHTML = actions.length
      ? actions.map((x) => `<li>${escapeHtml(x)}</li>`).join('')
      : '<li>No action required.</li>';
  }
}

async function refreshSystemReadiness() {
  const summary = document.getElementById('systemReadinessSummary');
  const blockersEl = document.getElementById('systemReadinessBlockers');
  if (!summary && !blockersEl) return null;
  try {
    const data = await j('/api/system/readiness');
    renderSystemReadiness(data);
    return data;
  } catch (err) {
    if (summary) {
      summary.innerHTML = `<div class="summary-card readiness-card"><span>Readiness</span><b>LOAD FAIL</b></div>`;
    }
    if (blockersEl) {
      blockersEl.innerHTML = `<div class="readiness-empty">${escapeHtml(errorMessage(err))}</div>`;
    }
    return null;
  }
}

async function refreshPositions() {
  await refreshTop();
  const data = await j('/api/positions');
  const s = data.summary || {};
  const summary = document.getElementById('summary');
  if (summary) {
    const guard = s.performance_guard || {};
    summary.innerHTML = [
      ['当前持仓', s.open_count],
      ['净浮盈', `${fmt(s.floating_pnl, 3)} USDT`],
      ['已用保证金', `${fmt(s.used_margin, 2)} USDT`],
      ['历史平仓记录', (data.closed || []).length],
      ['盈利笔数', s.win_count],
      ['亏损笔数', s.loss_count],
      ['总净盈亏', fmt(s.total_pnl, 3)],
      ['胜率', `${fmt(s.win_rate, 1)}%`],
      ['恢复模式', guard.recovery_mode ? '开启' : '关闭'],
      ['可用余额', fmt(s.available_balance, 2)],
    ].map((x) => `<div class="summary-card"><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');
  }

  const openBody = document.querySelector('#openTable tbody');
  if (openBody) {
    const openRows = data.open || [];
    if (!openRows.length) {
      openBody.innerHTML = '<tr><td colspan="18" class="muted">当前没有未平仓持仓；下方列表是历史已平仓记录，不代表现在还在持仓。</td></tr>';
    } else {
      openBody.innerHTML = openRows.map((p) => {
      const notional = Number(p.notional || 0) || Number(p.entry_price || 0) * Number(p.initial_quantity || p.quantity || 0);
      const fee = Number(p.entry_fee || 0) + Number(p.realized_fee || 0);
      const risk = p.risk_usdt ? `${fmt(p.risk_usdt, 2)} / ${fmt(p.risk_pct, 2)}%` : '--';
      return `
        <tr>
          <td>${p.symbol}</td>
          <td><span class="badge ${p.side === 'SHORT' ? 'red' : 'green'}">${sideText(p.side)}</span></td>
          <td>${p.stage}<br><small>${p.lifecycle_state || '--'}</small></td><td>${fmt(p.score, 0)}</td><td>${fmt(p.entry_price, 5)}</td><td>${fmt(p.quantity, 3)}</td>
          <td>${fmt(p.margin, 2)}</td><td>${fmt(notional, 2)}</td><td>${risk}</td><td>${fmt(fee, 4)}</td>
          <td>${fmt(p.stop_loss, 5)}<br><small>${p.lock_status || '--'}</small></td>
          <td>${fmt(p.tp1, 5)}</td><td>${fmt(p.tp2, 5)}</td><td>${fmt(p.current_price, 5)}<br>${priceStatus(p)}</td>
          <td class="${cls(p.unrealized_pnl)}">${fmt(p.unrealized_pnl, 3)}</td><td class="${cls(p.roi)}">${fmt(p.roi, 2)}%</td>
          <td>${ts(p.open_time)}</td><td><button class="btn danger" type="button" data-confirm="manual-close-position" data-busy-label="平仓中..." onclick="manualClose('${p.position_id}', event)">手动平仓</button></td>
        </tr>`;
      }).join('');
    }
  }

  const closedBody = document.querySelector('#closedTable tbody');
  if (closedBody) {
    closedBody.innerHTML = (data.closed || []).map((p) => {
      const notional = Number(p.notional || 0) || Number(p.entry_price || 0) * Number(p.quantity || 0);
      const gross = p.gross_pnl === undefined ? p.pnl : p.gross_pnl;
      const fee = p.fee === undefined ? 0 : p.fee;
      const risk = p.risk_usdt ? `${fmt(p.risk_usdt, 2)} / ${fmt(p.risk_pct, 2)}%` : '--';
      return `
        <tr>
          <td>${p.symbol}</td><td>${sideText(p.side)}</td><td>${fmt(p.entry_price, 5)}</td><td>${fmt(p.exit_price, 5)}</td><td>${fmt(p.quantity, 3)}</td>
          <td>${fmt(p.margin, 2)}</td><td>${fmt(notional, 2)}</td><td class="${cls(gross)}">${fmt(gross, 3)}</td>
          <td>${fmt(fee, 4)}</td><td class="${cls(p.pnl)}">${fmt(p.pnl, 3)}</td><td class="${cls(p.roi)}">${fmt(p.roi, 2)}%</td>
          <td>${risk}</td><td>${p.close_reason}</td><td>${ts(p.open_time)}</td><td>${ts(p.close_time)}</td><td>${p.source_signal_id}</td>
        </tr>`;
    }).join('');
  }
}

async function refreshAutoTradeDiagnostics() {
  const summary = document.getElementById('autoTradeDiagSummary');
  const body = document.querySelector('#autoTradeDiagTable tbody');
  if (!summary || !body) return;
  try {
    const data = await j('/api/autotrade/diagnostics');
    const gate = data.candidate_filter && data.candidate_filter.gate ? data.candidate_filter.gate : {};
    const counts = data.candidate_filter && data.candidate_filter.counts ? data.candidate_filter.counts : {};
    const strategyFilter = data.strategy_filter || {};
    const candidateSymbols = data.candidate_symbols_before_strategy || [];
    const loopGuard = data.loop_start_guard || {};
    const ai = data.ai_strategy || {};
    const codex = ai.codex_cli || {};
    const aiCards = [
      ['AI', ai.will_invoke_for_current_candidates ? 'INVOKE' : 'IDLE'],
      ['AI reason', ai.not_invoked_reason || '--'],
      ['Codex', codexGenerationSummary(codex)],
    ];
    summary.innerHTML = [
      ...aiCards,
      ['循环守卫', `${loopGuard.ok ? '允许' : '阻止'} · ${loopGuard.reason || '--'}`],
      ['候选来源', data.candidate_source || '--'],
      ['纸面候选', candidateSymbols.length ? candidateSymbols.join(', ') : '无'],
      ['有效分数门槛', gate.min_score === undefined ? '--' : fmt(gate.min_score, 2)],
      ['资金确认门槛', `${gate.min_fund_confirm || '--'} 项确认`],
      ['影线噪音预算', gate.max_wick_ratio === undefined ? '--' : fmt(gate.max_wick_ratio, 2)],
      ['严格候选', counts.strict_candidates || 0],
      ['全门槛候选', counts.paper_top_all_gates || 0],
      ['噪音预算通过', counts.paper_noise_budget_ok === undefined ? '--' : counts.paper_noise_budget_ok],
      ['可用策略', strategyFilter.usable_strategy_count || 0],
      ['真实交易', data.safety && data.safety.live_trading_enabled ? '开启' : '关闭'],
    ].map((x) => `<div class="summary-card"><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');

    const rows = strategyFilter.per_strategy || [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="4" class="muted">暂无可用策略诊断；通常代表没有 active/eligible 策略或当前候选为空。</td></tr>';
      return;
    }
    body.innerHTML = rows.map((row) => {
      const rejects = Object.entries(row.rejection_counts || {})
        .map(([key, value]) => `${key}: ${value}`)
        .join(' / ') || '--';
      const examples = (row.examples || []).slice(0, 3).map((x) => {
        const failed = (x.failed || []).join(', ') || 'PASS';
        return `${x.symbol} ${x.side} score=${fmt(x.score, 2)} fund=${x.fund_confirm} -> ${failed}`;
      }).join('<br>') || '--';
      return `
        <tr>
          <td>${row.name || row.strategy_id || '--'}<br><small>${row.strategy_id || ''}</small></td>
          <td>${row.matched_count || 0}/${row.candidate_count || 0}</td>
          <td>${rejects}</td>
          <td>${examples}</td>
        </tr>`;
    }).join('');
  } catch (err) {
    summary.innerHTML = `<div class="summary-card"><span>不开仓诊断</span><b>加载失败</b></div>`;
    body.innerHTML = `<tr><td colspan="4" class="muted">${errorMessage(err)}</td></tr>`;
  }
}

function attributionLabels(items) {
  if (!items || !items.length) return '--';
  return items.slice(0, 3).map((x) => x.label || x.code || x.factor || x.reason).join(' / ');
}

async function refreshDeepAttribution() {
  const summary = document.getElementById('deepAttributionSummary');
  const table = document.querySelector('#deepAttributionTable tbody');
  const tradeTable = document.querySelector('#deepTradeTable tbody');
  if (!summary || !table || !tradeTable) return;
  try {
    const data = await j('/api/learning/attribution/deep?limit=12');
    summary.innerHTML = [
      ['归因样本', data.sample_count],
      ['归因胜率', `${fmt((data.win_rate || 0) * 100, 1)}%`],
      ['归因PF', fmt(data.profit_factor, 2)],
      ['归因PnL', `${fmt(data.pnl, 3)}U`],
      ['亏损样本', data.loss_count],
      ['盈利样本', data.win_count],
      ['根因数量', (data.root_causes || []).length],
      ['行动规则', (data.action_rules || []).length],
    ].map((x) => `<div class="summary-card"><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');

    const rootRows = (data.root_causes || []).map((x) => ({ ...x, kind: '亏损根因' }));
    const driverRows = (data.profit_drivers || []).map((x) => ({ ...x, kind: '盈利驱动' }));
    const ruleRows = (data.action_rules || []).map((x) => ({
      kind: x.severity || '规则',
      label: x.label,
      samples: '--',
      win_rate: null,
      profit_factor: null,
      pnl: null,
      advice: x.action,
    }));
    table.innerHTML = [...rootRows, ...driverRows, ...ruleRows].slice(0, 24).map((x) => `
      <tr>
        <td><span class="badge ${x.kind === '盈利驱动' ? 'green' : x.kind === '亏损根因' ? 'red' : ''}">${x.kind}</span></td>
        <td>${x.label || x.code || '--'}</td>
        <td>${x.samples}</td>
        <td>${x.win_rate === null || x.win_rate === undefined ? '--' : `${fmt(x.win_rate * 100, 1)}%`}</td>
        <td>${x.profit_factor === null || x.profit_factor === undefined ? '--' : fmt(x.profit_factor, 2)}</td>
        <td class="${cls(x.pnl || 0)}">${x.pnl === null || x.pnl === undefined ? '--' : `${fmt(x.pnl, 3)}U`}</td>
        <td>${x.advice || '--'}</td>
      </tr>
    `).join('');

    tradeTable.innerHTML = (data.recent_loss_trades || []).slice(0, 12).map((x) => `
      <tr>
        <td>${x.symbol}</td>
        <td>${sideText(x.side)}</td>
        <td class="${cls(x.pnl)}">${fmt(x.pnl, 4)}U</td>
        <td>${x.close_reason || '--'}</td>
        <td>${fmt(x.margin, 2)} / ${fmt(x.notional, 2)}</td>
        <td>${fmt(x.fee, 5)}</td>
        <td>${attributionLabels(x.root_causes)}<br><small>${x.lesson || '--'}</small></td>
      </tr>
    `).join('');
  } catch (err) {
    summary.innerHTML = `<div class="summary-card"><span>AI 深度归因</span><b>加载失败</b></div>`;
    table.innerHTML = '';
    tradeTable.innerHTML = '';
  }
}

async function manualClose(id, event) {
  if (event && event.currentTarget) {
    return withButtonBusy(event, () => manualClose(id), { lockKey: `manual-close-${id}` });
  }
  await j(`/api/positions/${id}/manual-close`, { method: 'POST' });
  refreshPositions();
}

async function runAutoOnce(event) {
  if (event && event.currentTarget) {
    return withButtonBusy(event, () => runAutoOnce(), { lockKey: 'run-auto-once' });
  }
  try {
    showSettingsOut('正在执行自动交易单次决策...');
    const out = await j('/api/autotrade/run-once', { method: 'POST' });
    showSettingsOut(out);
  } catch (err) {
    showSettingsOut(`自动交易单次决策失败：${errorMessage(err)}`);
  }
}

async function startAuto(event) {
  if (event && event.currentTarget) {
    return withButtonBusy(event, () => startAuto(), { lockKey: 'start-auto' });
  }
  try {
    const out = await j('/api/autotrade/start', { method: 'POST' });
    showSettingsOut(out);
    await refreshTop();
  } catch (err) {
    showSettingsOut(`开启自动交易循环失败：${errorMessage(err)}`);
  }
}

async function stopAuto(event) {
  if (event && event.currentTarget) {
    return withButtonBusy(event, () => stopAuto(), { lockKey: 'stop-auto' });
  }
  try {
    const out = await j('/api/autotrade/stop', { method: 'POST' });
    showSettingsOut(out);
    await refreshTop();
  } catch (err) {
    showSettingsOut(`关闭自动交易循环失败：${errorMessage(err)}`);
  }
}

async function refreshAccount() {
  const el = document.getElementById('accountBox');
  if (!el) return;
  try {
    const a = await j('/api/account');
    el.innerHTML = `
      <div class="summary-card"><span>账户模式</span><b>${a.mode}${a.testnet ? ' / Testnet' : ''}</b></div>
      <div class="summary-card"><span>API状态</span><b>${a.configured ? '已配置' : '未配置'}</b></div>
      <div class="summary-card"><span>钱包余额</span><b>${fmt(a.walletBalance, 3)} USDT</b></div>
      <div class="summary-card"><span>可用余额</span><b>${fmt(a.availableBalance, 3)} USDT</b></div>
      <div class="summary-card"><span>未实现盈亏</span><b class="${cls(a.unrealizedProfit)}">${fmt(a.unrealizedProfit, 3)}</b></div>
      <div class="summary-card"><span>可交易</span><b>${a.canTrade ? '是' : '否'}</b></div>
      ${a.error_code ? `<div class="summary-card"><span>账户错误</span><b>${a.error_code}</b></div>` : ''}`;
    if (a.error_message) showSettingsOut(`Binance 账户接口错误：${a.error_message}`);
    else if (a.error) showSettingsOut(`Binance 账户接口错误：${a.error}`);
    else clearRecoveredAccountError(a);
  } catch (err) {
    el.innerHTML = `<div class="summary-card"><span>账户接口</span><b>加载失败</b></div>`;
    showSettingsOut(`账户状态加载失败：${errorMessage(err)}`);
  }
}

async function refreshMainnetConfig() {
  const el = document.getElementById('mainnetConfigStatus');
  if (!el) return;
  try {
    const c = await j('/api/config/mainnet');
    el.textContent = `${c.binance_testnet ? '测试网' : '主网'} · ${c.configured ? '已配置' : '未配置'}${c.api_key_tail ? ' · *' + c.api_key_tail : ''} · 下单${c.live_trading_enabled ? '开启' : '关闭'}`;
  } catch (err) {
    el.textContent = '配置状态加载失败';
    showSettingsOut(`主网配置状态加载失败：${errorMessage(err)}`);
  }
}

async function refreshAutoTradeParams() {
  const form = document.getElementById('autoTradeParamsForm');
  if (!form) return;
  try {
    const p = await j('/api/autotrade/params');
    document.getElementById('autoCandidateMode').value = p.auto_trading_candidate_mode || 'paper_top';
    document.getElementById('autoCandidateMinScore').value = p.auto_trading_candidate_min_score;
    document.getElementById('autoCandidateLimit').value = p.auto_trading_candidate_limit;
    document.getElementById('paperAccountEquity').value = p.paper_account_equity_usdt;
    document.getElementById('maxOpenPositions').value = p.max_open_positions;
    document.getElementById('tradeTargetMarginPct').value = p.trade_target_margin_pct;
    document.getElementById('tradeMaxMarginPct').value = p.trade_max_margin_pct;
    document.getElementById('tradeMaxRiskPct').value = p.trade_max_risk_pct;
    document.getElementById('tradeMinNetProfitUsdt').value = p.trade_min_net_profit_usdt;
    document.getElementById('tradeMinProfitCostRatio').value = p.trade_min_profit_cost_ratio;
    document.getElementById('tradeMinMarginUsdt').value = p.trade_min_margin_usdt;
    document.getElementById('tradeMinNotionalUsdt').value = p.trade_min_notional_usdt;
    document.getElementById('tradeReservedBalancePct').value = p.trade_reserved_balance_pct;
    document.getElementById('strategyMinPaperWinRate').value = p.strategy_min_paper_win_rate;
    document.getElementById('strategyMinPaperConfidence').value = p.strategy_min_paper_confidence;
    document.getElementById('strategyMinExpectedR').value = p.strategy_min_expected_r;
    document.getElementById('strategyMinTp2R').value = p.strategy_min_tp2_r;
    document.getElementById('autoUseActiveStrategyFilter').checked = !!p.auto_trading_use_active_strategy_filter;
    document.getElementById('autoUsePerformanceGuard').checked = !!p.auto_trading_use_performance_guard;
  } catch (err) {
    showSettingsOut(`闭环参数加载失败：${errorMessage(err)}`);
  }
}

async function saveAutoTradeParams(event) {
  if (event && event.currentTarget) {
    if (event.preventDefault) event.preventDefault();
    return withButtonBusy(event, () => saveAutoTradeParams(), { lockKey: 'save-auto-trade-params' });
  }
  if (event) event.preventDefault();
  const payload = {
    auto_trading_candidate_mode: document.getElementById('autoCandidateMode').value,
    auto_trading_candidate_min_score: Number(document.getElementById('autoCandidateMinScore').value),
    auto_trading_candidate_limit: Number(document.getElementById('autoCandidateLimit').value),
    auto_trading_use_active_strategy_filter: document.getElementById('autoUseActiveStrategyFilter').checked,
    auto_trading_use_performance_guard: document.getElementById('autoUsePerformanceGuard').checked,
    paper_account_equity_usdt: Number(document.getElementById('paperAccountEquity').value),
    max_open_positions: Number(document.getElementById('maxOpenPositions').value),
    trade_target_margin_pct: Number(document.getElementById('tradeTargetMarginPct').value),
    trade_max_margin_pct: Number(document.getElementById('tradeMaxMarginPct').value),
    trade_max_risk_pct: Number(document.getElementById('tradeMaxRiskPct').value),
    trade_min_net_profit_usdt: Number(document.getElementById('tradeMinNetProfitUsdt').value),
    trade_min_profit_cost_ratio: Number(document.getElementById('tradeMinProfitCostRatio').value),
    trade_min_margin_usdt: Number(document.getElementById('tradeMinMarginUsdt').value),
    trade_min_notional_usdt: Number(document.getElementById('tradeMinNotionalUsdt').value),
    trade_reserved_balance_pct: Number(document.getElementById('tradeReservedBalancePct').value),
    strategy_min_paper_win_rate: Number(document.getElementById('strategyMinPaperWinRate').value),
    strategy_min_paper_confidence: Number(document.getElementById('strategyMinPaperConfidence').value),
    strategy_min_expected_r: Number(document.getElementById('strategyMinExpectedR').value),
    strategy_min_tp2_r: Number(document.getElementById('strategyMinTp2R').value),
  };
  try {
    const out = await j('/api/autotrade/params', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showSettingsOut(out);
    await refreshAutoTradeParams();
    await refreshMainnetConfig();
  } catch (err) {
    showSettingsOut(`闭环参数保存失败：${errorMessage(err)}`);
  }
}

async function refreshLearning(event) {
  if (event && event.currentTarget) {
    return withButtonBusy(event, () => refreshLearning(), { lockKey: 'refresh-learning' });
  }
  const el = document.getElementById('learningBox');
  if (!el) return;
  try {
    const memory = await j('/api/learning/memory');
    const strategies = await j('/api/learning/strategies');
    const active = strategies.active;
    const best = (strategies.strategies || [])[0];
    const bestMetrics = best && best.metrics ? best.metrics : {};
    const calibration = memory.calibration || {};
    const attribution = memory.attribution || {};
    const radarWeights = memory.radar_weight_calibration || {};
    const radarWeightFactors = (radarWeights.adjusted_features || []).slice(0, 4).join(', ') || '--';
    const lossCause = attribution.main_loss_causes && attribution.main_loss_causes.length ? attribution.main_loss_causes[0] : null;
    const profitDriver = attribution.main_profit_drivers && attribution.main_profit_drivers.length ? attribution.main_profit_drivers[0] : null;
    el.innerHTML = [
      ['强样本', memory.joined_samples],
      ['回放样本', memory.replay ? memory.replay.replay_samples : 0],
      ['样本胜率', `${fmt(memory.win_rate * 100, 1)}%`],
      ['回放R净值', memory.replay ? fmt(memory.replay.pnl_r, 3) : '--'],
      ['事件样本', calibration.sample_count || 0],
      ['事件胜率', `${fmt((calibration.global_win_rate || 0) * 100, 1)}%`],
      ['事件PF', fmt(calibration.global_profit_factor || 0, 2)],
      ['归因样本', attribution.sample_count || 0],
      ['Radar weights', radarWeights.active ? 'ACTIVE' : (radarWeights.reason || 'default')],
      ['Weight samples', radarWeights.sample_count || 0],
      ['Weight factors', radarWeightFactors],
      ['主亏损因子', lossCause ? `${lossCause.label} / ${fmt(lossCause.pnl, 2)}U` : '--'],
      ['主盈利因子', profitDriver ? `${profitDriver.label} / ${fmt(profitDriver.pnl, 2)}U` : '--'],
      ['ACTIVE策略', active ? active.name : '无'],
      ['ACTIVE状态', active ? active.status : '未晋级'],
      ['最近候选', best ? best.status : '--'],
      ['最近回测', best ? `${bestMetrics.trades || 0}笔 / ${fmt((bestMetrics.win_rate || 0) * 100, 1)}%` : '--'],
    ].map((x) => `<div class="summary-card"><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');
  } catch (err) {
    const msg = err && err.message ? err.message : String(err);
    el.innerHTML = `<div class="summary-card"><span>策略进化</span><b>加载失败</b></div>`;
    const settingsOut = document.getElementById('settingsOut');
    if (settingsOut) settingsOut.textContent = `策略进化状态加载失败：${msg}`;
  }
}

async function evolveStrategies(useCodex, maybeUseCodex) {
  if (useCodex && useCodex.currentTarget) {
    const event = useCodex;
    const nextUseCodex = !!maybeUseCodex;
    return withButtonBusy(event, () => evolveStrategies(nextUseCodex), {
      lockKey: nextUseCodex ? 'evolve-codex' : 'evolve-local',
    });
  }
  try {
    showSettingsOut(useCodex ? '正在调用 Codex 生成候选并回测...' : '正在本地进化并回测...');
    const out = await j('/api/learning/evolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_codex: !!useCodex, promote: false }),
    });
    showSettingsOut(out);
    await refreshLearning();
  } catch (err) {
    showSettingsOut(`策略进化失败：${errorMessage(err)}`);
  }
}

async function paperRepairVerify(event) {
  if (event && event.currentTarget) {
    return withButtonBusy(event, () => paperRepairVerify(), { lockKey: 'paper-repair-verify' });
  }
  try {
    showSettingsOut('正在用纸面/回放数据修复并验证：本地进化 -> 切纸面候选 -> run-once...');
    const out = await j('/api/learning/paper-repair', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ promote: false, run_once: true }),
    });
    showSettingsOut(out);
    await refreshAutoTradeParams();
    await refreshLearning();
    if (window.PAGE === 'positions') await refreshPositions();
  } catch (err) {
    showSettingsOut(`纸面数据修复验证失败：${errorMessage(err)}`);
  }
}

function escapeHtml(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function fillStrategyQuestion(question) {
  const el = document.getElementById('strategyAiQuestion');
  if (el) {
    el.value = question;
    el.focus();
  }
}

function setStrategyAiBusy(busy, status) {
  const btn = document.getElementById('strategyAiAskBtn');
  const statusEl = document.getElementById('strategyAiStatus');
  if (btn) {
    btn.disabled = busy;
    btn.textContent = busy ? '思考中...' : '提问';
  }
  if (statusEl) statusEl.textContent = status || '';
}

async function askStrategyAi(event) {
  event.preventDefault();
  const questionEl = document.getElementById('strategyAiQuestion');
  const answerEl = document.getElementById('strategyAiAnswer');
  const contextEl = document.getElementById('strategyAiContext');
  const question = questionEl ? questionEl.value.trim() : '';
  if (!question) {
    if (answerEl) answerEl.textContent = '先输入你要问策略 AI 的问题。';
    return;
  }
  setStrategyAiBusy(true, '正在调用策略 AI，只读分析中...');
  if (answerEl) answerEl.textContent = '策略 AI 正在读取当前雷达、持仓、胜率和归因上下文...';
  try {
    const out = await j('/api/strategy-ai/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    if (!out.ok) {
      throw new Error(out.error || out.message || 'strategy_ai_failed');
    }
    const meta = `provider=${out.provider || '--'} · model=${out.model || '--'} · reasoning=${out.reasoning_effort || '--'} · tier=${out.service_tier || '--'}${out.warning ? ` · warning=${out.warning}` : ''}`;
    setStrategyAiBusy(false, meta);
    if (answerEl) answerEl.innerHTML = escapeHtml(out.answer || '').replace(/\n/g, '<br>');
    if (contextEl) contextEl.textContent = JSON.stringify(out.context || {}, null, 2);
  } catch (err) {
    setStrategyAiBusy(false, '调用失败');
    if (answerEl) answerEl.textContent = `策略 AI 调用失败：${errorMessage(err)}`;
  }
}

async function saveMainnetConfig(event) {
  if (event && event.currentTarget) {
    if (event.preventDefault) event.preventDefault();
    return withButtonBusy(event, () => saveMainnetConfig(), { lockKey: 'save-mainnet-config' });
  }
  if (event) event.preventDefault();
  const apiKey = document.getElementById('mainnetApiKey').value.trim();
  const apiSecret = document.getElementById('mainnetApiSecret').value.trim();
  try {
    if (!apiKey || !apiSecret) {
      showSettingsOut('API Key 和 API Secret 都必须填写。');
      return;
    }
    const out = await j('/api/config/mainnet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey, api_secret: apiSecret }),
    });
    showSettingsOut(out);
    if (out.ok) {
      document.getElementById('mainnetApiSecret').value = '';
      await refreshMainnetConfig();
      await refreshAccount();
      await refreshTop();
    }
  } catch (err) {
    showSettingsOut(`保存主网配置失败：${errorMessage(err)}`);
  }
}

window.refreshMainnetConfig = refreshMainnetConfig;
window.saveMainnetConfig = saveMainnetConfig;
window.refreshAutoTradeParams = refreshAutoTradeParams;
window.saveAutoTradeParams = saveAutoTradeParams;
window.refreshLearning = refreshLearning;
window.evolveStrategies = evolveStrategies;
window.paperRepairVerify = paperRepairVerify;
window.fillStrategyQuestion = fillStrategyQuestion;
window.askStrategyAi = askStrategyAi;
window.scanNow = scanNow;
window.runAutoOnce = runAutoOnce;
window.startAuto = startAuto;
window.stopAuto = stopAuto;
window.manualClose = manualClose;
window.refreshDeepAttribution = refreshDeepAttribution;
window.refreshAutoTradeDiagnostics = refreshAutoTradeDiagnostics;
window.refreshDashboardOverview = refreshDashboardOverview;
window.refreshSystemReadiness = refreshSystemReadiness;

activateNav();
refreshTop();
setInterval(refreshTop, 5000);
if (window.PAGE === 'settings' || location.pathname.startsWith('/settings')) {
  refreshAccount();
  refreshMainnetConfig();
  refreshAutoTradeParams();
  refreshLearning();
  setInterval(refreshAccount, 5000);
}
