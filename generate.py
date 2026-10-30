#!/usr/bin/env python3
"""美元汇率 + 锂电材料价格：每日抓取与页面生成器。

只用 Python 标准库，不需要安装任何依赖，可在 GitHub Actions 的
ubuntu-latest + Python 3.12 上直接跑。

抓取策略（全部免费、公开渠道，绝不编造数字）：
  - 汇率：Frankfurter 免费接口（欧洲央行每日参考汇率），无 key。
  - 碳酸锂期货：新浪财经日线接口，广期所主力连续（品种代码 LC + 0 = LC0）。
    该接口在本机验证环境因域名策略屏蔽未能直接打开，接口形式有多份
    公开资料佐证；生成器将其作为首选尝试，失败则保留上次序列并告警。
  - 磷酸铁锂 / 六氟磷酸锂：生意社公开页面正则抓取（日度基准价行）。
    命中机器人验证页或解析不到时，保留上次值并告警。
  - 负极 / 隔膜：无公开免费日度接口（Mysteel 日度页需登录/JS，
    TrendForce 与 SMM 为周度或付费），策略为保留上次已知值并标明日期。

用法：
  python3 generate.py            # 正常跑：抓取 + 合并 + 生成 index.html
  python3 generate.py --offline  # 不联网：只用 data.json 渲染（本地测试用）

退出码永远为 0（单个源失败只告警、不中断整页生成），
是否提交由工作流根据 git diff 判断。
"""

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data.json"
TEMPLATE_FILE = ROOT / "template.html"
OUTPUT_FILE = ROOT / "index.html"

UA = {"User-Agent": "Mozilla/5.0 (compatible; fx-lithium-autoupdate/1.0)"}
BJ = timezone(timedelta(hours=8))
WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

SOURCES = {
    "fx": "欧洲央行每日参考汇率（经 Frankfurter 转发）",
    "lc": "新浪财经期货行情（广期所主力连续）",
    "lfp": "生意社磷酸铁锂参考价",
    "lipf": "生意社六氟磷酸锂（电池级）基准价",
    "anode": "Mysteel 人造石墨负极材料市场价",
    "separator": "Mysteel 隔膜日度价格",
}


def http_get(url, timeout=30):
    """返回页面/接口文本；按 UTF-8 解码，失败则按 GBK 解码。"""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def merge_series(old, new_points):
    """按日期合并（新值覆盖同日旧值），按日期排序。"""
    merged = {d: v for d, v in old}
    for d, v in new_points:
        merged[d] = v
    return sorted(merged.items())


def trim_window(series, latest, days):
    """只保留 latest 往前 days 个自然日内的点。"""
    cutoff = (date.fromisoformat(latest) - timedelta(days=days)).isoformat()
    return [p for p in series if p[0] >= cutoff]


# ---------------------------------------------------------------- 抓取
def fetch_fx():
    today = date.today()
    start = today - timedelta(days=130)
    url = (
        "https://api.frankfurter.dev/v1/%s..%s?from=USD&to=CNY"
        % (start.isoformat(), today.isoformat())
    )
    data = json.loads(http_get(url))
    rates = data.get("rates", {})
    if not rates:
        raise RuntimeError("Frankfurter 未返回汇率数据")
    return sorted((d, float(v["CNY"])) for d, v in rates.items())


def fetch_lc():
    """新浪日线：返回 [{date, open, high, low, close}]（按日期升序）。"""
    last_err = None
    for url in (
        "https://stock2.finance.sina.com.cn/futures/api/json.php"
        "/IndexService.getInnerFuturesDailyKLine?symbol=LC0",
        "http://stock2.finance.sina.com.cn/futures/api/json.php"
        "/IndexService.getInnerFuturesDailyKLine?symbol=LC0",
    ):
        try:
            rows = json.loads(http_get(url))
            series = []
            for r in rows:
                # 行格式：[日期, 开, 高, 低, 收, 量]
                series.append(
                    {
                        "date": str(r[0])[:10],
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                    }
                )
            series.sort(key=lambda x: x["date"])
            if series:
                return series
        except Exception as exc:  # noqa: BLE001 - 换下一个地址重试
            last_err = exc
    raise RuntimeError("新浪 LC0 日线抓取失败：%s" % last_err)


def _parse_100ppi_dates(text, patterns, today):
    """从生意社页面文本里解析 (月, 日, 价) -> [(日期, 价)]。"""
    found = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            month, day = int(m.group(1)), int(m.group(2))
            val = float(m.group(3).replace(",", ""))
            year = today.year
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            if d > today:  # 跨年：月份大于当前月时算上一年
                d = date(year - 1, month, day)
            found.append((d.isoformat(), val))
    return found


def fetch_100ppi(url, patterns):
    """生意社页面尽力抓取；命中验证页或无匹配时返回 None（保留上次值）。"""
    text = http_get(url)
    if "安全检查" in text and "参考价" not in text and "基准价" not in text:
        return None
    pts = _parse_100ppi_dates(text, patterns, date.today())
    return pts or None


LFP_PATTERNS = [
    r"(\d{1,2})月(\d{1,2})日，磷酸铁锂参考价为([\d,]+(?:\.\d+)?)",
    r"(\d{1,2})月(\d{1,2})日生意社磷酸铁锂基准价为([\d,]+(?:\.\d+)?)元/吨",
]
LIPF_PATTERNS = [
    r"(\d{1,2})月(\d{1,2})日生意社六氟磷酸锂\(电池级\)基准价为([\d,]+(?:\.\d+)?)元/吨",
    r"(\d{1,2})月(\d{1,2})日，六氟磷酸锂参考价为([\d,]+(?:\.\d+)?)",
]

LFP_URL = "https://lfp.100ppi.com/"
LIPF_URL = "https://www.100ppi.com/news/list--1217-1.html"


# ---------------------------------------------------------------- 格式化
def fmt_int(x):
    return "{:,.0f}".format(round(x))


def fmt_signed(x, decimals=0):
    sign = "+" if x > 0 else ("−" if x < 0 else "")
    if decimals:
        return "%s%.*f" % (sign, decimals, abs(x))
    return "%s%s" % (sign, fmt_int(abs(x)))


def pct_str(pct):
    if pct is None:
        return "—"
    sign = "+" if pct > 0 else ("−" if pct < 0 else "")
    return "%s%.2f%%" % (sign, abs(pct))


def chip_for_pct(pct):
    """涨跌 chip：涨红、跌绿、平灰；与上线版配色约定一致。"""
    if pct is None:
        return '<span class="chip flat">— 暂无对比</span>'
    if pct > 0.005:
        return '<span class="chip up">▲ %s</span>' % pct_str(pct)
    if pct < -0.005:
        return '<span class="chip down">▼ %s</span>' % pct_str(pct)
    return '<span class="chip flat">— 持平</span>'


def md(d):
    p = d.split("-")
    return "%s-%s" % (p[1], p[2])


def zh_date(d):
    p = d.split("-")
    return "%s年%s月%s日" % (p[0], int(p[1]), int(p[2]))


def nearest_before(series, target):
    """series 为 [(日期, 值)]；返回日期 <= target 的最后一个点，没有则返回 None。"""
    prev = None
    for d, v in series:
        if d <= target:
            prev = (d, v)
        else:
            break
    return prev


def change_over(series, latest_date, days):
    """latest_date 往前 days 个自然日附近的累计涨跌幅；序列太短则返回 None。"""
    if len(series) < 2:
        return None
    target = (date.fromisoformat(latest_date) - timedelta(days=days)).isoformat()
    base = nearest_before(series, target)
    if base is None or base[0] == latest_date:
        return None
    last = series[-1][1]
    return (last - base[1]) / base[1] * 100


# ---------------------------------------------------------------- 主流程
def main():
    offline = "--offline" in sys.argv
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    status = {}

    # ---- 汇率 ----
    if not offline:
        try:
            fx_new = fetch_fx()
            data["fx"] = merge_series(data["fx"], fx_new)
            data["fx"] = trim_window(data["fx"], data["fx"][-1][0], 100)
            status["fx"] = "ok (%s)" % data["fx"][-1][0]
        except Exception as exc:  # noqa: BLE001 - 单源失败只告警
            status["fx"] = "FAILED, 保留上次值：%s" % exc
            print("WARN fx:", exc, file=sys.stderr)
    else:
        status["fx"] = "offline, 保留上次值"

    # ---- 碳酸锂期货 ----
    lc_range = data.get("lc_range")
    if not offline:
        try:
            lc_rows = fetch_lc()
            data["lc"] = merge_series(
                data["lc"], [(r["date"], r["close"]) for r in lc_rows]
            )
            data["lc"] = data["lc"][-90:]
            last = lc_rows[-1]
            lc_range = {"date": last["date"], "high": last["high"], "low": last["low"]}
            data["lc_range"] = lc_range
            status["lc"] = "ok (%s)" % last["date"]
        except Exception as exc:  # noqa: BLE001
            status["lc"] = "FAILED, 保留上次值：%s" % exc
            print("WARN lc:", exc, file=sys.stderr)
    else:
        status["lc"] = "offline, 保留上次值"

    # ---- 生意社现货（尽力抓取） ----
    for key, url, patterns, series_key in (
        ("lfp", LFP_URL, LFP_PATTERNS, "lfp"),
        ("lipf", LIPF_URL, LIPF_PATTERNS, "lipf"),
    ):
        if offline:
            status[key] = "offline, 保留上次值"
            continue
        try:
            pts = fetch_100ppi(url, patterns)
            if pts:
                data[series_key] = merge_series(data[series_key], pts)
                data[series_key] = data[series_key][-30:]
                status[key] = "ok (%s)" % data[series_key][-1][0]
            else:
                status[key] = "未解析到新值（可能命中验证页），保留上次值"
        except Exception as exc:  # noqa: BLE001
            status[key] = "FAILED, 保留上次值：%s" % exc
            print("WARN %s:" % key, exc, file=sys.stderr)

    # ---- 负极 / 隔膜：无公开免费日度接口，保留上次已知值 ----
    status["anode"] = "无公开免费日度接口，保留上次值 (%s)" % data["spot"]["anode"]["date"]
    status["separator"] = "无公开免费日度接口，保留上次值 (%s)" % data["spot"]["separator"]["date"]

    # ---- 计算展示值 ----
    fx = data["fx"]
    lc = data["lc"]
    lfp = data["lfp"]
    lipf = data["lipf"]
    fx_last, fx_prev = fx[-1], fx[-2]
    lc_last, lc_prev = lc[-1], lc[-2]
    lfp_last, lfp_prev = lfp[-1], lfp[-2]
    lipf_last, lipf_prev = lipf[-1], lipf[-2]
    fx_chg = (fx_last[1] - fx_prev[1]) / fx_prev[1] * 100
    lc_chg = (lc_last[1] - lc_prev[1]) / lc_prev[1] * 100
    lfp_chg = (lfp_last[1] - lfp_prev[1]) / lfp_prev[1] * 100
    lipf_chg = (lipf_last[1] - lipf_prev[1]) / lipf_prev[1] * 100
    fx_30d = change_over(fx, fx_last[0], 30)
    lfp_2w = change_over(lfp, lfp_last[0], 14)
    lipf_2w = change_over(lipf, lipf_last[0], 14)
    anode = data["spot"]["anode"]
    sep = data["spot"]["separator"]

    now_bj = datetime.now(BJ)
    tokens = {
        "PAGE_DATE": zh_date(now_bj.strftime("%Y-%m-%d")),
        "WEEKDAY": WEEKDAYS[now_bj.weekday()],
        "SNAPSHOT_LINE": "数据更新 · 抓取于 %s月%s日 %s（北京时间）"
        % (now_bj.month, now_bj.day, now_bj.strftime("%H:%M")),
        "LEDE": (
            "美元兑人民币汇率、碳酸锂期货，再到正极、负极、隔膜、电解液四条材料线——"
            "本页由 GitHub Actions 每天自动抓取、经 Netlify 自动发布，收齐最新一个交易日的价格，"
            "看往哪边走、走了多少。最新数据：美元兑人民币 <b>%s</b>（%s，欧洲央行参考价），"
            "碳酸锂期货主力收盘 <b>%s 元/吨</b>（%s收盘，单日%s）；正极磷酸铁锂 <b>%s 元/吨</b>"
            "（%s，生意社参考价），电解液锂盐六氟磷酸锂 <b>%s 元/吨</b>（%s，生意社基准价）。"
            % (
                "%.4f" % fx_last[1],
                md(fx_last[0]),
                fmt_int(lc_last[1]),
                md(lc_last[0]),
                pct_str(lc_chg),
                fmt_int(lfp_last[1]),
                md(lfp_last[0]),
                fmt_int(lipf_last[1]),
                md(lipf_last[0]),
            )
        ),
        # KPI
        "KPI_FX_DATE": md(fx_last[0]),
        "KPI_FX_VALUE": "%.4f" % fx_last[1],
        "KPI_FX_SUB": "%s&nbsp;较前日 %s，人民币%s"
        % (
            chip_for_pct(fx_chg),
            fmt_signed(fx_last[1] - fx_prev[1], 4),
            "小幅升值" if fx_chg < 0 else ("小幅贬值" if fx_chg > 0 else "持平"),
        ),
        "KPI_LC_DATE": md(lc_last[0]) + " 收盘",
        "KPI_LC_VALUE": fmt_int(lc_last[1]),
        "KPI_LC_SUB": "%s&nbsp;单日 %s 元/吨"
        % (chip_for_pct(lc_chg), fmt_signed(lc_last[1] - lc_prev[1])),
        "KPI_LFP_DATE": md(lfp_last[0]),
        "KPI_LFP_VALUE": fmt_int(lfp_last[1]),
        "KPI_LIPF_DATE": md(lipf_last[0]),
        "KPI_LIPF_VALUE": fmt_int(lipf_last[1]),
        "KPI_LFP_SUB": "%s&nbsp;近两周累计 %s"
        % (chip_for_pct(lfp_2w), pct_str(lfp_2w) if lfp_2w is not None else "暂无连续序列"),
        "KPI_LIPF_SUB": "%s&nbsp;近两周累计 %s"
        % (chip_for_pct(lipf_2w), pct_str(lipf_2w) if lipf_2w is not None else "暂无连续序列"),
        # 大图
        "FX_BIG_VALUE": "%.4f" % fx_last[1],
        "FX_BIG_DATE": zh_date(fx_last[0])[5:],
        "FX_BIG_META": "%s\n                <span>前值 %.4f</span>\n                <span>近30日 %s（%s）</span>"
        % (
            chip_for_pct(fx_chg),
            fx_prev[1],
            pct_str(fx_30d) if fx_30d is not None else "—",
            "人民币升值" if (fx_30d or 0) < 0 else "人民币贬值",
        ),
        "LC_BIG_VALUE": fmt_int(lc_last[1]),
        "LC_BIG_DATE": zh_date(lc_last[0])[5:],
        "LC_BIG_META": "%s\n                <span>昨收 %s</span>\n                <span>今日区间 %s</span>"
        % (
            chip_for_pct(lc_chg),
            fmt_int(lc_prev[1]),
            ("%s – %s" % (fmt_int(lc_range["low"]), fmt_int(lc_range["high"])))
            if lc_range and lc_range.get("date") == lc_last[0]
            else "暂无当日区间（源未更新）",
        ),
        # 材料卡
        "LFP_PRICE": fmt_int(lfp_last[1]),
        "LFP_DATE": zh_date(lfp_last[0])[5:],
        "LFP_CHIP": chip_for_pct(lfp_chg),
        "ANODE_PRICE": fmt_int(anode["value"]),
        "ANODE_DATE": zh_date(anode["date"])[5:],
        "ANODE_CHIP": '<span class="chip flat">— 以最新报价为准</span>',
        "SEP_PRICE": "%.2f" % sep["value"],
        "SEP_DATE": zh_date(sep["date"])[5:],
        "SEP_CHIP": '<span class="chip flat">— 以最新报价为准</span>',
        "LIPF_PRICE": fmt_int(lipf_last[1]),
        "LIPF_DATE": zh_date(lipf_last[0])[5:],
        "LIPF_CHIP": chip_for_pct(lipf_chg),
        "NOTE_LFP": "最新报价 <b>%s 元/吨</b>（%s，%s）；较前次发布 %s。分档与口径说明见上线版备注："
        "生意社参考价与 Mysteel 动力型中间价口径不同，看趋势以同一来源为准。"
        % (
            fmt_int(lfp_last[1]),
            zh_date(lfp_last[0]),
            SOURCES["lfp"],
            pct_str(lfp_chg),
        ),
        "NOTE_ANODE": "最新已知报价 <b>%s 元/吨</b>（%s，%s）。该品种无公开免费日度序列，"
        "本页保留上次已知值并标明日期；下表分档价为 10月28日公开值，仅作参考分档，不随每日任务刷新。"
        % (fmt_int(anode["value"]), zh_date(anode["date"]), SOURCES["anode"]),
        "NOTE_SEP": "最新已知报价 <b>%s 元/㎡</b>（%s，%s）。该品种无公开免费日度序列，"
        "本页保留上次已知值并标明日期；下表为 TrendForce 10月22日均价分档，仅作参考，不随每日任务刷新。"
        % ("%.2f" % sep["value"], zh_date(sep["date"]), SOURCES["separator"]),
        "NOTE_LIPF": "最新报价 <b>%s 元/吨</b>（%s，%s）；较前次发布 %s，近两周累计 %s。"
        "成品电解液价格请以 SMM 周度回顾为准，本页锂盐卡只展示锂盐基准价。"
        % (
            fmt_int(lipf_last[1]),
            zh_date(lipf_last[0]),
            SOURCES["lipf"],
            pct_str(lipf_chg),
            pct_str(lipf_2w) if lipf_2w is not None else "暂无连续序列",
        ),
        "CMP_LFP_2W": pct_str(lfp_2w) if lfp_2w is not None else "—",
        "CMP_LIPF_2W": pct_str(lipf_2w) if lipf_2w is not None else "—",
        "FOOTER_LINE": "本页由 GitHub Actions 每日自动更新：汇率截至 %s（欧洲央行），碳酸锂期货截至 %s收盘，"
        "磷酸铁锂截至 %s、六氟磷酸锂截至 %s（生意社），负极、隔膜为最新已知报价（%s、%s）。"
        "四大材料的完整日度现货序列主要在 SMM、上海钢联等付费库内，本页用公开报价呈现；"
        "个别品种日度波动极小、以最新报价为准。"
        % (
            zh_date(fx_last[0]),
            zh_date(lc_last[0]),
            zh_date(lfp_last[0]),
            zh_date(lipf_last[0]),
            zh_date(anode["date"]),
            zh_date(sep["date"]),
        ),
        "FX_JSON": json.dumps(fx, ensure_ascii=False),
        "LC_JSON": json.dumps(lc, ensure_ascii=False),
        "LFP_JSON": json.dumps(lfp, ensure_ascii=False),
        "LIPF_JSON": json.dumps(lipf, ensure_ascii=False),
    }

    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    rendered = template
    for key, val in tokens.items():
        rendered = rendered.replace("{{%s}}" % key, val)
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", rendered)
    if leftover:
        raise RuntimeError("模板占位符未替换完：%s" % sorted(set(leftover)))

    changed = True
    if OUTPUT_FILE.exists() and OUTPUT_FILE.read_text(encoding="utf-8") == rendered:
        changed = False
    OUTPUT_FILE.write_text(rendered, encoding="utf-8")

    old_data = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else None
    if old_data != data:
        DATA_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        data_changed = True
    else:
        data_changed = False

    print("抓取状态：")
    for k, v in status.items():
        print("  %s: %s" % (k, v))
    print(
        "最新：汇率 %s（%s），期货 %s（%s），磷酸铁锂 %s（%s），六氟磷酸锂 %s（%s）"
        % (
            "%.4f" % fx_last[1],
            fx_last[0],
            fmt_int(lc_last[1]),
            lc_last[0],
            fmt_int(lfp_last[1]),
            lfp_last[0],
            fmt_int(lipf_last[1]),
            lipf_last[0],
        )
    )
    print("页面已生成：%s（内容变化：%s；数据变化：%s）" % (OUTPUT_FILE, changed, data_changed))


if __name__ == "__main__":
    main()
