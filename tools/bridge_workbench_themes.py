#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工作台题材分析师 -> TrendRadar 题材清单 桥接器

读取 ResearchConsole 工作台的题材分析产出（work/analysis-*.json，
schema research-console-synthesis/v1 的 themes 数组），转换成
config/analyst_themes.json，供 jiuyan_hot_theme.py 深挖时合并检索。

设计要点（对应用户方法论的三个真金点）：
- 主源是工作台题材分析师的正式产出（七字段题材矩阵），不臆造角色。
- 括号内的细分（如"靶材·光刻胶·光刻机·第四代半导体"）拆成独立题材词，
  解决"细分/嵌套题材遗漏"。
- 退潮期 / 低动量题材不进正式清单，但写入 watchlist 跨轮复查（不丢弃）。

用法：
  python tools/bridge_workbench_themes.py                    # 自动找最新 analysis
  python tools/bridge_workbench_themes.py --src <json路径>    # 指定源
  python tools/bridge_workbench_themes.py --dry-run          # 只看转换结果不落盘
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKBENCH_WORK = Path("D:/ResearchConsole/work")
OUT = ROOT / "config" / "analyst_themes.json"

# 退潮/低动量关键词：命中则不进正式深挖，进观察清单（跨轮复查，不丢弃）
FADING_STAGE_KW = ("退潮", "退潮确认", "衰退", "结束")
MIN_MOMENTUM = 40  # 动量分低于此值降为观察


def find_latest_analysis():
    """在 ResearchConsole/work 下找最新的 analysis-*.json。"""
    if not WORKBENCH_WORK.exists():
        return None
    cands = sorted(
        WORKBENCH_WORK.glob("analysis-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return cands[0] if cands else None


def split_subthemes(name):
    """从题材名里拆出括号内的细分：'XX（A·B·C）' -> ['A','B','C']。"""
    m = re.search(r"[（(]([^）)]+)[）)]", name)
    if not m:
        return []
    inner = m.group(1)
    parts = re.split(r"[·、/,，]", inner)
    out = []
    for p in parts:
        p = p.strip()
        # 过滤非题材的描述性短语（含空格多、动词、明显解释性文字）
        if not p or len(p) > 16:
            continue
        if re.search(r"(防御|扰动|硬件链|护盘核心|支线|国产替代链|情绪)", p):
            continue
        out.append(p)
    return out


def clean_main(name):
    """取括号前的题材主名。"""
    return re.split(r"[（(]", name, maxsplit=1)[0].strip()


def convert(src_path):
    d = json.load(open(src_path, encoding="utf-8"))
    themes = d.get("themes", [])
    target = d.get("targetTime", "")[:10]
    generated = d.get("generatedAt", "")

    out_themes, watch = [], []
    seen = set()
    for t in sorted(themes, key=lambda x: x.get("rank", 99)):
        name = t.get("name", "")
        stage = t.get("stage", "")
        score = t.get("momentum_score", 0) or 0
        driver = t.get("driver", "")
        fade = t.get("fade_window", "")
        try:
            score_n = float(score)
        except (ValueError, TypeError):
            score_n = 0.0

        fading = any(k in stage for k in FADING_STAGE_KW) or score_n < MIN_MOMENTUM
        main = clean_main(name)
        subs = split_subthemes(name)

        # 细分题材优先（更精准），主名作为补充；每个都带工作台的评分与阶段
        candidates = []
        for s in subs:
            candidates.append((s, f"工作台细分 | {stage} | 动量{score} | {driver[:50]}"))
        candidates.append((main, f"工作台主线#{t.get('rank')} | {stage} | 动量{score} | {driver[:50]}"))

        for kw, reason in candidates:
            key = kw.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            item = {
                "name": key,
                "type": "a_share",
                "reason": reason,
                "source": f"工作台分析师(rank{t.get('rank')})",
                "_stage": stage,
                "_score": score_n,
                "_fade": fade,
                "_target": target,
            }
            if fading:
                item["_watch_reason"] = f"{stage}/动量{score}，暂不进深挖，跨轮复查"
                watch.append(item)
            else:
                out_themes.append(item)

    return {
        "_说明": "题材分析师给出的题材清单。jiuyan_hot_theme.py 深挖时与韭研热点 TOP N 合并去重后全渠道检索。",
        "_维护": "由 bridge_workbench_themes.py 从 ResearchConsole 工作台题材矩阵自动生成，勿手工大改。watchlist 里的题材不参与深挖，仅跨轮复查防漏。",
        "_方法论": "源=工作台七字段题材矩阵；括号内细分拆独立词防嵌套遗漏；退潮/低动量题材进观察清单不丢弃。",
        "_来源文件": str(src_path),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": f"工作台题材分析师 | target={target} | generated={generated}",
        "themes": [
            {k: v for k, v in t.items() if not k.startswith("_")} for t in out_themes
        ],
        "watchlist": [
            {k: v for k, v in t.items() if not k.startswith("_") and k != "_watch_reason"}
            for t in watch
        ],
        "_detail": {
            "themes": out_themes,
            "watchlist": watch,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="", help="工作台 analysis json 路径，默认自动找最新")
    ap.add_argument("--out", default=str(OUT), help="输出 analyst_themes.json 路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    a = ap.parse_args()

    src = Path(a.src) if a.src else find_latest_analysis()
    if not src or not Path(src).exists():
        print(f"[桥接] 未找到工作台分析产出（{WORKBENCH_WORK}/analysis-*.json）", file=sys.stderr)
        sys.exit(2)

    data = convert(src)
    print(f"[桥接] 源: {src}")
    print(f"[桥接] 正式题材 {len(data['themes'])} 个 / 观察 {len(data['watchlist'])} 个")
    for t in data["themes"]:
        print(f"   + {t['name']}")
    for t in data["watchlist"]:
        d = data["_detail"]["watchlist"]
        print(f"   ~ {t['name']} (观察)")

    if a.dry_run:
        print("[桥接] dry-run，未落盘")
        return

    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 落盘时去掉 _detail 辅助字段，保持清单简洁（_watch_reason 并入 watchlist reason）
    clean = dict(data)
    detail = clean.pop("_detail")
    for i, w in enumerate(clean["watchlist"]):
        w["reason"] = detail["watchlist"][i].get("_watch_reason", w.get("reason", ""))
    json.dump(clean, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[桥接] 已写入 {out_path}")


if __name__ == "__main__":
    main()
