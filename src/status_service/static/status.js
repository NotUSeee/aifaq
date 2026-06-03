/* status_service — page polling + ECharts + stale-data detection.
   Vanilla ES, no framework. Four intervals at distinct cadences:
     /api          15s — per-service current
     /api/shards   15s — bot cluster grid
     /api/graph    60s — response time chart
     /api/timeline 5min — 90-day uptime timeline
*/

(function () {
  'use strict';

  const POLL_CURRENT_MS  = 15000;
  const POLL_SHARDS_MS   = 15000;
  const POLL_GRAPH_MS    = 60000;
  const POLL_TIMELINE_MS = 300000;

  let lastFetchOk = true;
  let consecutiveFailures = 0;
  let chart = null;
  let probeIntervalSec = 60;

  // ── DOM refs ────────────────────────────────────────────────────────
  const overallPill = document.getElementById('overall-pill');
  const lastUpdatedText = document.getElementById('last-updated-text');
  const statusPulse = document.getElementById('status-pulse');
  const slaActual = document.getElementById('sla-actual');
  const servicesList = document.getElementById('services-list');
  const shardsGrid = document.getElementById('shards-grid');
  const shardsOnline = document.getElementById('shards-online');
  const shardsGuilds = document.getElementById('shards-guilds');
  const shardsClusters = document.getElementById('shards-clusters');
  const timelineGrid = document.getElementById('timeline-grid');

  // ── Fetch helpers ───────────────────────────────────────────────────
  async function fetchJSON(url) {
    const r = await fetch(url, { headers: { 'Cache-Control': 'no-cache' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  function noteFetchOk() {
    lastFetchOk = true;
    consecutiveFailures = 0;
    statusPulse.classList.remove('is-down', 'is-stale');
  }

  function noteFetchFail() {
    consecutiveFailures += 1;
    if (consecutiveFailures >= 3) {
      lastFetchOk = false;
      statusPulse.classList.add('is-down');
      lastUpdatedText.textContent = 'Status page connection error';
    }
  }

  // ── Stale data detector ────────────────────────────────────────────
  function checkStaleness(meta) {
    if (!meta) return;
    if (typeof meta.probe_interval_seconds === 'number') {
      probeIntervalSec = meta.probe_interval_seconds;
    }
    const stale = meta.staleness_seconds;
    if (stale === null || stale === undefined) return;
    const threshold = probeIntervalSec * 2;
    if (stale > threshold) {
      const minutes = Math.floor(stale / 60);
      overallPill.textContent = 'Status data stale — last update ' + minutes + ' min ago';
      overallPill.className = 'status-pill status-pill-stale';
      statusPulse.classList.add('is-stale');
    }
  }

  // ── /api → current services + overall ──────────────────────────────
  async function pollCurrent() {
    try {
      const data = await fetchJSON('/api');
      noteFetchOk();
      checkStaleness(data.meta);
      renderServices(data.current);
      if (data.meta && data.meta.staleness_seconds !== null && data.meta.staleness_seconds <= probeIntervalSec * 2) {
        renderOverall(data.overall);
      }
      updateLastUpdated();
      if (data.meta && data.meta.sla) {
        slaActual.textContent = data.meta.sla.actual_pct.toFixed(3) + '%';
        slaActual.classList.toggle('sla-below', data.meta.sla.below_target);
      }
    } catch (e) {
      noteFetchFail();
    }
  }

  function renderOverall(overall) {
    overallPill.className = 'status-pill status-pill-' + overall;
    const labels = {
      operational:    'All systems operational',
      degraded:       'Degraded performance',
      partial_outage: 'Partial outage',
      outage:         'Major outage',
      unknown:        'Initializing…',
    };
    overallPill.textContent = labels[overall] || 'Status: ' + overall;
  }

  function renderServices(currents) {
    if (!servicesList) return;
    const html = currents.map(function (s) {
      const st = safeStatus(s.status);
      const ms = (s.response_ms !== null && s.response_ms !== undefined)
        ? '<span class="service-row-ms">' + s.response_ms + 'ms</span>'
        : '';
      return (
        '<li class="service-row service-row-' + st + '" data-service="' + escapeAttr(s.name) + '">' +
        '  <span class="service-row-name">' + escapeHtml(s.name) + '</span>' +
        '  <span class="service-row-meta">' +
        ms +
        '    <span class="service-row-pill status-pill-' + st + '">' + escapeHtml(s.status) + '</span>' +
        '  </span>' +
        '</li>'
      );
    }).join('');
    servicesList.innerHTML = html;
  }

  function updateLastUpdated() {
    if (!lastUpdatedText) return;
    if (!lastFetchOk) return;
    lastUpdatedText.textContent = 'Live · updating every 15s';
  }

  // ── /api/shards → bot cluster grid ─────────────────────────────────
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
    shardsOnline.textContent = (totals.online || 0) + '/' + (totals.shards || 0);
    shardsGuilds.textContent = (totals.guilds || 0).toLocaleString();
    shardsClusters.textContent = (data.clusters || []).length;

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
             '  <div class="shard-cluster-label">Cluster ' + idx + '</div>' +
             '  <div class="shard-cluster-shards">' + dots + '</div>' +
             '  <div class="shard-cluster-detail" role="tooltip">' +
             '    <div class="shard-cluster-detail-title">Cluster ' + idx + '</div>' +
             '    <div class="shard-cluster-detail-row"><span>Status</span><span class="shard-cluster-detail-val ' + clusterStatus + '">' +
             clusterStatus.charAt(0).toUpperCase() + clusterStatus.slice(1) + '</span></div>' +
             '    <div class="shard-cluster-detail-row"><span>Shards</span><span class="shard-cluster-detail-val">' +
             online + '/' + shards.length + ' online</span></div>' +
             '    <div class="shard-cluster-detail-row"><span>Guilds</span><span class="shard-cluster-detail-val">' +
             guilds.toLocaleString() + '</span></div>' +
             '    <div class="shard-cluster-detail-row"><span>Avg latency</span><span class="shard-cluster-detail-val">' +
             (avgLatency !== null ? avgLatency + 'ms' : '—') + '</span></div>' +
             '    <div class="shard-cluster-detail-chips">' + chips + '</div>' +
             '  </div>' +
             '</div>';
    }).join('');
  }

  // ── /api/graph → ECharts response-time chart ───────────────────────
  async function loadChart() {
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
        name: name,
        type: 'line',
        smooth: true,
        showSymbol: false,
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

  // ── /api/timeline → 90-day uptime grid ─────────────────────────────
  async function loadTimeline() {
    try {
      const data = await fetchJSON('/api/timeline?days=90');
      noteFetchOk();
      renderTimeline(data);
    } catch (e) {
      noteFetchFail();
    }
  }

  function renderTimeline(data) {
    if (!timelineGrid) return;
    const days = data.days || 90;
    const series = data.series || {};
    const names = Object.keys(series);
    if (names.length === 0) {
      timelineGrid.innerHTML = '<div style="color:var(--muted); font-family:var(--font-mono); font-size:0.78rem;">No timeline data yet — collecting…</div>';
      return;
    }

    const today = new Date();
    const dayKeys = [];
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(today.getTime() - i * 86400000);
      dayKeys.push(d.toISOString().slice(0, 10));
    }

    const html = names.map(function (name) {
      const byDay = {};
      (series[name] || []).forEach(function (d) { byDay[d.day] = d; });
      let totalPct = 0, totalCount = 0;
      const blocks = dayKeys.map(function (day) {
        const entry = byDay[day];
        if (!entry) return '<div class="timeline-block empty" title="' + day + ' · no data"></div>';
        totalPct += entry.uptime_pct;
        totalCount += 1;
        let cls = '';
        if (entry.uptime_pct < 99) cls = 'down';
        else if (entry.uptime_pct < 99.9) cls = 'degraded';
        return '<div class="timeline-block ' + cls + '" title="' + day + ' · ' + entry.uptime_pct.toFixed(2) + '%"></div>';
      }).join('');
      const avg = totalCount > 0 ? (totalPct / totalCount) : 100;
      return '<div class="timeline-row">' +
             '  <span class="timeline-row-name">' + escapeHtml(name) + '</span>' +
             '  <span class="timeline-row-blocks">' + blocks + '</span>' +
             '  <span class="timeline-row-pct">' + avg.toFixed(2) + '%</span>' +
             '</div>';
    }).join('');
    timelineGrid.innerHTML = html;
  }

  // ── Helpers ─────────────────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c];
    });
  }
  function escapeAttr(s) { return escapeHtml(s); }
  // Whitelist a status string before it flows into a CSS class name. Upstream
  // (/api, /api/shards) returns enums, but this keeps a buggy/compromised
  // source from injecting markup via the class attribute.
  const KNOWN_STATUS = ['operational', 'degraded', 'down', 'unknown', 'partial_outage', 'outage', 'stale'];
  function safeStatus(s) { return KNOWN_STATUS.indexOf(s) !== -1 ? s : 'unknown'; }

  // ── Polling lifecycle ──────────────────────────────────────────────
  let intervals = [];
  function startPolling() {
    pollCurrent();
    pollShards();
    loadChart();
    loadTimeline();
    intervals.push(setInterval(pollCurrent, POLL_CURRENT_MS));
    intervals.push(setInterval(pollShards, POLL_SHARDS_MS));
    intervals.push(setInterval(loadChart, POLL_GRAPH_MS));
    intervals.push(setInterval(loadTimeline, POLL_TIMELINE_MS));
  }
  function stopPolling() {
    intervals.forEach(clearInterval);
    intervals = [];
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stopPolling();
    else if (intervals.length === 0) startPolling();
  });
  window.addEventListener('resize', function () { if (chart) chart.resize(); });

  startPolling();
})();
