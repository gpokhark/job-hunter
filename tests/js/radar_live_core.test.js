const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../../scripts/templates/radar_live_core.js');

function facts(over) {
  return Object.assign({
    score: 80, posted: '2026-09-20', state: 'MI', country: 'US', hasSalary: false,
    isNew: false, longStanding: false, arrangement: 'onsite', sponsorship: 'unmentioned',
    title: 'ADAS Engineer', company: 'Ford', location: 'Dearborn, MI',
  }, over || {});
}
const TODAY = '2026-09-26';

test('empty filters pass everything', () => {
  assert.equal(core.rowPasses(facts(), core.emptyFilters(), null, TODAY), true);
  assert.equal(core.filtersActive(core.emptyFilters()), false);
});

test('query matches company or title, case-insensitively', () => {
  const f = core.emptyFilters();
  f.query = 'FORD';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), true);
  f.query = 'lidar';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), false);
});

test('tag chips are a union (some), as in the static toolbar', () => {
  const f = core.emptyFilters();
  f.tags = ['new', 'long-standing'];
  assert.equal(core.rowPasses(facts({ isNew: true }), f, null, TODAY), true);
  assert.equal(core.rowPasses(facts({ longStanding: true }), f, null, TODAY), true);
  assert.equal(core.rowPasses(facts(), f, null, TODAY), false);
});

test('min score excludes unreviewed (null score) rows once set', () => {
  const f = core.emptyFilters();
  f.minScore = 70;
  assert.equal(core.rowPasses(facts({ score: 70 }), f, null, TODAY), true);
  assert.equal(core.rowPasses(facts({ score: 69 }), f, null, TODAY), false);
  assert.equal(core.rowPasses(facts({ score: null }), f, null, TODAY), false);
});

test('posted-within uses whole days and excludes undated rows', () => {
  const f = core.emptyFilters();
  f.postedDays = 7;
  assert.equal(core.rowPasses(facts({ posted: '2026-09-19' }), f, null, TODAY), true); // 7 days
  assert.equal(core.rowPasses(facts({ posted: '2026-09-18' }), f, null, TODAY), false); // 8 days
  assert.equal(core.rowPasses(facts({ posted: '' }), f, null, TODAY), false);
});

test('company is an exact, case-insensitive match', () => {
  const f = core.emptyFilters();
  f.company = 'ford';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), true);
  f.company = 'for';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), false);
});

test('location text searches location, state and country', () => {
  const f = core.emptyFilters();
  f.location = 'dearborn';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), true);
  f.location = 'mi';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), true);
  f.location = 'texas';
  assert.equal(core.rowPasses(facts(), f, null, TODAY), false);
});

test('has-salary and feedback filters', () => {
  const f = core.emptyFilters();
  f.hasSalary = true;
  assert.equal(core.rowPasses(facts({ hasSalary: false }), f, null, TODAY), false);
  assert.equal(core.rowPasses(facts({ hasSalary: true }), f, null, TODAY), true);

  const g = core.emptyFilters();
  g.feedback = 'untagged';
  assert.equal(core.rowPasses(facts(), g, null, TODAY), true);
  assert.equal(core.rowPasses(facts(), g, 'okay', TODAY), false);
  g.feedback = 'okay';
  assert.equal(core.rowPasses(facts(), g, 'okay', TODAY), true);
  assert.equal(core.rowPasses(facts(), g, 'relevant', TODAY), false);
  assert.equal(core.rowPasses(facts(), g, undefined, TODAY), false);
});

test('sortOrder: default is identity; score/newest/company are stable with nulls last', () => {
  const list = [
    facts({ score: 60, posted: '2026-09-01', company: 'Zeta' }),
    facts({ score: null, posted: '', company: 'alpha' }),
    facts({ score: 90, posted: '2026-09-10', company: 'Beta' }),
    facts({ score: 60, posted: '2026-09-20', company: 'Gamma' }),
  ];
  assert.deepEqual(core.sortOrder(list, 'default'), [0, 1, 2, 3]);
  assert.deepEqual(core.sortOrder(list, 'score'), [2, 0, 3, 1]); // 60s keep original order
  assert.deepEqual(core.sortOrder(list, 'newest'), [3, 2, 0, 1]); // undated last
  assert.deepEqual(core.sortOrder(list, 'company'), [1, 2, 3, 0]);
});

test('hash round-trips every field and omits defaults', () => {
  assert.equal(core.encodeHash(core.emptyFilters()), '');
  const f = core.emptyFilters();
  f.query = 'lidar & radar';
  f.tags = ['new'];
  f.arrangement = ['remote', 'hybrid'];
  f.sponsorship = ['available'];
  f.minScore = 75;
  f.postedDays = 14;
  f.company = 'Ford Motor';
  f.location = 'MI';
  f.hasSalary = true;
  f.feedback = 'untagged';
  f.sort = 'newest';
  assert.deepEqual(core.decodeHash('#' + core.encodeHash(f)), f);
});

test('decodeHash ignores malformed and out-of-range input', () => {
  const f = core.decodeHash('min=abc&days=-3&fb=bogus&sort=nope&q=%E0%A4%A&x&=y&min=250');
  assert.deepEqual(f, core.emptyFilters());
});

test('enqueue keeps only the latest write per kind+key and caps the length', () => {
  let box = [];
  box = core.enqueue(box, { kind: 'feedback', key: 'a|1', payload: { label: 'okay' } });
  box = core.enqueue(box, { kind: 'feedback', key: 'b|2', payload: { label: 'okay' } });
  box = core.enqueue(box, { kind: 'feedback', key: 'a|1', payload: { label: null } });
  assert.deepEqual(box.map((o) => o.key), ['b|2', 'a|1']);
  assert.equal(box[1].payload.label, null);
  let capped = [];
  for (let i = 0; i < 5; i++) capped = core.enqueue(capped, { kind: 'feedback', key: 'k' + i, payload: {} }, 3);
  assert.deepEqual(capped.map((o) => o.key), ['k2', 'k3', 'k4']);
});

test('classifyStatus separates success, permanent rejection and retryable failures', () => {
  assert.equal(core.classifyStatus(200), 'ok');
  assert.equal(core.classifyStatus(400), 'permanent');
  assert.equal(core.classifyStatus(404), 'permanent');
  assert.equal(core.classifyStatus(403), 'permanent');
  assert.equal(core.classifyStatus(408), 'retry');
  assert.equal(core.classifyStatus(429), 'retry');
  assert.equal(core.classifyStatus(500), 'retry');
  assert.equal(core.classifyStatus(503), 'retry');
});

test('backoff doubles from 2s and caps at 30s', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 10].map(core.backoffMs), [2000, 4000, 8000, 16000, 30000, 30000]);
});

test('reconcile pulls server state for keys with no pending write', () => {
  const local = { 'a|1': 'okay', 'b|2': 'relevant', 'c|3': 'okay' };
  const server = { 'a|1': { label: 'okay' }, 'b|2': { label: 'irrelevant' }, 'd|4': { label: 'okay' } };
  const changes = core.reconcile(local, server, ['c|3']);
  assert.deepEqual(changes.sort((x, y) => x.key.localeCompare(y.key)), [
    { key: 'b|2', label: 'irrelevant' },
    { key: 'd|4', label: 'okay' },
  ]);
  // A locally-labelled key the server no longer has is cleared unless a write is pending.
  assert.deepEqual(core.reconcile({ 'x|9': 'okay' }, {}, []), [{ key: 'x|9', label: null }]);
  assert.deepEqual(core.reconcile({ 'x|9': 'okay' }, {}, ['x|9']), []);
});
