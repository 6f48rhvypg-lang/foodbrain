// Sample shopping-list data, shared by all shopping-* mockups (sibling to
// _items.js — different shape, mirrors GET /api/shopping/list + the
// staples/diet-focus data the "Vorräte verwalten" settings screen needs).
// Each mockup only wires up the containers it actually has on the page:
// #shoplist / #suggestlist / #staplelist / #dietchips.

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// tone -> [fg token, bg token] — reuses the existing --hot/--warm/--cool/--staple
// tokens from _base.css; --accent gets its bg mixed inline (no new colors).
const SHOP_TONE = {
  hot:    ['var(--hot)',    'var(--hot-bg)'],
  warm:   ['var(--warm)',   'var(--warm-bg)'],
  cool:   ['var(--cool)',   'var(--cool-bg)'],
  staple: ['var(--staple)', 'var(--staple-bg)'],
  accent: ['var(--accent)', 'color-mix(in srgb, var(--accent) 14%, transparent)'],
};

// source/signal -> label + tone. Covers every value api.py can emit today
// (manual/depleted/low_qty/interval/diet) plus the two reserved-but-unused
// ones (recipe/frequent) so the UI has a design for them when they land.
const SIGNAL_META = {
  manual:   { label: 'Manuell',           tone: 'staple' },
  depleted: { label: 'Aufgebraucht',      tone: 'hot' },
  low_qty:  { label: 'Fast leer',         tone: 'warm' },
  interval: { label: 'Überfällig',        tone: 'cool' },
  diet:     { label: 'Ernährungsfokus',   tone: 'accent' },
  recipe:   { label: 'Für ein Rezept',    tone: 'staple' },
  frequent: { label: 'Oft gekauft',       tone: 'staple' },
};

// ---- fixture data --------------------------------------------------------

// Mirrors items[] from GET /api/shopping/list. `unit` is a mockup-only
// display convenience — the real payload only carries the qu_id.
const ITEMS = [
  { id: '10', product_id: '1',  name: 'Vollmilch 3,5%',   amount: 2, qu_id: '2', unit: 'Stück',
    done: false, source: 'manual',   reason: '', added_ts: '2026-07-11T08:02:00+00:00' },
  { id: '11', product_id: '9',  name: 'Senf',              amount: 1, qu_id: '2', unit: 'Stück',
    done: false, source: 'depleted', reason: 'vor 2 Tagen aufgebraucht', added_ts: '2026-07-10T18:41:00+00:00' },
  { id: '12', product_id: null, name: 'Küchenrolle',       amount: 1, qu_id: null, unit: 'Stück',
    done: true,  source: 'manual',   reason: '', added_ts: '2026-07-09T21:10:00+00:00' },
  { id: '13', product_id: '5',  name: 'Gouda jung',        amount: 1, qu_id: '3', unit: 'Stück',
    done: false, source: 'interval', reason: 'meist alle 12 Tage gekauft — seit 15 Tagen fällig', added_ts: '2026-07-10T07:55:00+00:00' },
  { id: '14', product_id: '21', name: 'Basmatireis',       amount: 1, qu_id: '3', unit: 'Packung',
    done: false, source: 'recipe',   reason: 'für „Gemüse-Curry“ diese Woche', added_ts: '2026-07-11T09:30:00+00:00' },
  { id: '15', product_id: '3',  name: 'Eier · Freiland',   amount: 1, qu_id: '3', unit: '6er-Pack',
    done: true,  source: 'depleted', reason: 'vor 1 Tag aufgebraucht', added_ts: '2026-07-10T19:02:00+00:00' },
];

// Mirrors suggestions[] from GET /api/shopping/list.
const SUGGESTIONS = [
  { name: 'Butter',        product_id: '7',  suggested_amount: 1, unit: 'Stück',
    signal: 'depleted', reason: 'vor 3 Tagen aufgebraucht', current_amount: 0,   typical_amount: 1, mode: null },
  { name: 'Joghurt Natur', product_id: '14', suggested_amount: 2, unit: 'Becher',
    signal: 'low_qty',  reason: 'nur noch 1 von meist 4 auf Vorrat', current_amount: 1,   typical_amount: 4, mode: 'suggest' },
  { name: 'Kaffeebohnen',  product_id: '8',  suggested_amount: 1, unit: 'Packung',
    signal: 'interval', reason: 'meist alle 18 Tage gekauft — seit 21 Tagen fällig', current_amount: 0.2, typical_amount: 1, mode: null },
  { name: 'Linsen',        product_id: null, suggested_amount: 1, unit: null,
    signal: 'diet',     reason: 'mehr pflanzliches Eiweiß laut Ernährungsfokus', current_amount: 0,   typical_amount: null, mode: null },
  { name: 'Toilettenpapier', product_id: '19', suggested_amount: 1, unit: 'Packung',
    signal: 'frequent', reason: 'wird regelmäßig nachgekauft', current_amount: 0,   typical_amount: 1, mode: null },
];

// For the "Vorräte verwalten" settings screen (POST /api/shopping/staple).
const STAPLES = [
  { name: 'Vollmilch 3,5%', emoji: '🥛', mode: 'auto',    buys: 9, interval_days: 6 },
  { name: 'Butter',         emoji: '🧈', mode: 'auto',    buys: 7, interval_days: 11 },
  { name: 'Kaffeebohnen',   emoji: '☕', mode: 'suggest', buys: 5, interval_days: 18 },
  { name: 'Toilettenpapier',emoji: '🧻', mode: 'suggest', buys: 6, interval_days: 24 },
  { name: 'Senf',           emoji: '🫙', mode: 'off',     buys: 3, interval_days: 40 },
  { name: 'Gouda jung',     emoji: '🧀', mode: null,      buys: 2, interval_days: 12 },
];

// For the diet-focus block of the settings screen (POST /api/shopping/diet,
// made sticky client-side in this mockup).
const DIET_FOCUS = {
  presets: ['Proteinreich', 'Mehr Gemüse', 'Weniger Zucker', 'Vorrat auffüllen', 'Low-Carb'],
  active: ['Proteinreich', 'Mehr Gemüse'],
  freetext: 'weniger Fertigprodukte, mehr Hülsenfrüchte',
  updated_ts: '2026-07-08T09:15:00+00:00',
};

// ---- rendering ------------------------------------------------------------

function amountLabel(amount, unit) {
  const n = Number(amount) || 0;
  const nStr = n % 1 === 0 ? String(n) : String(n.toFixed(1)).replace('.', ',');
  return unit ? `${nStr} ${unit}` : nStr;
}

function relTime(ts) {
  if (!ts) return '';
  const days = Math.round((Date.now() - new Date(ts).getTime()) / 86400000);
  if (days <= 0) return 'heute';
  if (days === 1) return 'vor 1 Tag';
  return `vor ${days} Tagen`;
}

function badgeHTML(sourceKey) {
  const meta = SIGNAL_META[sourceKey] || SIGNAL_META.manual;
  const [c, cbg] = SHOP_TONE[meta.tone];
  return `<span class="sbadge" style="--c:${c};--cbg:${cbg}">${esc(meta.label)}</span>`;
}

function itemRowHTML(it) {
  const meta = SIGNAL_META[it.source] || SIGNAL_META.manual;
  const [c, cbg] = SHOP_TONE[meta.tone];
  return `<div class="srow${it.done ? ' bought' : ''}" data-id="${esc(it.id)}" data-source="${esc(it.source)}" style="--c:${c};--cbg:${cbg}">
    <button class="sdone${it.done ? ' checked' : ''}" type="button" data-act="done" aria-label="im Einkaufswagen"></button>
    <div class="sbody">
      <div class="sname">${esc(it.name)}</div>
      <div class="smeta"><span class="samt">${amountLabel(it.amount, it.unit)}</span>${it.source !== 'manual' ? badgeHTML(it.source) : ''}</div>
      ${it.reason ? `<div class="sreason">${esc(it.reason)}</div>` : ''}
    </div>
    <button class="sbuy" type="button" data-act="buy">✓ gekauft</button>
  </div>`;
}

function suggestionRowHTML(s) {
  const meta = SIGNAL_META[s.signal] || SIGNAL_META.manual;
  const [c, cbg] = SHOP_TONE[meta.tone];
  return `<div class="srow sugg" data-name="${esc(s.name)}" data-source="${esc(s.signal)}" style="--c:${c};--cbg:${cbg}">
    <div class="sbody">
      <div class="sname">${esc(s.name)}</div>
      <div class="smeta"><span class="samt">${amountLabel(s.suggested_amount, s.unit)}</span>${badgeHTML(s.signal)}</div>
      <div class="sreason">${esc(s.reason)}</div>
    </div>
    <button class="sadd" type="button" data-act="add" aria-label="zur Liste hinzufügen">+</button>
  </div>`;
}

function stapleRowHTML(st) {
  const mode = st.mode || 'suggest';
  return `<div class="strow" data-name="${esc(st.name)}">
    <div class="sticon">${st.emoji}</div>
    <div class="stbody">
      <div class="stname">${esc(st.name)}</div>
      <div class="stmeta">${st.buys}&times; gekauft &middot; alle ~${st.interval_days} Tage${!st.mode ? ' &middot; Standard' : ''}</div>
    </div>
    <div class="stmode" role="group" aria-label="Modus für ${esc(st.name)}">
      <button class="smopt${mode === 'auto' ? ' active' : ''}" type="button" data-mode="auto">Auto</button>
      <button class="smopt${mode === 'suggest' ? ' active' : ''}" type="button" data-mode="suggest">Vorschlag</button>
      <button class="smopt${mode === 'off' ? ' active' : ''}" type="button" data-mode="off">Aus</button>
    </div>
  </div>`;
}

let emptySuggestions = false;

function renderShopList() {
  const el = document.getElementById('shoplist');
  if (!el) return;
  el.innerHTML = ITEMS.map(itemRowHTML).join('');
  const count = document.getElementById('shopCount');
  if (count) count.textContent = ITEMS.length;
  el.querySelectorAll('[data-act="done"]').forEach(b => b.addEventListener('click', () => {
    b.classList.toggle('checked');
    b.closest('.srow').classList.toggle('bought', b.classList.contains('checked'));
  }));
  el.querySelectorAll('[data-act="buy"]').forEach(b => b.addEventListener('click', () => {
    const row = b.closest('.srow');
    row.classList.add('bought', 'committed');
    b.textContent = '✓ verbucht';
    b.disabled = true;
  }));
}

function renderSuggestList() {
  const el = document.getElementById('suggestlist');
  if (!el) return;
  if (emptySuggestions || !SUGGESTIONS.length) {
    el.innerHTML = '<div class="sheet-error">Keine Vorschläge — dein Vorrat ist gut gefüllt.</div>';
  } else {
    el.innerHTML = SUGGESTIONS.map(suggestionRowHTML).join('');
  }
  const count = document.getElementById('suggCount');
  if (count) count.textContent = emptySuggestions ? 0 : SUGGESTIONS.length;
  el.querySelectorAll('[data-act="add"]').forEach(b => b.addEventListener('click', () => {
    const row = b.closest('.srow');
    row.classList.add('moved');
    setTimeout(() => {
      const shop = document.getElementById('shoplist');
      if (shop) shop.insertAdjacentHTML('afterbegin', itemRowHTML({
        id: 'new-' + Math.random().toString(36).slice(2, 7),
        name: row.dataset.name, amount: 1, unit: '', done: false,
        source: SUGGESTIONS.find(s => s.name === row.dataset.name)?.signal || 'manual',
        reason: SUGGESTIONS.find(s => s.name === row.dataset.name)?.reason || '',
      }));
      row.remove();
      const shopCount = document.getElementById('shopCount');
      if (shop && shopCount) shopCount.textContent = shop.children.length;
      const sc = document.getElementById('suggCount');
      const remaining = document.querySelectorAll('#suggestlist .srow').length;
      if (sc) sc.textContent = remaining;
      wireShopActions();
    }, 220);
  }));
}

function wireShopActions() {
  document.getElementById('shoplist')?.querySelectorAll('[data-act="done"]').forEach(b => {
    if (b.dataset.wired) return; b.dataset.wired = '1';
    b.addEventListener('click', () => {
      b.classList.toggle('checked');
      b.closest('.srow').classList.toggle('bought', b.classList.contains('checked'));
    });
  });
  document.getElementById('shoplist')?.querySelectorAll('[data-act="buy"]').forEach(b => {
    if (b.dataset.wired) return; b.dataset.wired = '1';
    b.addEventListener('click', () => {
      const row = b.closest('.srow');
      row.classList.add('bought', 'committed');
      b.textContent = '✓ verbucht';
      b.disabled = true;
    });
  });
}

function toggleEmptySuggestions() {
  emptySuggestions = !emptySuggestions;
  renderSuggestList();
}

function renderStapleList() {
  const el = document.getElementById('staplelist');
  if (!el) return;
  el.innerHTML = STAPLES.map(stapleRowHTML).join('');
  el.querySelectorAll('.strow').forEach(row => {
    row.querySelectorAll('.smopt').forEach(btn => btn.addEventListener('click', () => {
      row.querySelectorAll('.smopt').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const meta = row.querySelector('.stmeta');
      if (meta && meta.textContent.includes('Standard')) {
        meta.innerHTML = meta.innerHTML.replace(/\s*&middot;\s*Standard/, '');
      }
    }));
  });
}

function renderDietChips() {
  const el = document.getElementById('dietchips');
  if (!el) return;
  el.innerHTML = `
    <div class="dchip-grid">${DIET_FOCUS.presets.map(p => `
      <button class="chip${DIET_FOCUS.active.includes(p) ? ' active' : ''}" type="button" data-chip="${esc(p)}">${esc(p)}</button>
    `).join('')}</div>
    <textarea class="dfree" placeholder="Eigene Notiz …" rows="2">${esc(DIET_FOCUS.freetext)}</textarea>
    <div class="dupdated" id="dietUpdated">zuletzt geändert ${relTime(DIET_FOCUS.updated_ts)}</div>`;
  el.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
    c.classList.toggle('active');
    const cap = document.getElementById('dietUpdated');
    if (cap) cap.textContent = 'zuletzt geändert gerade eben';
  }));
  el.querySelector('.dfree')?.addEventListener('input', () => {
    const cap = document.getElementById('dietUpdated');
    if (cap) cap.textContent = 'zuletzt geändert gerade eben';
  });
}

document.addEventListener('DOMContentLoaded', () => {
  renderShopList();
  renderSuggestList();
  renderStapleList();
  renderDietChips();
});
