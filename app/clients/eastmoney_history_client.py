"""期货历史行情与联想搜索（东方财富）。

日K / 分时走 push2his.eastmoney.com，联想走 searchapi.eastmoney.com，
全部为东财域名，服务器上不再访问新浪（新浪对云服务器 IP 会 403 / 456）。

返回值刻意保持与改造前一致的形状，前端与 futures_history_service 无需改动：
  get_daily_kline() -> [{"d","o","h","l","c","v","p","s"}]
  get_minute_line() -> [[时间, 价, 均价, 成交量, 持仓量, 昨结, 日期]]
"""
import logging
import re

from fastapi import HTTPException

from app.clients.eastmoney_client import (
    MARKET_IDS,
    display_name,
    http_json,
    http_json_multi,
    market_of_variety,
    num_str,
    to_float,
    to_secid,
    to_symbol,
)

logger = logging.getLogger(__name__)

_KLINE_PATH = "/api/qt/stock/kline/get"
_TRENDS_PATH = "/api/qt/stock/trends2/get"
_SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"

# 历史行情主机回退链：主站 push2his -> 延迟站 push2delay。
# 部分出口 IP 会被 push2his 断连；push2delay 可达（分时数据一致），
# 但它**不提供日K**（返回 dktotal=0/klines=[]），故日K 用 require 判定非空。
_HIST_HOSTS = ("push2his.eastmoney.com", "push2delay.eastmoney.com")

_UT = "7eea3edcaed734bea9cbfc24409ed989"
_SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"

# 东财K线字段： f51 日期 f52 开 f53 收 f54 高 f55 低 f56 量 f57 额
#               f58 振幅 f59 涨跌幅 f60 涨跌额 f61 换手 f62 未知 f63 持仓量 f64 日增
_KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6,f7,f8"
_KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64"
_KLINE_HOLD_INDEX = 12          # 持仓量在 klines 行中的下标（0 基）


def _secid(symbol: str) -> str | None:
    """合约代码（SA2701 / rb2610）-> 东财 secid。"""
    return to_secid("nf_" + (symbol or "").strip())


def _safe(url: str, params: dict) -> dict:
    """调用东财并把失败统一转成 502，保持与改造前 http_get 一致的对外行为。"""
    try:
        return http_json(url, params) or {}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("东财历史行情请求失败 %s：%r", url, exc)
        raise HTTPException(status_code=502, detail=f"东财历史行情请求失败: {exc}")


def _safe_multi(path: str, params: dict, require=None, tag: str = "") -> dict:
    """按主机回退链调用东财历史接口，失败统一转 502。"""
    try:
        return http_json_multi(path, params, _HIST_HOSTS, require=require, tag=tag) or {}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("东财历史行情请求失败 %s（%s）：%r", path, tag or path, exc)
        raise HTTPException(status_code=502, detail=f"东财历史行情请求失败: {exc}")


def _has_klines(payload) -> bool:
    """判定 kline 响应是否真带K线（push2delay 会返回空 klines，需继续换主机）。"""
    return bool(((payload or {}).get("data") or {}).get("klines"))


def _has_trends(payload) -> bool:
    """判定分时响应是否真带分钟数据。"""
    return bool(((payload or {}).get("data") or {}).get("trends"))


# ---------------------------------------------------------------- 日K

def get_daily_kline(symbol: str):
    """某合约的日K线（前复权）。取不到返回空列表。"""
    secid = _secid(symbol)
    if not secid:
        logger.warning("无法映射到东财合约：%s", symbol)
        return []
    payload = _safe_multi(_KLINE_PATH, {
        "secid": secid, "klt": "101", "fqt": "1",
        "lmt": "10000", "end": "20500000", "iscca": "1",
        "fields1": _KLINE_FIELDS1, "fields2": _KLINE_FIELDS2,
        "ut": _UT, "forcect": "1",
    }, require=_has_klines, tag="dailykline")
    klines = ((payload.get("data") or {}).get("klines")) or []
    rows: list[dict] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        rows.append({
            "d": parts[0],            # 日期 YYYY-MM-DD
            "o": parts[1],            # 开
            "c": parts[2],            # 收
            "h": parts[3],            # 高
            "l": parts[4],            # 低
            "v": parts[5],            # 成交量
            "p": parts[_KLINE_HOLD_INDEX] if len(parts) > _KLINE_HOLD_INDEX else "",
            "s": "",                  # 东财日K不含结算价
        })
    return rows


# ---------------------------------------------------------------- 分时

def get_minute_line(symbol: str):
    """某合约当日分时。空数据返回空列表。

    首行第 6 个元素（下标 5）是昨结算，前端据此画昨结参考线。
    """
    secid = _secid(symbol)
    if not secid:
        logger.warning("无法映射到东财合约：%s", symbol)
        return []
    payload = _safe_multi(_TRENDS_PATH, {
        "secid": secid, "ndays": "1", "iscr": "0",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ut": _UT,
    }, require=_has_trends, tag="minline")
    data = payload.get("data") or {}
    pre_settle = to_float(data.get("preSettlement"))
    trends = data.get("trends") or []
    rows: list[list] = []
    for line in trends:
        parts = str(line).split(",")
        if len(parts) < 8:
            continue
        stamp = parts[0]
        date_part, _, time_part = stamp.partition(" ")
        rows.append([
            time_part or stamp,       # 时间 HH:MM
            parts[2],                 # 最新价（收盘）
            parts[7],                 # 均价
            parts[5],                 # 成交量
            "",                       # 持仓量（东财分时不含）
            num_str(pre_settle),      # 昨结
            date_part,                # 日期
        ])
    return rows


# ---------------------------------------------------------------- 联想搜索

# 东财 A 股市场号 -> 项目前缀（沪 sh / 深 sz / 北 bj）
_ASHARE_PREFIX_BY_NAME = {"沪A": "sh", "深A": "sz", "京A": "bj", "北A": "bj"}
_ASHARE_PREFIX_BY_MKT = {"1": "sh", "0": "sz"}
# 期货市场号（东财只有这 6 个市场是境内期货，期权/外盘另有市场号，需排除）
_FUTURES_MKTNUMS = {str(m) for m in MARKET_IDS.values()}

_CODE4_RE = re.compile(r"^([A-Za-z]{1,4})(\d{4})$")


def _suggest(text: str) -> list[dict]:
    payload = _safe(_SUGGEST_URL, {
        "input": text, "type": "14", "token": _SUGGEST_TOKEN, "count": "30",
    })
    table = payload.get("QuotationCodeTable") or {}
    rows = table.get("Data") or []
    return [rows] if isinstance(rows, dict) else rows


def _suggest_inputs(key: str) -> list[str]:
    """东财的郑商所代码是 3 位月份，用户按项目口径输入 4 位时要补一次简称查询。"""
    inputs = [key]
    match = _CODE4_RE.match((key or "").strip())
    if match and market_of_variety(match.group(1)) == MARKET_IDS["CZCE"]:
        inputs.append(f"{match.group(1).upper()}{match.group(2)[1:]}")
    return inputs


def _row_to_item(row: dict) -> dict | None:
    classify = str(row.get("Classify") or "")
    raw_code = str(row.get("Code") or "").strip()
    name = str(row.get("Name") or "").strip() or raw_code
    market_num = str(row.get("MktNum") or "")

    if market_num in _FUTURES_MKTNUMS:          # 境内期货（含中金所）
        symbol = to_symbol(int(market_num), raw_code)
        if not symbol:
            return None
        return {"code": "nf_" + symbol,
                "name": display_name(name, symbol) or symbol,
                "market": "期货"}

    if classify == "AStock":
        prefix = _ASHARE_PREFIX_BY_NAME.get(str(row.get("SecurityTypeName") or ""))
        if prefix is None:
            prefix = _ASHARE_PREFIX_BY_MKT.get(market_num)
        if prefix is None or len(raw_code) != 6 or not raw_code.isdigit():
            return None
        return {"code": prefix + raw_code, "name": name, "market": "A股"}

    return None


def search_symbols(key: str, limit: int = 20) -> list[dict]:
    """按关键字联想「国内期货 + A股」，返回 [{code, name, market}]。"""
    cleaned = (key or "").strip()
    if not cleaned:
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for text in _suggest_inputs(cleaned):
        for row in _suggest(text):
            item = _row_to_item(row)
            if item is None or item["code"] in seen:
                continue
            seen.add(item["code"])
            result.append(item)
            if len(result) >= limit:
                return result
    return result
