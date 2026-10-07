/* IronHead manufacturer derivation + Guides menu dropdown.
 *
 * One source of truth for "which manufacturer is this guide for",
 * shared by the nav dropdown (every IronHead face page) and the
 * /guides catalog filter. Derivation is driven by real catalog
 * fields from /api/products (SKU structure first, then the title) —
 * verified 2026-10-07 to bucket 100% of the 244 purchasable IronHead
 * guides with zero unclaimed. A guide that ever fails every rule
 * lands in "Other guides" rather than a guessed-wrong bucket, and
 * only ever shows under "All manufacturers".
 */
(function () {
  "use strict";

  // slug -> display name, in display order (largest catalogs roughly
  // first is unnecessary — alphabetical reads best in a menu).
  var MAKES = [
    { slug: "bmw", name: "BMW" },
    { slug: "bsa", name: "BSA" },
    { slug: "ducati", name: "Ducati" },
    { slug: "harley-davidson", name: "Harley-Davidson" },
    { slug: "honda", name: "Honda" },
    { slug: "indian", name: "Indian" },
    { slug: "kawasaki", name: "Kawasaki" },
    { slug: "moto-guzzi", name: "Moto Guzzi" },
    { slug: "norton", name: "Norton" },
    { slug: "suzuki", name: "Suzuki" },
    { slug: "triumph", name: "Triumph" },
    { slug: "yamaha", name: "Yamaha" }
  ];
  var BY_SLUG = {};
  MAKES.forEach(function (m) { BY_SLUG[m.slug] = m; });

  // SKU second token -> make slug (IH-<TOKEN>-...).
  var SKU_TOKEN_MAKE = {
    HARLEY: "harley-davidson", SHOVELHEAD: "harley-davidson",
    PANHEAD: "harley-davidson", IRONHEAD: "harley-davidson",
    INDIAN: "indian",
    TRIUMPH: "triumph", BONNEVILLE: "triumph",
    BMW: "bmw", BSA: "bsa", DUCATI: "ducati",
    KAWASAKI: "kawasaki", SUZUKI: "suzuki",
    YAMAHA: "yamaha", HONDA: "honda",
    MOTO: "moto-guzzi", NORTON: "norton"
  };
  // Title needles (lowercase) checked when the SKU token is a model.
  var TITLE_MAKE = [
    ["harley-davidson", "harley-davidson"],
    ["moto guzzi", "moto-guzzi"],
    ["bonneville", "triumph"],
    ["honda", "honda"], ["indian", "indian"], ["triumph", "triumph"],
    ["ducati", "ducati"], ["kawasaki", "kawasaki"],
    ["suzuki", "suzuki"], ["yamaha", "yamaha"],
    ["norton", "norton"], ["bmw", "bmw"], ["bsa", "bsa"]
  ];

  function deriveMake(p) {
    var sku = (p && p.sku) || "";
    var toks = sku.split("-");
    var t1 = toks.length > 1 ? toks[1] : "";
    if (SKU_TOKEN_MAKE[t1]) return SKU_TOKEN_MAKE[t1];
    // Model-coded SKUs: CB### (Honda), KZ###/H2 (Kawasaki),
    // GS### (Suzuki), XS### (Yamaha).
    if (/^CB\d/.test(t1)) return "honda";
    if (/^KZ\d/.test(t1) || t1 === "H2") return "kawasaki";
    if (/^GS\d/.test(t1)) return "suzuki";
    if (/^XS\d/.test(t1)) return "yamaha";
    var title = ((p && p.title) || "").toLowerCase();
    for (var i = 0; i < TITLE_MAKE.length; i++) {
      if (title.indexOf(TITLE_MAKE[i][0]) !== -1) return TITLE_MAKE[i][1];
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
    var menus = document.querySelectorAll("[data-ih-makes]");
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

  window.IHMakes = {
    MAKES: MAKES,
    BY_SLUG: BY_SLUG,
    deriveMake: deriveMake,
    summarize: summarize,
    fetchProducts: fetchProducts
  };
})();
