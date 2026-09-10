"""新浪行情接口适配层（K线/分钟线/联想/合约目录）。

实时行情已迁移到 akshare（东财兜底），见 clients.akshare_client。
注意：本模块请求的是 stock2/vip.stock.finance.sina.com.cn，
与被机房 IP 封禁的 hq.sinajs.cn 不是同一个域；若服务器上
minline/dailykline 也报错，再按同样思路切东财。

项目其他模块只调用这里的业务方法，不直接感知新浪域名、路径、编码或 JSONP。
"""
import json
import urllib.parse

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
