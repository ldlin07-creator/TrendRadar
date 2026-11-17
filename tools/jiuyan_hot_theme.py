# coding=utf-8
"""
韭研公社 24 小时题材热度上升探测器 (jiuyan_hot_theme.py)

不预设题材：抓韭研公社 RSS（多镜像容错）最近 24h 的讨论 → 按题材词典聚类 →
用「最近时段 vs 较早时段」计算热度上升速度 → 输出上升最快的题材 + 相关讨论总结。
同时落盘历史快照，后续运行可用真实快照差值判断趋势。

纯标准库，零依赖。

用法：
  python tools/jiuyan_hot_theme.py
  python tools/jiuyan_hot_theme.py --json out.json --report out.html
  python tools/jiuyan_hot_theme.py --push          # 推送到已配置的渠道（读环境变量）
  python tools/jiuyan_hot_theme.py --deep-dive     # 发现热点后，自动对 TOP 热点做全渠道深挖并一起推送
  python tools/jiuyan_hot_theme.py --deep-dive --deep-top 3 --deep-per 5

「发现热点 → 自动深挖」链路（--deep-dive）：
  1. 本脚本先算出韭研 24h 热度上升最快的题材
  2. 取 TOP N 题材，调用 theme_intel.py 的抓取+匹配，在全渠道（路由表 a_share）
     查每个题材的相关信息
  3. 深挖结果写入 HTML 报告，并作为第二条消息推送
  题材词做归一化（"算力/AI芯片" → "算力"）以命中同义词扩展表。

推送渠道（环境变量，与 TrendRadar 的 GitHub Secrets 对齐）：
  WEWORK_WEBHOOK_URL   企业微信群机器人 webhook
  GENERIC_WEBHOOK_URL  通用 webhook（钉钉/飞书/自建，自动识别格式）
  NTFY_SERVER_URL + NTFY_TOPIC   ntfy（最简单，手机装 ntfy app 订阅 topic 即可）
  可同时配多个，每个渠道独立推送。推送失败不影响其他渠道。
"""

import argparse
import concurrent.futures
import html as html_mod
import json
import base64
import os
import re
import smtplib
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime, formataddr, formatdate, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIRRORS = [
    "https://rsshub.rssforever.com/jiuyangongshe/community",
    "https://rsshub.app/jiuyangongshe/community",
    "https://rsshub.privatered.site/jiuyangongshe/community",
    "https://rsshub.rssforever.com/jiuyangongshe/community",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
CST = timezone(timedelta(hours=8))
SNAPSHOT_DIR = ROOT / "output" / "jiuyan_heat"

# 常见邮箱 SMTP 配置：domain -> (server, port, use_ssl)
# use_ssl=True 用 SMTP_SSL(465)；False 用 STARTTLS(587)
SMTP_CONFIGS = {
    "qq.com": ("smtp.qq.com", 465, True),
    "foxmail.com": ("smtp.qq.com", 465, True),
    "163.com": ("smtp.163.com", 465, True),
    "126.com": ("smtp.126.com", 465, True),
    "gmail.com": ("smtp.gmail.com", 587, False),
    "outlook.com": ("smtp-mail.outlook.com", 587, False),
    "hotmail.com": ("smtp-mail.outlook.com", 587, False),
    "live.com": ("smtp-mail.outlook.com", 587, False),
    "sina.com": ("smtp.sina.com", 465, True),
    "sohu.com": ("smtp.sohu.com", 465, True),
    "icloud.com": ("smtp.mail.me.com", 587, False),
    "139.com": ("smtp.139.com", 465, True),
}

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

# 题材词典：题材名 → 命中关键词（标题包含任一词即归入该题材）
THEME_DICT = {
    "算力/AI芯片": ["算力", "英伟达", "GPU", "AI芯片", "昇腾", "国产算力", "服务器", "智算", "数据中心", "光模块", "CPO", "HBM", "寒武纪"],
    "半导体/国产替代": ["半导体", "芯片", "光刻", "晶圆", "存储", "封测", "国产替代", "先进制程", "靶材", "光刻胶", "第三代半导体", "碳化硅"],
    "机器人/具身智能": ["机器人", "人形机器人", "具身智能", "宇树", "智元", "减速器", "丝杠", "灵巧手", "特斯拉Optimus"],
    "消费电子/端侧AI": ["消费电子", "端侧AI", "AI手机", "AI眼镜", "可穿戴", "折叠屏", "果链", "苹果", "华为", "Pb"],
    "新能源/储能": ["光伏", "储能", "锂电", "固态电池", "钠电池", "风电", "逆变器", "特高压", "电网"],
    "汽车/自动驾驶": ["自动驾驶", "智驾", "智能汽车", "比亚迪", "特斯拉", "FSD", "无人驾驶", "车企", "汽车"],
    "医药/创新药": ["创新药", "医药", "CXO", "减肥药", "GLP", "生物医药", "疫苗", "中药", "医疗"],
    "军工/航天": ["军工", "航天", "卫星", "商业航天", "火箭", "低空经济", "eVTOL", "无人机", "国防"],
    "数据要素/信创": ["数据要素", "信创", "国产软件", "操作系统", "数据库", "国资云", "数据资产"],
    "金融/券商": ["券商", "银行", "保险", "牛市", "A股", "指数", "IPO", "重组", "并购"],
    "地产/基建": ["地产", "房地产", "基建", "城中村", "建材", "水泥"],
    "资源/周期": ["黄金", "铜", "稀土", "有色", "煤炭", "石油", "化工", "磷", "氟"],
    "政策/宏观": ["政策", "国常会", "央行", "降息", "降准", "财政", "关税", "证监会", "发改委", "BIS", "制裁"],
}

# 时段划分：最近 6h 为 hot，前 6-24h 为 baseline
HOT_HOURS = 6
WINDOW_HOURS = 24


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,application/xml,*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.read()


def fetch_rss():
    """多镜像容错抓取，返回 (items, used_mirror)"""
    last_err = ""
    for url in MIRRORS:
        try:
            raw = http_get(url)
            root = ET.fromstring(raw)
            items = root.findall(".//item")
            if not items:
                raise ValueError("空 feed")
            out = []
            for it in items:
                def txt(tag):
                    el = it.find(tag)
                    return el.text.strip() if el is not None and el.text else ""
                title = txt("title")
                pub_raw = txt("pubDate")
                pub = None
                if pub_raw:
                    try:
                        pub = parsedate_to_datetime(pub_raw)
                        if pub.tzinfo is None:
                            pub = pub.replace(tzinfo=timezone.utc)
                        pub = pub.astimezone(CST)
                    except Exception:
                        pub = None
                out.append({"title": title, "url": txt("link"),
                            "pub": pub, "pub_str": pub.strftime("%m-%d %H:%M") if pub else ""})
            return out, url
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:60]}"
            continue
    raise RuntimeError(f"所有镜像均失败，最后错误: {last_err}")


def classify(title):
    """返回标题命中的题材列表"""
    t = title.lower()
    hit = []
    for theme, kws in THEME_DICT.items():
        if any(kw.lower() in t for kw in kws):
            hit.append(theme)
    return hit


def load_last_snapshot():
    if not SNAPSHOT_DIR.exists():
        return None
    files = sorted(SNAPSHOT_DIR.glob("snapshot_*.json"))
    if not files:
        return None
    try:
        with open(files[-1], encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_snapshot(snap):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(CST).strftime("%Y%m%d_%H%M")
    with open(SNAPSHOT_DIR / f"snapshot_{ts}.json", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)


def analyze():
    items, mirror = fetch_rss()
    now = datetime.now(CST)
    recent_24 = [i for i in items if i["pub"] and (now - i["pub"]).total_seconds() <= WINDOW_HOURS * 3600]
    # 无日期的条目也纳入，但没有时间权重
    no_time = [i for i in items if not i["pub"]]
    pool = recent_24 + no_time

    hot_counter, base_counter = Counter(), Counter()
    theme_items = defaultdict(list)
    for it in pool:
        themes = classify(it["title"])
        if not themes:
            continue
        age_h = (now - it["pub"]).total_seconds() / 3600 if it["pub"] else 99
        bucket = hot_counter if age_h <= HOT_HOURS else base_counter
        for th in themes:
            bucket[th] += 1
            theme_items[th].append(it)

    # 热度上升分：近6h频次归一化 - 前段频次归一化；无历史时用时段差
    scores = []
    all_themes = set(hot_counter) | set(base_counter)
    for th in all_themes:
        hot = hot_counter[th]
        base = base_counter[th]
        base_rate = base / (WINDOW_HOURS - HOT_HOURS)  # 每小时
        hot_rate = hot / HOT_HOURS
        rise = hot_rate - base_rate
        # 历史快照增量（若有）
        hist_delta = None
        snap = globals().get("_last_snap")
        if snap and "counts" in snap:
            prev = snap["counts"].get(th, 0)
            hist_delta = (hot + base) - prev
        scores.append({"theme": th, "hot": hot, "base": base, "rise": round(rise, 3),
                       "total": hot + base, "hist_delta": hist_delta,
                       "discussion": [{"title": i["title"], "url": i.get("url", ""), "time": i["pub_str"]}
                                      for i in theme_items[th][:8]]})
    scores.sort(key=lambda x: (x["rise"], x["total"]), reverse=True)

    snap = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "mirror": mirror,
            "total_items": len(items), "counts": {t: hot_counter[t] + base_counter[t] for t in all_themes}}
    return {"generated_at": now.strftime("%Y-%m-%d %H:%M:%S"), "mirror": mirror,
            "total_fetched": len(items), "in_window": len(recent_24), "no_time": len(no_time),
            "themes": scores, "top": scores[:5], "_snapshot": snap}


# ---- 发现热点 → 自动深挖 --------------------------------------------------

def split_keywords(theme_name, expand):
    """把复合题材名拆成关键词列表，并叠加同义词扩展。
    '金融/券商' -> ['金融','券商', ...扩展]
    '算力/AI芯片' -> ['算力','AI芯片','英伟达','国产算力', ...]
    """
    segs = [s.strip() for s in re.split(r"[/／(（·、]", theme_name) if s.strip()]
    kws = list(segs) or [theme_name.strip()]
    for seg in segs:
        for k, syns in (expand or {}).items():
            if k in seg or seg in k:
                kws.extend(syns)
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def load_analyst_themes():
    """读题材分析师清单 config/analyst_themes.json，返回启用中的题材任务列表。"""
    cfg_path = ROOT / "config" / "analyst_themes.json"
    if not cfg_path.exists():
        return []
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        tasks = []
        for t in cfg.get("themes", []):
            if t.get("enabled", True) and t.get("name"):
                tasks.append({"name": t["name"], "type": t.get("type", "a_share"),
                              "source": "题材分析师", "reason": t.get("reason", ""),
                              "updated_at": cfg.get("updated_at", ""),
                              "rise": None, "hot": None})
        return tasks
    except Exception as e:
        print(f"[深挖] 读取分析师题材清单失败: {type(e).__name__}: {str(e)[:60]}")
        return []


def build_dive_tasks(top, top_n=3, use_analyst=True):
    """合并两路深挖题材：韭研热度 TOP N + 题材分析师清单。按归一化关键词去重。"""
    tasks = []
    # 第一路：韭研自动发现的热点
    for t in top[:top_n]:
        tasks.append({"name": t["theme"], "type": "a_share", "source": "韭研热点",
                      "reason": f"近6h {t['hot']} 条 / 上升分 {t['rise']}",
                      "rise": t["rise"], "hot": t["hot"]})
    # 第二路：题材分析师给定题材
    if use_analyst:
        tasks.extend(load_analyst_themes())
    # 去重：按核心词（第一段）判重，韭研热点优先保留
    seen, uniq = set(), []
    for t in tasks:
        key = re.split(r"[/／(（·、]", t["name"], maxsplit=1)[0].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(t)
    print(f"[深挖] 题材合并：韭研TOP{len(top[:top_n])} + 分析师{len(load_analyst_themes() if use_analyst else [])} "
          f"→ 去重后 {len(uniq)} 个")
    return uniq


def deep_dive(top, top_n=3, per_theme=5, use_analyst=True):
    """对「韭研 TOP N + 分析师题材」做全渠道深挖。
    返回 [{rank, theme, keyword, source, reason, hits:[...], errors}]，单个题材失败不影响其他。
    """
    try:
        import theme_intel
    except Exception as e:
        print(f"[深挖] 无法加载 theme_intel: {e}")
        return []

    cfg = theme_intel.load_routes()
    registry = cfg["channel_registry"]
    tasks = build_dive_tasks(top, top_n, use_analyst)

    results = []
    for rank, t in enumerate(tasks, 1):
        route = cfg["routes"].get(t["type"]) or cfg["routes"].get("a_share") or cfg["routes"].get("all")
        channels = [registry[c] for c in route["channels"] if c in registry]
        channel_names = [c["name"] for c in channels]
        keywords = split_keywords(t["name"], route.get("keywords_expand", {}))
        print(f"\n[深挖 {rank}/{len(tasks)}] [{t['source']}] {t['name']} → 关键词 {'/'.join(keywords[:6])}")
        all_items, errors = [], []
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                futs = [ex.submit(theme_intel.fetch_channel, ch) for ch in channels]
                for fut in concurrent.futures.as_completed(futs):
                    name, items, err = fut.result()
                    if err:
                        errors.append((name, err))
                    else:
                        all_items.extend(items)
            hits = []
            for item in all_items:
                ok, kw = theme_intel.match(item, keywords, True)
                if ok:
                    item["matched"] = kw
                    hits.append(item)
            seen, uniq = set(), []
            for h in hits:
                if h["title"] not in seen:
                    seen.add(h["title"])
                    uniq.append(h)
            clean = [{"title": h.get("title", ""), "url": h.get("url", ""),
                      "source": h.get("source", ""), "time": h.get("time", ""),
                      "matched": h.get("matched", "")} for h in uniq[:per_theme]]
            print(f"  抓取 {len(all_items)} 条 / 命中 {len(uniq)} 条，保留 {len(clean)} 条")
            results.append({"rank": rank, "theme": t["name"],
                            "keyword": "/".join(keywords[:6]),
                            "source": t["source"], "reason": t.get("reason", ""),
                            "rise": t.get("rise"), "hot": t.get("hot"),
                            "type": t["type"],
                            "total_fetched": len(all_items), "hits_total": len(uniq),
                            "channels": channel_names, "errors": errors, "hits": clean})
        except Exception as e:
            print(f"[深挖] {t['name']} 失败: {type(e).__name__}: {str(e)[:60]}")
            results.append({"rank": rank, "theme": t["name"],
                            "keyword": "/".join(keywords[:6]),
                            "source": t["source"], "reason": t.get("reason", ""),
                            "rise": t.get("rise"), "hot": t.get("hot"), "type": t["type"],
                            "hits": [], "errors": [("深挖", f"{type(e).__name__}: {str(e)[:60]}")]})
    return results


def write_report(path, data):
    def esc(x):
        return html_mod.escape(str(x or ""))
    secs = []
    for idx, t in enumerate(data["top"], 1):
        disc = "".join(
            f'<li><a href="{esc(d["url"])}" target="_blank">{esc(d["title"])}</a>'
            f'<span class="tm">{esc(d["time"])}</span></li>' for d in t["discussion"]
        ) or "<li style='color:#999'>无</li>"
        delta = ""
        if t["hist_delta"] is not None:
            sign = "+" if t["hist_delta"] >= 0 else ""
            delta = f'<span class="delta">较上次 {sign}{t["hist_delta"]}</span>'
        secs.append(f"""<div class="theme">
<div class="th"><span class="num">{idx}</span>{esc(t['theme'])}{delta}</div>
<div class="bar"><span style="width:{min(100, max(8, t['rise']*60+15))}%"></span></div>
<div class="meta">近6h {t['hot']} 条 · 前段 {t['base']} 条 · 上升分 {t['rise']} · 合计 {t['total']} 条</div>
<ul>{disc}</ul></div>""")
    body = "\n".join(secs) if secs else "<div class='empty'>最近24小时未识别到明显题材聚集</div>"

    # 深挖板块
    deep_html = ""
    dd = data.get("deep_dive") or []
    if dd:
        dsecs = []
        for r in dd:
            rows = []
            for h in r["hits"]:
                link = (f'<a href="{esc(h["url"])}" target="_blank">{esc(h["title"])}</a>'
                        if h.get("url") else esc(h["title"]))
                rows.append(f'<li><span class="src">[{esc(h["source"])}]</span> {link}'
                            f'<span class="tm">{esc(h.get("matched",""))}</span></li>')
            hit_list = "".join(rows) or "<li style='color:#999'>未命中相关信息</li>"
            errs = ""
            if r.get("errors"):
                errs = '<div class="warn">抓取异常: ' + "; ".join(f"{esc(n)}({esc(e)})" for n, e in r["errors"]) + "</div>"
            src = esc(r.get("source", ""))
            badge_cls = "analyst" if r.get("source") == "题材分析师" else "hot"
            src_badge = f'<span class="badge {badge_cls}">{src}</span>'
            reason = f'<div class="reason">{esc(r.get("reason", ""))}</div>' if r.get("reason") else ""
            rise_txt = f' · 上升分 {r["rise"]}' if r.get("rise") is not None else ""
            dsecs.append(f"""<div class="theme">
<div class="th"><span class="num dives">{r['rank']}</span>{esc(r['theme'])}{src_badge}</div>
{reason}<div class="meta">关键词「{esc(r['keyword'])}」· 全渠道抓取 {r.get('total_fetched','?')} 条 · 命中 {r.get('hits_total','?')} 条{rise_txt}</div>
{errs}<ul>{hit_list}</ul></div>""")
        deep_html = f"""<div class="card"><h2>🔥 热点深挖 · 全渠道详情</h2>
<sub>韭研热点 + 题材分析师清单，共 {len(dd)} 个题材自动检索，渠道：{esc(', '.join(dd[0].get('channels', [])))}</sub>
{''.join(dsecs)}</div>"""

    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>韭研题材热度 · {esc(data['generated_at'])}</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f6f8;color:#1a1a1a;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:860px;margin:28px auto;padding:0 18px}}.card{{background:#fff;border:1px solid #e8e8ec;border-radius:12px;padding:22px;margin-bottom:16px}}
h1{{font-size:21px;margin:0 0 4px}}h2{{font-size:14px;margin:0 0 12px;color:#444}}sub{{color:#999;font-size:12px}}
.meta{{color:#777;font-size:12px;margin:6px 0}}.theme{{padding:14px 0;border-top:1px solid #eee}}.theme:first-child{{border-top:0}}
.th{{font-size:15px;font-weight:500;display:flex;align-items:center;gap:9px}}.num{{background:#c0392b;color:#fff;width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px}}
.num.dives{{background:#2b5fa8}}
.badge{{font-size:10px;font-weight:400;border-radius:4px;padding:2px 7px;margin-left:2px}}
.badge.hot{{background:#fdece8;color:#c0392b}}.badge.analyst{{background:#eaf1fb;color:#2b5fa8}}
.reason{{color:#888;font-size:11px;margin:5px 0 2px;line-height:1.5}}
.delta{{color:#999;font-size:11px;font-weight:400}}
.bar{{height:5px;background:#f0f0f0;border-radius:3px;margin:9px 0 5px}}.bar span{{display:block;height:100%;background:#c0392b;border-radius:3px}}
ul{{margin:8px 0 0;padding-left:18px}}li{{font-size:13px;line-height:1.7}}a{{color:#2b5fa8;text-decoration:none}}a:hover{{text-decoration:underline}}
.src{{color:#888;font-size:11px}}.tm{{color:#aaa;font-size:11px;margin-left:8px}}.empty{{color:#999;text-align:center;padding:30px}}
.warn{{background:#fff6e5;color:#8a5a00;border-radius:6px;padding:8px 10px;font-size:11px;margin:8px 0}}
.foot{{color:#aaa;font-size:11px;text-align:center;margin:16px 0}}
</style></head><body><div class="wrap">
<div class="card"><h1>韭研公社 · 24h 题材热度上升榜</h1>
<sub>{esc(data['generated_at'])} · 抓取 {data['total_fetched']} 条 / 窗口内 {data['in_window']} 条 · 源 {esc(data['mirror'].split('/')[2])}</sub></div>
<div class="card"><h2>热度上升最快的题材 TOP{len(data['top'])}</h2>{body}</div>
{deep_html}
<div class="foot">题材词典匹配 + 分时段频次对比 + 全渠道深挖 · 仅供题材跟踪参考，非投资建议</div>
</div></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def build_markdown(data):
    """把分析结果转成推送用的 Markdown 文本"""
    lines = [f"## 韭研公社 · 24h 题材热度上升榜", ""]
    lines.append(f"> {data['generated_at']} ｜ 抓取 {data['total_fetched']} 条 / 窗口内 {data['in_window']} 条")
    lines.append("")
    if not data["top"]:
        lines.append("最近24小时未识别到明显题材聚集。")
        return "\n".join(lines)
    for i, t in enumerate(data["top"], 1):
        delta = ""
        if t["hist_delta"] is not None:
            sign = "+" if t["hist_delta"] >= 0 else ""
            delta = f"（较上次 {sign}{t['hist_delta']}）"
        lines.append(f"**{i}. {t['theme']}** — 上升分 {t['rise']} · 近6h {t['hot']} 条{delta}")
        for d in t["discussion"][:4]:
            title = d["title"].replace("*", "＊")
            tm = f"[{d['time']}] " if d["time"] else ""
            if d.get("url"):
                lines.append(f"  - {tm}[{title}]({d['url']})")
            else:
                lines.append(f"  - {tm}{title}")
        lines.append("")
    lines.append("— 游资视角题材热度信号，非投资建议")

    # 深挖部分（作为同一条推送的下半段）
    dd = data.get("deep_dive") or []
    if dd:
        lines.append("")
        lines.append("---")
        lines.append("### 🔍 热点深挖 · 全渠道详情")
        for r in dd:
            lines.append("")
            src = r.get("source", "")
            tag = "【分析师】" if src == "题材分析师" else "【韭研热点】"
            lines.append(f"**{r['rank']}. {tag}{r['theme']}** — 关键词「{r['keyword']}」，"
                         f"全渠道命中 {r.get('hits_total', len(r['hits']))} 条")
            for h in r["hits"][:4]:
                title = h["title"].replace("*", "＊")
                src = f"[{h['source']}] " if h.get("source") else ""
                if h.get("url"):
                    lines.append(f"  - {src}[{title}]({h['url']})")
                else:
                    lines.append(f"  - {src}{title}")
        lines.append("")
        lines.append("— 多源交叉验证，非投资建议")
    return "\n".join(lines)


def send_email(html_path, data):
    """用 SMTP 发送 HTML 报告到邮箱。凭证读环境变量：
    EMAIL_FROM / EMAIL_PASSWORD / EMAIL_TO（多个收件人逗号分隔）
    可选 EMAIL_SMTP_SERVER / EMAIL_SMTP_PORT 覆盖自动识别。
    返回 (ok, message)。纯标准库实现。
    """
    sender = os.environ.get("EMAIL_FROM", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "").strip()
    to_raw = os.environ.get("EMAIL_TO", "").strip()
    if not (sender and password and to_raw):
        return False, "未配置邮箱（需 EMAIL_FROM / EMAIL_PASSWORD / EMAIL_TO）"

    # 读取 HTML 正文
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html_body = f.read()
    except Exception as e:
        return False, f"读取报告失败: {type(e).__name__}: {str(e)[:50]}"

    # 确定 SMTP 服务器/端口/加密
    custom_server = os.environ.get("EMAIL_SMTP_SERVER", "").strip()
    custom_port = os.environ.get("EMAIL_SMTP_PORT", "").strip()
    domain = sender.split("@")[-1].lower()
    if custom_server and custom_port:
        server, port = custom_server, int(custom_port)
        use_ssl = port == 465
    elif domain in SMTP_CONFIGS:
        server, port, use_ssl = SMTP_CONFIGS[domain]
    else:
        server, port, use_ssl = f"smtp.{domain}", 465, True

    recipients = [a.strip() for a in to_raw.split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr(("韭研题材热度", sender))
    msg["To"] = ", ".join(recipients)
    subject = f"韭研题材热度 · {data['generated_at']}"
    msg["Subject"] = Header(subject, "utf-8")
    msg["MIME-Version"] = "1.0"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    top_line = " / ".join(f"{t['theme']}(+{t['rise']})" for t in data.get("top", [])[:3])
    text_part = MIMEText(f"韭研题材热度上升榜\n{data['generated_at']}\n\n"
                         f"TOP题材: {top_line or '无'}\n\n请用支持HTML的邮件客户端查看完整报告。",
                         "plain", "utf-8")
    msg.attach(text_part)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if use_ssl:
            smtp = smtplib.SMTP_SSL(server, port, timeout=30, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(server, port, timeout=30)
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(sender, password)
        smtp.send_message(msg)
        smtp.quit()
        return True, f"ok -> {to_raw}"
    except smtplib.SMTPAuthenticationError:
        return False, "认证失败：检查邮箱/授权码（多数邮箱需用授权码而非登录密码）"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:70]}"


def post_json(url, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
        return r.status, r.read().decode("utf-8", "ignore")[:200]


def push_all(data, html_path=""):
    """按已配置的环境变量推送到各渠道，返回 {渠道: 成功/失败信息}"""
    text = build_markdown(data)
    result = {}

    # 企业微信群机器人
    ww = os.environ.get("WEWORK_WEBHOOK_URL", "").strip()
    if ww:
        try:
            post_json(ww, {"msgtype": "markdown", "markdown": {"content": text}})
            result["wework"] = "ok"
        except Exception as e:
            result["wework"] = f"FAIL {type(e).__name__}: {str(e)[:60]}"

    # 通用 webhook：按 URL 猜测平台格式
    gw = os.environ.get("GENERIC_WEBHOOK_URL", "").strip()
    if gw:
        try:
            if "dingtalk" in gw:
                payload = {"msgtype": "markdown", "markdown": {"title": "韭研题材热度", "text": text}}
            elif "feishu" in gw or "larksuite" in gw:
                payload = {"msg_type": "text", "content": {"text": text}}
            else:
                payload = {"title": "韭研题材热度", "content": text, "text": text}
            post_json(gw, payload)
            result["generic_webhook"] = "ok"
        except Exception as e:
            result["generic_webhook"] = f"FAIL {type(e).__name__}: {str(e)[:60]}"

    # ntfy：最简单，手机装 app 订阅 topic
    ntfy_server = os.environ.get("NTFY_SERVER_URL", "https://ntfy.sh").strip()
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
    if ntfy_topic:
        try:
            url = f"{ntfy_server.rstrip('/')}/{urllib.parse.quote(ntfy_topic)}"
            title_enc = "=?utf-8?b?" + base64.b64encode("韭研题材热度上升榜".encode("utf-8")).decode() + "?="
            req = urllib.request.Request(
                url, data=text.encode("utf-8"),
                headers={"Title": title_enc,
                         "Content-Type": "text/plain; charset=utf-8", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
                result["ntfy"] = f"ok({r.status})"
        except Exception as e:
            result["ntfy"] = f"FAIL {type(e).__name__}: {str(e)[:60]}"

    # 邮件：发完整 HTML 报告
    email_from = os.environ.get("EMAIL_FROM", "").strip()
    if email_from:
        ok, msg = send_email(html_path, data)
        result["email"] = msg if ok else f"FAIL {msg}"

    if not result:
        return {"_warn": "未配置任何推送渠道（需设置 EMAIL_FROM+EMAIL_PASSWORD+EMAIL_TO / WEWORK_WEBHOOK_URL / GENERIC_WEBHOOK_URL / NTFY_TOPIC）"}
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="结构化结果输出路径")
    ap.add_argument("--report", default="", help="HTML 报告输出路径")
    ap.add_argument("--push", action="store_true", help="推送到环境变量配置的渠道")
    ap.add_argument("--deep-dive", action="store_true", help="发现热点后自动对 TOP 题材+分析师题材做全渠道深挖")
    ap.add_argument("--deep-top", type=int, default=3, help="深挖几个韭研 TOP 题材（默认3）")
    ap.add_argument("--deep-per", type=int, default=5, help="每个题材保留几条命中（默认5）")
    ap.add_argument("--no-analyst", action="store_true", help="深挖时不含题材分析师清单（默认包含）")
    a = ap.parse_args()

    globals()["_last_snap"] = load_last_snapshot()
    data = analyze()
    save_snapshot(data.pop("_snapshot"))

    # 发现热点 → 自动深挖
    if a.deep_dive:
        data["deep_dive"] = deep_dive(data["top"], top_n=a.deep_top, per_theme=a.deep_per,
                                      use_analyst=not a.no_analyst)

    print(f"=== 韭研公社 24h 题材热度 · {data['generated_at']} ===")
    print(f"抓取 {data['total_fetched']} 条 / 窗口内 {data['in_window']} 条 / 无时间 {data['no_time']} 条")
    print(f"源: {data['mirror']}\n")
    for i, t in enumerate(data["top"], 1):
        print(f"{i}. {t['theme']}  上升分={t['rise']}  近6h={t['hot']} 前段={t['base']} 合计={t['total']}")
        for d in t["discussion"][:3]:
            print(f"     - [{d['time']}] {d['title'][:58]}")

    if data.get("deep_dive"):
        print("\n=== 全渠道深挖结果 ===")
        for r in data["deep_dive"]:
            print(f"  [{r['rank']}] {r['theme']} → 「{r['keyword']}」 命中 {r.get('hits_total', len(r['hits']))} 条")
            for h in r["hits"][:3]:
                print(f"       · [{h['source']}] {h['title'][:56]}")

    if a.json:
        p = Path(a.json); p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n[JSON] {p}")
    rp = Path(a.report) if a.report else ROOT / f"韭研题材热度-{datetime.now(CST).strftime('%Y%m%d_%H%M')}.html"
    write_report(rp, data)
    print(f"[报告] {rp}")

    if a.push:
        print("\n[推送] 开始推送...")
        res = push_all(data, html_path=str(rp))
        for ch, status in res.items():
            print(f"  {ch}: {status}")


if __name__ == "__main__":
    main()
