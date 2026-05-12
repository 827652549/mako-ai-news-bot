#!/usr/bin/env python3
"""
AI News Bot - Daily report generator for Feishu card notifications.
Fetches from GitHub Releases, Trending, Search, HN Algolia, DEV Community, and web sources.
Writes .ai-news-bot/latest-report.json and pushes to a date-based branch.

Usage:
    python bot.py                  # run with GITHUB_TOKEN env var
    GITHUB_TOKEN=ghp_... python bot.py
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode, quote

import yaml  # pip install pyyaml


# ─── helpers ───────────────────────────────────────────────────────────────

def http_get(url, headers=None, timeout=15):
    """Return (status_code, body_text). Never raises; returns (0, '') on error."""
    req = Request(url, headers=headers or {})
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        return e.code, ""
    except (URLError, Exception):
        return 0, ""


def github_headers():
    token = os.environ.get("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def truncate_title(title, max_len=40):
    if len(title) <= max_len:
        return title
    cut = title[:max_len]
    for sep in [" ", "·", "|", "：", "，", "-"]:
        idx = cut.rfind(sep)
        if idx > max_len // 2:
            return cut[:idx] + "…"
    return cut + "…"


def in_window(dt_str, lookback_hours):
    """True if ISO datetime string is within the lookback window from now."""
    if not dt_str:
        return False
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        return dt >= cutoff
    except ValueError:
        return False


def contains_any(text, keywords):
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def contains_excluded(text, exclude_keywords):
    return contains_any(text, exclude_keywords)


def fetch_jina(url):
    """Fetch a URL via Jina Reader (r.jina.ai)."""
    status, body = http_get(f"https://r.jina.ai/{url}")
    return status, body


# ─── Step 2A: GitHub Releases ──────────────────────────────────────────────

def fetch_github_releases(owner_repo, lookback_hours, skipped):
    """Return list of {title, url, why_hint, published_at, score} dicts."""
    url = f"https://api.github.com/repos/{owner_repo}/releases?per_page=5"
    status, body = http_get(url, headers=github_headers())
    if status != 200 or not body:
        skipped.append(f"{owner_repo} releases（HTTP {status}）")
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        skipped.append(f"{owner_repo} releases（JSON 解析失败）")
        return []

    items = []
    seen_repos = {}
    for release in data:
        pub = release.get("published_at", "")
        if not in_window(pub, lookback_hours):
            continue
        tag = release.get("tag_name", "")
        html_url = release.get("html_url", "")
        body_text = release.get("body", "") or ""
        # same-day same-repo dedup: keep only the latest
        repo_key = owner_repo.lower()
        if repo_key in seen_repos:
            continue
        seen_repos[repo_key] = True
        items.append({
            "title": f"{owner_repo.split('/')[-1]} {tag}",
            "url": html_url,
            "why_hint": f"release_notes: {body_text[:300]}",
            "published_at": pub,
            "score": 100,
            "source": "github_releases",
        })
    return items


# ─── Step 2B: GitHub Trending ──────────────────────────────────────────────

def fetch_github_trending(must_match_any, skipped):
    """Return list of trending repos matching must_match_any keywords."""
    results = []
    for lang_param in ["", "&spoken_language_code=zh"]:
        url = f"https://github.com/trending?since=daily{lang_param}"
        status, html = http_get(url)
        if status != 200 or len(html) < 500:
            skipped.append(f"GitHub Trending{lang_param or ''}（HTTP {status}）")
            continue
        # Extract articles
        repo_blocks = re.findall(r'<article[^>]*class="Box-row"[^>]*>(.*?)</article>', html, re.DOTALL)
        for block in repo_blocks:
            href_match = re.search(r'<h2[^>]*>.*?<a[^>]+href="(/[^"]+)"', block, re.DOTALL)
            desc_match = re.search(r'<p[^>]*col-9[^"]*"[^>]*>\s*(.*?)\s*</p>', block, re.DOTALL)
            stars_match = re.search(r'([\d,]+)\s*stars today', block)
            if not href_match:
                continue
            repo_path = href_match.group(1).strip("/")
            desc = re.sub(r'<[^>]+>', '', desc_match.group(1)).strip() if desc_match else ""
            stars_today = int(stars_match.group(1).replace(",", "")) if stars_match else 0
            combined = f"{repo_path} {desc}".lower()
            if not contains_any(combined, must_match_any):
                continue
            results.append({
                "title": f"{repo_path.split('/')[-1]} · {desc[:60]}" if desc else repo_path,
                "url": f"https://github.com/{repo_path}",
                "why_hint": f"trending repo: {desc}",
                "score": stars_today,
                "source": "github_trending",
            })
    return results


# ─── Step 2C: GitHub Search ────────────────────────────────────────────────

def fetch_github_search(query, lookback_hours, skipped):
    """Return list of repos matching the query that are within the time window."""
    encoded = quote(query)
    url = f"https://api.github.com/search/repositories?q={encoded}&sort=stars&order=desc&per_page=10"
    status, body = http_get(url, headers=github_headers())
    if status == 403:
        skipped.append(f"GitHub Search（Rate Limit 耗尽）")
        return []
    if status != 200 or not body:
        skipped.append(f"GitHub Search（HTTP {status}）")
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []

    remaining = data.get("rate_limit_remaining", 999)  # not in body but check anyway
    items = []
    for repo in data.get("items", []):
        created = repo.get("created_at", "")
        pushed = repo.get("pushed_at", "")
        # Accept if created_at or pushed_at is in window
        if not (in_window(created, lookback_hours * 14) or in_window(pushed, lookback_hours)):
            continue
        items.append({
            "title": f"{repo['name']} · {(repo.get('description') or '')[:50]}",
            "url": repo.get("html_url", ""),
            "why_hint": f"stars: {repo.get('stargazers_count', 0)}, desc: {repo.get('description', '')}",
            "score": repo.get("stargazers_count", 0),
            "source": "github_search",
        })
    return items


# ─── Step 2D: HN Algolia ───────────────────────────────────────────────────

def fetch_hn(keywords, min_points, lookback_hours, skipped):
    """Return deduplicated HN stories for all keywords."""
    since_ts = int(time.time()) - int(lookback_hours * 3600)
    seen_ids = set()
    items = []
    for kw in keywords:
        encoded_kw = quote(kw)
        url = (
            f"https://hn.algolia.com/api/v1/search"
            f"?query={encoded_kw}&tags=story"
            f"&numericFilters=created_at_i>{since_ts},points>{min_points}"
            f"&hitsPerPage=5"
        )
        status, body = http_get(url)
        if status != 200 or not body:
            skipped.append(f"HN Algolia [{kw}]（HTTP {status}）")
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        for hit in data.get("hits", []):
            oid = hit.get("objectID", "")
            if oid in seen_ids:
                continue
            seen_ids.add(oid)
            story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
            items.append({
                "title": hit.get("title", ""),
                "url": story_url,
                "why_hint": f"HN pts={hit.get('points', 0)}",
                "score": hit.get("points", 0),
                "source": "hn",
            })
    return items


# ─── Step 2E: Web scraping ─────────────────────────────────────────────────

def fetch_web(source_cfg, lookback_hours, skipped):
    """Scrape a URL (direct or via Jina) and return article list."""
    url = source_cfg["url"]
    use_jina = source_cfg.get("use_jina", False)
    must_match = source_cfg.get("must_match_any", [])

    if use_jina:
        status, body = fetch_jina(url)
    else:
        status, body = http_get(url)
        if status != 200 or len(body) < 500:
            status, body = fetch_jina(url)

    if status != 200 or not body:
        skipped.append(f"{url}（HTTP {status}）")
        return []

    # Simple extraction: look for markdown-style links from Jina output
    items = []
    lines = body.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Jina returns markdown links: [Title](url)
        link_match = re.search(r'\[([^\]]{5,120})\]\((https?://[^\)]+)\)', line)
        if not link_match:
            continue
        title = link_match.group(1).strip()
        item_url = link_match.group(2).strip()
        if not title or len(title) < 5:
            continue
        if must_match and not contains_any(f"{title} {line}", must_match):
            continue
        items.append({
            "title": title,
            "url": item_url,
            "why_hint": f"web source: {url}",
            "score": 10,
            "source": "web",
        })
    return items[:20]


# ─── Step 2F: DEV Community ────────────────────────────────────────────────

def fetch_dev_community(tag, lookback_hours, skipped):
    """Return articles for a DEV.to tag published within lookback_hours."""
    url = f"https://dev.to/api/articles?tag={tag}&per_page=10&top=1"
    status, body = http_get(url)
    if status != 200 or not body:
        skipped.append(f"DEV Community [{tag}]（HTTP {status}）")
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    items = []
    for article in data:
        pub = article.get("published_at", "")
        if not in_window(pub, lookback_hours):
            continue
        items.append({
            "title": article.get("title", ""),
            "url": article.get("url", ""),
            "why_hint": f"dev.to reactions={article.get('positive_reactions_count', 0)}",
            "score": article.get("positive_reactions_count", 0) * 10 + article.get("comments_count", 0),
            "published_at": pub,
            "source": "dev_community",
        })
    return items


# ─── Step 3: Filter & deduplicate ──────────────────────────────────────────

def title_similarity(a, b):
    """Rough character-level overlap ratio."""
    set_a = set(a.lower().split())
    set_b = set(b.lower().split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / max(len(set_a), len(set_b))


def filter_and_dedup(raw_items, exclude_keywords, seen_urls, max_items, today):
    """Apply all dedup and filter rules. Returns (output_items, updated_seen, hot_items)."""
    hot_items = []
    output = []
    seen_urls_this_run = {}  # url → item (within-run dedup)

    for item in raw_items:
        title = item.get("title", "")
        url = item.get("url", "")
        if not url or not title:
            continue

        # 1. Exclude keywords
        if contains_excluded(f"{title}", exclude_keywords):
            continue

        # 2. Cross-day dedup with seen.json
        if url in seen_urls:
            entry = seen_urls[url]
            entry["last_seen"] = today
            entry["count"] = entry.get("count", 1) + 1
            if entry["count"] >= 3:
                hot_items.append({**item, "count": entry["count"]})
            continue  # don't output to main topic

        # 3. Within-run URL dedup
        if url in seen_urls_this_run:
            continue

        # 4. Within-run title-similarity dedup: keep higher score
        duplicate = False
        for existing_url, existing in list(seen_urls_this_run.items()):
            if title_similarity(title, existing.get("title", "")) > 0.8:
                if item.get("score", 0) > existing.get("score", 0):
                    del seen_urls_this_run[existing_url]
                else:
                    duplicate = True
                break
        if duplicate:
            continue

        seen_urls_this_run[url] = item
        # Add to seen.json
        seen_urls[url] = {
            "title": title[:80],
            "url": url,
            "first_seen": today,
            "last_seen": today,
            "count": 1,
        }
        output.append(item)

    # Sort by score desc, cap at max_items
    output.sort(key=lambda x: -x.get("score", 0))
    return output[:max_items], hot_items


# ─── Step 4: Generate "why" text ───────────────────────────────────────────
# This uses the Claude API when available; falls back to why_hint if not.

def generate_why(item):
    """
    Generate a specific, actionable "why this matters" blurb (30-50 chars) for a P6/P7
    frontend engineer focused on Claude Code toolchain, AI coding tools, and Chinese cloud.
    If the ANTHROPIC_API_KEY is set, delegates to Claude; otherwise uses why_hint.
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        hint = item.get("why_hint", "")
        return hint[:100] if hint else "相关技术动态，请查阅原文"

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_key)
        prompt = f"""你是一位 P6/P7 前端工程师的技术资讯助手。
对以下资讯条目，用 30-50 字写"为什么关注"：
- 必须说明与前端/AI工具/国内云厂商的具体关联
- 不写"建议关注"、"建议收藏"等无信息量套话
- 优先说明：①对日常编码工作流的直接影响，或②面试高频考点，或③技术趋势判断依据

资讯标题：{item.get('title', '')}
资讯URL：{item.get('url', '')}
背景信息：{item.get('why_hint', '')[:300]}

只输出"为什么关注"文本，不加引号，不加前缀。"""
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return item.get("why_hint", "")[:100]


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Step 1: Read watch.yaml
    watch_path = "watch.yaml"
    if not os.path.exists(watch_path):
        print("ERROR: watch.yaml not found", file=sys.stderr)
        sys.exit(1)
    with open(watch_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    topics = cfg.get("topics", [])
    filters = cfg.get("filters", {})
    output_cfg = cfg.get("output", {})
    lookback_hours = filters.get("lookback_hours", 24)
    max_items = filters.get("max_items_per_topic", 5)
    exclude_kw = filters.get("exclude_keywords", [])
    seen_ttl = filters.get("seen_ttl_days", 7)
    skip_if_empty = output_cfg.get("skip_if_empty", True)

    # Step 1: Read seen.json
    seen_path = ".ai-news-bot/seen.json"
    seen_urls = {}
    if os.path.exists(seen_path):
        try:
            with open(seen_path, "r", encoding="utf-8") as f:
                seen_urls = json.load(f).get("urls", {})
        except (json.JSONDecodeError, KeyError):
            seen_urls = {}

    # Clean expired entries
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=seen_ttl)).strftime("%Y-%m-%d")
    seen_urls = {
        k: v for k, v in seen_urls.items()
        if v.get("last_seen", "9999") >= cutoff_date
    }

    # Step 2: Fetch all sources
    skipped_sources = []
    topic_raw = {}  # topic_id → raw item list

    for topic in topics:
        topic_id = topic["id"]
        raw = []
        srcs = topic.get("sources", {})

        # A. GitHub Releases
        for repo in srcs.get("github_releases", []):
            raw.extend(fetch_github_releases(repo, lookback_hours, skipped_sources))

        # B. GitHub Trending
        if "github_trending" in srcs:
            t_cfg = srcs["github_trending"]
            must_match = t_cfg.get("must_match_any", [])
            raw.extend(fetch_github_trending(must_match, skipped_sources))

        # C. GitHub Search
        if "github_search" in srcs:
            s_cfg = srcs["github_search"]
            for query in s_cfg.get("queries", []):
                raw.extend(fetch_github_search(query, lookback_hours, skipped_sources))

        # D. HN Algolia
        if "hn" in srcs:
            hn_cfg = srcs["hn"]
            raw.extend(fetch_hn(
                hn_cfg.get("keywords", []),
                hn_cfg.get("min_points", 30),
                lookback_hours,
                skipped_sources,
            ))

        # E. Web
        for web_src in srcs.get("web", []):
            raw.extend(fetch_web(web_src, lookback_hours, skipped_sources))

        # F. DEV Community
        if "dev_community" in srcs:
            dev_cfg = srcs["dev_community"]
            for tag in dev_cfg.get("tags", []):
                raw.extend(fetch_dev_community(tag, lookback_hours, skipped_sources))

        topic_raw[topic_id] = raw

    # Step 3: Filter + dedup per topic
    topic_labels = {t["id"]: t["name"] for t in topics}
    results = {}
    all_hot = []
    for topic in topics:
        tid = topic["id"]
        raw = topic_raw.get(tid, [])
        filtered, hot = filter_and_dedup(raw, exclude_kw, seen_urls, max_items, today)
        # Generate "why" for each filtered item
        for item in filtered:
            if not item.get("why"):
                item["why"] = generate_why(item)
        results[tid] = filtered
        all_hot.extend(hot)

    # Step 4: Build card
    has_content = any(bool(v) for v in results.values())
    if not has_content and skip_if_empty:
        print("No content found; skip_if_empty=true, exiting.")
        return

    elements = []
    for topic in topics:
        tid = topic["id"]
        items = results.get(tid, [])
        if not items:
            continue
        label = topic_labels.get(tid, tid)
        lines = [f"**{label}**"]
        for item in items:
            t = truncate_title(item["title"])
            lines.append(f"· [{t}]({item['url']})")
            lines.append(f"  _{item.get('why', '')}_ ")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        elements.append({"tag": "hr"})

    # Hot items section
    unique_hot = {h["url"]: h for h in all_hot}.values()
    hot_sorted = sorted(unique_hot, key=lambda x: -x.get("count", 0))[:5]
    if hot_sorted:
        lines = ["**🔥 持续热点（连续多日高热）**"]
        for item in hot_sorted:
            t = truncate_title(item["title"])
            lines.append(f"· [{t}]({item['url']}) · 已连续 {item.get('count', 0)} 天")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        elements.append({"tag": "hr"})

    skip_msg = "、".join(dict.fromkeys(skipped_sources)) if skipped_sources else "无"
    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": (
                "数据来源：GitHub Releases · GitHub Trending · GitHub Search · "
                "HN Algolia · DEV Community · 雷峰网（Jina Reader）"
                "｜修改关注维度：编辑 watch.yaml\n"
                f"⚠️ 跳过：{skip_msg}"
            )
        }]
    })

    card_data = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📡 AI 资讯速报 · {today}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }

    # Step 5: Write and validate
    os.makedirs(".ai-news-bot", exist_ok=True)
    report_path = ".ai-news-bot/latest-report.json"

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(card_data, f, ensure_ascii=False, indent=2)

    with open(report_path, "r", encoding="utf-8") as f:
        json.load(f)  # validate; raises on bad JSON

    with open(seen_path, "w", encoding="utf-8") as f:
        json.dump({"urls": seen_urls}, f, ensure_ascii=False, indent=2)

    # Git push to report/YYYY-MM-DD
    branch = f"report/{today}"
    try:
        subprocess.run(["git", "config", "user.email", "routine-bot@ai-news"], check=True)
        subprocess.run(["git", "config", "user.name", "AI News Routine"], check=True)
        result = subprocess.run(["git", "checkout", "-b", branch], capture_output=True)
        if result.returncode != 0:
            subprocess.run(["git", "checkout", branch], check=True)
        subprocess.run(["git", "add", report_path, seen_path], check=True)
        subprocess.run(["git", "commit", "-m", f"chore: daily report {today}"], check=True)
        # Retry push up to 4 times with exponential backoff
        for attempt, wait in enumerate([0, 2, 4, 8, 16]):
            if wait:
                time.sleep(wait)
            push = subprocess.run(["git", "push", "-u", "origin", branch])
            if push.returncode == 0:
                print(f"✓ Pushed to origin/{branch}")
                break
            print(f"  Push attempt {attempt + 1} failed, retrying in {wait}s…")
        else:
            print("ERROR: All push attempts failed. Printing report:", file=sys.stderr)
            with open(report_path) as f:
                print(f.read(), file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"Git error: {e}", file=sys.stderr)
        with open(report_path) as f:
            print(f.read(), file=sys.stderr)


if __name__ == "__main__":
    main()
