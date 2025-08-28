import os, sys, json, time, re, random, datetime, pathlib, textwrap
import requests
import yaml
from slugify import slugify

# Optional: pytrends can be noisy to import; handle gracefully
try:
    from pytrends.request import TrendReq
except Exception as e:
    print("Failed to import pytrends:", e, file=sys.stderr)
    TrendReq = None

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE_CONTENT = ROOT / "site" / "content"

def load_cfg():
    cfg_path = pathlib.Path(__file__).with_name("config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

CFG = load_cfg()

def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def fetch_trending_terms(regions):
    if TrendReq is None:
        return []
    pytrends = TrendReq(hl='en-US', tz=360)
    keywords = []
    for region in regions:
        try:
            df = pytrends.trending_searches(pn=region)
            for term in df[0].tolist():
                if isinstance(term, str):
                    keywords.append(term.strip())
        except Exception as e:
            print(f"[warn] pytrends failed for {region}: {e}")
    # Deduplicate while preserving order
    seen = set()
    uniq = []
    for k in keywords:
        if k.lower() not in seen:
            seen.add(k.lower())
            uniq.append(k)
    return uniq[:20]

def fetch_related_queries(term):
    if TrendReq is None:
        return []
    try:
        pytrends = TrendReq(hl='en-US', tz=360)
        pytrends.build_payload([term])
        rq = pytrends.related_queries()
        # Pick rising queries if available
        related = []
        data = rq.get(term) or {}
        for key in ("rising", "top"):
            df = data.get(key)
            if df is not None:
                related.extend([str(x) for x in df["query"].tolist()[:10]])
        # Fallback: add generic angles
        if not related:
            related = [f"{term} explained", f"{term} pros and cons", f"{term} vs alternatives", f"{term} latest news"]
        # Deduplicate
        out, seen = [], set()
        for q in related:
            qn = q.strip().lower()
            if qn and qn not in seen and qn != term.lower():
                seen.add(qn); out.append(q.strip())
        return out[:8]
    except Exception as e:
        print(f"[warn] related queries failed for {term}: {e}")
        return [f"{term} overview", f"{term} key facts", f"{term} FAQs", f"{term} timeline"]

def wiki_search(term, limit=3):
    # Use Wikipedia opensearch then page summaries
    tries = []
    try:
        resp = requests.get("https://en.wikipedia.org/w/api.php", params={
            "action": "opensearch", "search": term, "limit": limit, "namespace": 0, "format": "json"
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        titles = data[1] if isinstance(data, list) and len(data) > 1 else []
        for t in titles:
            tries.append(f"https://en.wikipedia.org/api/rest_v1/page/summary/{t}")
    except Exception as e:
        print("[warn] wiki opensearch failed:", e)
    results = []
    for url in tries:
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                js = r.json()
                page = js.get("content_urls", {}).get("desktop", {}).get("page")
                if page:
                    results.append({"title": js.get("title"), "url": page})
        except Exception as e:
            pass
    return results[:limit]

def guardian_search(term, key, limit=3):
    if not key: return []
    try:
        r = requests.get("https://content.guardianapis.com/search", params={
            "api-key": key, "q": term, "page-size": limit, "show-fields": "headline,trailText"
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        out = []
        for res in data.get("response", {}).get("results", []):
            out.append({"title": res.get("webTitle"), "url": res.get("webUrl")})
        return out[:limit]
    except Exception as e:
        print("[warn] guardian search failed:", e)
        return []

def nyt_search(term, key, limit=3):
    if not key: return []
    try:
        r = requests.get("https://api.nytimes.com/svc/search/v2/articlesearch.json", params={
            "q": term, "api-key": key, "page": 0
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        out = []
        for doc in data.get("response", {}).get("docs", [])[:limit]:
            out.append({"title": doc.get("headline", {}).get("main"), "url": doc.get("web_url")})
        return out
    except Exception as e:
        print("[warn] nyt search failed:", e)
        return []

def pick_image(term):
    # Prefer Pexels, then Unsplash, else Wikimedia Commons
    pexels = os.getenv("PEXELS_API_KEY")
    unsplash = os.getenv("UNSPLASH_ACCESS_KEY")
    if pexels:
        try:
            r = requests.get("https://api.pexels.com/v1/search", params={"query": term, "per_page": 1}, headers={"Authorization": pexels}, timeout=20)
            r.raise_for_status()
            data = r.json()
            if data.get("photos"):
                p = data["photos"][0]
                return {
                    "url": p["src"]["large"],
                    "credit_text": f"Photo by {p.get('photographer')} on Pexels",
                    "credit_url": p.get("url")
                }
        except Exception as e:
            print("[warn] Pexels failed:", e)
    if unsplash:
        try:
            r = requests.get("https://api.unsplash.com/search/photos", params={"query": term, "per_page": 1}, headers={"Authorization": f"Client-ID {unsplash}"}, timeout=20)
            r.raise_for_status()
            data = r.json()
            if data.get("results"):
                p = data["results"][0]
                user = p.get("user", {})
                return {
                    "url": p["urls"]["regular"],
                    "credit_text": f"Photo by {user.get('name')} on Unsplash",
                    "credit_url": user.get("links", {}).get("html")
                }
        except Exception as e:
            print("[warn] Unsplash failed:", e)
    if CFG.get("ALLOW_WIKIMEDIA_IMAGES", True):
        try:
            r = requests.get("https://en.wikipedia.org/w/api.php", params={
                "action": "query", "prop": "pageimages|pageterms", "piprop": "original", "format": "json",
                "generator": "search", "gsrsearch": term, "gsrlimit": 1
            }, timeout=20)
            data = r.json()
            pages = data.get("query", {}).get("pages", {})
            for _, page in pages.items():
                original = page.get("original")
                if original and "source" in original:
                    title = page.get("title")
                    return {
                        "url": original["source"],
                        "credit_text": f"Wikimedia Commons / {title}",
                        "credit_url": f"https://commons.wikimedia.org/"
                    }
        except Exception as e:
            print("[warn] Wikimedia image failed:", e)
    return None

def make_article(topic, subtopic, sources, image, min_words=900):
    title = subtopic if subtopic.lower() != topic.lower() else f"{topic}: What you need to know"
    slug = slugify(title)[:80]
    path = SITE_CONTENT / slug / "index.md"
    if path.exists():
        return None  # don't overwrite
    path.parent.mkdir(parents=True, exist_ok=True)
    # Simple outline to encourage > min_words
    outline = f"""
## Overview

## Key Points

## Deep Dive

## FAQs

## Sources
"""
    fm = {
        "title": title,
        "date": now_iso(),
        "tags": [slugify(topic), "trending"],
        "draft": bool(CFG.get("HUMAN_REVIEW", False))
    }
    if image:
        fm["image"] = {"url": image["url"], "credit_text": image["credit_text"], "credit_url": image["credit_url"]}
    fm["sources"] = sources

    header = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n"
    body_intro = textwrap.dedent(f"""
    **Topic:** {topic}

    *This article was generated from current trending searches and augmented with reputable sources. It is reviewed periodically for accuracy.*
    """).strip() + "\n\n" + outline

    with open(path, "w", encoding="utf-8") as f:
        f.write(header + body_intro)

    return str(path)

def main():
    cfg = CFG
    regions = cfg.get("REGIONS", ["united_states"])
    terms = fetch_trending_terms(regions)
    if not terms:
        print("No trending terms found; exiting.")
        return
    # Seed on first term to expand into 4-5 subtopics
    seed = terms[0]
    subtopics = fetch_related_queries(seed)[: cfg.get("ARTICLES_PER_CYCLE", 5)]
    gkey = os.getenv("GUARDIAN_API_KEY")
    nytkey = os.getenv("NYT_API_KEY")

    created = []
    for sub in subtopics:
        srcs = []
        if cfg.get("FETCH_SOURCES", True):
            srcs.extend(wiki_search(sub, limit=2))
            if gkey: srcs.extend(guardian_search(sub, gkey, limit=2))
            if nytkey: srcs.extend(nyt_search(sub, nytkey, limit=1))
            # Deduplicate by URL
            seen = set(); uniq = []
            for s in srcs:
                u = s.get("url")
                if u and u not in seen:
                    uniq.append(s); seen.add(u)
            srcs = uniq[:5]

        img = pick_image(sub) if cfg.get("FETCH_IMAGES", True) else None
        p = make_article(seed, sub, srcs, img, min_words=cfg.get("MIN_WORDS", 900))
        if p:
            created.append(p)

    print(json.dumps({"created": created}, indent=2))

if __name__ == "__main__":
    main()
