// Behavior for the /applications page. Requires window.RadarLive (radar_live_core.js),
// window.RadarLiveSync (radar_live_sync.js) and window.__APPS_LIVE__ (emitted by
// scripts/render_applications.py). Shares the radar page's outbox (same localStorage key), so a
// write queued while offline on either page is flushed by whichever page is open next.
(function () {
  'use strict';
  var L = window.RadarLive, S = window.RadarLiveSync, boot = window.__APPS_LIVE__;
  if (!L || !S || !boot) return;

  var SEND_URL = { feedback: '/api/feedback', application: '/api/application' };
  var container = document.getElementById('app-rows');
  function $(id) { return document.getElementById(id); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function todayIso() {
    var d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
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
  function rowsList() { return Array.prototype.slice.call(container.querySelectorAll('.app-row')); }
  function keyOf(row) { return row.dataset.sourceKey + '|' + row.dataset.jobId; }

  var noticeTimer = null;
  function notice(message) {
    var el = $('live-notice');
    if (!el) return;
    el.textContent = message;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(function () { el.textContent = ''; }, 8000);
  }
  function showReload() { var b = $('live-reload'); if (b) b.hidden = false; }
  function renderStatus(state, unsaved) {
    var el = $('live-status');
    if (!el) return;
    el.dataset.state = state;
    el.textContent = state === 'saving' ? 'Saving…'
      : state === 'offline' ? 'Offline (' + unsaved + ' unsaved)' : 'Live';
  }

  function daysText(status, appliedAt) {
    if (status === 'saved' || !appliedAt) return '';
    var n = Math.round((Date.parse(todayIso() + 'T00:00:00Z') - Date.parse(appliedAt + 'T00:00:00Z')) / 86400000);
    if (isNaN(n)) return '';
    return n <= 0 ? 'Applied today' : n === 1 ? 'Applied 1 day ago' : 'Applied ' + n + ' days ago';
  }

  function updateCounts() {
    var counts = { all: 0 };
    rowsList().forEach(function (row) {
      counts.all += 1;
      counts[row.dataset.status] = (counts[row.dataset.status] || 0) + 1;
    });
    Array.prototype.forEach.call(document.querySelectorAll('#app-counts [data-count-status]'), function (el) {
      var b = el.querySelector('b');
      if (b) b.textContent = String(counts[el.dataset.countStatus] || 0);
    });
    var empty = $('app-empty');
    if (empty) empty.hidden = counts.all > 0;
  }

  function refreshRow(row, app) {
    row.dataset.status = app.status;
    row.dataset.appliedAt = app.applied_at || '';
    var sel = row.querySelector('.app-status'), date = row.querySelector('.app-date');
    var notes = row.querySelector('.app-notes');
    sel.value = app.status;
    date.value = app.applied_at || '';
    date.disabled = app.status === 'saved';
    if (document.activeElement !== notes) notes.value = app.notes || '';
    row.querySelector('.app-days').textContent = daysText(app.status, app.applied_at);
    updateCounts();
  }

  function rowApp(row) {
    return {
      status: row.dataset.status, applied_at: row.dataset.appliedAt || null,
      notes: row.querySelector('.app-notes').value.trim() === '' ? null : row.querySelector('.app-notes').value
    };
  }

  var sync = S.createSync({
    storage: safeStorage(),
    storageKey: 'job-hunter-outbox',
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
      if (item.kind !== 'application') return;
      var it = res.json && res.json.item;
      var row = rowsList().filter(function (r) { return keyOf(r) === item.key; })[0];
      if (!superseded && row) {
        if (it) refreshRow(row, it); else { row.remove(); updateCounts(); }
      }
      if (res.json && res.json.export_warning) {
        notice('Saved, but the export files could not be refreshed: ' + res.json.export_warning);
      }
    },
    onRejected: function (item, res) {
      notice('Could not save that change: ' + ((res.json && res.json.error) || 'HTTP ' + res.status) +
        ' — reload to see the current state.');
      showReload();
    },
    onError: function (e) { setTimeout(function () { throw e; }); }
  });

  function saveRow(row) {
    var status = row.querySelector('.app-status').value;
    var date = row.querySelector('.app-date').value;
    var notes = row.querySelector('.app-notes').value;
    var payload = {
      source_key: row.dataset.sourceKey, job_id: row.dataset.jobId, status: status,
      notes: notes.trim() === '' ? null : notes, client_ts: L.isoNow(Date.now())
    };
    if (status !== 'saved' && date) payload.applied_at = date;
    refreshRow(row, {
      status: status,
      applied_at: status === 'saved' ? null : (payload.applied_at || row.dataset.appliedAt || todayIso()),
      notes: payload.notes
    });
    sync.queue({ kind: 'application', key: keyOf(row), payload: payload });
  }

  container.addEventListener('change', function (evt) {
    var field = evt.target.closest ? evt.target.closest('.app-status, .app-date, .app-notes') : null;
    if (!field) return;
    var row = field.closest('.app-row');
    if (row) saveRow(row);
  });
  container.addEventListener('click', function (evt) {
    var del = evt.target.closest ? evt.target.closest('.app-delete') : null;
    if (!del) return;
    var row = del.closest('.app-row');
    if (!row || !window.confirm('Stop tracking this job and delete its notes?')) return;
    var payload = {
      source_key: row.dataset.sourceKey, job_id: row.dataset.jobId, status: null,
      client_ts: L.isoNow(Date.now())
    };
    var key = keyOf(row);
    row.remove();
    updateCounts();
    sync.queue({ kind: 'application', key: key, payload: payload });
  });

  // Pending application writes from an earlier page load: show them as the row's state.
  sync.restored().filter(function (o) { return o.kind === 'application'; }).forEach(function (o) {
    var row = rowsList().filter(function (r) { return keyOf(r) === o.key; })[0];
    if (!row) return;
    if (o.payload.status === null) { row.remove(); return; }
    refreshRow(row, {
      status: o.payload.status,
      applied_at: o.payload.status === 'saved' ? null : (o.payload.applied_at || row.dataset.appliedAt || todayIso()),
      notes: o.payload.notes === undefined ? rowApp(row).notes : o.payload.notes
    });
  });

  // Changes made elsewhere (another tab, the radar page): tell the user, never reshuffle rows.
  var known = (boot.versions || {}).applications;
  function domMap() {
    var map = {};
    rowsList().forEach(function (row) { map[keyOf(row)] = rowApp(row); });
    return map;
  }
  function poll() {
    if (document.visibilityState !== 'visible') return;
    fetch('/api/state').then(function (r) { return r.json(); }).then(function (s) {
      var v = (s.versions || {}).applications;
      if (v === known) return;
      return fetch('/api/applications').then(function (r) { return r.json(); }).then(function (body) {
        known = v;
        if (L.reconcileApps(domMap(), body.applications || {}, sync.pending('application')).length) showReload();
      });
    }).catch(function () { /* server briefly unreachable; the next tick retries */ });
  }
  setInterval(poll, 10000);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') { poll(); sync.flush(); }
  });
  window.addEventListener('focus', sync.flush);
  var reloadLink = $('live-reload-link');
  if (reloadLink) reloadLink.addEventListener('click', function (evt) { evt.preventDefault(); location.reload(); });

  updateCounts();
  sync.flush();
})();
