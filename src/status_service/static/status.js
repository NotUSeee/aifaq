/* status_service — page behaviour. Vanilla ES, no dependencies.

   The server draws everything. This script:
     - swaps the live regions in from /live every 15 seconds
     - keeps "last checked" ticking and flags stale or unreachable data
     - shows times in the visitor's timezone
     - adds the readouts for the 90-day bars and the response-time chart
     - handles the uptime period switch
   With scripts off the page still shows the current status and numbers. */

(function () {
  'use strict';

  // yourbot.gg tells customers this page "refreshes itself every 15
  // seconds" (the hosting page and its FAQ). Change both or neither.
  var REFRESH_MS = 15000;
  var banner = document.getElementById('overall-banner');
  var components = document.getElementById('components');
  var daytip = document.getElementById('daytip');

  var failures = 0;
  var receivedAt = performance.now();   // when the current live regions arrived

  // ── Times in the visitor's timezone ─────────────────────────────────
  function localizeTimes(root) {
    (root || document).querySelectorAll('time.ts-local[datetime]').forEach(function (el) {
      var iso = el.getAttribute('datetime');
      var d = new Date(iso);
      if (!iso || isNaN(d.getTime())) return;
      var prefix = el.getAttribute('data-ts-prefix') || '';
      el.textContent = prefix + d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
      el.title = iso + ' (UTC)';
    });
    (root || document).querySelectorAll('time.ts-day[datetime]').forEach(function (el) {
      var d = new Date(el.getAttribute('datetime') + 'T00:00:00Z');
      if (isNaN(d.getTime())) return;
      el.textContent = d.toLocaleDateString([], { month: 'long', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
    });
    (root || document).querySelectorAll('.chart-tick[data-ts]').forEach(function (el) {
      var d = new Date(el.getAttribute('data-ts'));
      if (isNaN(d.getTime())) return;
      el.textContent = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    });
  }

  // ── "Last checked" ──────────────────────────────────────────────────
  function ago(seconds) {
    if (seconds < 10) return 'just now';
    if (seconds < 60) return Math.floor(seconds) + ' seconds ago';
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes === 1 ? '1 minute ago' : minutes + ' minutes ago';
    var hours = Math.floor(minutes / 60);
    return hours === 1 ? '1 hour ago' : hours + ' hours ago';
  }

  function tick() {
    var el = document.getElementById('checked-ago');
    if (!el || !banner) return;
    var base = parseFloat(el.getAttribute('data-age'));
    if (isNaN(base)) return;
    // Age when the server rendered it, plus the time it has sat here. Built
    // from elapsed time rather than the visitor's clock, which may be wrong.
    var age = base + (performance.now() - receivedAt) / 1000;
    var interval = parseFloat(el.getAttribute('data-interval')) || 60;
    var offline = failures >= 3;
    var stale = !offline && age > interval * 2.5;
    banner.classList.toggle('is-offline', offline);
    banner.classList.toggle('is-stale', stale);
    if (offline) {
      el.textContent = 'Cannot reach the status server. Showing the last results received, from ' + ago(age) + '.';
    } else if (stale) {
      el.textContent = 'Last checked ' + ago(age) + '. These results may be out of date.';
    } else {
      el.textContent = 'Last checked ' + ago(age);
    }
  }

  // ── Live regions ────────────────────────────────────────────────────
  function syncBannerState() {
    var main = banner && banner.querySelector('[data-live="banner"]');
    if (!main) return;
    var keep = [];
    if (banner.classList.contains('is-stale')) keep.push('is-stale');
    if (banner.classList.contains('is-offline')) keep.push('is-offline');
    banner.className = ['overall', 'overall-' + (main.getAttribute('data-overall') || 'unknown')].concat(keep).join(' ');
  }

  // What the server last sent for each region, so a region whose content
  // has not changed is left alone: no flicker, no lost hover, and nothing
  // re-announced to a screen reader.
  var lastSent = {};

  // What an element is, in words that are the same in the next copy of its
  // region: its tag, id and classes, and the nearest thing around it that
  // has a name (a component, or a "checks behind this" block).
  function identity(el, root) {
    var anchor = el.parentElement ? el.parentElement.closest('[id], details[data-key]') : null;
    if (anchor && anchor !== root && !root.contains(anchor)) anchor = null;
    return el.tagName + '#' + (el.id || '') + '.' + (el.getAttribute('class') || '') + '@' +
      (anchor ? (anchor.id || anchor.getAttribute('data-key') || '') : '');
  }

  // Returns a function that finds "the same element" in a fresh copy of the
  // region, or null when it is not there any more.
  function locator(root, el) {
    var id = identity(el, root);
    var tag = el.tagName;
    function matches(scope) {
      return Array.prototype.filter.call(scope.querySelectorAll(tag), function (x) { return identity(x, scope) === id; });
    }
    var ordinal = matches(root).indexOf(el);
    return function (freshRoot) { return ordinal < 0 ? null : (matches(freshRoot)[ordinal] || null); };
  }

  function swapRegion(fresh) {
    var name = fresh.getAttribute('data-live');
    var current = document.querySelector('[data-live="' + name + '"]');
    if (!current) return;
    var sent = fresh.innerHTML.replace(/ data-age="[^"]*"/, '');
    if (lastSent[name] === sent) {
      // Same content. Only the age of the last check moves on.
      var freshAge = fresh.querySelector('#checked-ago');
      var age = current.querySelector('#checked-ago');
      if (freshAge && age) age.setAttribute('data-age', freshAge.getAttribute('data-age'));
      return;
    }
    var opened = {}, closed = {};
    current.querySelectorAll('details[data-key]').forEach(function (d) {
      (d.open ? opened : closed)[d.getAttribute('data-key')] = true;
    });
    var node = document.importNode(fresh, true);
    node.querySelectorAll('details[data-key]').forEach(function (d) {
      var key = d.getAttribute('data-key');
      if (opened[key]) d.open = true;
      else if (closed[key]) d.open = false;
    });
    // Someone has the keyboard focus in this region. Clicking "the checks
    // behind this" is enough: the focus stays on that line while they read.
    // The fresh region still goes in, and the focus goes with it to the same
    // element. (This used to hold the region back "until the next refresh".
    // But the focus does not go away by itself, so the statuses under an
    // opened component stayed frozen for as long as the visitor looked at
    // them, with the banner above saying something else.)
    // Only when that element no longer exists does the region wait, so the
    // focus is never dropped to the top of the page.
    var focused = current.contains(document.activeElement) ? document.activeElement : null;
    var again = focused ? locator(current, focused)(node) : null;
    if (focused && !again) return;
    current.replaceWith(node);
    lastSent[name] = sent;
    if (again) again.focus({ preventScroll: true });
  }

  // Someone reading a 90-day bar or the chart with the arrow keys: which day
  // or point they are on, so a refresh does not send them back to the end.
  function readingPosition() {
    var el = document.activeElement;
    if (!el || !el.classList) return null;
    if (el.classList.contains('daybar') && activeCell && activeCell.parentNode === el) {
      return { kind: 'day', index: Array.prototype.indexOf.call(el.children, activeCell) };
    }
    if (el.classList.contains('chart-plot') && typeof el._index === 'number') {
      var tip = el.querySelector('.chart-tip');
      if (tip && !tip.hidden) return { kind: 'chart', index: el._index };
    }
    return null;
  }

  function restoreReading(reading) {
    var el = document.activeElement;
    if (!reading || !el || !el.classList) return;
    if (reading.kind === 'day' && el.classList.contains('daybar') && el.children[reading.index]) {
      showDayTip(el.children[reading.index]);
    } else if (reading.kind === 'chart' && el.classList.contains('chart-plot')) {
      showChartPoint(el, reading.index);
    }
  }

  async function refresh() {
    if (!document.querySelector('[data-live]')) return;
    try {
      var r = await fetch('/live', { cache: 'no-store', headers: { 'Accept': 'text/html' } });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var doc = new DOMParser().parseFromString(await r.text(), 'text/html');
      var reading = readingPosition();
      hideDayTip();
      doc.querySelectorAll('[data-live]').forEach(swapRegion);
      failures = 0;
      receivedAt = performance.now();
      syncBannerState();
      localizeTimes();
      restoreReading(reading);
    } catch (e) {
      failures += 1;
    }
    tick();
  }

  // ── Uptime period switch ────────────────────────────────────────────
  function setWindow(key) {
    if (!components) return;
    var ok = false;
    components.querySelectorAll('.window-switch button').forEach(function (b) {
      var on = b.getAttribute('data-window') === key;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (on) ok = true;
    });
    if (!ok) return;
    components.setAttribute('data-window', key);
    try { localStorage.setItem('yb-status-window', key); } catch (e) { /* private mode */ }
  }
  if (components) {
    components.addEventListener('click', function (ev) {
      var b = ev.target.closest('.window-switch button');
      if (b) setWindow(b.getAttribute('data-window'));
    });
    try {
      var saved = localStorage.getItem('yb-status-window');
      if (saved) setWindow(saved);
    } catch (e) { /* private mode */ }
  }

  // ── 90-day bar readout ──────────────────────────────────────────────
  var activeCell = null;

  function hideDayTip() {
    if (activeCell) activeCell.classList.remove('is-active');
    activeCell = null;
    if (daytip) daytip.hidden = true;
  }

  function showDayTip(cell) {
    if (!daytip || !cell) return;
    if (activeCell && activeCell !== cell) activeCell.classList.remove('is-active');
    activeCell = cell;
    cell.classList.add('is-active');
    daytip.textContent = cell.getAttribute('data-tip') || '';
    daytip.hidden = false;
    var rect = cell.getBoundingClientRect();
    var tipRect = daytip.getBoundingClientRect();
    var left = rect.left + rect.width / 2 - tipRect.width / 2 + window.scrollX;
    var max = document.documentElement.clientWidth - tipRect.width - 8 + window.scrollX;
    daytip.style.left = Math.max(8 + window.scrollX, Math.min(left, max)) + 'px';
    daytip.style.top = (rect.top + window.scrollY - tipRect.height - 8) + 'px';
  }

  // The whole bar is the target: the pointer picks the nearest day, so
  // nobody has to land on a cell a few pixels wide.
  function cellAt(bar, clientX) {
    var rect = bar.getBoundingClientRect();
    var cells = bar.children;
    if (!cells.length || rect.width <= 0) return null;
    var i = Math.floor((clientX - rect.left) / rect.width * cells.length);
    return cells[Math.max(0, Math.min(cells.length - 1, i))];
  }

  document.addEventListener('pointermove', function (ev) {
    var bar = ev.target.closest && ev.target.closest('.daybar');
    if (bar) showDayTip(cellAt(bar, ev.clientX));
    else if (activeCell && ev.pointerType === 'mouse') hideDayTip();
  });
  document.addEventListener('pointerdown', function (ev) {
    var bar = ev.target.closest && ev.target.closest('.daybar');
    if (bar) showDayTip(cellAt(bar, ev.clientX));
    else hideDayTip();
  });
  document.addEventListener('focusin', function (ev) {
    if (ev.target.classList && ev.target.classList.contains('daybar') && ev.target.matches(':focus-visible')) {
      showDayTip(ev.target.lastElementChild);
    }
  });
  document.addEventListener('focusout', function (ev) {
    if (ev.target.classList && ev.target.classList.contains('daybar')) hideDayTip();
  });
  document.addEventListener('keydown', function (ev) {
    var bar = ev.target.classList && ev.target.classList.contains('daybar') ? ev.target : null;
    if (bar) {
      var cells = Array.prototype.slice.call(bar.children);
      var i = activeCell && activeCell.parentNode === bar ? cells.indexOf(activeCell) : cells.length - 1;
      if (ev.key === 'ArrowLeft') i -= 1;
      else if (ev.key === 'ArrowRight') i += 1;
      else if (ev.key === 'Home') i = 0;
      else if (ev.key === 'End') i = cells.length - 1;
      else if (ev.key === 'Escape') { hideDayTip(); return; }
      else return;
      ev.preventDefault();
      showDayTip(cells[Math.max(0, Math.min(cells.length - 1, i))]);
      return;
    }
    if (ev.key === 'Escape') {
      var sub = document.getElementById('subscribe');
      if (sub && sub.open) { sub.open = false; sub.querySelector('summary').focus(); }
    }
  });
  window.addEventListener('scroll', hideDayTip, { passive: true });
  window.addEventListener('resize', hideDayTip);

  // ── Response-time chart readout ─────────────────────────────────────
  function chartParts(plot) {
    var wrap = plot.closest('.chart-wrap');
    if (!wrap) return null;
    if (!wrap._points) {
      try { wrap._points = JSON.parse(wrap.getAttribute('data-points') || '[]'); }
      catch (e) { wrap._points = []; }
    }
    return {
      points: wrap._points,
      cross: plot.querySelector('.chart-cross'),
      dot: plot.querySelector('.chart-cross-dot'),
      tip: plot.querySelector('.chart-tip'),
    };
  }

  function showChartPoint(plot, index) {
    var parts = chartParts(plot);
    if (!parts || !parts.points.length) return;
    index = Math.max(0, Math.min(parts.points.length - 1, index));
    plot._index = index;
    var p = parts.points[index];
    parts.cross.style.left = p.x + '%';
    parts.dot.style.left = p.x + '%';
    parts.dot.style.top = p.y + '%';
    parts.cross.hidden = false;
    parts.dot.hidden = false;

    var when = new Date(p.t);
    parts.tip.textContent = '';
    var value = document.createElement('strong');
    value.textContent = p.p50 + ' ms';
    parts.tip.appendChild(value);
    var rest = ' typical';
    if (p.p95 > p.p50) rest += ' · 95% under ' + p.p95 + ' ms';
    if (!isNaN(when.getTime())) rest += ' · ' + when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    parts.tip.appendChild(document.createTextNode(rest));
    parts.tip.hidden = false;
    // Keep the readout inside the plot at either edge.
    var half = parts.tip.offsetWidth / 2;
    var x = p.x / 100 * plot.clientWidth;
    parts.tip.style.left = Math.max(half, Math.min(x, plot.clientWidth - half)) + 'px';
  }

  function hideChart(plot) {
    var parts = chartParts(plot);
    if (!parts) return;
    parts.cross.hidden = true;
    parts.dot.hidden = true;
    parts.tip.hidden = true;
  }

  function nearestPoint(plot, clientX) {
    var parts = chartParts(plot);
    if (!parts || !parts.points.length) return -1;
    var rect = plot.getBoundingClientRect();
    var x = (clientX - rect.left) / rect.width * 100;
    var best = 0, bestDist = Infinity;
    parts.points.forEach(function (p, i) {
      var d = Math.abs(p.x - x);
      if (d < bestDist) { bestDist = d; best = i; }
    });
    return best;
  }

  document.addEventListener('pointermove', function (ev) {
    var plot = ev.target.closest && ev.target.closest('.chart-plot');
    document.querySelectorAll('.chart-plot').forEach(function (other) {
      if (other !== plot && other !== document.activeElement) hideChart(other);
    });
    if (plot) showChartPoint(plot, nearestPoint(plot, ev.clientX));
  });
  document.addEventListener('pointerdown', function (ev) {
    var plot = ev.target.closest && ev.target.closest('.chart-plot');
    if (plot) showChartPoint(plot, nearestPoint(plot, ev.clientX));
  });
  document.addEventListener('focusin', function (ev) {
    if (ev.target.classList && ev.target.classList.contains('chart-plot') && ev.target.matches(':focus-visible')) {
      var parts = chartParts(ev.target);
      if (parts) showChartPoint(ev.target, parts.points.length - 1);
    }
  });
  document.addEventListener('focusout', function (ev) {
    if (ev.target.classList && ev.target.classList.contains('chart-plot')) hideChart(ev.target);
  });
  document.addEventListener('keydown', function (ev) {
    var plot = ev.target.classList && ev.target.classList.contains('chart-plot') ? ev.target : null;
    if (!plot) return;
    var parts = chartParts(plot);
    if (!parts || !parts.points.length) return;
    var i = typeof plot._index === 'number' ? plot._index : parts.points.length - 1;
    if (ev.key === 'ArrowLeft') i -= 1;
    else if (ev.key === 'ArrowRight') i += 1;
    else if (ev.key === 'Home') i = 0;
    else if (ev.key === 'End') i = parts.points.length - 1;
    else if (ev.key === 'Escape') { hideChart(plot); return; }
    else return;
    ev.preventDefault();
    showChartPoint(plot, i);
  });

  // ── Subscribe menu: close on an outside click ───────────────────────
  document.addEventListener('click', function (ev) {
    var sub = document.getElementById('subscribe');
    if (sub && sub.open && !sub.contains(ev.target)) sub.open = false;
  });

  // ── Deep links (/#announcement-3, /#incident-7 from the RSS feed) ───
  if (location.hash) {
    var target = document.getElementById(location.hash.slice(1));
    if (target) {
      var holder = target.closest('details') || (target.tagName === 'DETAILS' ? target : null);
      if (holder) holder.open = true;
      (target.closest('.event') || target).scrollIntoView({ block: 'start' });
    }
  }

  // ── Lifecycle ───────────────────────────────────────────────────────
  localizeTimes();
  tick();
  setInterval(tick, 1000);
  setInterval(function () { if (!document.hidden) refresh(); }, REFRESH_MS);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && performance.now() - receivedAt > REFRESH_MS) refresh();
  });
})();
