"""国内期货实时行情与合约目录（东方财富）。

只服务 nf_ 开头的国内期货代码。数据全部来自东方财富，不再使用新浪
vip.stock.finance.sina.com.cn / hq.sinajs.cn（这两个域对云服务器 IP 会直接
返回 403 / 456「IP 存在异常访问」，是此前 /api/futures 502 的根因）。

用到的接口（均为东财公开行情接口，无需鉴权）：
  实时行情  push2.eastmoney.com/api/qt/ulist.np/get        一次可取多合约
  合约目录  futsseapi.eastmoney.com/list/{市场码}            一次返回该市场全部挂牌合约
  品种表    futsse-static.eastmoney.com/redis?msgid={市场码}
  合约明细  futsse-static.eastmoney.com/redis?msgid={市场码}_{序号}

代码换算（项目内部统一「品种 + 4 位交割年月」，如 SA2701 / RB2610 / IF2609）：
  CZCE 115  SA2701 <-> SA701   郑商所用 3 位月份（年份末位 + 2 位），大写
  SHFE 113  RB2610 <-> rb2610  小写 4 位
  DCE  114  M2610  <-> m2610   小写 4 位
  INE  142  SC2610 <-> sc2610  小写 4 位
  GFEX 225  SI2610 <-> si2610  小写 4 位
  CFFEX 220 IF2609 <-> IF2609  大写 4 位
  主力连续   nf_SA0 -> 115.sam（品种小写 + m）、nf_IF0 -> 220.ifm

返回结构（与前端约定保持不变）：
  {code, source_symbol, name, open, high, low, price, yestclose, volume, time}
其中 yestclose 是「昨结算」（涨跌额以昨结为基准，与交易所口径一致），
由 最新价 - 涨跌额 反推，取不到时退回东财的昨收字段。
"""
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# 行情源全是国内站点，环境/系统代理（V2Ray/Clash 等）只会 ProxyError
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

# 东财会对 python-requests 默认 UA 直接断开，统一换 Chrome UA
_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _CHROME_UA,
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}
_HTTP_TIMEOUT = float(os.getenv("FUTURES_HTTP_TIMEOUT", "15"))
_RETRY_TIMES = int(os.getenv("FUTURES_RETRY_TIMES", "3"))

# 东财时间戳按北京时间解释（服务器时区不同也不会跑偏）
_CST = timezone(timedelta(hours=8))

# 同一进程内的上游请求串行化：避免多个浏览器同时刷新时把并发打上去
_UPSTREAM_LOCK = threading.Lock()
_LAST_ERRORS: list[str] = []    # 本次请求的失败原因链，502 时回传给前端便于定位

# 东财限流码：重试只会加重封禁，命中后进入冷却期，其他请求快速失败
_BLOCK_CODES = {403, 429, 456}
_COOLDOWN = int(os.getenv("FUTURES_EM_COOLDOWN", "600"))
_blocked_until = 0.0


class UpstreamBlocked(RuntimeError):
    """东方财富对当前出口 IP 触发了限流（HTTP 403/429/456）。"""


def cooldown_left() -> float:
    """距限流冷却结束还剩多少秒；0 表示当前可以正常请求。"""
    return max(0.0, _blocked_until - time.monotonic())


def _mark_blocked(status: int) -> None:
    global _blocked_until
    _blocked_until = time.monotonic() + _COOLDOWN
    logger.error("东财限流 HTTP %s：暂停 %ss 内的东财请求（可用 FUTURES_EM_COOLDOWN 调整）",
                 status, _COOLDOWN)


def _note_error(msg: str) -> None:
    """记录失败原因：既进日志（服务器 uvicorn 能看到），也进 502 的 detail。"""
    logger.error("行情失败原因: %s", msg)
    _LAST_ERRORS.append(msg)


# ---------------------------------------------------------------- HTTP 底座

_SESSION = None


def _session():
    """懒加载 requests.Session：绕过系统代理 + 固定东财 UA/Referer。"""
    global _SESSION
    if _SESSION is None:
        import requests
        session = requests.Session()
        session.trust_env = False          # 忽略 http_proxy/https_proxy 环境变量
        session.headers.update(_HEADERS)
        _SESSION = session
    return _SESSION


def _is_conn_error(exc: Exception) -> bool:
    """区分「连接被掐断 / 超时」与「连上了但响应异常」。

    前者（RemoteDisconnected、ConnectionError、Timeout）说明**这台主机在这个出口
    不可用**，原地重试既没用又要多等退避 sleep —— 换主机才是正解；
    后者（偶发空响应、JSON 截断）才值得原地重试。
    """
    try:
        import requests
    except Exception:  # pragma: no cover - requests 一定装得上
        return False
    return isinstance(exc, (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
    ))


def _brief_exc(exc: Exception) -> str:
    """把异常压成一行「根因」，日志里不要倒出整串 repr。

    形如 RuntimeError("东财请求失败 https://…：ConnectionError(ProtocolError(
    'Connection aborted.', RemoteDisconnected('Remote end closed…')))")
    只保留最内层的 RemoteDisconnected —— 那才是能看出问题的信息。
    """
    root = exc
    for _ in range(8):
        if root.__cause__ is None:
            break
        root = root.__cause__
    # 根因常被 repr 拼进字符串（ConnectionError(ProtocolError(RemoteDisconnected(...)))），
    # 按异常类名从外到内取名，取最内层 —— 那才是能看出问题的那个
    names = re.findall(
        r"[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Disconnected|Timeout)", repr(root)
    )
    if names:
        return names[-1].rsplit(".", 1)[-1]
    text = (str(root).strip().splitlines() or [""])[0]
    return f"{type(root).__name__}（{text[:48]}）" if text else type(root).__name__


def http_json(url: str, params: dict):
    """GET 并解析 JSON。

    瞬时网络异常（东财偶发空响应）按 _RETRY_TIMES 重试；
    连接层被掐断不重试（换主机更有效）；
    命中限流码直接抛 UpstreamBlocked，不再重试。
    """
    left = cooldown_left()
    if left > 0:
        raise UpstreamBlocked(f"东财限流冷却中，{int(left)}s 后再试")
    last: Exception | None = None
    for attempt in range(_RETRY_TIMES):
        try:
            resp = _session().get(url, params=params, timeout=_HTTP_TIMEOUT)
            if resp.status_code in _BLOCK_CODES:
                _mark_blocked(resp.status_code)
                raise UpstreamBlocked(f"东财限流 HTTP {resp.status_code}")
            resp.raise_for_status()
            if not resp.content.strip():
                raise RuntimeError("东财返回空响应")
            return resp.json()
        except UpstreamBlocked:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if _is_conn_error(exc):
                break                      # 主机不通，交回 http_json_multi 换下一台
            if attempt < _RETRY_TIMES - 1:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"东财请求失败 {url}：{last!r}") from last


# 东财行情主机回退链。
# 实测：部分出口 IP（云机房 / VPN 出口）会被主站 push2 直接断连 —— TLS 握手成功、
# 请求已发出，但对端不返回任何字节（RemoteDisconnected）；同一个出口访问延迟站
# push2delay 却完全正常，且返回的数值与主站一致。故按「主站 -> 延迟站」顺序回退：
# 正常网络下永远走主站（真·实时），受限网络自动降级，看板不至于整屏空白。
_ULIST_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com")

# 记录每个接口最近一次成功的主机，供 diagnose / 日志排查用
_LAST_OK_HOST: dict[str, str] = {}

# 主机级冷却：某台主机连续失败后，一段时间内直接跳过它。
# 动机：受限出口下 push2 每请求必被断连，若不做记忆，每次刷新都要白试一次
# （连接 + 退避 sleep，实测每次约 1.4s）并刷一条同样的日志。
# 之所以要求「连续失败 N 次」才冷却，是为了防止偶发抖动把一台好主机打入冷宫。
_HOST_DOWN_AFTER = int(os.getenv("FUTURES_HOST_DOWN_AFTER", "2"))
_HOST_DOWN_COOLDOWN = int(os.getenv("FUTURES_HOST_COOLDOWN", "300"))
_host_down_until: dict[str, float] = {}
_host_fail_streak: dict[str, int] = {}
_nodata_warned: set[tuple[str, str]] = set()      # (接口, 主机) 已提示过「无所需数据」


def host_cooldown_left(host: str) -> float:
    """该主机距冷却结束还剩多少秒；0 表示可以尝试。"""
    return max(0.0, _host_down_until.get(host, 0.0) - time.monotonic())


def host_state() -> dict:
    """各主机当前状态，供 /api/futures/diag 一眼看清回退链。"""
    hosts = set(_ULIST_HOSTS) | set(_host_down_until) | set(_host_fail_streak)
    return {
        h: {"cooldown_left": round(host_cooldown_left(h), 1),
            "fail_streak": _host_fail_streak.get(h, 0)}
        for h in sorted(hosts)
    }


def _mark_host_fail(host: str) -> bool:
    """记一次主机失败；返回本次是否**刚进入冷却**（由调用方决定怎么记日志）。"""
    streak = _host_fail_streak.get(host, 0) + 1
    _host_fail_streak[host] = streak
    if streak < _HOST_DOWN_AFTER:
        return False
    entering = host_cooldown_left(host) <= 0
    _host_down_until[host] = time.monotonic() + _HOST_DOWN_COOLDOWN
    return entering


def _mark_host_ok(host: str) -> None:
    _host_fail_streak.pop(host, None)
    if _host_down_until.pop(host, None) is not None:
        logger.info("东财主机 %s 已恢复", host)


def http_json_multi(path: str, params: dict, hosts, require=None, tag: str = ""):
    """按顺序尝试多个东财主机，返回第一个「可用」的 JSON。

    require: 可选判定函数，返回 True 才算可用（例如日K要求 klines 非空）；
             不满足则继续试下一台主机。需要重试的情形交给 http_json 内部处理。
    限流（UpstreamBlocked）不换主机 —— 同一出口 IP 换域名没用，只会加重封禁。
    冷却中的主机会被直接跳过；若所有主机都在冷却里，则清空冷却**强探一轮**，
    保证网络恢复后能自动切回来（否则会一直等冷却结束才恢复）。
    """
    label = tag or path
    order = list(hosts)
    todo = [h for h in order if host_cooldown_left(h) <= 0]
    if not todo:
        _host_down_until.clear()
        _host_fail_streak.clear()
        todo = order
        logger.info("东财 %s：所有主机均在冷却中，强制重探一轮", label)

    last_exc: Exception | None = None
    last_payload = None
    for host in todo:
        try:
            payload = http_json(f"https://{host}{path}", params)
        except UpstreamBlocked:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            # 一次失败只出一行：不再既在 _mark_host_fail 里报、又在这里报一遍。
            brief = _brief_exc(exc)
            if _mark_host_fail(host):
                logger.warning(
                    "东财 %s：主机 %s 连接失败（%s），已自动降级走备机，"
                    "%ds 内不再尝试该主机（FUTURES_HOST_COOLDOWN 可调）",
                    label, host, brief, _HOST_DOWN_COOLDOWN,
                )
            else:
                logger.info("东财 %s：主机 %s 连接失败（%s），自动改用备机",
                            label, host, brief)
            continue
        if require is None or require(payload):
            _mark_host_ok(host)
            _nodata_warned.discard((label, host))
            if _LAST_OK_HOST.get(label) != host:
                _LAST_OK_HOST[label] = host
                logger.info("东财 %s 命中主机 %s", label, host)
            return payload
        # 连上了但不含所需数据（如 push2delay 不提供日K）：不算「主机不通」，不冷却。
        # 这是稳定的数据口径差异而非故障，同类只提示一次，避免每次刷新都刷屏。
        last_payload = payload
        if (label, host) not in _nodata_warned:
            _nodata_warned.add((label, host))
            logger.warning("东财主机 %s 响应不含所需数据（%s），回退下一台（同类不再重复提示）",
                           host, label)
    if last_payload is not None:
        return last_payload
    raise RuntimeError(f"东财所有主机均失败 {path}：{last_exc!r}")



# ---------------------------------------------------------------- 代码换算

# 项目内部交易所代码 -> 东财市场码。
# 注意：能源中心（SC/LU/NR/BC/EC）在东财是独立市场 142，库里仍归在 SHFE 下。
MARKET_IDS: dict[str, int] = {
    "SHFE": 113, "DCE": 114, "CZCE": 115, "INE": 142, "CFFEX": 220, "GFEX": 225,
}
MARKET_NAMES: dict[int, str] = {
    113: "上期所", 114: "大商所", 115: "郑商所",
    142: "能源中心", 220: "中金所", 225: "广期所",
}
# 市场码 -> 项目内部交易所代码（能源中心并入 SHFE，与 futures_base.exchange 口径一致）
DB_EXCHANGE_OF_MARKET: dict[int, str] = {
    113: "SHFE", 114: "DCE", 115: "CZCE", 142: "SHFE", 220: "CFFEX", 225: "GFEX",
}

# 品种代码 -> 东财市场码（2026-09-10 按东财各市场品种表核对生成）。
# 新增品种可在品种表里查到，运行时也会自动从东财品种表补齐（见 _variety_index）。
_VARIETY_MARKET: dict[str, int] = {
    # ---- SHFE 上期所（113）----
    "AD": 113, "AG": 113, "AL": 113, "AO": 113, "AU": 113, "BR": 113, "BU": 113,
    "CU": 113, "FU": 113, "HC": 113, "NI": 113, "OP": 113, "PB": 113, "RB": 113,
    "RU": 113, "SN": 113, "SP": 113, "SS": 113, "WR": 113, "ZN": 113,
    # ---- INE 能源中心（142，库里归 SHFE）----
    "BC": 142, "EC": 142, "LU": 142, "NR": 142, "SC": 142,
    # ---- DCE 大商所（114）----
    "A": 114, "B": 114, "BB": 114, "BZ": 114, "C": 114, "CS": 114, "EB": 114,
    "EG": 114, "FB": 114, "I": 114, "J": 114, "JD": 114, "JM": 114, "L": 114,
    "LG": 114, "LH": 114, "M": 114, "P": 114, "PG": 114, "PP": 114, "RR": 114,
    "V": 114, "Y": 114,
    # ---- CZCE 郑商所（115）----
    "AP": 115, "CF": 115, "CJ": 115, "CY": 115, "FG": 115, "JR": 115, "LR": 115,
    "MA": 115, "OI": 115, "PF": 115, "PK": 115, "PL": 115, "PM": 115, "PR": 115,
    "PX": 115, "RI": 115, "RM": 115, "RS": 115, "SA": 115, "SF": 115, "SH": 115,
    "SM": 115, "SR": 115, "TA": 115, "UR": 115, "WH": 115, "ZC": 115,
    # ---- CFFEX 中金所（220）----
    "IC": 220, "IF": 220, "IH": 220, "IM": 220, "T": 220, "TF": 220, "TL": 220, "TS": 220,
    # ---- GFEX 广期所（225）----
    "LC": 225, "PD": 225, "PS": 225, "PT": 225, "SI": 225,
}

_variety_index_cache: dict[str, int] | None = None


def _variety_index() -> dict[str, int]:
    """品种代码 -> 市场码。本地表优先；新增品种则从东财品种表补齐（进程内缓存一次）。"""
    global _variety_index_cache
    if _variety_index_cache is not None:
        return _variety_index_cache
    index = dict(_VARIETY_MARKET)
    for market in MARKET_IDS.values():
        try:
            rows = http_json(_REDIS_URL, {"msgid": str(market)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("东财品种表 %s 读取失败：%s", market, exc)
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            code = str(row.get("vcode") or "").strip().upper()
            if code and code not in index:
                index[code] = market
    _variety_index_cache = index
    return index


def market_of_variety(variety: str) -> int | None:
    """品种代码（SA/RB/IF）-> 东财市场码；查不到返回 None。"""
    code = (variety or "").strip().upper()
    if not code:
        return None
    market = _VARIETY_MARKET.get(code)
    if market:
        return market
    return _variety_index().get(code)


def _expand_czce(digits3: str, now: datetime | None = None) -> str:
    """郑商所 3 位月份 -> 4 位交割年月：'701' -> '2701'（年份末位 + 2 位月份）。"""
    current = (now or datetime.now()).year
    year = current - current % 10 + int(digits3[0])
    if year < current - 1:
        year += 10
    return f"{year % 100:02d}{digits3[1:]}"


_NF_CODE_RE = re.compile(r"^nf_([A-Za-z]+)(\d*)$")
_EM_CODE_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def to_secid(code: str) -> str | None:
    """项目代码 -> 东财 secid。

    nf_SA2701 -> 115.SA701 ; nf_RB2610 -> 113.rb2610 ;
    nf_IF2609 -> 220.IF2609 ; nf_SA0/nf_SA -> 115.sam。无法映射返回 None。
    """
    match = _NF_CODE_RE.match((code or "").strip())
    if not match:
        return None
    variety = match.group(1).upper()
    digits = match.group(2) or ""
    market = market_of_variety(variety)
    if market is None:
        return None
    if digits in ("", "0"):
        return f"{market}.{variety.lower()}m"        # 主力连续
    if len(digits) != 4:
        return None
    year, month = digits[:2], digits[2:]
    if not 1 <= int(month) <= 12:
        return None
    if market == MARKET_IDS["CZCE"]:
        return f"{market}.{variety}{year[1]}{month}"   # 郑商所 3 位月份
    if market == MARKET_IDS["CFFEX"]:
        return f"{market}.{variety}{year}{month}"      # 中金所大写
    return f"{market}.{variety.lower()}{year}{month}"


def to_symbol(market: int, em_code: str) -> str:
    """东财代码 -> 项目 4 位代码。

    (115,'SA701') -> 'SA2701' ; (113,'rb2610') -> 'RB2610'。
    无法识别（如大商所的月均价合约 lF）返回空串。
    """
    match = _EM_CODE_RE.match((em_code or "").strip())
    if not match:
        return ""
    variety = match.group(1).upper()
    digits = match.group(2)
    if len(digits) == 3:                     # 郑商所
        if not 1 <= int(digits[1:]) <= 12:
            return ""
        digits = _expand_czce(digits)
    elif len(digits) != 4:
        return ""
    if not 1 <= int(digits[2:]) <= 12:
        return ""
    return f"{variety}{digits}"


# ---------------------------------------------------------------- 数值与时间

def to_float(value) -> float | None:
    """东财数值 -> float；None / '-' / 空串一律 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in ("", "-", "None", "nan"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def num_str(value) -> str:
    """数值 -> 去掉多余零的字符串（1075.0 -> '1075'、4542.20 -> '4542.2'）。"""
    number = to_float(value)
    if number is None:
        return ""
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_time(stamp) -> str:
    """东财更新时间戳（秒）-> HH:MM:SS（北京时间）。"""
    seconds = to_float(stamp)
    if not seconds:
        return ""
    try:
        return datetime.fromtimestamp(seconds, tz=_CST).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


# ---------------------------------------------------------------- 数据源：实时行情

_ULIST_PATH = "/api/qt/ulist.np/get"
_ULIST_URL = f"https://{_ULIST_HOSTS[0]}{_ULIST_PATH}"     # 兼容旧引用/诊断展示
_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_QUOTE_FIELDS = "f1,f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18,f124"
_QUOTE_CHUNK = 80                          # 单次请求最多带多少个 secid


def _has_quote_rows(payload) -> bool:
    """判定 ulist 响应是否真的带行情行（空 data 说明该主机没用，应换主机）。"""
    data = (payload or {}).get("data") or {}
    diff = data.get("diff") if isinstance(data, dict) else data
    return bool(diff)


def _fetch_quote_rows(secids: list[str]) -> dict[tuple[int, str], dict]:
    """批量取实时行情，返回 {(市场码, 东财代码大写): 原始行}。"""
    rows: dict[tuple[int, str], dict] = {}
    for start in range(0, len(secids), _QUOTE_CHUNK):
        chunk = secids[start:start + _QUOTE_CHUNK]
        if not chunk:
            continue
        payload = http_json_multi(_ULIST_PATH, {
            "fltt": "2", "invt": "2", "np": "1",
            "secids": ",".join(chunk),
            "fields": _QUOTE_FIELDS,
            "ut": _UT,
        }, _ULIST_HOSTS, require=_has_quote_rows, tag="realtime")
        data = (payload or {}).get("data") or {}
        diff = data.get("diff") if isinstance(data, dict) else data
        if isinstance(diff, dict):
            diff = [diff]
        for row in diff or []:
            try:
                market = int(row.get("f13"))
            except (TypeError, ValueError):
                continue
            code = str(row.get("f12") or "").strip().upper()
            if code:
                rows[(market, code)] = row
    return rows


_MONTH_IN_NAME_RE = re.compile(r"^(.*?)(\d{3,4})$")


def display_name(raw_name: str, project_symbol: str) -> str:
    """东财合约名 -> 项目口径。

    郑商所东财用 3 位月份（「纯碱701」），项目统一 4 位（「纯碱2701」）；
    主力连续（纯碱主连）没有月份，原样保留。
    """
    name = (raw_name or "").strip()
    if not name or len(project_symbol) != 6:      # 只有「品种+4位年月」才需要换月份
        return name
    match = _MONTH_IN_NAME_RE.match(name)
    if not match or len(match.group(2)) == 4:
        return name
    return f"{match.group(1)}{project_symbol[-4:]}"


def _row_from_quote(code: str, row: dict) -> dict:
    price = to_float(row.get("f2"))
    change = to_float(row.get("f4"))
    # 涨跌额以「昨结算」为基准，所以昨结 = 最新价 - 涨跌额
    yestclose = round(price - change, 4) if price is not None and change is not None else None
    if yestclose is None:
        yestclose = to_float(row.get("f18"))
    project_symbol = code[3:].strip().upper()
    display = display_name(str(row.get("f14") or ""), project_symbol)
    return {
        "code": code,
        "source_symbol": str(row.get("f12") or ""),
        "name": display or project_symbol or code,
        "open": num_str(row.get("f17")),
        "high": num_str(row.get("f15")),
        "low": num_str(row.get("f16")),
        "price": num_str(price),
        "yestclose": num_str(yestclose),
        "volume": num_str(row.get("f5")),
        "time": _fmt_time(row.get("f124")),
    }


# ---------------------------------------------------------------- 数据源：合约目录

_FUTS_API = "https://futsseapi.eastmoney.com"
_REDIS_URL = "https://futsse-static.eastmoney.com/redis"
_CONTRACT_FIELDS = "dm,sc,name,p,o,h,l,zjsj,zde,zdf,vol,ccl,cje"


def fetch_market_contracts(market: int) -> list[dict]:
    """某市场当前挂牌的全部合约（东财一次返回，无需按品种逐个请求）。

    返回 [{"symbol", "name", "variety", "market", "exchange"}]。
    """
    payload = http_json(f"{_FUTS_API}/list/{market}", {
        "orderBy": "dm", "sort": "asc", "pageSize": "20000", "pageIndex": "0",
        "field": _CONTRACT_FIELDS,
    })
    out: list[dict] = []
    for row in (payload or {}).get("list") or []:
        symbol = to_symbol(market, str(row.get("dm") or ""))
        if not symbol:
            continue
        out.append({
            "symbol": symbol,
            "name": str(row.get("name") or "").strip(),
            "variety": symbol[:len(symbol) - 4],
            "market": market,
            "exchange": DB_EXCHANGE_OF_MARKET.get(market, ""),
        })
    return out


def fetch_varieties() -> list[dict]:
    """东财全部品种表（码表缺失时的回退来源）。

    返回 [{"name", "variety", "exchange", "market"}]。
    """
    out: list[dict] = []
    for market in MARKET_IDS.values():
        rows = http_json(_REDIS_URL, {"msgid": str(market)})
        if not isinstance(rows, list):
            continue
        for row in rows:
            variety = str(row.get("vcode") or "").strip().upper()
            name = str(row.get("vname") or "").strip()
            if not variety or not name or len(variety) > 4:
                continue
            out.append({
                "name": name, "variety": variety, "market": market,
                "exchange": DB_EXCHANGE_OF_MARKET.get(market, ""),
            })
    return out


# ---------------------------------------------------------------- 统一入口

def get_quotes(codes: list[str]) -> list[dict]:
    """国内期货实时行情入口（只处理 nf_ 代码）。返回顺序跟随入参。

    只有「一个都没取到」才抛 502；部分取到则返回部分结果，
    由前端把取不到的合约显示为无数据。
    """
    try:
        with _UPSTREAM_LOCK:
            return _get_quotes(codes)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("期货行情获取异常: %s", exc)
        raise HTTPException(status_code=502, detail=f"期货行情获取异常: {exc}")


def _get_quotes(codes: list[str]) -> list[dict]:
    _LAST_ERRORS.clear()
    clean = [str(c).strip() for c in codes if str(c).strip()]

    secid_map: dict[str, str] = {}
    for code in clean:
        if not code.lower().startswith("nf_"):
            _note_error(f"{code} 不是 nf_ 开头的国内期货代码")
            continue
        secid = to_secid(code)
        if secid:
            secid_map[code] = secid
        else:
            _note_error(f"{code} 无法映射到东财合约（品种未收录，或代码不是「品种+4位年月」）")

    rows: dict[tuple[int, str], dict] = {}
    bulk_error = False                      # 整批请求是否根本没成功（网络/主机问题）
    if secid_map:
        try:
            rows = _fetch_quote_rows(sorted(set(secid_map.values())))
            bulk_error = not rows
        except UpstreamBlocked as exc:
            bulk_error = True
            _note_error(str(exc))
        except Exception as exc:  # noqa: BLE001
            bulk_error = True
            _note_error(f"东财实时行情请求整体失败：{exc!r}")

    result: dict[str, dict] = {}
    missing: list[str] = []
    for code, secid in secid_map.items():
        market_text, _, em_code = secid.partition(".")
        row = rows.get((int(market_text), em_code.upper())) if rows else None
        if row is None:
            missing.append(code)
            continue
        result[code] = _row_from_quote(code, row)

    # 整批就失败时不要逐条刷「未挂牌」——那会把网络问题说成合约问题，误导排查
    if missing:
        if bulk_error:
            _note_error(
                f"{len(missing)} 个合约未取到行情（本次批量请求整体失败，与合约本身无关）："
                + "、".join(missing[:8]) + ("…" if len(missing) > 8 else "")
            )
        else:
            for code in missing:
                _note_error(f"东财未返回 {code} 的行情，可能尚未挂牌或已下市")

    if not result and clean:
        reason = "；".join(_LAST_ERRORS[-5:]) or "未知原因（东财返回空）"
        raise HTTPException(status_code=502, detail=f"东财未取到任何期货行情 [{codes}]：{reason}")
    return [result[code] for code in clean if code in result]


# ---------------------------------------------------------------- 自诊断

def diagnose(codes: list[str]) -> dict:
    """逐层探测行情链路，定位 502 断在哪一环。/api/futures/diag 用。"""
    import platform
    import sys

    report: dict = {
        "source": "eastmoney",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "proxy_env": {k: os.environ.get(k) for k in
                      ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                       "no_proxy", "NO_PROXY")},
        "cooldown_left": round(cooldown_left(), 1),
        "codes": codes,
        "hosts_used": dict(_LAST_OK_HOST),      # 各接口实际命中的主机（诊断回退是否生效）
        "hosts_state": host_state(),            # 各主机冷却剩余秒数 / 连续失败次数
    }

    # 三个域名各自探一次连通性
    probes = (
        ("quote_ulist", lambda: _fetch_quote_rows(["115.sam"])),
        ("contract_list", lambda: fetch_market_contracts(MARKET_IDS["CZCE"])),
        ("variety_table", lambda: http_json(_REDIS_URL, {"msgid": str(MARKET_IDS["CZCE"])})),
    )
    for label, probe in probes:
        try:
            probe()
            report[label] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            report[label] = {"ok": False, "error": f"{exc!r}"}

    per_code: list[dict] = []
    for code in codes:
        item: dict = {"code": code}
        secid = to_secid(code)
        item["secid"] = secid
        if not secid:
            item["error"] = "无法映射到东财合约（品种未收录，或代码不是「品种+4位年月」）"
            per_code.append(item)
            continue
        market_text, _, em_code = secid.partition(".")
        try:
            rows = _fetch_quote_rows([secid])
            row = rows.get((int(market_text), em_code.upper()))
            item["quote"] = {
                "ok": row is not None,
                "name": str(row.get("f14")) if row else None,
                "price": num_str(row.get("f2")) if row else None,
                "time": _fmt_time(row.get("f124")) if row else None,
            }
        except Exception as exc:  # noqa: BLE001
            item["quote"] = {"ok": False, "error": f"{exc!r}"}
        per_code.append(item)
    report["per_code"] = per_code

    ok = any(i.get("quote", {}).get("ok") for i in per_code)
    report["结论"] = ("东财行情链路正常" if ok else
                     "所有合约都取不到：看 per_code 与三个域名的 ok/error 字段")
    return report
