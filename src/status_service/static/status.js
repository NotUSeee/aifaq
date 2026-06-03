/* status_service — clean status page: live polling, inline 90-day uptime
   bars, grouped components, collapsible live metrics + ECharts.
   Vanilla ES, no framework.
     /api          15s  — per-service current + overall
     /api/timeline 5min — inline 90-day uptime bars (+ overall headline)
     /api/shards   15s  — bot cluster grid   (lazy: first metrics open)
     /api/graph    60s  — response-time chart (lazy: first metrics open)
*/

(function () {
  'use strict';

  const POLL_CURRENT_MS  = 15000;
  const POLL_UPTIME_MS   = 300000;
  const POLL_SHARDS_MS   = 15000;
  const POLL_GRAPH_MS    = 60000;

  let lastFetchOk = true;
  let consecutiveFailures = 0;
  let chart = null;
  let probeIntervalSec = 60;
  let metricsStarted = false;

  // ── DOM refs ────────────────────────────────────────────────────────
  const overallBanner   = document.getElementById('overall-banner');
  const overallHeadline = document.getElementById('overall-headline');
  const overallIcon     = document.getElementById('overall-icon');
  const overallUptime   = document.getElementById('overall-uptime');
  const lastUpdatedText = document.getElementById('last-updated-text');
  const statusPulse     = document.getElementById('status-pulse');
  const shardsGrid      = document.getElementById('shards-grid');
  const shardsOnline    = document.getElementById('shards-online');
  const shardsGuilds    = document.getElementById('shards-guilds');
  const shardsClusters  = document.getElementById('shards-clusters');
  const liveMetrics     = document.getElementById('live-metrics');

  // ── Helpers ─────────────────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c];
    });
  }
  function escapeAttr(s) { return escapeHtml(s); }
  const KNOWN_STATUS = ['operational', 'degraded', 'down', 'unknown', 'partial_outage', 'outage', 'stale'];
  function safeStatus(s) { return KNOWN_STATUS.indexOf(s) !== -1 ? s : 'unknown'; }
  function pillLabel(s) {
    return s === 'operational' ? 'Operational'
         : s === 'degraded'    ? 'Degraded'
         : s === 'down'        ? 'Down' : 'Unknown';
  }
  const ICON_OK   = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  const ICON_DOWN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/></svg>';
  const ICON_WARN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>';
  function iconFor(o) { return o === 'operational' ? ICON_OK : o === 'outage' ? ICON_DOWN : ICON_WARN; }

  async function fetchJSON(url) {
    const r = await fetch(url, { headers: { 'Cache-Control': 'no-cache' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  function noteFetchOk() {
    lastFetchOk = true;
    consecutiveFailures = 0;
    if (statusPulse) statusPulse.classList.remove('is-down', 'is-stale');
  }
  function noteFetchFail() {
    consecutiveFailures += 1;
    if (consecutiveFailures >= 3) {
      lastFetchOk = false;
      if (statusPulse) statusPulse.classList.add('is-down');
      if (lastUpdatedText) lastUpdatedText.textContent = 'Connection error — showing last known status';
    }
  }

  // ── Stale-data detector ─────────────────────────────────────────────
  function checkStaleness(meta) {
    if (!meta) return false;
    if (typeof meta.probe_interval_seconds === 'number') probeIntervalSec = meta.probe_interval_seconds;
    const stale = meta.staleness_seconds;
    if (stale === null || stale === undefined) return false;
    if (stale > probeIntervalSec * 2) {
      const minutes = Math.floor(stale / 60);
      if (overallBanner) overallBanner.className = 'overall overall-degraded';
      if (overallHeadline) overallHeadline.textContent = 'Status data stale — last update ' + minutes + ' min ago';
      if (overallIcon) overallIcon.innerHTML = ICON_WARN;
      if (statusPulse) statusPulse.classList.add('is-stale');
      return true;
    }
    return false;
  }

  // ── /api → components + overall ─────────────────────────────────────
  async function pollCurrent() {
    try {
      const data = await fetchJSON('/api');
      noteFetchOk();
      const stale = checkStaleness(data.meta);
      renderComponents(data.current);
      if (!stale) renderOverall(data.overall);
      updateLastUpdated();
    } catch (e) {
      noteFetchFail();
    }
  }

  function renderOverall(overall) {
    overall = safeStatus(overall);
    if (overallBanner) overallBanner.className = 'overall overall-' + overall;
    if (overallIcon) overallIcon.innerHTML = iconFor(overall);
    const labels = {
      operational:    'All Systems Operational',
      degraded:       'Degraded Performance',
      partial_outage: 'Partial Outage',
      outage:         'Major Outage',
      unknown:        'Checking status…',
    };
    if (overallHeadline) overallHeadline.textContent = labels[overall] || ('Status: ' + overall);
  }

  // Update the server-rendered component rows in place (no regroup in JS).
  function renderComponents(currents) {
    if (!currents) return;
    const byName = {};
    currents.forEach(function (s) { byName[s.name] = s; });
    document.querySelectorAll('.component-row[data-service]').forEach(function (row) {
      const s = byName[row.getAttribute('data-service')];
      if (!s) return;
      const st = safeStatus(s.status);
      row.className = 'component-row component-row-' + st;
      const pill = row.querySelector('[data-role="pill"]');
      if (pill) {
        pill.className = 'status-pill status-pill-' + st;
        pill.textContent = pillLabel(st);
      }
    });
  }

  function updateLastUpdated() {
    if (!lastUpdatedText || !lastFetchOk) return;
    lastUpdatedText.textContent = 'Live · updating every 15s';
  }

  // ── /api/timeline → inline 90-day uptime bars + overall headline ────
  async function loadUptime() {
    try {
      const data = await fetchJSON('/api/timeline?days=90');
      noteFetchOk();
      renderUptime(data);
    } catch (e) {
      noteFetchFail();
    }
  }

  function renderUptime(data) {
    const days = data.days || 90;
    const series = data.series || {};
    const today = new Date();
    const dayKeys = [];
    for (let i = days - 1; i >= 0; i--) {
      dayKeys.push(new Date(today.getTime() - i * 86400000).toISOString().slice(0, 10));
    }
    let overallSum = 0, overallCount = 0;
    document.querySelectorAll('.component-row[data-service]').forEach(function (row) {
      const name = row.getAttribute('data-service');
      const bar = row.querySelector('[data-role="bar"]');
      const pct = row.querySelector('[data-role="pct"]');
      if (!bar) return;
      const byDay = {};
      (series[name] || []).forEach(function (d) { byDay[d.day] = d; });
      let sum = 0, count = 0;
      bar.innerHTML = dayKeys.map(function (day) {
        const entry = byDay[day];
        if (!entry) return '<div class="uptime-day empty" title="' + day + ' · no data"></div>';
        sum += entry.uptime_pct; count += 1;
        let cls = entry.uptime_pct < 99 ? 'down' : entry.uptime_pct < 99.9 ? 'degraded' : '';
        return '<div class="uptime-day ' + cls + '" title="' + day + ' · ' + entry.uptime_pct.toFixed(2) + '%"></div>';
      }).join('');
      const avg = count > 0 ? sum / count : 100;
      if (pct) pct.textContent = avg.toFixed(2) + '%';
      if (count > 0) { overallSum += avg; overallCount += 1; }
    });
    if (overallUptime && overallCount > 0) {
      overallUptime.textContent = (overallSum / overallCount).toFixed(2) + '% uptime over 90 days';
    }
  }

  // ── /api/shards → bot cluster grid (lazy) ───────────────────────────
  async function pollShards() {
    try {
      const data = await fetchJSON('/api/shards');
      noteFetchOk();
      renderShards(data);
    } catch (e) {
      noteFetchFail();
    }
  }

  function renderShards(data) {
    if (!shardsGrid) return;
    const totals = data.totals || {};
    if (shardsOnline) shardsOnline.textContent = (totals.online || 0) + '/' + (totals.shards || 0);
    if (shardsGuilds) shardsGuilds.textContent = (totals.guilds || 0).toLocaleString();
    if (shardsClusters) shardsClusters.textContent = (data.clusters || []).length;
    if (!data.clusters || data.clusters.length === 0) {
      shardsGrid.innerHTML = '<div style="color:var(--muted); font-family:var(--font-mono); font-size:0.78rem;">No shard data yet — waiting for first probe…</div>';
      return;
    }
    shardsGrid.innerHTML = data.clusters.map(function (c, idx) {
      const shards = c.shards || [];
      let online = 0, degraded = 0, down = 0, guilds = 0, latSum = 0, latCount = 0;
      shards.forEach(function (s) {
        if (s.status === 'operational') online += 1;
        else if (s.status === 'degraded') degraded += 1;
        else if (s.status === 'down') down += 1;
        guilds += s.guild_count || 0;
        if (typeof s.latency_ms === 'number') { latSum += s.latency_ms; latCount += 1; }
      });
      const avgLatency = latCount ? Math.round(latSum / latCount) : null;
      const clusterStatus = down ? 'down' : degraded ? 'degraded' : online ? 'operational' : 'unknown';
      const dots = shards.map(function (s) {
        const tip = 'Shard ' + s.shard_id + ' · ' + s.status +
          (typeof s.latency_ms === 'number' ? ' · ' + s.latency_ms + 'ms' : '') +
          ' · ' + (s.guild_count || 0).toLocaleString() + ' guilds';
        return '<span class="shard-dot ' + safeStatus(s.status) + '" title="' + escapeAttr(tip) + '"></span>';
      }).join('');
      const chips = shards.map(function (s) {
        const ms = typeof s.latency_ms === 'number' ? ' · ' + s.latency_ms + 'ms' : '';
        return '<span class="shard-chip ' + safeStatus(s.status) + '">#' + s.shard_id + ms + '</span>';
      }).join('');
      return '<div class="shard-cluster shard-cluster-' + clusterStatus + '">' +
        '<div class="shard-cluster-label">Cluster ' + idx + '</div>' +
        '<div class="shard-cluster-shards">' + dots + '</div>' +
        '<div class="shard-cluster-detail" role="tooltip">' +
        '<div class="shard-cluster-detail-title">Cluster ' + idx + '</div>' +
        '<div class="shard-cluster-detail-row"><span>Status</span><span class="shard-cluster-detail-val ' + clusterStatus + '">' + clusterStatus.charAt(0).toUpperCase() + clusterStatus.slice(1) + '</span></div>' +
        '<div class="shard-cluster-detail-row"><span>Shards</span><span class="shard-cluster-detail-val">' + online + '/' + shards.length + ' online</span></div>' +
        '<div class="shard-cluster-detail-row"><span>Guilds</span><span class="shard-cluster-detail-val">' + guilds.toLocaleString() + '</span></div>' +
        '<div class="shard-cluster-detail-row"><span>Avg latency</span><span class="shard-cluster-detail-val">' + (avgLatency !== null ? avgLatency + 'ms' : '—') + '</span></div>' +
        '<div class="shard-cluster-detail-chips">' + chips + '</div>' +
        '</div></div>';
    }).join('');
  }

  // ── /api/graph → ECharts response-time chart (lazy) ─────────────────
  async function loadChart() {
    if (!liveMetrics || !liveMetrics.open) return;
    try {
      const data = await fetchJSON('/api/graph?hours=6');
      noteFetchOk();
      renderChart(data);
    } catch (e) {
      noteFetchFail();
    }
  }

  function renderChart(data) {
    if (typeof echarts === 'undefined') return;
    const el = document.getElementById('chart');
    if (!el) return;
    if (!chart) chart = echarts.init(el, null, { renderer: 'svg' });
    const palette = ['#3b82f6', '#34c4f4', '#9b7fe8', '#74b3ff', '#6bcb8b', '#e0a33e'];
    const seriesNames = Object.keys(data.series || {}).slice(0, 5);
    const series = seriesNames.map(function (name, i) {
      return {
        name: name, type: 'line', smooth: true, showSymbol: false,
        data: (data.series[name] || []).map(function (p) { return [p.t, p.ms]; }),
        lineStyle: { color: palette[i % palette.length], width: 1.5 },
        itemStyle: { color: palette[i % palette.length] },
      };
    });
    chart.setOption({
      backgroundColor: 'transparent',
      grid: { top: 40, left: 50, right: 20, bottom: 30 },
      tooltip: { trigger: 'axis', backgroundColor: 'rgba(7,8,13,0.95)', borderColor: 'rgba(59,130,246,0.3)', textStyle: { color: '#dde1f2' } },
      legend: { textStyle: { color: '#dde1f2', fontFamily: 'JetBrains Mono', fontSize: 11 }, top: 5 },
      xAxis: { type: 'time', axisLine: { lineStyle: { color: 'rgba(96,128,210,0.2)' } }, axisLabel: { color: '#9698b0', fontFamily: 'JetBrains Mono', fontSize: 10 } },
      yAxis: { type: 'value', name: 'ms', nameTextStyle: { color: '#9698b0', fontFamily: 'JetBrains Mono' }, splitLine: { lineStyle: { color: 'rgba(96,128,210,0.08)' } }, axisLabel: { color: '#9698b0', fontFamily: 'JetBrains Mono', fontSize: 10 } },
      series: series,
    });
  }

  // ── Lazy-start the live-metrics section on first open ───────────────
  if (liveMetrics) {
    liveMetrics.addEventListener('toggle', function () {
      if (!liveMetrics.open) return;
      if (!metricsStarted) {
        metricsStarted = true;
        pollShards();
        loadChart();
        setInterval(function () { if (!document.hidden && liveMetrics.open) pollShards(); }, POLL_SHARDS_MS);
        setInterval(function () { if (!document.hidden && liveMetrics.open) loadChart(); }, POLL_GRAPH_MS);
      } else if (chart) {
        chart.resize();
      }
    });
  }

  // ── Lifecycle ───────────────────────────────────────────────────────
  pollCurrent();
  loadUptime();
  setInterval(function () { if (!document.hidden) pollCurrent(); }, POLL_CURRENT_MS);
  setInterval(function () { if (!document.hidden) loadUptime(); }, POLL_UPTIME_MS);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) pollCurrent(); });
  window.addEventListener('resize', function () { if (chart) chart.resize(); });
})();
