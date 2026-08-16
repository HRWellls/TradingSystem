#!/usr/bin/env python3
"""Serve the trading system and cache fund NAV history from AKShare."""

import argparse
import json
import math
import re
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "data" / "fund-history"
HOLDINGS_CACHE_DIR = ROOT / "data" / "fund-holdings"
DISCOVERY_CACHE_DIR = ROOT / "data" / "fund-discovery"
CACHE_TTL = timedelta(hours=6)
HOLDINGS_CACHE_TTL = timedelta(days=1)
DISCOVERY_CACHE_TTL = timedelta(days=30)
DISCOVERY_CACHE_VERSION = 2
SCORE_CACHE_DIR = ROOT / "data" / "stock-score"
SCORE_CACHE_TTL = timedelta(hours=12)
EASTMONEY_HOST = "fundf10.eastmoney.com"
EASTMONEY_API_HOST = "api.fund.eastmoney.com"
EASTMONEY_PDF_HOST = "pdf.dfcfw.com"
FUND_CODES = {
    "003629",
    "019547",
    "017641",
    "007280",
    "006282",
    "005613",
    "008163",
    "022436",
    # 债券基金
    "006932",  # 平安0-3年期政策性金融债债券A
    "008558",  # 永赢邦利债券A
    "002811",  # 博时裕顺纯债A
    "003376",  # 广发中债7-10年国开债A
    "017837",  # 博时中债7-10年政策性金融债指数A
    "020387",  # 兴业稳福120天A
    "022807",  # 创金合信恒睿90天A
    "051736",  # 长盛盛裕纯债D
    "006184",  # 格林泓鑫纯债A
    "007520",  # 富安达富利纯债A
}
RETIREMENT_STOCKS = {
    "600900": {"name": "长江电力", "targetYield": 5.0},
    "600377": {"name": "宁沪高速", "targetYield": 5.0},
    "600036": {"name": "招商银行", "targetYield": 7.0},
    "600795": {"name": "国电电力", "targetYield": 7.0},
    "600887": {"name": "伊利股份", "targetYield": 7.0},
    "601919": {"name": "中远海控", "targetYield": 9.0},
}
BACKTEST_STRATEGIES = {
    "003629": {"base_amount": 6.0, "currency": "USD", "step": 5.0, "max_multiple": 5},
    "017641": {"base_amount": 30.0, "currency": "CNY", "step": 7.5, "max_multiple": 5},
    "019547": {"base_amount": 20.0, "currency": "CNY", "step": 10.0, "max_multiple": 5},
    "005613": {"base_amount": 10.0, "currency": "CNY", "step": 5.0, "max_multiple": 5},
    "007280": {"base_amount": 20.0, "currency": "CNY", "step": 10.0, "max_multiple": 5},
    "006282": {"base_amount": 20.0, "currency": "CNY", "step": 7.5, "max_multiple": 5},
    "008163": {"base_amount": 10.0, "currency": "CNY", "step": 5.0, "max_multiple": 5},
    "022436": {"base_amount": 20.0, "currency": "CNY", "step": 10.0, "max_multiple": 5},
}
BACKTEST_PERIODS = {
    "1m": {"months": 1, "label": "过去一个月"},
    "3m": {"months": 3, "label": "过去三个月"},
    "6m": {"months": 6, "label": "过去六个月"},
    "1y": {"months": 12, "label": "过去一年"},
    "3y": {"months": 36, "label": "过去三年"},
    "5y": {"months": 60, "label": "过去五年"},
    "10y": {"months": 120, "label": "过去十年"},
    "all": {"months": None, "label": "成立以来"},
}


def read_cache(code):
    path = CACHE_DIR / f"{code}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def read_holdings_cache(code):
    path = HOLDINGS_CACHE_DIR / f"{code}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def read_discovery_cache(code):
    path = DISCOVERY_CACHE_DIR / f"{code}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def read_score_cache(code):
    path = SCORE_CACHE_DIR / f"{code}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_score_payload(code, payload):
    SCORE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = SCORE_CACHE_DIR / f"{code}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
    return payload


def score_cache_is_fresh(payload):
    try:
        fetched_at = datetime.fromisoformat(payload["fetchedAt"])
        return datetime.now(timezone.utc) - fetched_at < SCORE_CACHE_TTL
    except (KeyError, TypeError, ValueError):
        return False


def cache_is_fresh(payload):
    try:
        fetched_at = datetime.fromisoformat(payload["fetchedAt"])
        return datetime.now(timezone.utc) - fetched_at < CACHE_TTL
    except (KeyError, TypeError, ValueError):
        return False


def holdings_cache_is_fresh(payload):
    try:
        fetched_at = datetime.fromisoformat(payload["fetchedAt"])
        return datetime.now(timezone.utc) - fetched_at < HOLDINGS_CACHE_TTL
    except (KeyError, TypeError, ValueError):
        return False


def discovery_cache_is_fresh(payload):
    try:
        if payload.get("version") != DISCOVERY_CACHE_VERSION:
            return False
        fetched_at = datetime.fromisoformat(payload["fetchedAt"])
        return datetime.now(timezone.utc) - fetched_at < DISCOVERY_CACHE_TTL
    except (KeyError, TypeError, ValueError):
        return False


def number_or_none(value):
    try:
        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").strip()
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def build_payload(code, source, points):
    points.sort(key=lambda point: point["date"])
    if not points:
        raise ValueError("数据源返回了空的历史净值")
    return {
        "fundCode": code,
        "source": source,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "latestDate": points[-1]["date"],
        "points": points,
    }


def save_payload(code, payload):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{code}.json"
    temporary_path = cache_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary_path.replace(cache_path)
    return payload


def save_holdings_payload(code, payload):
    HOLDINGS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = HOLDINGS_CACHE_DIR / f"{code}.json"
    temporary_path = cache_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary_path.replace(cache_path)
    return payload


def save_discovery_payload(code, payload):
    DISCOVERY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = DISCOVERY_CACHE_DIR / f"{code}.json"
    temporary_path = cache_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary_path.replace(cache_path)
    return payload


def fetch_history_akshare(code):
    import akshare as ak

    frame = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    required = {"净值日期", "单位净值"}
    if frame.empty or not required.issubset(frame.columns):
        raise ValueError("AKShare did not return recognizable NAV history")

    accumulated_frame = ak.fund_open_fund_info_em(symbol=code, indicator="累计净值走势")
    accumulated_by_date = {}
    if not accumulated_frame.empty and {"净值日期", "累计净值"}.issubset(accumulated_frame.columns):
        for _, row in accumulated_frame.iterrows():
            date_value = row["净值日期"]
            date_text = date_value.strftime("%Y-%m-%d") if hasattr(date_value, "strftime") else str(date_value)[:10]
            accumulated_by_date[date_text] = number_or_none(row["累计净值"])

    points = []
    for _, row in frame.iterrows():
        nav = number_or_none(row["单位净值"])
        if nav is None:
            continue
        date_value = row["净值日期"]
        date_text = date_value.strftime("%Y-%m-%d") if hasattr(date_value, "strftime") else str(date_value)[:10]
        points.append({
            "date": date_text,
            "nav": nav,
            "accumulatedNav": accumulated_by_date.get(date_text),
            "dailyReturn": number_or_none(row.get("日增长率")),
        })

    return save_payload(code, build_payload(code, "AKShare / 东方财富", points))


def fetch_history_sina(code):
    """Fallback for networks that cannot resolve fund.eastmoney.com."""
    import requests

    rows = []
    callback = "fundHistoryCallback"
    for page in range(1, 1001):
        response = requests.get(
            "https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/CaihuiFundInfoService.getNav",
            params={"callback": callback, "symbol": code, "datefrom": "", "dateto": "", "page": page},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
        match = re.search(r"\((\{.*\})\)\s*;?\s*$", response.text, re.S)
        if not match:
            raise ValueError("新浪财经返回格式无法识别")
        result = json.loads(match.group(1))
        page_rows = result.get("result", {}).get("data", {}).get("data", [])
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < 20:
            break

    rows_by_date = {}
    for row in rows:
        date_text = str(row.get("fbrq", ""))[:10]
        nav = number_or_none(row.get("jjjz"))
        if date_text and nav is not None:
            rows_by_date[date_text] = {
                "date": date_text,
                "nav": nav,
                "accumulatedNav": number_or_none(row.get("ljjz")),
            }

    points = list(rows_by_date.values())
    points.sort(key=lambda point: point["date"])
    previous_nav = None
    for point in points:
        point["dailyReturn"] = ((point["nav"] / previous_nav) - 1) * 100 if previous_nav else None
        previous_nav = point["nav"]
    return save_payload(code, build_payload(code, "新浪财经", points))


def fetch_history(code):
    errors = []
    for fetcher in (fetch_history_akshare, fetch_history_sina):
        try:
            return fetcher(code)
        except Exception as error:
            errors.append(f"{fetcher.__name__}: {error}")
    raise RuntimeError("；".join(errors))


def normalize_holdings_frame(frame, source):
    required = {"股票代码", "股票名称", "占净值比例"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        raise ValueError("数据源未返回可解析的股票或基金持仓")
    rows = []
    for _, row in frame.iterrows():
        name = str(row.get("股票名称", "")).strip()
        code = str(row.get("股票代码", "")).strip()
        ratio = number_or_none(row.get("占净值比例"))
        if not name or name == "nan" or ratio is None:
            continue
        rows.append({
            "code": code if code != "nan" else "",
            "name": name,
            "ratio": ratio,
            "shares": number_or_none(row.get("持股数")),
            "marketValue": number_or_none(row.get("持仓市值")),
            "category": "股票/基金持仓",
        })
    if not rows:
        raise ValueError("数据源返回了空的可解析持仓")
    return rows


def latest_report_period(periods):
    def sort_key(period):
        match = re.search(r"(20\d{2})年([1-4])季度", str(period))
        return (int(match.group(1)), int(match.group(2))) if match else (-1, -1)

    return max(periods, key=sort_key) if periods else "最新公开报告期"


def resolve_host_over_https(host):
    """Resolve a blocked DNS name without weakening TLS certificate checks."""
    import requests

    for resolver in ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve"):
        try:
            response = requests.get(
                resolver,
                params={"name": host, "type": "A"},
                headers={"Accept": "application/dns-json", "User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            response.raise_for_status()
            addresses = [answer.get("data") for answer in response.json().get("Answer", [])]
            addresses = [address for address in addresses if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", str(address or ""))]
            if addresses:
                return addresses
        except Exception:
            continue
    raise RuntimeError(f"无法通过备用 DNS 解析 {host}")


def request_host(host, path, params=None, referer=None):
    import requests
    import urllib3

    params = params or {}
    headers = {
        "Accept": "*/*",
        "Referer": referer or f"https://{host}/",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    url = f"https://{host}{path}"
    try:
        response = requests.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        return response.content
    except requests.RequestException as original_error:
        last_error = original_error
        query = "&".join(f"{key}={value}" for key, value in params.items())
        request_path = f"{path}?{query}"
        for address in resolve_host_over_https(host):
            try:
                pool = urllib3.HTTPSConnectionPool(
                    address,
                    port=443,
                    assert_hostname=host,
                    server_hostname=host,
                    timeout=20,
                    cert_reqs="CERT_REQUIRED",
                )
                result = pool.request(
                    "GET",
                    request_path,
                    headers={**headers, "Host": host},
                )
                if result.status >= 400:
                    raise RuntimeError(f"HTTP {result.status}")
                return result.data
            except Exception as error:
                last_error = error
        raise RuntimeError(f"{host} 请求失败：{last_error}") from original_error


def request_eastmoney(path, params):
    return request_host(EASTMONEY_HOST, path, params).decode("utf-8", errors="replace")


def fetch_holdings_eastmoney_direct(code):
    import pandas as pd
    from akshare.utils import demjson
    from bs4 import BeautifulSoup

    text = request_eastmoney(
        "/FundArchivesDatas.aspx",
        {"type": "jjcc", "code": code, "topline": "100", "year": "", "month": ""},
    )
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("东方财富持仓返回格式无法识别")
    payload = demjson.decode(text[start : end + 1])
    content = payload.get("content", "") if isinstance(payload, dict) else ""
    if not content:
        raise ValueError("东方财富未披露可解析的股票或基金持仓")

    soup = BeautifulSoup(content, features="lxml")
    labels = [item.get_text(" ", strip=True) for item in soup.find_all(name="h4", attrs={"class": "t"})]
    tables = pd.read_html(StringIO(content), converters={"股票代码": str})
    frames = []
    for index, table in enumerate(tables):
        if not {"股票代码", "股票名称"}.issubset(table.columns):
            continue
        ratio_column = next((column for column in table.columns if "占净值" in str(column)), None)
        if ratio_column is None:
            continue
        table = table.rename(columns={
            ratio_column: "占净值比例",
            next((column for column in table.columns if "持股数" in str(column)), "持股数"): "持股数",
            next((column for column in table.columns if "持仓市值" in str(column)), "持仓市值"): "持仓市值",
        })
        table["占净值比例"] = table["占净值比例"].astype(str).str.replace("%", "", regex=False)
        table["季度"] = labels[index] if index < len(labels) else "最新公开报告期"
        frames.append(table)
    if not frames:
        raise ValueError("东方财富未披露可解析的股票或基金持仓")
    frame = pd.concat(frames, ignore_index=True)
    periods = [str(value).strip() for value in frame["季度"].dropna().unique()]
    report_period = latest_report_period(periods)
    frame = frame[frame["季度"].astype(str).str.strip() == report_period]
    rows = normalize_holdings_frame(frame, "东方财富")
    rows.sort(key=lambda item: item["ratio"], reverse=True)
    report_match = re.search(r"20\d{2}年[1-4]季度", report_period)
    return save_holdings_payload(code, {
        "fundCode": code,
        "source": "东方财富",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "reportPeriod": report_match.group(0) if report_match else report_period,
        "rows": rows[:10],
    })


def fetch_holdings_akshare(code):
    import akshare as ak

    frame = ak.fund_portfolio_hold_em(symbol=code, date="")
    rows = normalize_holdings_frame(frame, "AKShare / 东方财富")
    report_period = "最新公开报告期"
    if "季度" in frame.columns and not frame["季度"].dropna().empty:
        periods = [str(value).strip() for value in frame["季度"].dropna().unique()]
        report_period = latest_report_period(periods)
        frame = frame[frame["季度"].astype(str).str.strip() == report_period]
        rows = normalize_holdings_frame(frame, "AKShare / 东方财富")
    rows.sort(key=lambda item: item["ratio"], reverse=True)
    return save_holdings_payload(code, {
        "fundCode": code,
        "source": "AKShare / 东方财富",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "reportPeriod": report_period,
        "rows": rows[:10],
    })


def fetch_holdings_sina(code):
    import pandas as pd
    import requests

    url = "https://stock.finance.sina.com.cn/fundInfo/view/FundInfo_CGMX.php"
    response = requests.get(url, params={"symbol": code}, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    response.raise_for_status()
    tables = pd.read_html(response.content)
    frame = next((table for table in tables if {"证券代码", "证券简称", "占基金净值比(%)"}.issubset(table.columns)), None)
    if frame is None:
        raise ValueError("新浪财经未披露可解析的股票持仓")
    frame = frame.rename(columns={"证券代码": "股票代码", "证券简称": "股票名称", "占基金净值比(%)": "占净值比例"})
    rows = normalize_holdings_frame(frame, "新浪财经")
    rows.sort(key=lambda item: item["ratio"], reverse=True)
    return save_holdings_payload(code, {
        "fundCode": code,
        "source": "新浪财经",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "reportPeriod": "最新公开报告期",
        "rows": rows[:10],
    })


def extract_fund_overview(code):
    from bs4 import BeautifulSoup

    html = request_eastmoney(f"/jbgk_{code}.html", {})
    text = re.sub(r"\s+", "", BeautifulSoup(html, features="lxml").get_text(" ", strip=True))
    if not text:
        raise ValueError("基金基本概况为空")
    return text


def list_periodic_reports(code):
    raw = request_host(
        EASTMONEY_API_HOST,
        "/f10/JJGG",
        {
            "fundcode": code,
            "pageIndex": "1",
            "pageSize": "1000",
            "type": "3",
            "_": int(datetime.now(timezone.utc).timestamp() * 1000),
        },
        referer=f"https://{EASTMONEY_HOST}/jjgg_{code}_3.html",
    )
    payload = json.loads(raw.decode("utf-8"))
    reports = payload.get("Data")
    if not isinstance(reports, list):
        raise ValueError("定期报告列表格式无法识别")
    return sorted(
        [report for report in reports if report.get("ID") and report.get("TITLE")],
        key=lambda report: str(report.get("PUBLISHDATE", "")),
        reverse=True,
    )


def extract_target_etf_from_report(code, report):
    from io import BytesIO
    from pypdf import PdfReader

    report_id = report["ID"]
    pdf = request_host(
        EASTMONEY_PDF_HOST,
        f"/pdf/H2_{report_id}_1.pdf",
        {},
        referer=f"https://{EASTMONEY_HOST}/jjgg_{code}_3.html",
    )
    if not pdf.startswith(b"%PDF"):
        raise ValueError("定期报告不是可读取的 PDF")
    reader = PdfReader(BytesIO(pdf))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    marker = re.search(r"目标基金基本情况", text)
    if not marker:
        raise ValueError("报告中未找到目标基金基本情况")
    section = text[marker.start() : marker.start() + 5000]
    code_match = re.search(r"基金主代码\s*[:：]?\s*(\d{6})", section)
    name_match = re.search(r"基金名称\s+(.+?)基金主代码", section, re.S)
    if not code_match:
        raise ValueError("报告中未找到目标基金代码")
    target_code = code_match.group(1)
    target_name = re.sub(r"\s+", "", name_match.group(1)) if name_match else ""
    if target_code == code:
        raise ValueError("报告中的目标基金代码与联接基金自身相同")
    target_overview = extract_fund_overview(target_code)
    if "交易型开放式" not in target_overview:
        raise ValueError(f"目标代码 {target_code} 未通过 ETF 类型校验")
    return {
        "isEtfLink": True,
        "underlyingFundCode": target_code,
        "underlyingFundName": target_name or target_code,
        "reportTitle": report.get("TITLE", ""),
        "reportId": report_id,
        "reportDate": report.get("PUBLISHDATEDesc", ""),
    }


def extract_fof_holdings_from_report(code, report):
    from io import BytesIO
    from pypdf import PdfReader

    pdf = request_host(
        EASTMONEY_PDF_HOST,
        f"/pdf/H2_{report['ID']}_1.pdf",
        {},
        referer=f"https://{EASTMONEY_HOST}/jjgg_{code}_3.html",
    )
    if not pdf.startswith(b"%PDF"):
        raise ValueError("定期报告不是可读取的 PDF")
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)
    marker = re.search(r"3\.9\s*报告期末[^\n]*基金投资明细", text)
    if not marker:
        raise ValueError("报告中未找到 FOF 基金投资明细")
    end_candidates = [text.find("3.10", marker.end()), text.find("§4", marker.end())]
    ends = [position for position in end_candidates if position >= 0]
    section = text[marker.start() : min(ends) if ends else len(text)]
    anchors = list(re.finditer(r"(?m)^\s*(\d{1,2})\s*(?=\n|\s+[A-Z])", section))
    rows = []
    categories = "股票型|债券型|混合型|货币型|指数型|商品型|另类投资型"
    for index, anchor in enumerate(anchors):
        segment = section[anchor.end() : anchors[index + 1].start() if index + 1 < len(anchors) else len(section)]
        category_match = re.search(rf"({categories})", segment)
        if not category_match:
            continue
        name = re.sub(r"\s+", " ", segment[: category_match.start()]).strip()
        numbers = re.findall(r"(?<![A-Za-z])\d[\d,]*\.\d+", segment)
        if not name or len(numbers) < 2:
            continue
        rows.append({
            "code": "",
            "name": name,
            "ratio": number_or_none(numbers[-1]),
            "shares": None,
            "marketValue": number_or_none(numbers[-2]),
            "category": f"基金持仓 · {category_match.group(1)}",
        })
    if not rows:
        raise ValueError("FOF 报告中未找到可解析的基金投资行")
    report_period = re.search(r"20\d{2}年第[1-4]季度报告|20\d{2}年年度报告", report["TITLE"])
    return save_holdings_payload(code, {
        "fundCode": code,
        "source": "东方财富 / 基金定期报告",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "reportPeriod": report_period.group(0) if report_period else report.get("PUBLISHDATEDesc", "最新公开报告期"),
        "holdingType": "fof",
        "rows": rows[:10],
    })


def fetch_fof_holdings(code):
    overview = extract_fund_overview(code)
    if "FOF" not in overview:
        raise ValueError("该基金不是 FOF")
    errors = []
    for report in list_periodic_reports(code)[:8]:
        try:
            return extract_fof_holdings_from_report(code, report)
        except Exception as error:
            errors.append(str(error))
    raise RuntimeError("无法从最近定期报告提取 FOF 持仓：" + "；".join(errors[-3:]))


def discover_underlying_etf(code):
    cached = read_discovery_cache(code)
    if cached and discovery_cache_is_fresh(cached):
        return cached if cached.get("isEtfLink") else None

    overview = extract_fund_overview(code)
    if "ETF联接" not in overview and "ETF发起式联接" not in overview:
        payload = save_discovery_payload(code, {
            "fundCode": code,
            "isEtfLink": False,
            "version": DISCOVERY_CACHE_VERSION,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        })
        return None

    reports = list_periodic_reports(code)
    errors = []
    for report in reports[:8]:
        try:
            payload = extract_target_etf_from_report(code, report)
            payload["fundCode"] = code
            payload["version"] = DISCOVERY_CACHE_VERSION
            payload["fetchedAt"] = datetime.now(timezone.utc).isoformat()
            return save_discovery_payload(code, payload)
        except Exception as error:
            errors.append(str(error))
    raise RuntimeError("无法从最近定期报告确认目标 ETF：" + "；".join(errors[-3:]))


def fetch_holdings(code):
    errors = []
    try:
        underlying = discover_underlying_etf(code)
    except Exception as error:
        underlying = None
        errors.append(f"自动识别目标 ETF: {error}")
    if underlying:
        try:
            underlying_code = underlying["underlyingFundCode"]
            underlying_name = underlying["underlyingFundName"]
            payload = fetch_holdings_eastmoney_direct(underlying_code)
            payload.update({
                "fundCode": code,
                "holdingType": "underlying-etf",
                "underlyingFundCode": underlying_code,
                "underlyingFundName": underlying_name,
                "source": f"东方财富 · 底层 ETF {underlying_code}",
            })
            return save_holdings_payload(code, payload)
        except Exception as error:
            errors.append(f"底层 ETF {underlying_code}: {error}")
    try:
        return fetch_fof_holdings(code)
    except Exception as error:
        errors.append(f"FOF 报告解析: {error}")
    for fetcher in (fetch_holdings_eastmoney_direct, fetch_holdings_akshare, fetch_holdings_sina):
        try:
            return fetcher(code)
        except Exception as error:
            errors.append(f"{fetcher.__name__}: {error}")
    raise RuntimeError("；".join(errors))


def get_holdings(code, force_refresh=False):
    cached = read_holdings_cache(code)
    if cached and not force_refresh and holdings_cache_is_fresh(cached):
        return cached
    try:
        return fetch_holdings(code)
    except Exception as error:
        if cached:
            cached["warning"] = f"更新失败，正在使用缓存数据：{error}"
            return cached
        raise


def get_history(code, force_refresh=False):
    cached = read_cache(code)
    if cached and not force_refresh and cache_is_fresh(cached):
        return cached
    try:
        return fetch_history(code)
    except Exception as error:
        if cached:
            cached["warning"] = f"更新失败，正在使用缓存数据：{error}"
            return cached
        raise


def months_earlier(value, months):
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    if month == 12:
        next_month = value.replace(year=year + 1, month=1, day=1)
    else:
        next_month = value.replace(year=year, month=month + 1, day=1)
    last_day = (next_month - timedelta(days=1)).day
    return value.replace(year=year, month=month, day=min(value.day, last_day))


def calculate_dca_backtest(code, history, period="1y"):
    strategy = BACKTEST_STRATEGIES.get(code)
    if strategy is None:
        raise ValueError("该基金暂未配置回测程序")
    period_config = BACKTEST_PERIODS.get(period)
    if period_config is None:
        raise ValueError("不支持的回测周期")

    valid_points = []
    for point in history.get("points", []):
        nav = number_or_none(point.get("nav"))
        accumulated_nav = number_or_none(point.get("accumulatedNav"))
        try:
            point_date = datetime.strptime(point.get("date", ""), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if nav is not None and nav > 0:
            signal_nav = accumulated_nav if accumulated_nav is not None and accumulated_nav > 0 else nav
            valid_points.append({
                "date": point_date,
                "dateText": point_date.isoformat(),
                "nav": nav,
                "signalNav": signal_nav,
                "dividendGap": signal_nav - nav if accumulated_nav is not None else None,
            })

    if len(valid_points) < 2:
        raise ValueError("没有足够的历史净值用于回测")
    valid_points.sort(key=lambda point: point["date"])
    months = period_config["months"]
    if months is None:
        points = valid_points
    else:
        cutoff = months_earlier(valid_points[-1]["date"], months)
        points = [point for point in valid_points if point["date"] >= cutoff]
    if len(points) < 2:
        raise ValueError(f"{period_config['label']}没有足够的历史净值用于回测")

    known_peak = points[0]["signalNav"]
    known_drawdown = 0.0
    strategy_shares = 0.0
    strategy_invested = 0.0
    fixed_shares = 0.0
    fixed_invested = 0.0
    highest_multiple = 1
    multiple_days = [0] * (strategy["max_multiple"] + 1)
    known_dividend_gap = points[0]["dividendGap"]
    strategy_dividends_reinvested = 0.0
    fixed_dividends_reinvested = 0.0
    series = []

    for point in points:
        dividend_per_share = 0.0
        dividend_gap = point["dividendGap"]
        if dividend_gap is not None and known_dividend_gap is None:
            known_dividend_gap = dividend_gap
        elif dividend_gap is not None:
            gap_increase = dividend_gap - known_dividend_gap
            if gap_increase > 0.0005:
                dividend_per_share = gap_increase
                known_dividend_gap = dividend_gap
        if dividend_per_share:
            strategy_dividend = strategy_shares * dividend_per_share
            fixed_dividend = fixed_shares * dividend_per_share
            strategy_shares += strategy_dividend / point["nav"]
            fixed_shares += fixed_dividend / point["nav"]
            strategy_dividends_reinvested += strategy_dividend
            fixed_dividends_reinvested += fixed_dividend

        multiple = min(
            strategy["max_multiple"],
            1 + math.floor((max(0.0, -known_drawdown) + 1e-8) / strategy["step"]),
        )
        strategy_amount = strategy["base_amount"] * multiple
        strategy_invested += strategy_amount
        strategy_shares += strategy_amount / point["nav"]
        fixed_invested += strategy["base_amount"]
        fixed_shares += strategy["base_amount"] / point["nav"]
        highest_multiple = max(highest_multiple, multiple)
        multiple_days[multiple] += 1

        strategy_value = strategy_shares * point["nav"]
        fixed_value = fixed_shares * point["nav"]
        series.append({
            "date": point["dateText"],
            "multiple": multiple,
            "signalDrawdown": known_drawdown,
            "strategyInvested": strategy_invested,
            "strategyValue": strategy_value,
            "strategyProfit": strategy_value - strategy_invested,
            "strategyReturn": (strategy_value / strategy_invested - 1) * 100,
            "fixedInvested": fixed_invested,
            "fixedValue": fixed_value,
            "fixedProfit": fixed_value - fixed_invested,
            "dividendPerShare": dividend_per_share,
        })

        known_peak = max(known_peak, point["signalNav"])
        known_drawdown = (point["signalNav"] / known_peak - 1) * 100

    final = series[-1]
    return {
        "fundCode": code,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "period": {
            "key": period,
            "label": period_config["label"],
            "start": points[0]["dateText"],
            "end": points[-1]["dateText"],
            "navDays": len(points),
        },
        "strategy": {
            "baseAmount": strategy["base_amount"],
            "currency": strategy["currency"],
            "step": strategy["step"],
            "maxMultiple": strategy["max_multiple"],
            "signal": "previous-nav-day-drawdown",
            "drawdownBasis": "rolling-period-high-accumulated-nav",
            "dividendMode": "reinvested",
            "feesIncluded": False,
            "topupsIncluded": False,
        },
        "metrics": {
            "strategyInvested": final["strategyInvested"],
            "strategyValue": final["strategyValue"],
            "strategyProfit": final["strategyProfit"],
            "strategyReturn": final["strategyReturn"],
            "fixedProfit": final["fixedProfit"],
            "excessProfit": final["strategyProfit"] - final["fixedProfit"],
            "strategyDividendsReinvested": strategy_dividends_reinvested,
            "fixedDividendsReinvested": fixed_dividends_reinvested,
            "highestMultiple": highest_multiple,
            "multipleDays": multiple_days,
        },
        "series": series,
    }


def run_dca_backtest(code, period="1y"):
    return calculate_dca_backtest(code, get_history(code), period)


def _score_number(value):
    return number_or_none(value)


def _score_date(value):
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _score_rows(frame, date_column="REPORT_DATE"):
    if frame is None or frame.empty or date_column not in frame.columns:
        return []
    rows = []
    for _, row in frame.iterrows():
        date = _score_date(row.get(date_column))
        if date and date.month == 12 and date.day == 31:
            rows.append((date.year, row))
    rows.sort(key=lambda item: item[0], reverse=True)
    return rows


def _score_item(key, label, maximum, score, metrics, formula, status="ok"):
    return {"key": key, "label": label, "max": maximum, "score": score, "metrics": metrics, "formula": formula, "status": status}


def _sina_stock_history(ak, code, start_date, end_date):
    """Fetch unadjusted A-share history from Sina when Eastmoney is unavailable."""
    exchange_code = ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
    return ak.stock_zh_a_daily(symbol=exchange_code, start_date=start_date, end_date=end_date, adjust="")


def _parse_stock_valuation_rows(code, rows):
    own = next((row for row in rows if str(row.get("CORRE_SECURITY_CODE")) == code), None)
    median = next((row for row in rows if "中值" in str(row.get("CORRE_SECURITY_NAME", ""))), None)
    if own is None or median is None:
        raise ValueError("同行估值接口未返回个股或行业中值")

    valuation = {}
    for label, field in (("市盈率-TTM", "PE_TTM"), ("市净率-MRQ", "PB_MRQ"), ("EV/EBITDA-24A", "QYBS")):
        value = _score_number(own.get(field))
        peer = _score_number(median.get(field))
        if value is not None or peer is not None:
            valuation[label] = {"value": value, "peerMedian": peer}
    if not valuation:
        raise ValueError("同行估值接口未返回可用指标")
    return valuation


def _fetch_stock_valuation(code):
    import requests

    exchange = "SH" if code.startswith(("5", "6", "9")) else "SZ"
    response = requests.get(
        "https://datacenter.eastmoney.com/securities/api/data/v1/get",
        params={
            "reportName": "RPT_PCF10_INDUSTRY_CVALUE",
            "columns": "ALL",
            "quoteColumns": "",
            "filter": f'(SECUCODE="{code}.{exchange}")',
            "pageNumber": "",
            "pageSize": "",
            "sortTypes": "1",
            "sortColumns": "PAIMING",
            "source": "HSF10",
            "client": "PC",
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json().get("result") or {}
    return _parse_stock_valuation_rows(code, result.get("data") or [])


def calculate_stock_score(code):
    if code not in RETIREMENT_STOCKS:
        raise ValueError("该标的不在养老高股息列表中")
    import akshare as ak
    config = RETIREMENT_STOCKS[code]
    warnings = []
    sources = []
    today = datetime.now(timezone.utc).date()
    price = None
    dividends = []
    indicator_rows = []
    cash_rows = []
    balance_rows = []
    valuation = {}
    price_points = []

    end = today.strftime("%Y%m%d")
    # Current price and historical percentile are separate concerns. A long
    # Eastmoney request can be rejected even when a short quote request works.
    try:
        recent_start = (today - timedelta(days=45)).strftime("%Y%m%d")
        recent_history = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=recent_start, end_date=end, adjust="")
        if not recent_history.empty:
            price = _score_number(recent_history.iloc[-1].get("收盘"))
        sources.append("ak.stock_zh_a_hist (recent)")
    except Exception as error:
        recent_error = error
    if price is None:
        try:
            recent_history = _sina_stock_history(ak, code, (today - timedelta(days=45)).strftime("%Y%m%d"), end)
            if not recent_history.empty:
                price = _score_number(recent_history.iloc[-1].get("close"))
            if price is not None:
                sources.append("ak.stock_zh_a_daily (Sina fallback)")
        except Exception as error:
            warnings.append(f"新浪当前行情回退失败: {error}")
            if "recent_error" in locals():
                warnings.append(f"当前行情获取失败: {recent_error}")
    try:
        long_start = (today - timedelta(days=365 * 6)).strftime("%Y%m%d")
        long_history = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=long_start, end_date=end, adjust="")
        for _, row in long_history.iterrows():
            date = _score_date(row.get("日期"))
            close = _score_number(row.get("收盘"))
            if date and close and close > 0:
                price_points.append((date, close))
        sources.append("ak.stock_zh_a_hist (history)")
    except Exception as error:
        history_error = error
    if not price_points:
        try:
            long_history = _sina_stock_history(ak, code, (today - timedelta(days=365 * 6)).strftime("%Y%m%d"), end)
            for _, row in long_history.iterrows():
                date = _score_date(row.get("date"))
                close = _score_number(row.get("close"))
                if date and close and close > 0:
                    price_points.append((date, close))
            if price_points:
                sources.append("ak.stock_zh_a_daily (Sina history fallback)")
        except Exception as error:
            warnings.append(f"新浪历史行情回退失败: {error}")
            if "history_error" in locals():
                warnings.append(f"历史行情获取失败，当前股价仍可用: {history_error}")

    try:
        frame = ak.stock_dividend_cninfo(symbol=code)
        for _, row in frame.iterrows():
            ex_date = _score_date(row.get("除权日"))
            dividend = _score_number(row.get("派息比例"))
            if ex_date and dividend is not None and ex_date <= today and dividend > 0:
                dividends.append({"date": ex_date.isoformat(), "year": ex_date.year, "perShare": dividend / 10})
        sources.append("ak.stock_dividend_cninfo")
    except Exception as error:
        warnings.append(f"历史分红获取失败: {error}")

    try:
        frame = ak.stock_financial_analysis_indicator_em(symbol=f"{code}.SH", indicator="按报告期")
        indicator_rows = _score_rows(frame)
        sources.append("ak.stock_financial_analysis_indicator_em")
    except Exception as error:
        warnings.append(f"财务指标获取失败: {error}")

    try:
        frame = ak.stock_cash_flow_sheet_by_yearly_em(symbol=f"SH{code}")
        cash_rows = _score_rows(frame)
        sources.append("ak.stock_cash_flow_sheet_by_yearly_em")
    except Exception as error:
        warnings.append(f"现金流量表获取失败: {error}")

    try:
        frame = ak.stock_balance_sheet_by_yearly_em(symbol=f"SH{code}")
        balance_rows = _score_rows(frame)
        sources.append("ak.stock_balance_sheet_by_yearly_em")
    except Exception as error:
        warnings.append(f"资产负债表获取失败: {error}")

    try:
        valuation = _fetch_stock_valuation(code)
        sources.append("东方财富同行估值")
    except Exception as error:
        warnings.append(f"同行估值获取失败: {error}")

    dividends.sort(key=lambda item: item["date"])
    ttm_dividend = sum(item["perShare"] for item in dividends if (today - datetime.strptime(item["date"], "%Y-%m-%d").date()).days <= 366)
    annual_dividends = {}
    for item in dividends:
        annual_dividends[item["year"]] = annual_dividends.get(item["year"], 0) + item["perShare"]
    latest_years = [year for year in sorted(annual_dividends, reverse=True) if annual_dividends[year] > 0]
    current_yield = ttm_dividend / price * 100 if price and ttm_dividend else None
    historical_yields = []
    for index in range(0, len(price_points), 63):
        point_date, point_price = price_points[index]
        point_dividend = sum(item["perShare"] for item in dividends if 0 <= (point_date - datetime.strptime(item["date"], "%Y-%m-%d").date()).days <= 366)
        if point_dividend > 0:
            historical_yields.append(point_dividend / point_price * 100)
    yield_percentile = None
    if current_yield is not None and len(historical_yields) >= 20:
        yield_percentile = sum(value <= current_yield for value in historical_yields) / len(historical_yields) * 100
    latest_indicators = {year: row for year, row in indicator_rows}
    latest_cash = {year: row for year, row in cash_rows}

    def values(field, years=5):
        return [(year, _score_number(latest_indicators[year].get(field))) for year in sorted(latest_indicators, reverse=True)[:years] if _score_number(latest_indicators[year].get(field)) is not None]

    profits = values("PARENTNETPROFIT")
    roe = values("ROEJQ", 3)
    margins = values("XSMLL", 3)
    cash_values = [(year, _score_number(row.get("NETCASH_OPERATE"))) for year, row in sorted(latest_cash.items(), reverse=True)[:5]]
    cash_values = [(year, value) for year, value in cash_values if value is not None]
    dividends_cash = [(year, _score_number(row.get("ASSIGN_DIVIDEND_PORFIT"))) for year, row in sorted(latest_cash.items(), reverse=True)[:3]]
    dividends_cash = [(year, value) for year, value in dividends_cash if value is not None and value >= 0]

    def cagr_score(series, high, mid, low):
        if len(series) < 2 or series[0][1] <= 0 or series[-1][1] <= 0:
            return 0, "数据不足或起止值非正"
        years = max(1, series[0][0] - series[-1][0])
        rate = ((series[0][1] / series[-1][1]) ** (1 / years) - 1) * 100
        return (high if rate >= 8 else mid if rate >= 3 else low if rate >= 0 else 0), f"CAGR {rate:.2f}%"

    a_score = (10 if current_yield is not None and current_yield >= 8 else 8 if current_yield is not None and current_yield >= 6 else 5 if current_yield is not None and current_yield >= 4 else 2 if current_yield is not None and current_yield >= 3 else 0) + (4 if latest_years else 0)
    percentile_score = 6 if yield_percentile is not None and yield_percentile >= 70 else 4 if yield_percentile is not None and yield_percentile >= 50 else 2 if yield_percentile is not None and yield_percentile >= 30 else 0
    a_score += percentile_score
    b_growth, b_growth_note = cagr_score([(year, annual_dividends[year]) for year in latest_years[:5]], 5, 3, 1)
    continuity = 5 if len(latest_years) >= 10 else 3 if len(latest_years) >= 5 else 0
    b_score = continuity + b_growth + (5 if len(latest_years) >= 1 and all(annual_dividends.get(year, 0) >= annual_dividends.get(year - 1, 0) for year in latest_years[:10] if year - 1 in annual_dividends) else 0)
    c_growth, c_growth_note = cagr_score(profits, 5, 3, 1)
    positive_years = sum(1 for _, value in profits if value > 0)
    c_score = c_growth + (5 if positive_years >= 5 else 3 if positive_years >= 4 else 1 if positive_years >= 2 else 0) + (3 if roe and sum(value for _, value in roe) / len(roe) >= 12 else 2 if roe and sum(value for _, value in roe) / len(roe) >= 8 else 1 if roe and sum(value for _, value in roe) / len(roe) >= 5 else 0) + (2 if len(margins) >= 2 and margins[0][1] >= margins[-1][1] - 3 else 0)
    ratio = sum(value for _, value in cash_values) / sum(value for _, value in profits[:len(cash_values)]) if cash_values and profits and sum(value for _, value in profits[:len(cash_values)]) > 0 else None
    d_score = 6 if ratio is not None and ratio >= 1 else 3 if ratio is not None and ratio >= .8 else 0
    free_cash_flows = []
    for year, row in sorted(latest_cash.items(), reverse=True)[:3]:
        operating = _score_number(row.get("NETCASH_OPERATE"))
        capital_spending = _score_number(row.get("CONSTRUCT_LONG_ASSET"))
        if operating is not None and capital_spending is not None:
            free_cash_flows.append((year, operating - capital_spending))
    d_score += 5 if len(free_cash_flows) >= 3 and all(value > 0 for _, value in free_cash_flows) else 2 if len(free_cash_flows) >= 2 and sum(value > 0 for _, value in free_cash_flows) >= 2 else 0
    payout = dividends_cash[0][1] / profits[0][1] * 100 if dividends_cash and profits and profits[0][1] > 0 else None
    d_score += 5 if payout is not None and 30 <= payout <= 70 else 3 if payout is not None and 20 <= payout <= 90 else 0
    coverage = cash_values[0][1] / dividends_cash[0][1] if cash_values and dividends_cash and dividends_cash[0][1] > 0 else None
    d_score += 4 if coverage is not None and coverage >= 1.2 else 2 if coverage is not None and coverage >= 1 else 0
    e_score = 0
    debt_ratio = _score_number(latest_indicators.get(profits[0][0], {}).get("ZCFZL")) if profits else None
    interest_coverage_values = values("INTEREST_COVERAGE_RATIO", 1)
    interest_coverage = interest_coverage_values[0][1] if interest_coverage_values else None
    e_score += 4 if interest_coverage is not None and interest_coverage >= 5 else 2 if interest_coverage is not None and interest_coverage >= 3 else 0
    latest_balance = balance_rows[0][1] if balance_rows else {}
    cash_balance = _score_number(latest_balance.get("MONETARYFUNDS"))
    short_debt_parts = [_score_number(latest_balance.get(field)) for field in ("SHORT_LOAN", "NONCURRENT_LIAB_1YEAR")]
    short_debt = sum(value for value in short_debt_parts if value is not None)
    cash_short_debt = cash_balance / short_debt if cash_balance is not None and short_debt > 0 else None
    e_score += 4 if cash_short_debt is not None and cash_short_debt >= 1 else 2 if cash_short_debt is not None and cash_short_debt >= .5 else 0
    e_score += 3 if debt_ratio is not None and (not values("ZCFZL", 3) or values("ZCFZL", 3)[0][1] - values("ZCFZL", 3)[-1][1] <= 5) else 0
    comparable = [item for item in valuation.values() if item["value"] is not None and item["peerMedian"] is not None and item["value"] > 0 and item["peerMedian"] > 0]
    f_score = 6 if sum(item["value"] <= item["peerMedian"] for item in comparable) >= 2 else 3 if comparable and any(item["value"] <= item["peerMedian"] for item in comparable) else 0
    f_score += 4 if current_yield is not None and current_yield >= config["targetYield"] else 0
    audit_opinion = str(cash_rows[0][1].get("OPINION_TYPE", "")) if cash_rows else ""
    g_score = 2 if "标准无保留" in audit_opinion else 0
    dimensions = [
        _score_item("A", "当前分红水平", 20, min(20, a_score), {"ttmYield": current_yield, "ttmDividendPerShare": ttm_dividend, "price": price, "yieldPercentile": yield_percentile, "historyPoints": len(historical_yields)}, "股息率分档 + 5 年历史分位 + 最近分红记录", "ok" if current_yield is not None else "insufficient_data"),
        _score_item("B", "分红连续性与增长", 15, min(15, b_score), {"dividendYears": len(latest_years), "annualDividends": annual_dividends}, f"连续年数 + {b_growth_note}", "ok" if latest_years else "insufficient_data"),
        _score_item("C", "盈利质量与稳定性", 15, min(15, c_score), {"profitYears": positive_years, "roe": roe, "margins": margins}, c_growth_note, "ok" if profits else "insufficient_data"),
        _score_item("D", "现金流与分配覆盖", 20, min(20, d_score), {"cashFlowProfitRatio": ratio, "payoutRatio": payout, "cashDividendCoverage": coverage, "freeCashFlows": free_cash_flows}, "经营现金流、自由现金流、支付率和分红覆盖", "ok" if cash_values else "insufficient_data"),
        _score_item("E", "资产负债表", 15, min(15, e_score), {"debtRatio": debt_ratio, "interestCoverage": interest_coverage, "cashShortDebt": cash_short_debt}, "利息覆盖、现金覆盖短债和负债率趋势", "ok" if debt_ratio is not None else "insufficient_data"),
        _score_item("F", "估值与价格纪律", 10, min(10, f_score), {"valuation": valuation, "targetYield": config["targetYield"]}, "同行中值比较 + 目标股息率", "ok" if valuation else "insufficient_data"),
        _score_item("G", "可验证披露与治理信号", 5, g_score, {"auditOpinion": audit_opinion}, "审计意见；质押和商誉字段缺失时不补分", "ok" if audit_opinion else "insufficient_data"),
    ]
    raw_score = sum(item["score"] for item in dimensions)
    effective_max = sum(item["max"] for item in dimensions if item["status"] != "insufficient_data")
    normalized = round(raw_score / effective_max * 100, 1) if effective_max else None
    return save_score_payload(code, {"code": code, "name": config["name"], "targetYield": config["targetYield"], "price": price, "fetchedAt": datetime.now(timezone.utc).isoformat(), "dataAsOf": today.isoformat(), "rawScore": raw_score, "effectiveMax": effective_max, "normalizedScore": normalized, "dimensions": dimensions, "warnings": warnings, "sources": sources, "manualReview": ["特别分红是否应剔除", "重大诉讼、处罚和关联交易", "债务到期集中度", "行业周期与正常化盈利", "竞争优势、管理层和组合适配"]})


def get_stock_score(code, force_refresh=False):
    cached = read_score_cache(code)
    if cached and not force_refresh and cached.get("price") is not None and score_cache_is_fresh(cached):
        return cached
    return calculate_stock_score(code)


class TradingSystemHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/funds/(\d{6})/history", parsed.path)
        if match:
            code = match.group(1)
            if code not in FUND_CODES:
                self.send_json(404, {"error": "该基金未配置"})
                return
            force_refresh = parse_qs(parsed.query).get("refresh") == ["1"]
            try:
                self.send_json(200, get_history(code, force_refresh))
            except Exception as error:
                self.send_json(502, {"error": "历史净值获取失败", "detail": str(error)})
            return
        match = re.fullmatch(r"/api/funds/(\d{6})/holdings", parsed.path)
        if match:
            code = match.group(1)
            force_refresh = parse_qs(parsed.query).get("refresh") == ["1"]
            try:
                self.send_json(200, get_holdings(code, force_refresh))
            except Exception as error:
                self.send_json(502, {"error": "基金持仓获取失败", "detail": str(error)})
            return
        match = re.fullmatch(r"/api/stocks/(\d{6})/score", parsed.path)
        if match:
            code = match.group(1)
            if code not in RETIREMENT_STOCKS:
                self.send_json(404, {"error": "该标的不在养老高股息列表中"})
                return
            force_refresh = parse_qs(parsed.query).get("refresh") == ["1"]
            try:
                self.send_json(200, get_stock_score(code, force_refresh))
            except Exception as error:
                cached = read_score_cache(code)
                if cached:
                    cached["warning"] = str(error)
                    self.send_json(200, cached)
                else:
                    self.send_json(502, {"error": "评分数据获取失败", "detail": str(error)})
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/funds/(\d{6})/backtest", parsed.path)
        if not match:
            self.send_json(404, {"error": "接口不存在"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            self.rfile.read(content_length)
        code = match.group(1)
        if code not in BACKTEST_STRATEGIES:
            self.send_json(404, {"error": "该基金暂未配置回测程序"})
            return
        period = parse_qs(parsed.query).get("period", ["1y"])[0]
        if period not in BACKTEST_PERIODS:
            self.send_json(400, {"error": "不支持的回测周期"})
            return
        try:
            self.send_json(200, run_dca_backtest(code, period))
        except Exception as error:
            self.send_json(502, {"error": "策略回测失败", "detail": str(error)})


def main():
    parser = argparse.ArgumentParser(description="Run the trading system web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), TradingSystemHandler)
    print(f"Trading system available at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
