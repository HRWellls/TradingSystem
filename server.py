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
EASTMONEY_HOST = "fundf10.eastmoney.com"
EASTMONEY_API_HOST = "api.fund.eastmoney.com"
EASTMONEY_PDF_HOST = "pdf.dfcfw.com"
FUND_CODES = {
    "003629",
    "019547",
    "017641",
    "007280",
    "006282",
    "000369",
    "006308",
    "008163",
    "022436",
}
BACKTEST_STRATEGIES = {
    "003629": {"base_amount": 6.0, "currency": "USD", "step": 5.0, "max_multiple": 5},
    "017641": {"base_amount": 30.0, "currency": "CNY", "step": 7.5, "max_multiple": 5},
    "019547": {"base_amount": 20.0, "currency": "CNY", "step": 10.0, "max_multiple": 5},
    "000369": {"base_amount": 10.0, "currency": "CNY", "step": 7.5, "max_multiple": 5},
    "006308": {"base_amount": 10.0, "currency": "CNY", "step": 10.0, "max_multiple": 5},
    "007280": {"base_amount": 20.0, "currency": "CNY", "step": 10.0, "max_multiple": 5},
    "006282": {"base_amount": 20.0, "currency": "CNY", "step": 7.5, "max_multiple": 5},
    "008163": {"base_amount": 10.0, "currency": "CNY", "step": 5.0, "max_multiple": 5},
    "022436": {"base_amount": 20.0, "currency": "CNY", "step": 10.0, "max_multiple": 5},
}
BACKTEST_PERIODS = {
    "1y": {"years": 1, "label": "过去一年"},
    "3y": {"years": 3, "label": "过去三年"},
    "all": {"years": None, "label": "成立以来"},
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


def years_earlier(value, years):
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


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
    years = period_config["years"]
    if years is None:
        points = valid_points
    else:
        cutoff = years_earlier(valid_points[-1]["date"], years)
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
        if parsed.path == "/":
            self.path = "/system.html"
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
