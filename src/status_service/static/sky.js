/* Vendored from the main YourBot site (mmo_maid/apps/dashboard/static/sky.js,
 * served as yourbot.gg/static/sky.js?v=2) so the status page carries the same
 * starfield while staying self-contained: it must render when yourbot.gg is
 * down. Keep it identical to the site's copy; do not edit it here. */
/* sky.js — the site-wide starfield.
 *
 * Drives every <canvas data-sky> on the page: a slow upward drift plus a
 * per-star twinkle, sized to the element (the shared _sky.html layer is a
 * fixed, full-viewport box; /about mounts its own canvas inside .ab-cosmos
 * so its stars ride above that page's ambient glow). Both mounts share this
 * file — there is exactly one starfield implementation.
 *
 * Star colour comes from the --star-rgb custom property, read off the canvas
 * itself so each mount inherits whatever its host scope defines, and re-read
 * when the footer toggle stamps data-theme on <html>.
 *
 * Alpha, radius and density come from custom properties too, for the same
 * reason: a light page needs a stronger field than a dark one to read at all,
 * and hard-coding them here forced both themes to share one setting. Every
 * default below is the value this file used before they were extracted, so a
 * scope that defines none of them renders exactly as it always has — dark mode
 * included. Only _sky.html's [data-theme="light"] block overrides them.
 *
 * Reduced motion: the field is still drawn, once, without drift or twinkle —
 * the preference is about movement, not about a blank sky.
 */
(function () {
  'use strict';

  var canvases = document.querySelectorAll('canvas[data-sky]');
  if (!canvases.length) return;

  var reduced = !!(window.matchMedia
                   && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var fields = [];

  function build(canvas) {
    if (!canvas.getContext) return null;
    var ctx = canvas.getContext('2d');
    if (!ctx) return null;

    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var stars = [];
    var color = '221,225,242';
    var W = 0, H = 0;
    // Defaults are this file's historical hard-coded values. Do not change them
    // to suit one theme: override in CSS instead, so the other theme is unmoved.
    var P = { aMin: 0.15, aSpan: 0.55, rMin: 0.4, rSpan: 1.15, density: 11000 };

    function num(cs, name, dflt) {
      var v = parseFloat(cs.getPropertyValue(name));
      return isFinite(v) && v >= 0 ? v : dflt;
    }

    function readParams() {
      var cs = getComputedStyle(canvas);
      var v = cs.getPropertyValue('--star-rgb').trim();
      if (v) color = v;
      P.aMin    = num(cs, '--star-alpha-min',  0.15);
      P.aSpan   = num(cs, '--star-alpha-span', 0.55);
      P.rMin    = num(cs, '--star-r-min',      0.4);
      P.rSpan   = num(cs, '--star-r-span',     1.15);
      P.density = num(cs, '--star-density',    11000) || 11000;
    }

    function wantCount() {
      return Math.min(170, Math.round(W * H / P.density));
    }

    function seed() {
      W = canvas.clientWidth;
      H = canvas.clientHeight;
      if (!W || !H) { stars = []; return; }
      canvas.width = W * dpr;
      canvas.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // Density by area, capped: ~170 stars on a large desktop viewport.
      var count = wantCount();
      stars = [];
      for (var i = 0; i < count; i++) {
        stars.push({
          x: Math.random() * W, y: Math.random() * H,
          // Store the 0..1 basis rather than the derived radius/alpha, so a
          // theme change can re-derive both from the new params WITHOUT
          // re-seeding. Re-seeding would jump every star to a new position
          // mid-crossfade, which reads as the field glitching.
          rb: Math.random(),
          ab: Math.random(),
          tw: 0.5 + Math.random() * 1.6,
          ph: Math.random() * Math.PI * 2,
          vy: 0.008 + Math.random() * 0.03
        });
      }
    }

    function draw(t, animate) {
      if (!W || !H) return;
      ctx.clearRect(0, 0, W, H);
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i];
        var base = P.aMin + s.ab * P.aSpan;
        var a = base;
        if (animate) {
          s.y -= s.vy;
          if (s.y < -2) { s.y = H + 2; s.x = Math.random() * W; }
          a = base * (0.55 + 0.45 * Math.sin(t * s.tw + s.ph));
        }
        ctx.beginPath();
        ctx.arc(s.x, s.y, P.rMin + s.rb * P.rSpan, 0, 6.2832);
        ctx.fillStyle = 'rgba(' + color + ',' + a.toFixed(3) + ')';
        ctx.fill();
      }
    }

    readParams();
    seed();
    // Density is the one param that cannot be re-derived from an existing star,
    // so a change to it is the only case that needs a re-seed.
    function retheme() {
      readParams();
      if (W && H && stars.length !== wantCount()) seed();
    }
    return { retheme: retheme, seed: seed, draw: draw };
  }

  Array.prototype.forEach.call(canvases, function (c) {
    var f = build(c);
    if (f) fields.push(f);
  });
  if (!fields.length) return;

  function each(fn) { for (var i = 0; i < fields.length; i++) fn(fields[i]); }

  // Re-read on theme toggle (the footer toggle stamps data-theme on <html>).
  if (window.MutationObserver) {
    new MutationObserver(function () {
      each(function (f) { f.retheme(); });
      if (reduced) each(function (f) { f.draw(0, false); });
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  }

  var rt;
  window.addEventListener('resize', function () {
    clearTimeout(rt);
    rt = setTimeout(function () {
      each(function (f) { f.seed(); if (reduced) f.draw(0, false); });
    }, 150);
  });

  if (reduced) {
    each(function (f) { f.draw(0, false); });
    return;
  }

  var t = 0;
  (function frame() {
    requestAnimationFrame(frame);
    // Background-tab power saver: keep the loop alive but stop painting.
    if (document.hidden) return;
    t += 0.016;
    each(function (f) { f.draw(t, true); });
  })();
})();
