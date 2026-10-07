"""PushRod storefront SEO test (2026-10-07).

Verifies the pushrodshop.com misc fixes end to end via the Flask test
client with an explicit Host: pushrodshop.com header — the raw bytes a
non-JS crawler reads, not the hydrated page:

  1. home <head>: meta description, self-canonical, OG tags, and a
     descriptive single <h1> (not the bare brand name)
  2. home crawlability: server-rendered door links plus one product
     link per listed product (the JS grid alone carries none)
  3. /sitemap.xml: home + exactly the listed SKUs /api/products serves
  4. /api/brand stats computed from that same listed catalog
  5. /about, /faq, /contact: 200 with unique titles + canonicals,
     linked from the home footer
  6. face isolation: face hosts keep their own pages/sitemap; the
     default host keeps the pre-fix shell behavior

Run: python3 backend/test_seo_pushrod.py (from pushrod-store/)
No Stripe keys or network needed.
"""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["PRINTFUL_DRY_RUN"] = "1"
os.environ.setdefault("BRAND", "gateway")

import app as store_app

client = store_app.app.test_client()
fails = []

PR = {"Host": "pushrodshop.com"}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name +
          (f" — {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(name)


# 1. home head + H1
r = client.get("/", headers=PR)
check("home 200 (pushrod host)", r.status_code == 200, r.status_code)
home = r.get_data(as_text=True)
m = re.search(r'<meta name="description" content="([^"]+)"', home)
check("home meta description", bool(m) and "hat" in m.group(1).lower(),
      home[:200])
check("home self-canonical apex",
      '<link rel="canonical" href="https://pushrodshop.com/">' in home)
check("home OG tags", all(t in home for t in
      ('property="og:title"', 'property="og:description"',
       'property="og:url"', 'property="og:image"')))
h1s = re.findall(r"<h1[^>]*>(.*?)</h1>", home, re.DOTALL)
check("home exactly one <h1>", len(h1s) == 1, str(h1s))
check("home <h1> descriptive", bool(h1s) and "Garage Gear" in h1s[0]
      and "Hats" in h1s[0], str(h1s))
check("home title descriptive",
      "<title>PushRod™ Garage Gear" in home)

# 2. crawlable home: door links + every listed product linked
prods = client.get("/api/products", headers=PR).get_json()
check("/api/products 200 list", isinstance(prods, list) and prods)
linked = re.findall(r'href="/product/([A-Za-z0-9-]+)"', home)
check("home links every listed product",
      sorted(linked) == sorted(p["sku"] for p in prods),
      f"linked={len(linked)} listed={len(prods)}")
check("home door index links", home.count('href="#door-') >= 4,
      str(home.count('href="#door-')))
check("JS grid shells intact", 'id="doors"' in home and 'id="grid"' in home)

# 3. sitemap: home + exactly the listed SKUs
sm = client.get("/sitemap.xml", headers=PR).get_data(as_text=True)
locs = re.findall(r"<loc>([^<]+)</loc>", sm)
check("sitemap loc count = listed + home", len(locs) == len(prods) + 1,
      f"locs={len(locs)} listed={len(prods)}")
check("sitemap product urls = listed SKUs",
      {u.rsplit("/product/", 1)[1] for u in locs if "/product/" in u}
      == {p["sku"] for p in prods})

# 4. /api/brand stats from the same listed catalog
b = client.get("/api/brand", headers=PR).get_json()
check("stats total = /api/products count",
      b["stats"]["total"] == len(prods), str(b["stats"]["total"]))
check("stats by_owner = /api/products owners",
      b["stats"]["by_owner"] == dict(Counter(p["owner"] for p in prods)),
      str(b["stats"]["by_owner"]))
check("stats purchasable = /api/products purchasable",
      b["stats"]["purchasable"]
      == sum(1 for p in prods if p["purchasable"]),
      str(b["stats"]["purchasable"]))

# 5. trust pages: 200, unique titles, canonicals, footer-linked
titles = {}
for path in ("/about", "/faq", "/contact"):
    rr = client.get(path, headers=PR)
    check(f"{path} 200 (pushrod host)", rr.status_code == 200, rr.status_code)
    body = rr.get_data(as_text=True)
    t = re.search(r"<title>(.*?)</title>", body)
    titles[path] = t.group(1) if t else ""
    check(f"{path} canonical",
          f'<link rel="canonical" href="https://pushrodshop.com{path}">' in body)
    check(f"{path} linked from home footer",
          f'href="{path}"' in home)
check("trust titles unique", len(set(titles.values())) == 3, str(titles))

# 6. face isolation: other hosts untouched by the PushRod home treatment
re_home = client.get("/", headers={"Host": "restoreessentials.com"})
check("RE face home still served", re_home.status_code == 200,
      re_home.status_code)
check("RE face home not PushRod-treated",
      "catalog-index" not in re_home.get_data(as_text=True))
re_sm = client.get("/sitemap.xml",
                   headers={"Host": "restoreessentials.com"})
re_locs = re.findall(r"<loc>([^<]+)</loc>",
                     re_sm.get_data(as_text=True))
check("RE sitemap lists no PushRod-owned SKUs",
      not any("/product/PR-" in u or "/product/7B-" in u for u in re_locs),
      str(len(re_locs)))
default_home = client.get("/").get_data(as_text=True)
check("default host home without catalog index",
      "catalog-index" not in default_home)
check("default host bare-brand <h1> preserved",
      '<h1 class="pagetitle">PUSHROD™</h1>' in default_home)
er_faq = client.get("/faq", headers={"Host": "everreadyfamily.com"})
check("EverReady face keeps its own /faq", er_faq.status_code == 200,
      er_faq.status_code)

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL PASS")
