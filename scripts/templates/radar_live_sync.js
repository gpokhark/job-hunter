// Outbox + retry engine shared by the live radar and Applications pages (extracted from
// radar_live_ui.js, behavior unchanged). DOM-free and dependency-injected so it can be unit
// tested with `node --test` (tests/js/radar_live_sync.test.js). Requires radar_live_core.js.
(function (root) {
  'use strict';
  var core = (typeof module !== 'undefined' && module.exports) ? require('./radar_live_core.js') : root.RadarLive;

  function createSync(opts) {
    var storage = opts.storage, storageKey = opts.storageKey;
    var setT = opts.setTimeout || function (fn, ms) { return setTimeout(fn, ms); };
    var clearT = opts.clearTimeout || function (id) { return clearTimeout(id); };
    var outbox = load();
    var loaded = outbox.slice();
    var flushing = false, attempt = 0, retryTimer = null;

    function load() {
      try {
        var parsed = JSON.parse(storage.getItem(storageKey) || '[]');
        return Array.isArray(parsed) ? parsed : [];
      } catch (e) { return []; }
    }
    function save() {
      try { storage.setItem(storageKey, JSON.stringify(outbox)); } catch (e) { /* private mode / quota */ }
    }
    function state() { return flushing ? 'saving' : (outbox.length ? 'offline' : 'live'); }
    function notify() { if (opts.onState) opts.onState(state(), outbox.length); }

    function scheduleRetry() {
      flushing = false;
      attempt += 1;
      notify();
      clearT(retryTimer);
      retryTimer = setT(flush, core.backoffMs(attempt - 1));
    }

    function flush() {
      if (flushing || !outbox.length) { notify(); return; }
      flushing = true;
      notify();
      var item = outbox[0];
      opts.send(item).then(function (res) {
        var kind = core.classifyStatus(res.status);
        if (kind === 'retry') { scheduleRetry(); return; }
        // Remove only this exact entry: a newer write for the same key may have replaced it
        // while the request was in flight, and must stay queued.
        outbox = outbox.filter(function (o) { return o !== item; });
        save();
        var superseded = outbox.some(function (o) { return o.kind === item.kind && o.key === item.key; });
        if (kind === 'ok') opts.onOk(item, res, superseded);
        else opts.onRejected(item, res, superseded);
        flushing = false;
        attempt = 0;
        flush();
      }, function () {
        // Network failure only (not post-processing errors).
        scheduleRetry();
      }).catch(function (e) {
        // Post-processing errors: reset so the queue cannot wedge, and surface the error.
        flushing = false;
        notify();
        (opts.onError || function (err) { setTimeout(function () { throw err; }); })(e);
      });
    }

    return {
      queue: function (item) { outbox = core.enqueue(outbox, item); save(); notify(); flush(); },
      flush: flush,
      restored: function () { return loaded.slice(); },
      items: function () { return outbox.slice(); },
      pending: function (kind) {
        return outbox.filter(function (o) { return o.kind === kind; }).map(function (o) { return o.key; });
      }
    };
  }

  var api = { createSync: createSync };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.RadarLiveSync = api;
})(typeof window !== 'undefined' ? window : globalThis);
