# coding=utf-8
"""
多源题材热点探测器 (multi_source_hot.py)

把「热点发现」从韭研单源扩展为六路并行。发现层不预设题材白名单：先保留全部
原始热点，再从标题/桥接字段动态归纳上升关键词；题材词典只做同义扩展线索，
不参与过滤。近6h vs 前段频次用于排序，输出与 jiuyan_hot_theme.analyze() 兼容。

六路来源（每路独立容错，单路失败不影响其他）：
  1. 韭研公社        RSSHub RSS（多镜像）
  2. 华尔街见闻      NewsNow wallstreetcn-hot
  3. 现有渠道归纳    财联社/头条/微博/知乎/雪球（NewsNow 热榜聚合）
  4. 东方财富        人气榜 API（股票代码 → 行情名 → 题材词典归类）
  5. 新华财经        MCP 桥接文件 config/xinhua_hot.json（WorkBuddy 任务调 MCP 写入并推送）
  6. 通达信涨停      MCP 桥接文件 config/tdx_hot.json（tdx_screener 带题材标签，WorkBuddy 写入）

关于淘股吧：其榜单为 JS 异步加载、无稳定公开 API、RSSHub 无对应路由，
无法可靠抓取。已用「雪球热榜(散户讨论维度)」并入第3路等效替代，不造假接空源。

关于新华社：新华财经资讯 MCP 只能在 WorkBuddy 侧调用，GitHub Actions 云端脚本
无法直接调用 MCP。故采用桥接：WorkBuddy 本地任务调 MCP 拿当日热点 → 写入
config/xinhua_hot.json → push 到仓库；本探测器读该 JSON 并入聚类。文件缺失时自动跳过。

纯标准库，零第三方依赖。
"""

import concurrent.futures
import json
import re
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CST = timezone(timedelta(hours=8))
NEWSNOW = "https://newsnow.busiyi.world/api/s?id={id}&latest"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

XINHUA_JSON = ROOT / "config" / "xinhua_hot.json"
TDX_JSON = ROOT / "config" / "tdx_hot.json"

MIRRORS = [
    "https://rsshub.rssforever.com/jiuyangongshe/community",
    "https://rsshub.app/jiuyangongshe/community",
    "https://rsshub.privatered.site/jiuyangongshe/community",
]

# 现有渠道归纳路：财联社(快讯权威) + 头条/微博(大众热度) + 知乎(讨论) + 雪球(散户股吧, 替代淘股吧维度)
AGGREGATE_IDS = ["cls-hot", "toutiao", "weibo", "zhihu", "xueqiu"]
AGGREGATE_NAMES = {"cls-hot": "财联社", "toutiao": "今日头条", "weibo": "微博",
                   "zhihu": "知乎", "xueqiu": "雪球"}

# 题材词典：与主脚本保持一致（导入不到时内嵌兜底）
try:
    import jiuyan_hot_theme as _base
    THEME_DICT = _base.THEME_DICT
    HOT_HOURS = _base.HOT_HOURS
    WINDOW_HOURS = _base.WINDOW_HOURS
except Exception:
    HOT_HOURS, WINDOW_HOURS = 6, 24
    THEME_DICT = {
        "算力/AI芯片": ["算力", "英伟达", "GPU", "AI芯片", "昇腾", "国产算力", "服务器", "智算", "数据中心", "光模块", "CPO", "HBM", "寒武纪"],
        "半导体/国产替代": ["半导体", "芯片", "光刻", "晶圆", "存储", "封测", "国产替代", "先进制程", "靶材", "光刻胶", "第三代半导体", "碳化硅"],
        "机器人/具身智能": ["机器人", "人形机器人", "具身智能", "宇树", "智元", "减速器", "丝杠", "灵巧手"],
        "消费电子/端侧AI": ["消费电子", "端侧AI", "AI手机", "AI眼镜", "可穿戴", "折叠屏", "果链", "苹果", "华为"],
        "新能源/储能": ["光伏", "储能", "锂电", "固态电池", "钠电池", "风电", "逆变器", "特高压", "电网"],
        "汽车/自动驾驶": ["自动驾驶", "智驾", "智能汽车", "比亚迪", "特斯拉", "FSD", "无人驾驶"],
        "医药/创新药": ["创新药", "医药", "CXO", "减肥药", "GLP", "生物医药", "疫苗", "中药"],
        "军工/航天": ["军工", "航天", "卫星", "商业航天", "火箭", "低空经济", "eVTOL", "无人机"],
        "数据要素/信创": ["数据要素", "信创", "国产软件", "操作系统", "数据库", "国资云"],
        "金融/券商": ["券商", "银行", "保险", "牛市", "A股", "指数", "IPO", "重组", "并购"],
        "地产/基建": ["地产", "房地产", "基建", "城中村", "建材", "水泥"],
        "资源/周期": ["黄金", "铜", "稀土", "有色", "煤炭", "石油", "化工", "磷", "氟"],
        "政策/宏观": ["政策", "国常会", "央行", "降息", "降准", "财政", "关税", "证监会", "发改委", "制裁"],
    }


def http_get(url, timeout=20, headers=None, data=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.read()


def classify(title):
    """仅用于诊断：看标题命中了哪些既有题材词典标签，不再作为发现层过滤条件。"""
    t = str(title).lower()
    return [th for th, kws in THEME_DICT.items()
            if any(str(k).lower() in t for k in kws)]


def _split_terms(text):
    return [s.strip() for s in re.split(r"[/／(（·、,，\s]+", str(text or "")) if s.strip()]


# 题材词典里的词只作为「已知说法/同义线索」，不是白名单。
KNOWN_KEYWORDS = set()
for _theme_name, _keywords in THEME_DICT.items():
    KNOWN_KEYWORDS.update(_split_terms(_theme_name))
    KNOWN_KEYWORDS.update(str(k) for k in _keywords)
KNOWN_KEYWORDS = {k for k in KNOWN_KEYWORDS if len(k) >= 2}

# 动态归纳时的噪声词。注意：AI/GPU/CPO/HBM/GLP/FSD/IPO/BIS/eVTOL 等业务词不在此列。
DISCOVERY_STOPWORDS = {
    "今日", "最新", "新闻", "快讯", "热榜", "热搜", "榜单", "排行", "排名", "人气榜",
    "股吧", "涨停", "涨停板", "涨跌", "上涨", "下跌", "涨幅", "跌幅", "成交", "成交额",
    "个股", "股票", "公司", "市场", "题材", "概念", "分析", "解读", "关注", "讨论",
    "社区", "公社", "财经", "华尔街见闻", "财联社", "微博", "知乎", "雪球", "新华财经",
    "通达信", "东方财富", "韭研公社", "推送", "报告", "汇总", "观察", "复盘", "投资",
    "建议", "风险", "提示", "来源", "时间", "标题", "链接", "第", "名", "条", "较上次",
    "气榜", "人榜", "科技", "股份", "集团", "控股", "实业", "有限",
    "如何", "怎么", "怎样", "为何", "为什么", "哪些", "是否", "看待", "评价",
}
DISCOVERY_SUBSTOPS = (
    "人气", "榜第", "股吧", "涨停", "涨幅", "跌幅", "成交", "最新", "今日", "新闻",
    "快讯", "热榜", "热搜", "排行", "排名", "财经", "公社", "社区", "微博", "知乎",
    "雪球", "东方", "财富", "华尔街", "见闻", "财联社", "新华", "通达", "复盘",
    "报告", "推送", "关注", "解读", "建议", "风险", "市场", "公司", "非投资",
    "如何", "什么", "怎么", "怎样", "为何", "哪些", "是否", "看待", "评价",
)
LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,}")
CN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def extract_candidates(title, theme_hint=""):
    """从单条热点动态归纳候选关键词：已知说法 + 英文词 + 中文 2-6 gram。"""
    text = str(title or "")
    low = text.lower()
    cands = set()

    # 1) 已知说法命中：只增加候选，不决定是否收录该热点
    for kw in KNOWN_KEYWORDS:
        if kw.lower() in low:
            cands.add(kw)

    # 2) 英文/缩写词
    for tok in LATIN_RE.findall(text):
        tok = tok.strip("+-.")
        if len(tok) < 2 or tok.lower() in DISCOVERY_STOPWORDS:
            continue
        if tok.isalpha() and len(tok) <= 5:
            tok = tok.upper()
        cands.add(tok)

    # 3) 中文片段：生成 2-6 字候选，再由频次/跨源数决定去留
    for run in CN_RE.findall(text):
        for size in (2, 3, 4, 5, 6):
            for i in range(len(run) - size + 1):
                gram = run[i:i + size]
                if gram in DISCOVERY_STOPWORDS:
                    continue
                if any(bad in gram for bad in DISCOVERY_SUBSTOPS):
                    continue
                cands.add(gram)

    # 4) MCP 桥接文件自带的题材标签，直接作为高置信候选
    for hint in _split_terms(theme_hint):
        if hint not in DISCOVERY_STOPWORDS:
            cands.add(hint)

    # 5) 若同一条里命中了更完整的已知说法，去掉它的碎片。
    # 例：命中「英伟达」时不再保留「伟达/英伟」；命中「机器人」时不再保留「机器/器人」。
    strong = {kw for kw in KNOWN_KEYWORDS if kw.lower() in low}
    if strong:
        strong_low = {kw.lower() for kw in strong}
        cands = {c for c in cands
                 if c in strong or not any(c.lower() != s and c.lower() in s for s in strong_low)}
    return cands


def _dedupe_topic_scores(scores):
    """去掉被更长、更强关键词覆盖的短词，避免「机器人/人形机器人」重复占位。"""
    kept = []
    for s in scores:
        name = s["theme"].lower()
        drop = False
        for k in list(kept):
            kname = k["theme"].lower()
            if name and name in kname and k["total"] >= s["total"] * 0.75 and k["hot"] >= s["hot"] * 0.6:
                drop = True
                break
            if kname and kname in name and s["total"] >= k["total"] * 0.85 and s["hot"] >= k["hot"] * 0.6:
                kept.remove(k)
                break
        if not drop:
            kept.append(s)
        if len(kept) >= 20:
            break
    return kept


def discover_topics(items, last_snap=None, top_n=5):
    """不预设题材：从全量热点里归纳近6h上升最快的关键词。"""
    now = datetime.now(CST)
    hot_c, base_c = Counter(), Counter()
    source_c = defaultdict(Counter)
    term_items = defaultdict(list)

    for it in items:
        pub = it.get("pub")
        age_h = (now - pub).total_seconds() / 3600 if pub else None
        bucket = base_c if (pub and age_h > HOT_HOURS) else hot_c
        src = it.get("source", "其他")
        cands = extract_candidates(it.get("title", ""), it.get("theme", ""))
        if not cands:
            continue
        for kw in cands:
            bucket[kw] += 1
            source_c[kw][src] += 1
            if len(term_items[kw]) < 12:
                term_items[kw].append(it)

    all_kws = set(hot_c) | set(base_c)
    all_scores = []
    for kw in all_kws:
        hot, base = hot_c[kw], base_c[kw]
        total = hot + base
        rise = hot / HOT_HOURS - base / (WINDOW_HOURS - HOT_HOURS)
        hist = None
        if last_snap and "counts" in last_snap:
            hist = total - last_snap["counts"].get(kw, 0)
        sources = dict(source_c[kw])
        all_scores.append({
            "theme": kw, "hot": hot, "base": base, "rise": round(rise, 3),
            "total": total, "hist_delta": hist, "sources": sources,
            "source_count": len(sources),
            "rank_key": round(rise + 0.08 * total + 0.15 * len(sources), 3),
            "discussion": [{
                "title": x.get("title", ""), "url": x.get("url", ""),
                "time": x.get("pub").strftime("%m-%d %H:%M") if x.get("pub") else "",
                "source": x.get("source", ""),
            } for x in sorted(term_items[kw],
                              key=lambda x: x.get("pub").timestamp() if x.get("pub") else 0,
                              reverse=True)[:10]],
        })

    # 常规阈值：至少出现 2 次；若热点太分散则放宽到 1 次，但必须是近6h词。
    pool = [s for s in all_scores if s["total"] >= 2]
    if len(pool) < top_n * 2:
        pool = [s for s in all_scores if s["hot"] >= 2] or all_scores
    pool.sort(key=lambda x: (x["rank_key"], x["rise"], x["total"]), reverse=True)
    scores = _dedupe_topic_scores(pool)[:top_n]
    for s in scores:
        s.pop("rank_key", None)
    return scores


# ---- 六路抓取，统一返回 [{title, url, pub, source, rank}] -----------------

def fetch_jiuyan():
    """路1：韭研公社 RSS 多镜像"""
    last = ""
    for url in MIRRORS:
        try:
            raw = http_get(url, timeout=20)
            root = ET.fromstring(raw)
            items = root.findall(".//item")
            if not items:
                raise ValueError("空feed")
            out = []
            for it in items:
                def txt(tag):
                    el = it.find(tag)
                    return el.text.strip() if el is not None and el.text else ""
                pub = None
                try:
                    pub = parsedate_to_datetime(txt("pubDate")) if txt("pubDate") else None
                    if pub and pub.tzinfo is None:
                        pub = pub.replace(tzinfo=timezone.utc)
                    if pub:
                        pub = pub.astimezone(CST)
                except Exception:
                    pub = None
                out.append({"title": txt("title"), "url": txt("link"), "pub": pub,
                            "source": "韭研公社", "rank": 0})
            return out, None
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:50]}"
    return [], f"韭研RSS全镜像失败({last})"


def _newsnow(channel_id, name):
    raw = http_get(NEWSNOW.format(id=urllib.parse.quote(channel_id)), timeout=20)
    d = json.loads(raw.decode("utf-8", "replace"))
    out = []
    for i, it in enumerate(d.get("items", []), 1):
        t = it.get("title")
        if not t or isinstance(t, float):
            continue
        # NewsNow 热榜无时间戳，用抓取时刻标记（视为"当前热"）
        out.append({"title": str(t).strip(), "url": it.get("url", ""),
                    "pub": datetime.now(CST), "source": name, "rank": i})
    return out


def fetch_wallstreetcn():
    """路2：华尔街见闻（NewsNow 热榜）"""
    try:
        return _newsnow("wallstreetcn-hot", "华尔街见闻"), None
    except Exception as e:
        return [], f"华尔街见闻: {type(e).__name__}: {str(e)[:50]}"


def fetch_aggregate():
    """路3：现有渠道归纳（财联社/头条/微博/知乎/雪球）"""
    out, errs = [], []
    for cid in AGGREGATE_IDS:
        try:
            items = _newsnow(cid, AGGREGATE_NAMES[cid])
            out.extend(items)
        except Exception as e:
            errs.append(f"{AGGREGATE_NAMES[cid]}:{type(e).__name__}")
    return out, ("渠道归纳部分失败:" + ",".join(errs) if errs else None)


def _stock_names(codes):
    """东财人气榜股票代码 -> 名称（push2 行情接口）"""
    if not codes:
        return {}
    secids = ",".join(codes[:50])
    url = f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids={secids}&fields=f12,f14"
    try:
        d = json.loads(http_get(url, timeout=20).decode("utf-8", "replace"))
        diff = (d.get("data") or {}).get("diff") or []
        return {x.get("f12"): x.get("f14") for x in diff if x.get("f12")}
    except Exception:
        return {}


def fetch_eastmoney():
    """路4：东方财富人气榜（实时个股关注度）。个股名再经题材词典归类。"""
    try:
        body = json.dumps({"appId": "appId01",
                           "globalId": "786e4c21-70dc-435a-93bb-38",
                           "marketType": "", "pageNo": 1, "pageSize": 50}).encode()
        raw = http_get("https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
                       timeout=20, headers={"Content-Type": "application/json"}, data=body)
        d = json.loads(raw.decode("utf-8", "replace"))
        rows = d.get("data") or []
        codes = []
        rank_map = {}
        for x in rows:
            sc = x.get("sc", "")  # 如 SZ002242
            if len(sc) == 8:
                mkt, code = sc[:2], sc[2:]
                secid = f"{1 if mkt == 'SH' else 0}.{code}"
                codes.append(secid)
                rank_map[code] = x.get("rk", 0)
        names = _stock_names(codes)
        out = []
        for code, name in names.items():
            if not name:
                continue
            out.append({"title": f"{name} 人气榜第{rank_map.get(code, '?')}名",
                        "url": f"https://guba.eastmoney.com/list,{code}.html",
                        "pub": datetime.now(CST), "source": "东方财富人气榜",
                        "rank": rank_map.get(code, 0), "stock": name})
        return out, None
    except Exception as e:
        return [], f"东财人气榜: {type(e).__name__}: {str(e)[:50]}"


def _fetch_bridge_json(path, label):
    """通用：读 MCP 桥接 JSON 文件（新华/通达信共用）。"""
    if not path.exists():
        return [], None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        out = []
        for x in d.get("items", []):
            t = x.get("title", "")
            if not t:
                continue
            pub = None
            ps = x.get("pub", "")
            if ps:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        pub = datetime.strptime(ps, fmt).replace(tzinfo=CST)
                        break
                    except ValueError:
                        continue
            out.append({"title": t, "url": x.get("url", ""), "pub": pub,
                        "source": x.get("source", label), "rank": x.get("rank", 0)})
        return out, None
    except Exception as e:
        return [], f"{label}桥接文件: {type(e).__name__}: {str(e)[:50]}"


def fetch_xinhua():
    """路5：新华财经热点（读 MCP 桥接文件 config/xinhua_hot.json）。"""
    return _fetch_bridge_json(XINHUA_JSON, "新华财经")


def fetch_tdx():
    """路6：通达信涨停题材（读 MCP 桥接文件 config/tdx_hot.json）。"""
    return _fetch_bridge_json(TDX_JSON, "通达信涨停")


def analyze_multi(last_snap=None):
    """六路并行抓取 + 统一聚类算上升分，返回与主脚本 analyze() 兼容的结构。"""
    now = datetime.now(CST)
    fetchers = [("韭研公社", fetch_jiuyan), ("华尔街见闻", fetch_wallstreetcn),
                ("渠道归纳", fetch_aggregate), ("东方财富", fetch_eastmoney),
                ("新华财经", fetch_xinhua), ("通达信", fetch_tdx)]
    all_items, source_stat, errors = [], {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fn): name for name, fn in fetchers}
        for fut in concurrent.futures.as_completed(futs):
            name = futs[fut]
            try:
                items, err = fut.result()
            except Exception as e:
                items, err = [], f"{type(e).__name__}: {str(e)[:50]}"
            all_items.extend(items)
            source_stat[name] = len(items)
            if err:
                errors.append(err)

    # 发现层：先保留全部原始热点，再动态归纳关键词；不按题材词典过滤。
    scores = discover_topics(all_items, last_snap=last_snap, top_n=5)

    # 按来源分组的原始热点：保留全部条目（含未进入关键词榜的），供报告按来源分栏展示。
    # 排序键不是抓取顺序：榜单类按平台热度名次，资讯类按命中关键词的上升分。
    kw_rise = {s["theme"]: s["rise"] for s in scores}

    def heat_of(it):
        t = str(it.get("title", "")).lower()
        return max((r for kw, r in kw_rise.items() if kw.lower() in t), default=0.0)

    by_source = defaultdict(list)
    for it in all_items:
        src = it.get("source", "其他")
        pub = it.get("pub")
        by_source[src].append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "time": pub.strftime("%m-%d %H:%M") if pub else "",
            "rank": it.get("rank") or 0,
            "_heat": heat_of(it),
            "_ts": pub.timestamp() if pub else 0,
        })
    by_source_out = {}
    for src, rows in by_source.items():
        if any(r["rank"] > 0 for r in rows):
            # 榜单类：rank 越小越热；无名次沉底；同名次再按命中词上升分、时间。
            rows.sort(key=lambda r: (r["rank"] or 10**9, -r["_heat"], -r["_ts"]))
        else:
            # 资讯类：按命中关键词上升分降序，同分按发布时间倒序。
            rows.sort(key=lambda r: (-r["_heat"], -r["_ts"]))
        for r in rows:
            r.pop("_ts", None)
            r.pop("_heat", None)
        by_source_out[src] = rows[:8]

    snap = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "sources": source_stat,
            "total_items": len(all_items),
            "counts": {t["theme"]: t["total"] for t in scores}}
    return {"generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "mirror": "多源: " + "+".join(f"{k}{v}" for k, v in source_stat.items()),
            "total_fetched": len(all_items),
            "in_window": sum(1 for x in all_items if x.get("pub")),
            "no_time": 0, "source_stat": source_stat, "errors": errors,
            "themes": scores, "top": scores[:5], "by_source": by_source_out,
            "_snapshot": snap}


if __name__ == "__main__":
    import sys
    snap = None
    try:
        import jiuyan_hot_theme as j
        snap = j.load_last_snapshot()
    except Exception:
        pass
    d = analyze_multi(snap)
    print(f"=== 多源热点关键词归纳 · {d['generated_at']} ===")
    print("各源条数:", d["source_stat"])
    if d["errors"]:
        print("异常:", "; ".join(d["errors"]))
    for i, t in enumerate(d["top"], 1):
        print(f"{i}. {t['theme']} 上升分={t['rise']} 近6h={t['hot']} 合计={t['total']} 来源={t['sources']}")
        for x in t["discussion"][:3]:
            print(f"    - [{x['source']}] {x['title'][:56]}")
