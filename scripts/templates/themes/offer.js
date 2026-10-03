// "Offer Season" layer for the forest themes. Injected after the page's own scripts by
// src/job_hunter/theme.py; reads <script type="application/json" id="mf-config">.
// Every dynamic string is written with textContent: the config is user-controlled text.
(function () {
  'use strict';
  var cfgEl = document.getElementById('mf-config');
  if (!cfgEl) return;
  var cfg;
  try { cfg = JSON.parse(cfgEl.textContent); } catch (e) { return; }
  var name = cfg.firstName || '';
  var DAWN = cfg.theme === 'forest-dawn';
  var root = document.documentElement;
  var KEY = 'job-hunter-offer-mode';
  var isRadar = !!document.querySelector('.masthead');

  root.setAttribute('data-theme', cfg.theme);
  try { if (localStorage.getItem(KEY) === 'off') root.classList.add('mf-off'); } catch (e) { /* storage blocked: default on */ }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function rnd(i, s) { var v = Math.sin(i * 12.9898 + s * 78.233) * 43758.5453; return v - Math.floor(v); }
  function isOff() { return root.classList.contains('mf-off'); }

  // ---- backdrop: static pines; moon (night) or sun, rays and stars (dawn); a few motes ----
  function pine(x, base, h, w, fill) {
    var p = [[x, base - h], [x + w * 0.45, base - h * 0.62], [x + w * 0.2, base - h * 0.62],
      [x + w * 0.6, base - h * 0.32], [x + w * 0.25, base - h * 0.32], [x + w * 0.8, base],
      [x - w * 0.8, base], [x - w * 0.25, base - h * 0.32], [x - w * 0.6, base - h * 0.32],
      [x - w * 0.2, base - h * 0.62], [x - w * 0.45, base - h * 0.62]];
    return '<polygon points="' + p.map(function (q) { return q[0].toFixed(0) + ',' + q[1].toFixed(0); }).join(' ') +
      '" fill="' + fill + '"/>';
  }
  function buildBackdrop() {
    var bg = el('div'); bg.id = 'mf-bg'; bg.setAttribute('aria-hidden', 'true');
    var fills = DAWN ? ['#1b1236', '#120b25', '#0a0614'] : ['#0d1c15', '#08130e', '#040906'];
    var W = 1600, H = 500, s = '', i;
    for (i = 0; i < 22; i++) s += pine(i * 76 + rnd(i, 1) * 30, H * 0.86, H * (0.35 + rnd(i, 2) * 0.3), 62 + rnd(i, 3) * 30, fills[0]);
    for (i = 0; i < 13; i++) s += pine(i * 128 + rnd(i, 4) * 50, H * 0.98, H * (0.5 + rnd(i, 5) * 0.4), 100 + rnd(i, 6) * 40, fills[1]);
    for (i = 0; i < 4; i++) {
      s += pine(i * 130 + 30, H * 1.02, H * (0.8 + rnd(i, 7) * 0.2), 150, fills[2]);
      s += pine(W - i * 130 - 30, H * 1.02, H * (0.8 + rnd(i, 8) * 0.2), 150, fills[2]);
    }
    (DAWN ? ['mf-stars', 'mf-skyglow', 'mf-rays', 'mf-sun'] : ['mf-moon']).forEach(function (c) { bg.appendChild(el('div', c)); });
    var trees = el('div', 'mf-trees');
    trees.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMax slice">' + s + '</svg>';
    bg.appendChild(trees);
    bg.appendChild(el('div', 'mf-ground'));
    for (i = 0; i < 16; i++) {
      var m = el('div', 'mf-mote');
      m.style.left = (rnd(i, 11) * 100) + '%'; m.style.top = (30 + rnd(i, 12) * 55) + '%';
      m.style.animationDelay = (rnd(i, 13) * 4) + 's';
      bg.appendChild(m);
    }
    document.body.insertBefore(bg, document.body.firstChild);
  }

  // ---- affirmations: [with first name, without]; the first shown line is a time-aware greeting ----
  var hr = new Date().getHours();
  var GREET = hr < 5 ? 'Early start' : hr < 12 ? 'Good morning' : hr < 17 ? 'Good afternoon' : 'Good evening';
  var LINES = [
    [GREET + ', {n}. The offer is already on its way.', GREET + '. The offer is already on its way.'],
    ['{n}, the offer is already yours. You\'re just finding the envelope.', 'The offer is already yours. You\'re just finding the envelope.'],
    ['A better, higher-paying role is already on its way to you, {n}.', 'A better, higher-paying role is already on its way to you.'],
    ['Somewhere in here, a hiring manager is already hoping you apply, {n}.', 'Somewhere in here, a hiring manager is already hoping you apply.'],
    ['You\'ve got the offer, {n}. This is just the paperwork part.', 'You\'ve got the offer. This is just the paperwork part.'],
    ['Future {n} is already settled in at the new desk. Say hi.', 'Future you is already settled in at the new desk. Say hi.'],
    ['Today\'s hunt, {n}: find the role that\'s already saying yes.', 'Today\'s hunt: find the role that\'s already saying yes.'],
    ['The right team is already looking for someone exactly like {n}.', 'The right team is already looking for someone exactly like you.'],
    ['{n}\'s new salary has already been decided. Go collect it.', 'Your new salary has already been decided. Go collect it.'],
    ['Every scroll is a step toward \u201cWelcome to the team, {n}.\u201d', 'Every scroll is a step toward \u201cWelcome to the team.\u201d'],
    ['You\'re not looking for a job, {n}. You\'re choosing between offers.', 'You\'re not looking for a job. You\'re choosing between offers.'],
    ['The hardest part is already over, {n}: you started.', 'The hardest part is already over: you started.'],
    ['Congratulations on the new role, {n}. Let\'s find out which one.', 'Congratulations on the new role. Let\'s find out which one.'],
    ['Offer in hand, {n}, feet up. The hunt is just the victory lap.', 'Offer in hand, feet up. The hunt is just the victory lap.'],
    ['Quiet forest, sharp instincts, signed offer letter. Well done, {n}.', 'Quiet forest, sharp instincts, signed offer letter.']
  ];
  var idx = 0;
  function lineText(i) {
    var pair = LINES[i];
    return name ? pair[0].split('{n}').join(name) : pair[1];
  }

  // ---- radar-only decoration: three names, three jobs ----
  function swapText(node, next) {
    node.setAttribute('data-mf-orig', node.textContent);
    node.setAttribute('data-mf-new', next);
    node.textContent = next;
  }
  function decorateRadar() {
    var head = document.querySelector('.masthead');
    var sub = head.querySelector('.subhead');
    var h1 = head.querySelector('h1');
    if (h1) {
      swapText(h1, 'Offer Season');
      h1.insertAdjacentElement('afterend', el('p', 'mf-tag', 'Open season on the right role.'));
    }
    if (sub) {
      var line = el('p', 'mf-line', lineText(idx));
      line.title = 'click for another';
      line.addEventListener('click', function () { idx = (idx + 1) % LINES.length; line.textContent = lineText(idx); });
      sub.insertAdjacentElement('afterend', line);
    }
    var card = el('aside', 'mf-offer');
    card.setAttribute('aria-label', 'Vision card');
    card.appendChild(el('div', 'k', 'Signed & Sealed'));
    card.appendChild(el('div', 't', 'Your next role, already in the envelope.'));
    if (name) card.appendChild(el('div', 'd', 'Dear ' + name + ','));
    card.appendChild(el('div', 'r', cfg.role));
    var pay = el('div', 'p');
    pay.appendChild(el('b', null, cfg.pay));
    pay.appendChild(document.createElement('br'));
    pay.appendChild(document.createTextNode(cfg.when));
    card.appendChild(pay);
    card.appendChild(el('div', 'seal', '\u2713'));
    head.insertBefore(card, head.firstChild);

    var subs = {
      'Strong matches': 'Somewhere in here, it\'s already a yes.',
      'For review': 'it only takes one yes',
      'Below 50': 'warm-ups, in case you\'re curious'
    };
    Array.prototype.forEach.call(document.querySelectorAll('summary.group-head h2'), function (h) {
      var original = h.textContent.trim();
      if (original === 'Strong matches') swapText(h, 'The Yes List');
      if (subs[original]) h.insertAdjacentElement('afterend', el('span', 'mf-sub', subs[original]));
    });
    Array.prototype.forEach.call(document.querySelectorAll('.empty-state'), function (e) {
      e.appendChild(document.createTextNode(' '));
      e.appendChild(el('span', 'mf-note', 'All clear \u2014 nothing standing between you and the offer.'));
    });
    var main = document.querySelector('main');
    if (main) main.appendChild(el('p', 'mf-foot', 'Close the tabs. The offer is already in motion.'));
  }

  // ---- offer mode switch: off restores every swapped label and hides the manifestation text ----
  function relabel(off) {
    Array.prototype.forEach.call(document.querySelectorAll('[data-mf-orig]'), function (n) {
      n.textContent = off ? n.getAttribute('data-mf-orig') : n.getAttribute('data-mf-new');
    });
  }
  function buildControls() {
    var tg = el('button'); tg.id = 'mf-toggle'; tg.type = 'button';
    function paint() {
      tg.textContent = '\u2726 offer mode: ';
      tg.appendChild(el('b', null, isOff() ? 'off' : 'on'));
    }
    paint();
    tg.addEventListener('click', function () {
      root.classList.toggle('mf-off');
      relabel(isOff());
      paint();
      try { localStorage.setItem(KEY, isOff() ? 'off' : 'on'); } catch (e) { /* storage blocked */ }
    });
    document.body.appendChild(tg);
    var pv = el('button', null, 'preview: the moment'); pv.id = 'mf-preview'; pv.type = 'button';
    pv.addEventListener('click', celebrate);
    document.body.appendChild(pv);
  }

  // ---- the moment ----
  var win = el('div'); win.id = 'mf-win'; win.setAttribute('role', 'dialog');
  win.setAttribute('aria-label', 'You got the offer');
  var h2 = el('h2');
  h2.appendChild(document.createTextNode('You got the '));
  h2.appendChild(el('em', null, 'offer' + (name ? ', ' + name : '') + '.'));
  win.appendChild(h2);
  win.appendChild(el('p', null, 'Of course you did.'));
  (function () {
    for (var i = 0; i < 26; i++) {
      var sp = el('div', 'mf-spark');
      sp.style.left = (rnd(i, 21) * 100) + '%'; sp.style.animationDelay = (rnd(i, 22) * 1.4) + 's';
      win.appendChild(sp);
    }
  })();
  var hideTimer = null;
  function hide() { win.classList.remove('on'); }
  win.addEventListener('click', hide);
  function celebrate() {
    win.classList.add('on');
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, 5200);
  }
  window.addEventListener('jobhunter:application-status', function (evt) {
    var d = evt && evt.detail;
    if (d && d.status === 'offer' && !isOff()) celebrate();
  });
  window.JobHunterTheme = { celebrate: celebrate };

  buildBackdrop();
  if (isRadar) decorateRadar();
  document.body.appendChild(win);
  buildControls();
  if (isOff()) relabel(true);
})();