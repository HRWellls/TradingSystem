#!/usr/bin/env python3
"""Serve the trading system and cache fund NAV history from AKShare."""

import argparse
import json
import math
import re
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "data" / "fund-history"
CACHE_TTL = timedelta(hours=6)
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


def read_cache(code):
    path = CACHE_DIR / f"{code}.json"
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


def number_or_none(value):
    try:
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
        if parsed.path == "/":
            self.path = "/system.html"
        super().do_GET()


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
