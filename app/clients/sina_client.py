"""新浪行情接口适配层。

项目其他模块只调用这里的业务方法，不直接感知新浪域名、路径、编码或 JSONP。
"""
import json
import re
import urllib.parse

from fastapi import HTTPException

from app.fetchutils import http_get, parse_jsonp


def _sina_get(host: str, path: str, encoding: str = "gb18030") -> str:
    return http_get("https://" + host + path, enc=encoding)


NODE_LIST_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/view/js/qihuohangqing.js"
CONTRACT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQFuturesData?page=1&sort=position&asc=0&node={node}&base=futures"
)
KLINE_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
    "var%20t=/InnerFuturesNewService.getDailyKLine?symbol={symbol}"
)

_HQ_LINE_RE = re.compile(r'var\s+hq_str_([A-Za-z0-9_$.]+?)="(.*?)"\s*;?', re.DOTALL)
_INDEX_FUTURE_RE = re.compile(r"^nf_(IF|IH|IC|IM|TF|TS|T\d|TL)")


def _parse_quote_item(code: str, fields: list[str]) -> dict | None:
    if len(fields) < 2:
        return None
    if code.startswith("nf_"):
        if _INDEX_FUTURE_RE.match(code):
            return {
                "code": code, "name": ((fields[49] if len(fields) > 49 else code) or code).replace('\"', ""),
                "open": fields[0], "high": fields[1], "low": fields[2], "price": fields[3],
                "yestclose": fields[13] if len(fields) > 13 else "",
                "volume": fields[4] if len(fields) > 4 else "",
                "time": fields[37] if len(fields) > 37 else "",
            }
        return {
            "code": code, "name": fields[0],
            "open": fields[2] if len(fields) > 2 else "",
            "high": fields[3] if len(fields) > 3 else "",
            "low": fields[4] if len(fields) > 4 else "",
            "price": fields[8] if len(fields) > 8 else "",
            "yestclose": fields[10] if len(fields) > 10 else "",
            "volume": fields[14] if len(fields) > 14 else "",
            "time": fields[1] if len(fields) > 1 else "",
        }
    if re.match(r"^(sh|sz|bj)\d", code):
        return {
            "code": code, "name": fields[0],
            "open": fields[1] if len(fields) > 1 else "",
            "yestclose": fields[2] if len(fields) > 2 else "",
            "price": fields[3] if len(fields) > 3 else "",
            "high": fields[4] if len(fields) > 4 else "",
            "low": fields[5] if len(fields) > 5 else "",
            "volume": fields[8] if len(fields) > 8 else "",
            "time": fields[31] if len(fields) > 31 else "",
        }
    return None


def get_quotes(codes: list[str]) -> list[dict]:
    encoded = [urllib.parse.quote(code.strip()) for code in codes if code.strip()]
    text = _sina_get("hq.sinajs.cn", "/list=" + ",".join(encoded))
    if "FAILED" in text:
        raise HTTPException(status_code=502, detail="新浪返回 FAILED")
    result = []
    for line in text.splitlines():
        match = _HQ_LINE_RE.search(line)
        if match:
            item = _parse_quote_item(match.group(1), match.group(2).split(","))
            if item is not None:
                result.append(item)
    return result


def search_symbols(key: str, limit: int = 20) -> list[dict]:
    path = "/suggest/type=11,85,88&key=" + urllib.parse.quote(key)
    text = _sina_get("suggest3.sinajs.cn", path)
    start, end = text.find('="') + 2, text.rfind('\"')
    if start < 2 or end <= start:
        return []
    result, seen = [], set()
    for raw_item in text[start:end].split(";"):
        fields = raw_item.split(",")
        if len(fields) < 5:
            continue
        market, raw = fields[1], (fields[3] or "").strip()
        if market in ("85", "88") and raw:
            code, market_name = "nf_" + raw.upper().removeprefix("NF_"), "期货"
        elif market == "11" and raw:
            code = raw.lower()
            if re.fullmatch(r"\d{6}", code):
                code = ("sh" if code[0] in "56" else "sz" if code[0] in "03" else "bj") + code
            if not re.fullmatch(r"(sh|sz|bj)\d{6}", code):
                continue
            market_name = "A股"
        else:
            continue
        if code in seen:
            continue
        seen.add(code)
        result.append({"code": code, "name": (fields[4] or fields[0] or "").strip() or code, "market": market_name})
    return result[:limit]


def get_minute_line(symbol: str):
    path = ("/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getMinLine?symbol="
            + urllib.parse.quote(symbol))
    return parse_jsonp(_sina_get("stock2.finance.sina.com.cn", path))


def get_daily_kline(symbol: str):
    url = KLINE_URL.format(symbol=urllib.parse.quote(symbol))
    return parse_jsonp(http_get(url, enc="utf-8"))


def get_node_list_text() -> str:
    return http_get(NODE_LIST_URL, enc="gb2312")


def get_contracts(node: str) -> list[dict]:
    return json.loads(http_get(CONTRACT_URL.format(node=urllib.parse.quote(node))))
