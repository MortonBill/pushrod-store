/* Restoration Essentials manufacturer derivation + Guides menu
 * dropdown.
 *
 * One source of truth for "which manufacturer is this guide for",
 * shared by the nav dropdown (every Restoration Essentials face
 * page) and the /guides catalog filter. Mirrors the IronHead
 * implementation (ih-makes.js). Derivation is driven by real
 * catalog fields from /api/products — SKU tokens first, then the
 * title's make word, then model-name needles — verified 2026-10-07
 * to bucket 383 of the 385 purchasable RE guides. The two that
 * derive nothing (the GM Squarebody truck guides, sold under
 * Chevrolet and GMC alike) land in no bucket rather than a
 * guessed-wrong one, and only ever show under "All manufacturers".
 */
(function () {
  "use strict";

  // slug -> display name, alphabetical (reads best in a menu).
  var MAKES = [
    { slug: "amc", name: "AMC" },
    { slug: "buick", name: "Buick" },
    { slug: "cadillac", name: "Cadillac" },
    { slug: "chevrolet", name: "Chevrolet" },
    { slug: "chrysler", name: "Chrysler" },
    { slug: "dodge", name: "Dodge" },
    { slug: "ford", name: "Ford" },
    { slug: "mercury", name: "Mercury" },
    { slug: "oldsmobile", name: "Oldsmobile" },
    { slug: "plymouth", name: "Plymouth" },
    { slug: "pontiac", name: "Pontiac" },
    { slug: "shelby", name: "Shelby" },
    { slug: "studebaker", name: "Studebaker" }
  ];
  var BY_SLUG = {};
  MAKES.forEach(function (m) { BY_SLUG[m.slug] = m; });

  // Make word (lowercase) -> slug. Used for SKU tokens and for the
  // title's first word once the leading year is stripped.
  var MAKE_WORDS = {
    amc: "amc", buick: "buick", cadillac: "cadillac",
    chevrolet: "chevrolet", chrysler: "chrysler", dodge: "dodge",
    ford: "ford", mercury: "mercury", oldsmobile: "oldsmobile",
    plymouth: "plymouth", pontiac: "pontiac", shelby: "shelby",
    studebaker: "studebaker"
  };
  // Title needles (lowercase) checked when the first word is a
  // model, not a make — factual nameplate mappings only.
  var TITLE_MAKE = [
    ["camaro", "chevrolet"],
    ["blazer", "chevrolet"],
    ["firebird", "pontiac"],
    ["trans am", "pontiac"],
    ["mustang", "ford"]
  ];

  function deriveMake(p) {
    var sku = (p && p.sku) || "";
    var toks = sku.split("-");
    for (var i = 0; i < toks.length; i++) {
      var t = toks[i].toLowerCase();
      if (MAKE_WORDS[t]) return MAKE_WORDS[t];
    }
    var title = (p && p.title) || "";
    var stripped = title.replace(/^\d{4}(\s*[–—-]\s*\d{2,4})?\s+/, "");
    var first = (stripped.split(/\s+/)[0] || "")
      .replace(/[^A-Za-z]/g, "").toLowerCase();
    if (MAKE_WORDS[first]) return MAKE_WORDS[first];
    var low = title.toLowerCase();
    for (var j = 0; j < TITLE_MAKE.length; j++) {
      if (low.indexOf(TITLE_MAKE[j][0]) !== -1) return TITLE_MAKE[j][1];
    }
    return null; // never guess a bucket
  }

  function summarize(products) {
    var counts = {};
    (products || []).forEach(function (p) {
      if (!p || !p.purchasable) return;
      var slug = deriveMake(p);
      if (slug) counts[slug] = (counts[slug] || 0) + 1;
    });
    return MAKES.filter(function (m) { return counts[m.slug]; })
      .map(function (m) {
        return { slug: m.slug, name: m.name, count: counts[m.slug] };
      });
  }

  function fetchProducts() {
    return fetch("/api/products")
      .then(function (r) { return r.json(); })
      .then(function (ps) {
        return (ps || []).filter(function (p) { return p && p.purchasable; });
      });
  }

  /* ---- nav dropdown ---- */
  function buildMenus() {
    var menus = document.querySelectorAll("[data-re-makes]");
    if (!menus.length) return;
    fetchProducts().then(function (ps) {
      var makes = summarize(ps);
      menus.forEach(function (menu) {
        menu.innerHTML = "";
        var all = document.createElement("a");
        all.href = "/guides";
        all.textContent = "All manufacturers";
        menu.appendChild(all);
        makes.forEach(function (m) {
          var a = document.createElement("a");
          a.href = "/guides?make=" + m.slug;
          a.textContent = m.name + " (" + m.count + ")";
          menu.appendChild(a);
        });
      });
    }).catch(function () { /* static fallback links stay as baked */ });

    // Mobile tap: caret button toggles; also close on outside tap.
    document.querySelectorAll(".navdd-btn").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        var dd = btn.closest(".navdd");
        var open = dd.classList.toggle("open");
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      });
    });
    document.addEventListener("click", function () {
      document.querySelectorAll(".navdd.open").forEach(function (dd) {
        dd.classList.remove("open");
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildMenus);
  } else {
    buildMenus();
  }

  window.REMakes = {
    MAKES: MAKES,
    BY_SLUG: BY_SLUG,
    deriveMake: deriveMake,
    summarize: summarize,
    fetchProducts: fetchProducts
  };
})();
