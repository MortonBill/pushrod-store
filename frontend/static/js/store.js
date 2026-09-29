/* PushRod storefront JS — talks to the backend JSON API only.
   Cart persists in localStorage; the server re-validates everything at checkout. */
const $ = s => document.querySelector(s);
const money = c => '$' + (c / 100).toFixed(2);

let BRAND = null, PRODUCTS = [], BY_SKU = {};
let DOOR = 'all';   // gateway door filter: 'all' or a SKU prefix

function applyTheme() {
  const t = BRAND.theme, r = document.documentElement.style;
  r.setProperty('--bg', t.bg); r.setProperty('--bg2', t.bg2);
  r.setProperty('--card', t.card); r.setProperty('--accent', t.accent);
  r.setProperty('--accent2', t.accent2); r.setProperty('--text', t.text);
  r.setProperty('--muted', t.muted);
  document.title = BRAND.name + ' — ' + BRAND.tagline;
  document.querySelectorAll('.brandname').forEach(e => e.textContent = BRAND.name);
  const tg = $('#tagline'); if (tg) tg.textContent = BRAND.tagline;
}

async function boot() {
  BRAND = await (await fetch('/api/brand')).json();
  PRODUCTS = await (await fetch('/api/products')).json();
  PRODUCTS.forEach(p => BY_SKU[p.sku] = p);
  applyTheme(); renderCart();
  buildTypeFilter();
  if (document.getElementById('grid')) { renderDoors(); renderGrid(); }
  if (document.getElementById('pdetail')) renderDetail();
  const sc = $('#stripe-note');
  if (sc && !BRAND.stripe_ready) sc.textContent = 'Checkout unavailable: Stripe test key not configured.';
}

/* ---------- gateway doors + filters ---------- */
function renderDoors() {
  const box = $('#doors');
  if (!box) return;
  const doors = BRAND.doors || [];
  if (!doors.length) { box.innerHTML = ''; return; }
  box.innerHTML = `<button class="door${DOOR === 'all' ? ' sel' : ''}" data-door="all">
      <strong>All doors</strong><span>${PRODUCTS.length} items</span></button>` +
    doors.map(d => {
      const n = PRODUCTS.filter(p => p.prefix === d.prefix).length;
      return `<button class="door${DOOR === d.prefix ? ' sel' : ''}" data-door="${d.prefix}">
        <strong>${d.label}</strong><span>${d.blurb} · ${n} items</span></button>`;
    }).join('');
  box.querySelectorAll('[data-door]').forEach(b => b.onclick = () => {
    DOOR = b.dataset.door; renderDoors(); renderGrid();
  });
}

/* Type filter options come from the live catalog — never a hardcoded list. */
function buildTypeFilter() {
  const sel = $('#ftype');
  if (!sel) return;
  const types = [...new Set(PRODUCTS.map(p => p.type))].sort();
  const cur = sel.value || 'all';
  sel.innerHTML = '<option value="all">All gear</option>' +
    types.map(t => `<option value="${t}">${t[0].toUpperCase() + t.slice(1)}s</option>`).join('');
  sel.value = types.includes(cur) ? cur : 'all';
}

/* ---------- grid ---------- */
function priceHTML(p) {
  if (!p.purchasable) return '<span class="tbd">Price TBD</span>';
  const d = p.price.status === 'draft' ? ' <span class="draft">intro price</span>' : '';
  return `<span class="price">$${p.price.amount.toFixed(2)}${d}</span>`;
}
function renderGrid() {
  const type = $('#ftype').value, q = $('#q').value.toLowerCase();
  const grid = $('#grid'); grid.innerHTML = '';
  PRODUCTS
    .filter(p => (DOOR === 'all' || p.prefix === DOOR) &&
                 (type === 'all' || p.type === type) &&
                 (!q || (p.title + ' ' + p.description + ' ' + p.sku).toLowerCase().includes(q)))
    .forEach(p => {
      const el = document.createElement('div');
      el.className = 'card';
      el.innerHTML = `
        <a href="/product/${p.sku}"><img loading="lazy" src="${p.image_url}" alt="${p.title}"></a>
        <div class="body">
          <div class="meta">${p.type} · ${p.base_color}</div>
          <h3><a href="/product/${p.sku}">${p.title}</a></h3>
          ${priceHTML(p)}
          ${p.purchasable ? `<button class="btn" data-add="${p.sku}">Add to cart</button>` : ''}
        </div>`;
      grid.appendChild(el);
    });
  grid.querySelectorAll('[data-add]').forEach(b =>
    b.onclick = () => quickAdd(b.dataset.add));
}
function quickAdd(sku) {
  const p = BY_SKU[sku];
  const size = p.needs_size ? null : null; // sized goods choose on detail page
  if (p.needs_size) { location.href = '/product/' + sku; return; }
  addToCart(sku, null, 1);
}

/* ---------- detail ---------- */
let SEL_SIZE = null;
function renderDetail() {
  const sku = location.pathname.split('/').pop();
  const p = BY_SKU[sku];
  if (!p) { $('#pdetail').innerHTML = '<p>Product not found.</p>'; return; }
  $('#pdetail').innerHTML = `
    <div><img src="${p.image_url}" alt="${p.title}"></div>
    <div>
      <div class="meta">${p.type} · ${p.base_color} · ${p.sku}</div>
      <h1>${p.title}</h1>
      ${priceHTML(p)}
      <p class="desc">${p.description}</p>
      ${p.needs_size ? `<div><strong>Size</strong><div class="sizes">${
        BRAND.sizes.map(s => `<button class="size" data-s="${s}">${s}</button>`).join('')
      }</div></div>` : ''}
      ${p.purchasable ? `
      <div class="qtyrow"><label>Qty</label><input id="qty" type="number" value="1" min="1" max="99"></div>
      <button class="btn" id="addbtn">Add to cart</button>` :
      `<div class="notice">Price coming soon — this item isn't for sale yet.</div>`}
    </div>`;
  document.querySelectorAll('.size').forEach(b => b.onclick = () => {
    document.querySelectorAll('.size').forEach(x => x.classList.remove('sel'));
    b.classList.add('sel'); SEL_SIZE = b.dataset.s;
  });
  const ab = $('#addbtn');
  if (ab) ab.onclick = () => {
    if (p.needs_size && !SEL_SIZE) { alert('Pick a size first.'); return; }
    addToCart(sku, SEL_SIZE, parseInt($('#qty').value) || 1);
  };
}

/* ---------- cart (localStorage) ---------- */
const load = () => JSON.parse(localStorage.getItem('pushrod_cart') || '[]');
const save = c => localStorage.setItem('pushrod_cart', JSON.stringify(c));

function addToCart(sku, size, qty) {
  const cart = load();
  const found = cart.find(i => i.sku === sku && i.size === size);
  if (found) found.qty = Math.min(99, found.qty + qty);
  else cart.push({ sku, size, qty });
  save(cart); renderCart(); openDrawer();
}
function setQty(sku, size, qty) {
  let cart = load();
  cart = cart.map(i => (i.sku === sku && i.size === size) ? { ...i, qty } : i)
             .filter(i => i.qty > 0);
  save(cart); renderCart();
}
function removeItem(sku, size) {
  save(load().filter(i => !(i.sku === sku && i.size === size)));
  renderCart();
}
function cartCount() { return load().reduce((n, i) => n + i.qty, 0); }

function renderCart() {
  const cart = load(), box = $('#cartitems');
  $('#cartcount').textContent = cartCount();
  if (!box) return;
  if (!cart.length) { box.innerHTML = '<p style="color:var(--muted)">Cart is empty.</p>'; $('#ctotal').textContent = '$0.00'; return; }
  let total = 0;
  box.innerHTML = '';
  cart.forEach(i => {
    const p = BY_SKU[i.sku]; if (!p || !p.purchasable) return;
    const unit = Math.round(p.price.amount * 100); total += unit * i.qty;
    const el = document.createElement('div');
    el.className = 'citem';
    el.innerHTML = `
      <img src="${p.image_url}" alt="">
      <div class="grow">
        <div class="row"><strong>${p.title}</strong>
          <a href="#" data-rm style="color:var(--muted)">✕</a></div>
        <div class="row" style="color:var(--muted);font-size:.85rem">
          <span>${i.size || p.type} · ${money(unit)}</span>
          <span><button data-d>-</button> ${i.qty} <button data-u>+</button></span>
        </div>
      </div>`;
    el.querySelector('[data-rm]').onclick = e => { e.preventDefault(); removeItem(i.sku, i.size); };
    el.querySelector('[data-d]').onclick = () => setQty(i.sku, i.size, i.qty - 1);
    el.querySelector('[data-u]').onclick = () => setQty(i.sku, i.size, i.qty + 1);
    box.appendChild(el);
  });
  $('#ctotal').textContent = money(total);
}

function openDrawer() { $('#drawer').classList.add('open'); }
function closeDrawer() { $('#drawer').classList.remove('open'); }

async function checkout() {
  const cart = load();
  if (!cart.length) return;
  const btn = $('#cobtn'); btn.disabled = true; btn.textContent = 'Working…';
  try {
    const r = await fetch('/api/checkout', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: cart }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'checkout failed');
    save([]); location.href = j.checkout_url;   // Stripe Checkout (TEST mode)
  } catch (e) { alert('Checkout error: ' + e.message); }
  btn.disabled = false; btn.textContent = 'Checkout';
}

document.addEventListener('DOMContentLoaded', () => {
  boot();
  const cb = $('#cartbtn'); if (cb) cb.onclick = openDrawer;
  const x = $('#closex'); if (x) x.onclick = closeDrawer;
  const co = $('#cobtn'); if (co) co.onclick = checkout;
  const ft = $('#ftype'); if (ft) ft.onchange = renderGrid;
  const qq = $('#q'); if (qq) qq.oninput = renderGrid;
});
