// "Inbox" layer for the Applications page of the forest themes. Appended after offer.js by
// render_applications.py only when a forest theme is active. Display only: it never changes a
// status, a count or a saved value, and every node it adds is built with textContent.
(function () {
  'use strict';
  var container = document.getElementById('app-rows');
  var countsBar = document.getElementById('app-counts');
  if (!container || !countsBar) return;
  var root = document.documentElement;
  root.classList.add('mf-apps');

  var STATUS = {
    saved: { badge: 'Bookmarked for greatness', chip: 'Bookmarked' },
    applied: { badge: 'Sent, and already loved', chip: 'Sent' },
    interviewing: { badge: 'They\u2019re clearly interested', chip: 'Interested' },
    offer: { badge: 'Yes. Obviously.', chip: 'Yes' },
    rejected: { badge: 'Redirected', chip: 'Redirected' },
    withdrawn: { badge: 'You chose differently', chip: 'Chose differently' }
  };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function isOff() { return root.classList.contains('mf-off'); }

  // ---- text swaps that must be restorable when offer mode is switched off ----
  var swaps = [];
  function addTextSwap(node, themed) {
    var original = node.nodeValue;
    swaps.push(function (on) { node.nodeValue = on ? themed : original; });
  }
  function applySwaps(on) { swaps.forEach(function (fn) { fn(on); }); }

  var h1 = document.querySelector('h1');
  if (h1 && h1.firstChild && h1.firstChild.nodeType === 3) {
    addTextSwap(h1.firstChild, 'Inbox: Offers Incoming');
    h1.insertAdjacentElement('afterend', el('p', 'mf-tag', 'Every row is a reply on its way.'));
  }
  Array.prototype.forEach.call(countsBar.querySelectorAll('[data-count-status]'), function (chip) {
    var label = chip.firstChild;
    if (!label || label.nodeType !== 3) return;
    var status = chip.getAttribute('data-count-status');
    var themed = status === 'all' ? 'In inbox' : (STATUS[status] || {}).chip;
    if (themed) addTextSwap(label, themed + ' ');
  });
  var empty = document.getElementById('app-empty');
  if (empty) {
    var originalNodes = Array.prototype.slice.call(empty.childNodes);
    var themedNode = document.createTextNode(
      'Your inbox is empty. Not for long: the first yes needs somewhere to land. ' +
      'Open the radar and press Track on a job.');
    swaps.push(function (on) {
      if (on) empty.replaceChildren(themedNode);
      else empty.replaceChildren.apply(empty, originalNodes);
    });
  }

  // ---- per-row decoration: added elements only, never a rewritten control ----
  function badgeFor(row) {
    var status = row.getAttribute('data-status');
    var info = STATUS[status];
    var main = row.querySelector('.app-main');
    if (!main) return;
    var badge = main.querySelector('.mf-badge');
    if (!info) { if (badge) badge.remove(); return; }
    if (!badge) {
      badge = el('span', 'mf-badge');
      main.insertBefore(badge, main.querySelector('.app-sub'));
    }
    badge.setAttribute('data-s', status);
    badge.textContent = info.badge;
  }
  function offerExtras(row) {
    var ribbon = row.querySelector('.mf-ribbon');
    var button = row.querySelector('.mf-compare');
    if (row.getAttribute('data-status') !== 'offer') {
      if (ribbon) ribbon.remove();
      if (button) button.remove();
      return;
    }
    if (!ribbon) row.insertBefore(el('span', 'mf-ribbon', 'Congratulations'), row.firstChild);
    var controls = row.querySelector('.app-controls');
    if (controls && !button) {
      var b = el('button', 'mf-compare', 'Compare this offer');
      b.type = 'button';
      controls.insertBefore(b, controls.querySelector('.app-delete'));
    }
  }
  function decorate(row) { badgeFor(row); offerExtras(row); }

  // ---- Compare this offer: copy a ready-to-paste salary-compare prompt ----
  var noticeTimer = null;
  function say(message, ms) {
    var box = document.getElementById('live-notice');
    if (!box) return;
    box.textContent = message;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(function () { box.textContent = ''; }, ms || 8000);
  }
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return false; });
    }
    return Promise.resolve(false);
  }
  container.addEventListener('click', function (evt) {
    var btn = evt.target.closest ? evt.target.closest('.mf-compare') : null;
    if (!btn) return;
    var row = btn.closest('.app-row');
    var titleEl = row && row.querySelector('.app-title');
    if (!titleEl) return;
    var sub = row.querySelector('.app-sub');
    var company = sub ? sub.textContent.split('\u00b7')[0].trim() : '';
    var prompt = 'Use the salary-compare skill. I have an offer for ' + titleEl.textContent.trim() +
      (company ? ' at ' + company : '') + '. Ask me for any details you are missing.';
    copyText(prompt).then(function (copied) {
      say(copied ? 'Copied. Paste it into your agent to start the salary-compare skill.' : prompt,
        copied ? 8000 : 30000);
    });
  });

  // ---- the sky follows your progress (forest-dawn only; applications.css reads --mf-rise) ----
  var DAWN = root.getAttribute('data-theme') === 'forest-dawn';
  var STAGE = { saved: 1, applied: 2, interviewing: 3, offer: 4 }; // rejected/withdrawn do not count
  if (DAWN) {
    root.classList.add('mf-dawn');
    var bg = document.getElementById('mf-bg');
    if (bg) bg.insertBefore(el('div', 'mf-dusk'), bg.firstChild);
  }
  function updateRise() {
    if (!DAWN) return;
    var best = 0;
    Array.prototype.forEach.call(container.querySelectorAll('.app-row'), function (row) {
      best = Math.max(best, STAGE[row.getAttribute('data-status')] || 0);
    });
    root.style.setProperty('--mf-rise', String(best === 0 ? 0.1 : best / 4));
  }

  // ---- keep rows in step with the page's own script (it rewrites data-status live) ----
  Array.prototype.forEach.call(container.querySelectorAll('.app-row'), decorate);
  updateRise();
  new MutationObserver(function (records) {
    records.forEach(function (rec) {
      if (rec.type === 'attributes') {
        if (rec.target.classList.contains('app-row')) decorate(rec.target);
        return;
      }
      Array.prototype.forEach.call(rec.addedNodes, function (n) {
        if (n.nodeType === 1 && n.classList.contains('app-row')) decorate(n);
      });
    });
    updateRise();
  }).observe(container, { attributes: true, attributeFilter: ['data-status'], subtree: true, childList: true });

  applySwaps(!isOff());
  window.addEventListener('jobhunter:offer-mode', function (evt) {
    applySwaps(!(evt.detail && evt.detail.off));
  });
})();
