// Pure, DOM-free logic for the live radar page (docs/live-radar-dashboard-plan.md section 4).
// Inlined into the page by scripts/render_radar.py (live mode only) and unit-tested with
// `node --test` (tests/js/radar_live_core.test.js). Nothing here touches the DOM, the network,
// or storage — radar_live_ui.js owns all of that.
(function (root) {
  'use strict';

  var MAX_OUTBOX = 200;
  var SORT_MODES = ['default', 'score', 'newest', 'company'];
  var FEEDBACK_FILTERS = ['untagged', 'relevant', 'okay', 'irrelevant'];

  function emptyFilters() {
    return {
      query: '', tags: [], arrangement: [], sponsorship: [], minScore: null, postedDays: null,
      company: '', location: '', hasSalary: false, feedback: '', sort: 'default'
    };
  }

  // Sorting is a view choice, not a filter, so it doesn't make the "Clear filters" button appear.
  function filtersActive(f) {
    return !!(f.query.trim() || f.tags.length || f.arrangement.length || f.sponsorship.length ||
      f.minScore !== null || f.postedDays !== null || f.company || f.location.trim() ||
      f.hasSalary || f.feedback);
  }

  function dayDiff(todayIso, dayIso) {
    return Math.round((Date.parse(todayIso + 'T00:00:00Z') - Date.parse(dayIso + 'T00:00:00Z')) / 86400000);
  }

  function rowPasses(facts, f, label, todayIso) {
    var q = f.query.trim().toLowerCase();
    if (q && (facts.company + ' ' + facts.title).toLowerCase().indexOf(q) === -1) return false;
    if (f.tags.length && !f.tags.some(function (t) {
      return (t === 'new' && facts.isNew) || (t === 'long-standing' && facts.longStanding);
    })) return false;
    if (f.arrangement.length && f.arrangement.indexOf(facts.arrangement) === -1) return false;
    if (f.sponsorship.length && f.sponsorship.indexOf(facts.sponsorship) === -1) return false;
    if (f.minScore !== null && (facts.score === null || facts.score < f.minScore)) return false;
    if (f.postedDays !== null) {
      if (!facts.posted) return false;
      if (dayDiff(todayIso, facts.posted) > f.postedDays) return false;
    }
    if (f.company && facts.company.toLowerCase() !== f.company.toLowerCase()) return false;
    var loc = f.location.trim().toLowerCase();
    if (loc && (facts.location + ' ' + facts.state + ' ' + facts.country).toLowerCase().indexOf(loc) === -1) {
      return false;
    }
    if (f.hasSalary && !facts.hasSalary) return false;
    if (f.feedback === 'untagged' && label) return false;
    if (f.feedback && f.feedback !== 'untagged' && label !== f.feedback) return false;
    return true;
  }

  // Indices of `factsList` in display order. Stable: ties keep original (server) order.
  function sortOrder(factsList, mode) {
    var idx = factsList.map(function (_, i) { return i; });
    if (mode === 'default') return idx;
    function cmp(a, b) {
      var x = factsList[a], y = factsList[b], d = 0;
      if (mode === 'score') {
        d = (y.score === null ? -1 : y.score) - (x.score === null ? -1 : x.score);
      } else if (mode === 'newest') {
        d = (y.posted || '').localeCompare(x.posted || '');
      } else if (mode === 'company') {
        d = x.company.toLowerCase().localeCompare(y.company.toLowerCase());
      }
      return d || a - b;
    }
    return idx.sort(cmp);
  }

  function encodeHash(f) {
    var parts = [];
    function add(k, v) { parts.push(k + '=' + encodeURIComponent(v)); }
    if (f.query) add('q', f.query);
    if (f.tags.length) add('tag', f.tags.join(','));
    if (f.arrangement.length) add('arr', f.arrangement.join(','));
    if (f.sponsorship.length) add('spon', f.sponsorship.join(','));
    if (f.minScore !== null) add('min', String(f.minScore));
    if (f.postedDays !== null) add('days', String(f.postedDays));
    if (f.company) add('co', f.company);
    if (f.location) add('loc', f.location);
    if (f.hasSalary) add('sal', '1');
    if (f.feedback) add('fb', f.feedback);
    if (f.sort !== 'default') add('sort', f.sort);
    return parts.join('&');
  }

  function decodeHash(text) {
    var f = emptyFilters();
    String(text || '').replace(/^#/, '').split('&').forEach(function (pair) {
      var i = pair.indexOf('=');
      if (i < 1) return;
      var key = pair.slice(0, i), value;
      try { value = decodeURIComponent(pair.slice(i + 1)); } catch (e) { return; }
      var list = value ? value.split(',') : [];
      var n = parseInt(value, 10);
      if (key === 'q') f.query = value;
      else if (key === 'tag') f.tags = list;
      else if (key === 'arr') f.arrangement = list;
      else if (key === 'spon') f.sponsorship = list;
      else if (key === 'min') { if (!isNaN(n) && n >= 0 && n <= 100) f.minScore = n; }
      else if (key === 'days') { if (!isNaN(n) && n > 0) f.postedDays = n; }
      else if (key === 'co') f.company = value;
      else if (key === 'loc') f.location = value;
      else if (key === 'sal') f.hasSalary = value === '1';
      else if (key === 'fb') { if (FEEDBACK_FILTERS.indexOf(value) !== -1) f.feedback = value; }
      else if (key === 'sort') { if (SORT_MODES.indexOf(value) !== -1) f.sort = value; }
    });
    return f;
  }

  // Latest write per (kind, key) wins; oldest entries fall off past the cap.
  function enqueue(outbox, item, max) {
    var limit = max || MAX_OUTBOX;
    var next = outbox.filter(function (o) { return !(o.kind === item.kind && o.key === item.key); });
    next.push(item);
    return next.length > limit ? next.slice(next.length - limit) : next;
  }

  // 'permanent' = the server understood and refused (unknown job, bad payload): retrying can
  // never succeed, so the UI drops the write and says so. Everything else is worth retrying.
  function classifyStatus(status) {
    if (status >= 200 && status < 300) return 'ok';
    if (status >= 400 && status < 500 && status !== 408 && status !== 429) return 'permanent';
    return 'retry';
  }

  function backoffMs(attempt) {
    return Math.min(30000, 2000 * Math.pow(2, Math.max(0, attempt)));
  }

  function isoNow(ms) { return new Date(ms).toISOString(); }

  // localLabels: {key: label}; serverMap: {key: {label}}; pendingKeys: keys with an unsent write.
  function reconcile(localLabels, serverMap, pendingKeys) {
    var changes = [], seen = {};
    Object.keys(serverMap).forEach(function (k) {
      seen[k] = true;
      if (pendingKeys.indexOf(k) !== -1) return;
      if (localLabels[k] !== serverMap[k].label) changes.push({ key: k, label: serverMap[k].label });
    });
    Object.keys(localLabels).forEach(function (k) {
      if (seen[k] || pendingKeys.indexOf(k) !== -1) return;
      changes.push({ key: k, label: null });
    });
    return changes;
  }

  var api = {
    SORT_MODES: SORT_MODES, emptyFilters: emptyFilters, filtersActive: filtersActive,
    rowPasses: rowPasses, sortOrder: sortOrder, encodeHash: encodeHash, decodeHash: decodeHash,
    enqueue: enqueue, classifyStatus: classifyStatus, backoffMs: backoffMs, isoNow: isoNow,
    reconcile: reconcile
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.RadarLive = api;
})(typeof window !== 'undefined' ? window : globalThis);
