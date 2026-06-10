#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def _http_get_json(url: str, headers: dict[str, str] | None = None, timeout_s: int = 20):
    req = urllib.request.Request(
        url,
        headers=headers
        or {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) CursorSkill/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {e.code} for {url}\n{body[:5000]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error for {url}: {e}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON from {url}:\n{raw[:5000]}") from e


def naver_stock_integration(itemcode: str):
    itemcode = itemcode.strip()
    if not re.fullmatch(r"\d{6}", itemcode):
        raise ValueError("itemcode must be 6 digits (e.g., 005930)")
    url = f"https://m.stock.naver.com/api/stock/{urllib.parse.quote(itemcode)}/integration"
    return _http_get_json(url)


def naver_index_integration(market: str):
    """KOSPI/KOSDAQ 종합지수 (`/api/index/{MARKET}/integration`)."""
    m = market.strip().upper()
    if m not in {"KOSPI", "KOSDAQ"}:
        raise ValueError("market must be KOSPI or KOSDAQ")
    url = f"https://m.stock.naver.com/api/index/{urllib.parse.quote(m)}/integration"
    return _http_get_json(url)


def naver_exchange_market_value(exchange: str, page: int = 1, page_size: int = 100):
    exchange = exchange.strip().upper()
    if exchange not in {"KOSPI", "KOSDAQ"}:
        raise ValueError("exchange must be KOSPI or KOSDAQ")
    if page < 1:
        raise ValueError("page must be >= 1")
    if not (1 <= page_size <= 500):
        raise ValueError("page_size must be 1..500")

    url = (
        f"https://api.stock.naver.com/stock/exchange/{exchange}/marketValue?"
        + urllib.parse.urlencode({"page": page, "pageSize": page_size})
    )
    return _http_get_json(url)


def naver_find_stock_codes_by_name(
    name_query: str,
    max_pages: int = 5,
    page_size: int = 100,
    exchanges: tuple[str, ...] = ("KOSPI", "KOSDAQ"),
):
    q = re.sub(r"\s+", "", name_query or "")
    if not q:
        raise ValueError("name_query is required")

    matches: list[dict] = []
    for ex in exchanges:
        for page in range(1, max_pages + 1):
            data = naver_exchange_market_value(ex, page=page, page_size=page_size)
            stocks = data.get("stocks") or data.get("data") or []
            if not isinstance(stocks, list) or len(stocks) == 0:
                break

            for s in stocks:
                # Observed common keys: stockName, itemCode/stockCode, code, symbol (may vary)
                stock_name = str(s.get("stockName") or s.get("name") or "").strip()
                key_name = re.sub(r"\s+", "", stock_name)
                if not stock_name:
                    continue
                if q in key_name:
                    code = (
                        s.get("itemCode")
                        or s.get("stockCode")
                        or s.get("code")
                        or s.get("symbol")
                        or ""
                    )
                    code = str(code).strip()
                    if re.fullmatch(r"\d{6}", code):
                        matches.append(
                            {
                                "exchange": ex,
                                "code": code,
                                "name": stock_name,
                                "raw": s,
                            }
                        )
    # De-dup by (code)
    dedup: dict[str, dict] = {}
    for m in matches:
        dedup[m["code"]] = m
    return list(dedup.values())


def ecos_key_statistics(api_key: str, start: int = 1, end: int = 200):
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("ECOS api_key is required")
    url = f"https://ecos.bok.or.kr/api/KeyStatisticList/{urllib.parse.quote(api_key)}/json/kr/{start}/{end}"
    return _http_get_json(url)


def ecos_statistic_search(
    api_key: str,
    stat_code: str,
    cycle: str,
    start_date: str,
    end_date: str,
    item_code: str | None = None,
    start: int = 1,
    end: int = 1000,
):
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("ECOS api_key is required")

    stat_code = stat_code.strip()
    cycle = cycle.strip().upper()
    if cycle not in {"D", "M", "Q", "A"}:
        raise ValueError("cycle must be one of D/M/Q/A")

    parts = [
        "https://ecos.bok.or.kr/api/StatisticSearch",
        urllib.parse.quote(api_key),
        "json",
        "kr",
        str(start),
        str(end),
        urllib.parse.quote(stat_code),
        cycle,
        urllib.parse.quote(start_date),
        urllib.parse.quote(end_date),
    ]
    if item_code:
        parts.append(urllib.parse.quote(item_code.strip()))
    url = "/".join(parts)
    return _http_get_json(url)


def _print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False))


def _now_local_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def cmd_stock(args: argparse.Namespace) -> int:
    if bool(args.code) == bool(args.name):
        raise ValueError("Provide exactly one of --code or --name")

    resolved = None
    if args.code:
        resolved = {"code": args.code, "name_query": None, "matches": []}
        code = args.code
    else:
        matches = naver_find_stock_codes_by_name(
            args.name,
            max_pages=args.max_pages,
            page_size=args.page_size,
        )
        resolved = {"code": None, "name_query": args.name, "matches": matches}
        if not matches:
            out = {
                "source": "naver_stock:search_via_exchange_marketValue",
                "queried_at": _now_local_iso(),
                "resolved": resolved,
                "error": "no_match",
            }
            _print_json(out)
            return 2
        code = matches[0]["code"]
        resolved["code"] = code

    data = naver_stock_integration(code)
    out = {
        "source": "naver_stock:integration",
        "queried_at": _now_local_iso(),
        "resolved": resolved,
        "data": data,
    }
    _print_json(out)
    return 0


def cmd_stock_search(args: argparse.Namespace) -> int:
    matches = naver_find_stock_codes_by_name(
        args.name,
        max_pages=args.max_pages,
        page_size=args.page_size,
    )
    out = {
        "source": "naver_stock:search_via_exchange_marketValue",
        "queried_at": _now_local_iso(),
        "query": {"name": args.name, "max_pages": args.max_pages, "page_size": args.page_size},
        "matches": matches,
    }
    _print_json(out)
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    markets = ["KOSPI", "KOSDAQ"] if args.all else [args.market]
    results = [{"market": m, "data": naver_index_integration(m)} for m in markets]
    if len(results) == 1:
        out = {
            "source": "naver_stock:index_integration",
            "queried_at": _now_local_iso(),
            "market": results[0]["market"],
            "data": results[0]["data"],
        }
    else:
        out = {
            "source": "naver_stock:index_integration",
            "queried_at": _now_local_iso(),
            "results": results,
        }
    _print_json(out)
    return 0


def cmd_key_stats(args: argparse.Namespace) -> int:
    api_key = args.ecos_key or os.environ.get("ECOS_API_KEY", "")
    raw = ecos_key_statistics(api_key, start=1, end=args.limit)
    rows = raw.get("KeyStatisticList", {}).get("row", []) or []

    pattern = re.compile(args.filter) if args.filter else None
    if pattern:
        rows = [r for r in rows if pattern.search(str(r.get("KEYSTAT_NAME", "")))]

    out = {
        "source": "ecos:KeyStatisticList",
        "queried_at": _now_local_iso(),
        "filter": args.filter,
        "rows": rows,
    }
    _print_json(out)
    return 0


def cmd_ecos_series(args: argparse.Namespace) -> int:
    api_key = args.ecos_key or os.environ.get("ECOS_API_KEY", "")
    raw = ecos_statistic_search(
        api_key=api_key,
        stat_code=args.stat_code,
        cycle=args.cycle,
        start_date=args.start,
        end_date=args.end,
        item_code=args.item_code,
        start=1,
        end=args.limit,
    )
    out = {
        "source": "ecos:StatisticSearch",
        "queried_at": _now_local_iso(),
        "query": {
            "stat_code": args.stat_code,
            "cycle": args.cycle,
            "start": args.start,
            "end": args.end,
            "item_code": args.item_code,
        },
        "raw": raw,
    }
    _print_json(out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="korea_econ_api.py",
        description="KR stock/index (Naver) + ECOS key stats/series (HTTP API JSON).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_stock = sub.add_parser("stock", help="Get stock integration data by code or by name (resolved to code).")
    p_stock.add_argument("--code", default="", help="6-digit stock code (e.g., 005930)")
    p_stock.add_argument("--name", default="", help="Stock name query (e.g., 삼성전자)")
    p_stock.add_argument("--max-pages", type=int, default=5, help="Max pages to scan per exchange (default: 5)")
    p_stock.add_argument("--page-size", type=int, default=100, help="Page size per request (default: 100)")
    p_stock.set_defaults(func=cmd_stock)

    p_search = sub.add_parser("stock-search", help="Search stock codes by Korean name (best-effort, API-based).")
    p_search.add_argument("--name", required=True, help="Stock name query (e.g., 삼성전자)")
    p_search.add_argument("--max-pages", type=int, default=5, help="Max pages to scan per exchange (default: 5)")
    p_search.add_argument("--page-size", type=int, default=100, help="Page size per request (default: 100)")
    p_search.set_defaults(func=cmd_stock_search)

    p_index = sub.add_parser(
        "index",
        help="KOSPI/KOSDAQ spot index summary (Naver index integration; do not use stock --code for indices).",
    )
    ix = p_index.add_mutually_exclusive_group(required=True)
    ix.add_argument("--market", choices=["KOSPI", "KOSDAQ"], help="Single index")
    ix.add_argument("--all", action="store_true", help="Fetch both KOSPI and KOSDAQ")
    p_index.set_defaults(func=cmd_index)

    p_key = sub.add_parser("key-stats", help="Get ECOS key statistics snapshot (KOSPI/KOSDAQ/GDP/CPI etc).")
    p_key.add_argument("--ecos-key", default="", help="ECOS API key (or set ECOS_API_KEY env var)")
    p_key.add_argument("--filter", default="", help="Python regex to filter KEYSTAT_NAME (e.g., 코스피|코스닥|소비자물가)")
    p_key.add_argument("--limit", type=int, default=200, help="Max rows to request (default: 200)")
    p_key.set_defaults(func=cmd_key_stats)

    p_series = sub.add_parser("ecos-series", help="Get ECOS time series via StatisticSearch (requires stat_code).")
    p_series.add_argument("--ecos-key", default="", help="ECOS API key (or set ECOS_API_KEY env var)")
    p_series.add_argument("--stat-code", required=True, help="ECOS STAT_CODE (e.g., 200Y101)")
    p_series.add_argument("--cycle", required=True, choices=["D", "M", "Q", "A"], help="Frequency: D/M/Q/A")
    p_series.add_argument("--start", required=True, help="Start date (format depends on cycle, e.g., 2015 or 201501)")
    p_series.add_argument("--end", required=True, help="End date (format depends on cycle, e.g., 2024 or 202412)")
    p_series.add_argument("--item-code", default="", help="Optional ECOS ITEM_CODE")
    p_series.add_argument("--limit", type=int, default=1000, help="Max rows to request (default: 1000)")
    p_series.set_defaults(func=cmd_ecos_series)

    return p


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "item_code", "") == "":
        args.item_code = None
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

