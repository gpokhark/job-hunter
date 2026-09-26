const test = require('node:test');
const assert = require('node:assert/strict');
const { createSync } = require('../../scripts/templates/radar_live_sync.js');

const tick = () => new Promise((r) => setImmediate(r));

function harness(over) {
  const store = {}, timers = [], sent = [], pending = [];
  const events = { ok: [], rejected: [], errors: [], states: [] };
  const opts = Object.assign({
    storage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
    storageKey: 'box',
    send: (item) => new Promise((resolve, reject) => { sent.push(item); pending.push({ resolve, reject }); }),
    setTimeout: (fn, ms) => { timers.push({ fn, ms, cancelled: false }); return timers.length - 1; },
    clearTimeout: (id) => { if (timers[id]) timers[id].cancelled = true; },
    onState: (s, n) => events.states.push([s, n]),
    onOk: (item, res, sup) => events.ok.push([item, res, sup]),
    onRejected: (item, res, sup) => events.rejected.push([item, res, sup]),
    onError: (e) => events.errors.push(e),
  }, over || {});
  const sync = createSync(opts);
  const active = () => timers.filter((t) => !t.cancelled);
  const saved = () => JSON.parse(store.box || '[]');
  return { sync, store, timers, sent, pending, events, active, saved };
}
const fb = (key, label) => ({ kind: 'feedback', key, payload: { label } });

test('a successful send removes the item, persists, and reports live', async () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  assert.equal(h.sent.length, 1);
  h.pending[0].resolve({ status: 200, json: { item: {} } });
  await tick();
  assert.equal(h.events.ok.length, 1);
  assert.equal(h.events.ok[0][2], false);
  assert.deepEqual(h.saved(), []);
  assert.deepEqual(h.events.states[h.events.states.length - 1], ['live', 0]);
});

test('a newer write for the same key survives an in-flight older one and is flagged superseded', async () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  h.sync.queue(fb('a|1', 'irrelevant')); // replaces the queued entry while the first is in flight
  assert.equal(h.sent.length, 1);
  h.pending[0].resolve({ status: 200, json: {} });
  await tick();
  assert.equal(h.events.ok[0][2], true); // superseded: the UI must not clobber the newer choice
  assert.equal(h.sent.length, 2);
  assert.equal(h.sent[1].payload.label, 'irrelevant');
  h.pending[1].resolve({ status: 200, json: {} });
  await tick();
  assert.equal(h.events.ok[1][2], false);
  assert.deepEqual(h.saved(), []);
});

test('a 503 keeps the item queued and schedules exactly one backoff timer', async () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  h.pending[0].resolve({ status: 503, json: null });
  await tick();
  assert.deepEqual(h.events.states[h.events.states.length - 1], ['offline', 1]);
  assert.equal(h.active().length, 1);
  assert.equal(h.active()[0].ms, 2000);
  assert.equal(h.saved().length, 1);
  assert.equal(h.events.errors.length, 0);
  // the timer retries; a second failure doubles the delay
  h.active()[0].fn();
  assert.equal(h.sent.length, 2);
  h.pending[1].resolve({ status: 429, json: null });
  await tick();
  assert.equal(h.active().length, 1);
  assert.equal(h.active()[0].ms, 4000);
  // success resets the attempt counter
  h.active()[0].fn();
  h.pending[2].resolve({ status: 200, json: {} });
  await tick();
  assert.equal(h.events.ok.length, 1);
  assert.deepEqual(h.saved(), []);
});

test('a network failure (rejected send) behaves like a retryable status, without double scheduling', async () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  h.pending[0].reject(new Error('offline'));
  await tick();
  assert.deepEqual(h.events.states[h.events.states.length - 1], ['offline', 1]);
  assert.equal(h.active().length, 1);
  assert.equal(h.active()[0].ms, 2000);
  assert.equal(h.events.errors.length, 0);
});

test('a permanent 4xx drops exactly that item, reports it, and the queue continues', async () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  h.sync.queue(fb('b|2', 'okay'));
  h.pending[0].resolve({ status: 404, json: { error: 'unknown job' } });
  await tick();
  assert.equal(h.events.rejected.length, 1);
  assert.equal(h.events.rejected[0][0].key, 'a|1');
  assert.equal(h.sent.length, 2);
  assert.equal(h.sent[1].key, 'b|2');
  assert.deepEqual(h.saved().map((o) => o.key), ['b|2']);
});

test('an exception in onOk resets the engine, is surfaced via onError, and never wedges the queue', async () => {
  const h = harness({ onOk: () => { throw new Error('boom'); } });
  h.sync.queue(fb('a|1', 'okay'));
  h.pending[0].resolve({ status: 200, json: {} });
  await tick();
  assert.equal(h.events.errors.length, 1);
  assert.equal(h.events.errors[0].message, 'boom');
  assert.deepEqual(h.saved(), []); // the item was removed and persisted before the callback ran
  assert.equal(h.active().length, 0); // not misrouted to the retry path
  h.sync.queue(fb('b|2', 'okay'));
  assert.equal(h.sent.length, 2); // flushing was reset
});

test('items persisted by an earlier page load are restored and flushed', async () => {
  const seeded = [fb('a|1', 'okay'), fb('b|2', null)];
  const h = harness({
    storage: { getItem: () => JSON.stringify(seeded), setItem: () => {} },
  });
  assert.deepEqual(h.sync.restored().map((o) => o.key), ['a|1', 'b|2']);
  h.sync.flush();
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].key, 'a|1');
});

test('corrupt stored JSON is treated as an empty outbox', () => {
  const h = harness({ storage: { getItem: () => '{not json', setItem: () => {} } });
  assert.deepEqual(h.sync.restored(), []);
});

test('pending(kind) lists queued keys of that kind only', () => {
  const h = harness();
  h.sync.queue(fb('a|1', 'okay'));
  h.sync.queue({ kind: 'application', key: 'b|2', payload: { status: 'saved' } });
  assert.deepEqual(h.sync.pending('application'), ['b|2']);
  assert.deepEqual(h.sync.pending('feedback'), ['a|1']);
});
