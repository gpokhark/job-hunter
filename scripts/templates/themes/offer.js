// "Offer Season" layer for the forest themes. Injected after the page's own scripts by
// src/job_hunter/theme.py; reads the JSON config block it is injected beside.
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
  // ---- the offer email: one builder for the masthead card and the sliding banner ----
  var WHO = name ? 'Dear ' + name + ',' : 'Hello,';
  var MAIL_GAP_PX = 32; // matches the page's own vertical rhythm between blocks
  var mailCard = null, mailGap = null;
  function mailHead() {
    var frag = document.createDocumentFragment();
    var top = el('div', 'mm-top');
    var app = el('span', 'mm-app');
    app.appendChild(el('i', 'mm-icon'));
    app.appendChild(document.createTextNode('MAIL'));
    top.appendChild(app);
    top.appendChild(el('span', null, 'now'));
    frag.appendChild(top);
    var row = el('div', 'mm-row');
    row.appendChild(el('div', 'mm-av', 'H'));
    var txt = el('div', 'mm-txt');
    var from = el('div', 'mm-from', 'Hiring Team ');
    from.appendChild(el('small', null, '\u00b7 Your future employer'));
    txt.appendChild(from);
    var subj = el('div', 'mm-subj');
    subj.appendChild(el('b', 'mm-dot'));
    subj.appendChild(document.createTextNode('Offer of employment: ' + cfg.role));
    txt.appendChild(subj);
    txt.appendChild(el('div', 'mm-prev',
      WHO + ' we\u2019re delighted to offer you the position of ' + cfg.role + '. ' + cfg.pay + ' ' + cfg.when));
    row.appendChild(txt);
    frag.appendChild(row);
    return frag;
  }
  // Keep the expanded email from covering the stats block: reserve exactly the missing space.
  // While the spacer is empty its margins collapse into its neighbours; once it has height the
  // smaller of (previous block's bottom margin, stats block's top margin) stops collapsing and is
  // added on top, so that amount is subtracted to land on exactly MAIL_GAP_PX below the card.
  function syncGap() {
    if (!mailCard || !mailGap) return;
    var stats = mailGap.parentNode && mailGap.parentNode.querySelector('.stats-group');
    var need = 0;
    if (stats && mailCard.classList.contains('open') && getComputedStyle(mailCard).position === 'absolute') {
      var prev = mailGap.previousElementSibling;
      var prevBottom = prev ? parseFloat(getComputedStyle(prev).marginBottom) || 0 : 0;
      var statsTop = parseFloat(getComputedStyle(stats).marginTop) || 0;
      var absorbed = Math.min(prevBottom, statsTop);
      var current = mailGap.offsetHeight;
      var resting = stats.getBoundingClientRect().top - (current > 0 ? current + absorbed : 0);
      need = Math.max(0, Math.ceil(mailCard.getBoundingClientRect().bottom + MAIL_GAP_PX - resting - absorbed));
    }
    mailGap.style.height = need + 'px';
  }
  function buildMailCard() {
    var card = el('aside', 'mf-offer mf-mail');
    card.setAttribute('aria-label', 'Offer email');
    card.setAttribute('role', 'button');
    card.setAttribute('aria-expanded', 'false');
    card.tabIndex = 0;
    card.appendChild(mailHead());
    var body = el('div', 'mm-body');
    body.appendChild(el('p', null, WHO));
    var p2 = el('p');
    p2.appendChild(document.createTextNode('We\u2019re delighted to offer you the position of '));
    p2.appendChild(el('b', null, cfg.role));
    p2.appendChild(document.createTextNode('. '));
    p2.appendChild(el('span', 'hl', cfg.pay));
    body.appendChild(p2);
    body.appendChild(el('p', null, cfg.when + '. Welcome to the team.'));
    var sig = el('p');
    sig.appendChild(document.createTextNode('Warm regards,'));
    sig.appendChild(document.createElement('br'));
    sig.appendChild(document.createTextNode('The Hiring Team'));
    body.appendChild(sig);
    var foot = el('div', 'mm-foot');
    foot.appendChild(el('span', 'mm-att', 'Offer_Letter.pdf \u00b7 1 page'));
    var accept = el('button', 'mm-accept', 'Accept offer \u2713');
    accept.type = 'button';
    foot.appendChild(accept);
    body.appendChild(foot);
    card.appendChild(body);
    function toggle() {
      var open = card.classList.toggle('open');
      card.setAttribute('aria-expanded', open ? 'true' : 'false');
      syncGap();
    }
    card.addEventListener('click', function (evt) {
      if (evt.target.closest('.mm-accept')) { celebrate(); return; }
      toggle();
    });
    card.addEventListener('keydown', function (evt) {
      if ((evt.key === 'Enter' || evt.key === ' ') && evt.target === card) { evt.preventDefault(); toggle(); }
    });
    return card;
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
    mailGap = el('div', 'mf-mail-gap');
    mailGap.setAttribute('aria-hidden', 'true');
    var firstStats = head.querySelector('.stats-group');
    if (firstStats) head.insertBefore(mailGap, firstStats); else head.appendChild(mailGap);
    mailCard = buildMailCard();
    head.insertBefore(mailCard, head.firstChild);
    window.addEventListener('resize', syncGap);

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
      try {
        window.dispatchEvent(new CustomEvent('jobhunter:offer-mode', { detail: { off: isOff() } }));
      } catch (e) { /* no CustomEvent: other layers simply keep their current text */ }
      paint();
      try { localStorage.setItem(KEY, isOff() ? 'off' : 'on'); } catch (e) { /* storage blocked */ }
    });
    document.body.appendChild(tg);
    var pv = el('button', null, 'preview: the moment'); pv.id = 'mf-preview'; pv.type = 'button';
    pv.addEventListener('click', announce);
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
  // The banner slides in first, then the full-screen moment; both stay silent in offer mode off.
  var TOAST_MS = 1800;
  var announcing = false;
  function announce() {
    if (announcing || isOff()) return;
    announcing = true;
    root.classList.add('mf-announcing'); // the masthead card steps aside while the banner shows
    var toast = el('div', 'mf-toast mf-mail');
    toast.id = 'mf-toast';
    toast.setAttribute('role', 'status');
    toast.appendChild(mailHead());
    document.body.appendChild(toast);
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () { toast.classList.add('in'); });
    });
    setTimeout(function () {
      toast.classList.remove('in');
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
        root.classList.remove('mf-announcing');
        announcing = false;
      }, 450);
      if (!isOff()) celebrate();
    }, TOAST_MS);
  }
  window.addEventListener('jobhunter:application-status', function (evt) {
    var d = evt && evt.detail;
    if (d && d.status === 'offer') announce();
  });
  window.JobHunterTheme = { celebrate: celebrate, announce: announce };

  buildBackdrop();
  if (isRadar) decorateRadar();
  document.body.appendChild(win);
  buildControls();
  if (isOff()) relabel(true);
})();