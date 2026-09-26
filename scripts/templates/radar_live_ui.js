// DOM wiring for the live radar page. Requires window.RadarLive (radar_live_core.js) and
// window.__RADAR_LIVE__ (emitted by scripts/render_radar.py). SQLite (via the server) is the
// source of truth; localStorage holds only a bounded outbox of unsent writes.
(function () {
  'use strict';
  var L = window.RadarLive, S = window.RadarLiveSync, boot = window.__RADAR_LIVE__;
  if (!L || !S || !boot) return;

  var OUTBOX_KEY = 'job-hunter-outbox';
  var GROUPS_KEY = 'job-hunter-groups:' + boot.stem;
  var ROW_SELECTOR = 'details.row, div.plain-row';
  var SORT_NOTE = {
    score: 'score, highest first', newest: 'posting date, newest first', company: 'company, A–Z'
  };
  var DEFAULT_NOTE_PREFIX = 'Sorted by score, highest first.';

  function $(id) { return document.getElementById(id); }
  function keyOf(row) { return row.dataset.sourceKey + '|' + row.dataset.jobId; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function todayIso() {
    var d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  // ---- row facts -------------------------------------------------------------------------
  var rows = Array.prototype.slice.call(document.querySelectorAll(ROW_SELECTOR));
  var factsOf = new Map();
  var rowsByKey = {};
  function text(row, sel) { var el = row.querySelector(sel); return el ? el.textContent.trim() : ''; }
  rows.forEach(function (row) {
    var d = row.dataset;
    factsOf.set(row, {
      score: d.score === '' || d.score === undefined ? null : parseInt(d.score, 10),
      posted: d.posted || '', state: d.state || '', country: d.country || '',
      hasSalary: d.hasSalary === '1', isNew: d.new === '1', longStanding: d.longStanding === '1',
      arrangement: d.arrangement || '', sponsorship: d.sponsorship || '',
      title: text(row, '.job-title'), company: text(row, '.job-company'),
      location: text(row, '.job-location')
    });
    (rowsByKey[keyOf(row)] = rowsByKey[keyOf(row)] || []).push(row);
  });

  // ---- feedback state + outbox -----------------------------------------------------------
  var labels = {};
  Object.keys(boot.feedback).forEach(function (k) { labels[k] = boot.feedback[k].label; });

  function paintRow(row) {
    var label = labels[keyOf(row)] || null;
    row.classList.toggle('fb-tagged', !!label);
    Array.prototype.forEach.call(row.querySelectorAll('.fb-btn'), function (b) {
      b.classList.toggle('fb-active', b.dataset.label === label);
    });
  }
  function setLabel(key, label) {
    if (label) labels[key] = label; else delete labels[key];
    (rowsByKey[key] || []).forEach(paintRow);
  }

  var noticeTimer = null;
  function notice(message) {
    var el = $('live-notice');
    if (!el) return;
    el.textContent = message;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(function () { el.textContent = ''; }, 8000);
  }

  function safeStorage() {
    try { var s = window.localStorage; s.getItem('job-hunter-probe'); return s; } catch (e) {
      var mem = {};
      return {
        getItem: function (k) { return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null; },
        setItem: function (k, v) { mem[k] = String(v); }
      };
    }
  }
  var SEND_URL = { feedback: '/api/feedback', application: '/api/application' };

  function renderStatus(state, unsaved) {
    var el = $('live-status');
    if (!el) return;
    el.dataset.state = state;
    el.textContent = state === 'saving' ? 'Saving\u2026'
      : state === 'offline' ? 'Offline (' + unsaved + ' unsaved)' : 'Live';
  }

  var sync = S.createSync({
    storage: safeStorage(),
    storageKey: OUTBOX_KEY,
    send: function (item) {
      return fetch(SEND_URL[item.kind], {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item.payload)
      }).then(function (res) {
        return res.json().catch(function () { return null; }).then(function (json) {
          return { status: res.status, json: json };
        });
      });
    },
    onState: renderStatus,
    onOk: function (item, res, superseded) {
      if (item.kind === 'feedback' && !superseded) {
        setLabel(item.key, res.json && res.json.item ? res.json.item.label : null);
        applyFilters();
      }
    },
    onRejected: function (item, res, superseded) {
      notice('Could not save that change: ' + ((res.json && res.json.error) || 'HTTP ' + res.status));
      if (item.kind === 'feedback' && !superseded) pullFeedback();
    },
    onError: function (e) { setTimeout(function () { throw e; }); }
  });

  // Apply pending outbox entries to labels so unsent writes are visible after reload.
  sync.restored().filter(function (o) { return o.kind === 'feedback'; }).forEach(function (o) {
    if (o.payload.label) labels[o.key] = o.payload.label; else delete labels[o.key];
  });
  function pendingKeys() { return sync.pending('feedback'); }

  document.addEventListener('click', function (evt) {
    var btn = evt.target.closest ? evt.target.closest('.fb-btn') : null;
    if (!btn) return;
    // Nested inside <summary>: without this the click would also toggle the row open/closed.
    evt.preventDefault();
    evt.stopPropagation();
    var row = btn.closest(ROW_SELECTOR);
    if (!row) return;
    var key = keyOf(row);
    var next = labels[key] === btn.dataset.label ? null : btn.dataset.label;
    setLabel(key, next);
    sync.queue({
      kind: 'feedback', key: key,
      payload: {
        source_key: row.dataset.sourceKey, job_id: row.dataset.jobId, label: next,
        client_ts: L.isoNow(Date.now())
      }
    });
    applyFilters();
  });
  Array.prototype.forEach.call(document.querySelectorAll('details.row > summary .apply-link'), function (link) {
    link.addEventListener('click', function (evt) { evt.stopPropagation(); });
  });

  // ---- server sync: tab reconcile + polling ----------------------------------------------
  var known = boot.versions || {};
  function pullFeedback() {
    return fetch('/api/feedback').then(function (r) { return r.json(); }).then(function (body) {
      L.reconcile(labels, body.feedback || {}, pendingKeys()).forEach(function (c) { setLabel(c.key, c.label); });
      applyFilters();
    }).catch(function () { /* transient; the outbox status already reflects real save failures */ });
  }
  function showReload() {
    var banner = $('live-reload');
    if (banner) banner.hidden = false;
  }
  function poll() {
    if (document.visibilityState !== 'visible') return;
    fetch('/api/state').then(function (r) { return r.json(); }).then(function (s) {
      var v = s.versions || {};
      if (v.archive !== known.archive || v.assessments !== known.assessments) showReload();
      if (v.feedback !== known.feedback) { known.feedback = v.feedback; pullFeedback(); }
    }).catch(function () { /* server briefly unreachable; the next tick retries */ });
  }
  setInterval(poll, 10000);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') { poll(); pullFeedback(); sync.flush(); }
  });
  window.addEventListener('focus', sync.flush);
  var reloadLink = $('live-reload-link');
  if (reloadLink) reloadLink.addEventListener('click', function (evt) { evt.preventDefault(); location.reload(); });

  // ---- filters, sort, counts, hash -------------------------------------------------------
  var toolbar = $('radar-toolbar');
  var search = $('filter-search'), chips = Array.prototype.slice.call(toolbar.querySelectorAll('.filter-chip'));
  var minScore = $('live-min-score'), postedDays = $('live-posted-days'), company = $('live-company');
  var locationInput = $('live-location'), hasSalary = $('live-has-salary');
  var feedbackSel = $('live-feedback'), sortSel = $('live-sort');
  var clearBtn = $('filter-clear'), countEl = $('filter-count');

  var companies = {};
  factsOf.forEach(function (f) { if (f.company) companies[f.company] = true; });
  Object.keys(companies).sort(function (a, b) { return a.toLowerCase().localeCompare(b.toLowerCase()); })
    .forEach(function (name) {
      var opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      company.appendChild(opt);
    });

  function readFilters() {
    var f = L.emptyFilters();
    f.query = search.value;
    chips.forEach(function (c) {
      if (!c.classList.contains('active')) return;
      var g = c.dataset.filterGroup, v = c.dataset.filterValue;
      if (g === 'tag') f.tags.push(v);
      else if (g === 'arrangement') f.arrangement.push(v);
      else if (g === 'sponsorship') f.sponsorship.push(v);
    });
    var min = parseInt(minScore.value, 10);
    f.minScore = isNaN(min) ? null : Math.max(0, Math.min(100, min));
    var days = parseInt(postedDays.value, 10);
    f.postedDays = isNaN(days) ? null : days;
    f.company = company.value;
    f.location = locationInput.value;
    f.hasSalary = hasSalary.checked;
    f.feedback = feedbackSel.value;
    f.sort = sortSel.value;
    return f;
  }
  function writeFilters(f) {
    search.value = f.query;
    chips.forEach(function (c) {
      var list = c.dataset.filterGroup === 'tag' ? f.tags
        : c.dataset.filterGroup === 'arrangement' ? f.arrangement : f.sponsorship;
      c.classList.toggle('active', list.indexOf(c.dataset.filterValue) !== -1);
    });
    minScore.value = f.minScore === null ? '' : String(f.minScore);
    postedDays.value = f.postedDays === null ? '' : String(f.postedDays);
    company.value = f.company;
    locationInput.value = f.location;
    hasSalary.checked = f.hasSalary;
    feedbackSel.value = f.feedback;
    sortSel.value = f.sort;
  }

  var containers = [];
  Array.prototype.forEach.call(document.querySelectorAll('.rows'), function (div) {
    var own = Array.prototype.filter.call(div.children, function (el) { return el.matches(ROW_SELECTOR); });
    if (own.length) containers.push({ div: div, original: own });
  });
  Array.prototype.forEach.call(document.querySelectorAll('.group-count, .group-note'), function (el) {
    el.dataset.orig = el.textContent;
  });

  function applyFilters() {
    var f = readFilters();
    var today = todayIso();
    var active = L.filtersActive(f);
    var visible = 0;
    rows.forEach(function (row) {
      var ok = L.rowPasses(factsOf.get(row), f, labels[keyOf(row)], today);
      row.hidden = !ok;
      if (ok) visible += 1;
    });
    containers.forEach(function (c) {
      var order = L.sortOrder(c.original.map(function (r) { return factsOf.get(r); }), f.sort);
      order.forEach(function (i) { c.div.appendChild(c.original[i]); });
      var shown = c.original.filter(function (r) { return !r.hidden; }).length;
      var group = c.div.closest('details.group');
      if (group) {
        var count = group.querySelector('.group-count');
        if (count) count.textContent = active ? count.dataset.orig + ' · ' + shown + ' shown' : count.dataset.orig;
        var note = group.querySelector('.group-note');
        if (note && note.dataset.orig.indexOf(DEFAULT_NOTE_PREFIX) === 0) {
          note.textContent = f.sort === 'default' || f.sort === 'score' ? note.dataset.orig
            : 'Sorted by ' + SORT_NOTE[f.sort] + '.' + note.dataset.orig.slice(DEFAULT_NOTE_PREFIX.length);
        }
      }
      var msg = c.div.nextElementSibling;
      if (msg && msg.classList.contains('filter-empty-state')) msg.hidden = shown > 0;
    });
    countEl.textContent = rows.length ? 'Showing ' + visible + ' of ' + rows.length : '';
    clearBtn.hidden = !active;
    var hash = L.encodeHash(f);
    try {
      history.replaceState(null, '', hash ? '#' + hash : location.pathname + location.search);
    } catch (e) { /* sandboxed frame */ }
  }

  chips.forEach(function (c) {
    c.addEventListener('click', function () { c.classList.toggle('active'); applyFilters(); });
  });
  [search, minScore, locationInput].forEach(function (el) { el.addEventListener('input', applyFilters); });
  [postedDays, company, hasSalary, feedbackSel, sortSel].forEach(function (el) {
    el.addEventListener('change', applyFilters);
  });
  clearBtn.addEventListener('click', function () {
    var keepSort = sortSel.value;
    writeFilters(L.emptyFilters());
    sortSel.value = keepSort;
    applyFilters();
  });

  // ---- group open/closed state (per tab session only) -------------------------------------
  var groups = Array.prototype.slice.call(document.querySelectorAll('details.group'));
  try {
    var saved = JSON.parse(sessionStorage.getItem(GROUPS_KEY) || 'null');
    if (Array.isArray(saved) && saved.length === groups.length) {
      groups.forEach(function (g, i) { g.open = !!saved[i]; });
    }
  } catch (e) { /* storage unavailable */ }
  groups.forEach(function (g) {
    g.addEventListener('toggle', function () {
      try {
        sessionStorage.setItem(GROUPS_KEY, JSON.stringify(groups.map(function (x) { return x.open; })));
      } catch (e) { /* storage unavailable */ }
    });
  });

  // ---- boot ------------------------------------------------------------------------------
  writeFilters(L.decodeHash(location.hash));
  rows.forEach(paintRow);
  applyFilters();
  sync.flush();
})();
