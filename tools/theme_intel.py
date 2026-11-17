# coding=utf-8
"""
题材情报编排器 (theme_intel.py)

流程：题材分析师给出题材词 → 按题材类型查路由表选定渠道 → 实时抓取 →
      关键词/同义词匹配 → 输出结构化情报 + HTML 报告。

设计原则：
- 纯标准库，零第三方依赖，可直接运行
- 渠道与路由全部配置化（config/theme_routes.json），加渠道/加题材类型只改配置
- 复用 TrendRadar 已验证的信息源（NewsNow 热榜 + RSSHub/RSS）

用法：
  python tools/theme_intel.py --theme 半导体 --type a_share
  python tools/theme_intel.py --theme 算力 --type a_share --html out.html
  python tools/theme_intel.py --theme AI --type tech --fuzzy
"""

import argparse
import concurrent.futures
import html as html_mod
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTES_FILE = ROOT / "config" / "theme_routes.json"
NEWSNOW_API = "https://newsnow.busiyi.world/api/s?id={id}&latest"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
CST = timezone(timedelta(hours=8))

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def load_routes():
    with open(ROUTES_FILE, encoding="utf-8") as f:
        return json.load(f)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return r.read()


def fetch_hot(channel):
    """NewsNow 热榜 → [{title, url, rank, source}]"""
    url = NEWSNOW_API.format(id=urllib.parse.quote(channel["id"]))
    data = json.loads(http_get(url))
    out = []
    for i, item in enumerate(data.get("items", []), 1):
        title = item.get("title")
        if not title or isinstance(title, float):
            continue
        out.append({"title": str(title).strip(), "url": item.get("url", ""),
                    "rank": i, "source": channel["name"], "time": ""})
    return out


def fetch_rss(channel):
    """RSS → [{title, url, time, source}]"""
    raw = http_get(channel["url"])
    root = ET.fromstring(raw)
    # 兼容 RSS2.0 与 Atom
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item")
    atom = False
    if not items:
        items = root.findall(".//a:entry", ns)
        atom = True
    out = []
    for it in items:
        def txt(tag, ns_prefix=None):
            if ns_prefix:
                el = it.find(ns_prefix, ns)
            else:
                el = it.find(tag)
            return el.text.strip() if el is not None and el.text else ""
        if atom:
            title = txt("a:title", "a:title")
            link_el = it.find("a:link", ns)
            url = link_el.get("href", "") if link_el is not None else ""
            pub = txt("a:published", "a:published") or txt("a:updated", "a:updated")
        else:
            title = txt("title")
            url = txt("link")
            pub = txt("pubDate")
        if title:
            out.append({"title": title, "url": url, "rank": 0,
                        "source": channel["name"], "time": pub[:25]})
    return out


def fetch_bridge(channel, theme=None):
    """MCP/搜索桥接文件 → [{title, url, time, source}]。

    通达信资讯/新华财经/题材专项网站都走这里。文件缺失/损坏返回空，不影响其他渠道。
    若 channel 带 theme_key 或条目带 theme 字段，则按当前深挖题材过滤：
    只返回属于该题材的条目（题材名互相包含也算命中），实现「每个题材专属信息源」。
    """
    p = ROOT / channel["file"]
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    out = []
    for i, it in enumerate(d.get("items", []), 1):
        title = it.get("title", "")
        if not title:
            continue
        # 题材过滤：条目 theme 与当前题材互相包含即命中
        item_theme = it.get("theme", "")
        if theme and item_theme:
            t = str(theme).lower()
            itt = str(item_theme).lower()
            if t not in itt and itt not in t and not any(
                    seg and seg in itt for seg in re.split(r"[/／(（·、]", t) if len(seg) >= 2):
                continue
        out.append({"title": title, "url": it.get("url", ""), "rank": it.get("rank", i),
                    "source": it.get("source", channel["name"]),
                    "time": it.get("time") or it.get("pub", "")[:25] or ""})
    return out


def fetch_channel(channel, theme=None):
    try:
        if channel["type"] == "hot":
            return channel["name"], fetch_hot(channel), None
        elif channel["type"] == "rss":
            return channel["name"], fetch_rss(channel), None
        elif channel["type"] == "bridge":
            return channel["name"], fetch_bridge(channel, theme=theme), None
    except Exception as e:
        return channel["name"], [], f"{type(e).__name__}: {str(e)[:80]}"
    return channel["name"], [], "未知渠道类型"


def build_keywords(theme, route, fuzzy):
    kws = [theme]
    expand = route.get("keywords_expand", {}) or {}
    for k, syns in expand.items():
        if k in theme or theme in k:
            kws.extend(syns)
    # 去重保序
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def match(item, keywords, fuzzy):
    text = item["title"].lower()
    for kw in keywords:
        k = kw.lower()
        if fuzzy:
            # 模糊：任意 2 字以上子串命中，或字符重合度
            if k in text:
                return True, kw
            if len(k) >= 2:
                grams = [k[i:i+2] for i in range(len(k)-1)]
                hit = sum(1 for g in grams if g in text)
                if len(grams) and hit / len(grams) >= 0.5:
                    return True, kw
        else:
            if k in text:
                return True, kw
    return False, ""


def run(theme, type_key, fuzzy, do_html, html_path):
    cfg = load_routes()
    registry = cfg["channel_registry"]
    routes = cfg["routes"]
    if type_key not in routes:
        print(f"[错误] 未知题材类型 {type_key!r}，可选: {', '.join(routes.keys())}")
        sys.exit(2)
    route = routes[type_key]
    channels = [registry[c] for c in route["channels"] if c in registry]
    keywords = build_keywords(theme, route, fuzzy)

    print(f"[题材] {theme}  |  类型: {route['name']}  |  渠道 {len(channels)} 个")
    print(f"[关键词] {', '.join(keywords)}")
    print(f"[抓取] 并发抓取中...")

    all_items, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_channel, ch): ch for ch in channels}
        for fut in concurrent.futures.as_completed(futs):
            name, items, err = fut.result()
            if err:
                errors.append((name, err))
                print(f"  [!] {name}: {err}")
            else:
                print(f"  [ok] {name}: {len(items)} 条")
                all_items.extend(items)

    hits = []
    for item in all_items:
        ok, kw = match(item, keywords, fuzzy)
        if ok:
            item["matched"] = kw
            hits.append(item)
    # 去重（按标题）
    seen, uniq = set(), []
    for h in hits:
        key = h["title"]
        if key not in seen:
            seen.add(key)
            uniq.append(h)
    hits = uniq

    print(f"\n=== 命中 {len(hits)} 条 ===")
    for h in hits[:30]:
        print(f"  [{h['source']}] {h['title'][:70]}  <{h['matched']}>")

    summary = {
        "theme": theme, "type": route["name"], "keywords": keywords,
        "channels": [c["name"] for c in channels], "errors": errors,
        "total_fetched": len(all_items), "hits": len(hits),
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if do_html:
        out = Path(html_path) if html_path else ROOT / f"题材情报-{theme}-{datetime.now(CST).strftime('%Y%m%d')}.html"
        write_html(out, summary, hits)
        print(f"\n[报告] {out}")

    return summary, hits


def write_html(path, s, hits):
    def esc(x):
        return html_mod.escape(str(x or ""))
    rows = []
    for h in hits:
        link = f'<a href="{esc(h["url"])}" target="_blank">{esc(h["title"])}</a>' if h.get("url") else esc(h["title"])
        rows.append(f'<tr><td class="src">{esc(h["source"])}</td><td>{link}</td>'
                    f'<td class="kw">{esc(h.get("matched",""))}</td><td class="tm">{esc(h.get("time",""))}</td></tr>')
    table = "\n".join(rows) if rows else '<tr><td colspan="4" style="text-align:center;color:#999">未命中相关信息</td></tr>'
    err = ""
    if s["errors"]:
        err = '<div class="warn">抓取异常: ' + "; ".join(f"{esc(n)}({esc(e)})" for n, e in s["errors"]) + "</div>"
    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>题材情报 · {esc(s['theme'])}</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f6f8;color:#1a1a1a;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:960px;margin:32px auto;padding:0 20px}}.card{{background:#fff;border:1px solid #e8e8ec;border-radius:12px;padding:24px;margin-bottom:18px}}
h1{{font-size:22px;margin:0 0 6px}}h2{{font-size:15px;margin:0 0 14px;color:#333}}sub{{color:#888;font-size:12px}}
.meta{{color:#666;font-size:13px;line-height:1.9}}.tag{{display:inline-block;background:#eef3fb;color:#2b5fa8;border-radius:5px;padding:2px 9px;margin:2px 4px 2px 0;font-size:12px}}
.stat{{display:flex;gap:28px;margin-top:10px}}.stat div{{font-size:13px;color:#666}}.stat b{{display:block;font-size:22px;color:#1a1a1a}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:9px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
th{{background:#fafafa;color:#666;font-weight:500}}a{{color:#2b5fa8;text-decoration:none}}a:hover{{text-decoration:underline}}
.src{{white-space:nowrap;color:#555}}.kw{{color:#b0402e;white-space:nowrap}}.tm{{color:#999;white-space:nowrap;font-size:12px}}
.warn{{background:#fff6e5;color:#8a5a00;border-radius:6px;padding:9px 12px;font-size:12px;margin-top:10px}}
.foot{{color:#999;font-size:11px;text-align:center;margin:18px 0}}
</style></head><body><div class="wrap">
<div class="card"><h1>题材情报 · {esc(s['theme'])}</h1>
<sub>类型 {esc(s['type'])} · 生成 {esc(s['generated_at'])}</sub>
<div class="meta" style="margin-top:12px">关键词: {''.join(f'<span class="tag">{esc(k)}</span>' for k in s['keywords'])}</div>
<div class="meta" style="margin-top:6px">渠道: {', '.join(esc(c) for c in s['channels'])}</div>
<div class="stat"><div>抓取<b>{s['total_fetched']}</b></div><div>命中<b>{s['hits']}</b></div><div>渠道<b>{len(s['channels'])}</b></div></div>
{err}</div>
<div class="card"><h2>命中信息（{len(hits)} 条）</h2>
<table><thead><tr><th>来源</th><th>标题</th><th>命中词</th><th>时间</th></tr></thead><tbody>{table}</tbody></table></div>
<div class="foot">由 theme_intel.py 生成 · 数据来自公开热榜/RSS，仅供题材跟踪参考</div>
</div></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def main():
    ap = argparse.ArgumentParser(description="题材情报编排器")
    ap.add_argument("--theme", required=True, help="题材词，如 半导体")
    ap.add_argument("--type", default="all", help="题材类型，见路由表 routes 的 key")
    ap.add_argument("--fuzzy", action="store_true", help="启用模糊匹配")
    ap.add_argument("--html", default="", help="HTML 报告输出路径（不填则自动命名）")
    ap.add_argument("--no-html", action="store_true", help="只打印终端，不生成 HTML")
    a = ap.parse_args()
    run(a.theme, a.type, a.fuzzy, not a.no_html, a.html)


if __name__ == "__main__":
    main()
