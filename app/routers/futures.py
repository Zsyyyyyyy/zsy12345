#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期货行情代理路由 —— 整合进 FastAPI 主后端。

原 modules/futures/server.py 的转发逻辑原样搬到这里：
  - 补 Referer（新浪 hq.sinajs.cn 严格校验 Referer，否则 403）
  - GB18030 -> UTF-8 转码
  - 港股代码自动加 rt_ 前缀拿实时行情

本版升级：后端直接解析新浪原始格式，返回结构化 JSON，
前端无需再写正则 / split 解析，可直接 await resp.json() 使用。

零第三方依赖，仅用 Python 标准库（urllib、re、json）。
端点用同步 def 声明，FastAPI 会自动把同步端点丢进线程池执行，
不阻塞事件循环。
"""
import json
import re
import socket
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
REFERER = 'http://finance.sina.com.cn/'
TIMEOUT = 15

router = APIRouter(tags=["futures"])


# ========== 新浪原始格式解析（服务端做，前端零负担） ==========

# hq.sinajs.cn 单行：var hq_str_<code>="f1,f2,...,fN";
_HQ_LINE_RE = re.compile(r'var\s+hq_str_([A-Za-z0-9_$.]+?)="(.*?)"\s*;?', re.DOTALL)

# 股指期货/国债期货识别（与原前端 parseSina 一致）
_INDEX_FUTURE_RE = re.compile(r'^nf_(IF|IH|IC|IM|TF|TS|T\d|TL)')


def _parse_hq_item(code: str, fields: list[str]) -> dict | None:
    """把新浪单行字段数组归一化为统一结构。失败返回 None。"""
    if len(fields) < 2:
        return None
    if code.startswith('nf_'):
        is_index_future = bool(_INDEX_FUTURE_RE.match(code))
        if is_index_future:
            # 字段数较多，名字在索引 49
            name = (fields[49] if len(fields) > 49 else code) or code
            name = str(name).replace('"', '')
            return {
                'code': code,
                'name': name,
                'open': fields[0],
                'high': fields[1],
                'low': fields[2],
                'price': fields[3],
                'yestclose': fields[13] if len(fields) > 13 else '',
                'volume': fields[4] if len(fields) > 4 else '',
                'time': fields[37] if len(fields) > 37 else '',
            }
        # 普通国内期货：名称,时间,开,高,低,持仓?,结算?,最新?,...,昨结,...
        return {
            'code': code,
            'name': fields[0],
            'open': fields[2] if len(fields) > 2 else '',
            'high': fields[3] if len(fields) > 3 else '',
            'low': fields[4] if len(fields) > 4 else '',
            'price': fields[8] if len(fields) > 8 else '',
            'yestclose': fields[10] if len(fields) > 10 else '',
            'volume': fields[14] if len(fields) > 14 else '',
            'time': fields[1] if len(fields) > 1 else '',
        }
    if code.startswith('hf_'):
        # 海外期货：开高低等位置与国内不同，原逻辑保留"价格异常时回退到 fields[2]"
        price = fields[0]
        try:
            if float(price) > float(fields[3]) or float(price) < float(fields[2]):
                price = fields[2]
        except (ValueError, IndexError):
            pass
        name = (fields[13] if len(fields) > 13 else code) or code
        name = str(name).rstrip('"')
        volume = (str(fields[14]).replace('"', '') if len(fields) >= 15 else '0')
        return {
            'code': code,
            'name': name,
            'open': fields[8] if len(fields) > 8 else '',
            'high': fields[4] if len(fields) > 4 else '',
            'low': fields[5] if len(fields) > 5 else '',
            'price': price,
            'yestclose': fields[7] if len(fields) > 7 else '',
            'volume': volume,
            'time': fields[6] if len(fields) > 6 else '',
        }
    if re.match(r'^(sh|sz|bj)\d', code):
        # A 股/指数：名称,今开,昨收,现价,最高,最低,买一,卖一,成交量,成交额,...,日期,时间
        return {
            'code': code,
            'name': fields[0],
            'open': fields[1] if len(fields) > 1 else '',
            'yestclose': fields[2] if len(fields) > 2 else '',
            'price': fields[3] if len(fields) > 3 else '',
            'high': fields[4] if len(fields) > 4 else '',
            'low': fields[5] if len(fields) > 5 else '',
            'volume': fields[8] if len(fields) > 8 else '',
            'time': fields[31] if len(fields) > 31 else '',
        }
    if code.startswith('rt_hk') or code.startswith('hk'):
        # 港股：英文名,中文名,今开,昨收,最高,最低,现价,涨跌额,涨跌幅,...,成交量,成交额,...,日期,时间
        # 服务器已对港股加 rt_ 前缀取实时数据，此处归一化回 hkXXX 与看盘代码对齐
        hk_code = re.sub(r'^rt_', '', code)
        return {
            'code': hk_code,
            'name': fields[1] if len(fields) > 1 else '',
            'open': fields[2] if len(fields) > 2 else '',
            'yestclose': fields[3] if len(fields) > 3 else '',
            'high': fields[4] if len(fields) > 4 else '',
            'low': fields[5] if len(fields) > 5 else '',
            'price': fields[6] if len(fields) > 6 else '',
            'volume': fields[11] if len(fields) > 11 else '',
            'time': fields[18] if len(fields) > 18 else '',
        }
    return None


def _parse_hq_text(text: str) -> list[dict]:
    """解析整个 hq.sinajs.cn 响应为统一结构的行情数组。"""
    out: list[dict] = []
    for line in (text or '').splitlines():
        m = _HQ_LINE_RE.search(line)
        if not m:
            continue
        code, raw = m.group(1), m.group(2)
        fields = raw.split(',')
        item = _parse_hq_item(code, fields)
        if item is not None:
            out.append(item)
    return out


def _parse_suggest_text(text: str) -> list[dict]:
    """解析 suggest3.sinajs.cn 返回的 `var suggest_value="..."` 文本。"""
    s = str(text or '')
    start = s.find('="') + 2
    end = s.rfind('"')
    if start < 2 or end <= start:
        return []
    body = s[start:end]
    if not body:
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for item in body.split(';'):
        a = item.split(',')
        if len(a) < 8:
            continue
        market = a[1]
        code = (a[3] or '').upper()
        if not code:
            continue
        if market == '85' or market == '88':
            final_code = 'nf_' + code
        elif market == '86':
            final_code = 'hf_' + code
        else:
            continue
        if final_code in seen:
            continue
        seen.add(final_code)
        out.append({
            'code': final_code,
            'name': a[0] or a[4],
            'market': '海外' if market == '86' else '国内',
        })
    return out[:20]


# JSONP `var t=<payload>;` —— 服务端剥壳，转纯 JSON 数组
_JSONP_RE = re.compile(r'var\s+t\s*=\s*(.*?)\s*;?\s*$', re.DOTALL)


def _parse_jsonp_payload(text: str) -> list | None:
    """从 `var t=(...);` 文本中提取 JSON 数组。失败或为 null 返回 None。

    新浪 JSONP 实际是 `var t=(<JSON>);` —— 外层圆括号是 JS 表达式分组，
    不是合法 JSON。需要剥掉再喂给 json.loads。
    """
    s = (text or '').strip()
    if not s:
        return None
    m = _JSONP_RE.search(s)
    if not m:
        return None
    payload = m.group(1).strip()
    if payload == 'null' or not payload:
        return None
    # 剥外层 JS 表达式括号：`var t=([[...]]);` -> `[[...]]`
    if payload.startswith('(') and payload.endswith(')'):
        payload = payload[1:-1].strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


# ========== HTTP 工具 ==========

def sina_get(host: str, path: str) -> str:
    """转发新浪接口，返回 utf8 文本。失败抛 HTTPException。"""
    url = 'https://' + host + path
    req = urllib.request.Request(url, headers={
        'Referer': REFERER,
        'User-Agent': UA,
        'Accept': '*/*',
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            buf = resp.read()
    except socket.timeout:
        raise HTTPException(status_code=504, detail='请求超时')
    except Exception as e:
        raise HTTPException(status_code=502, detail='代理请求失败: ' + str(e))

    try:
        return buf.decode('gb18030')  # 新浪返回 GB18030
    except Exception:
        return buf.decode('utf-8', errors='replace')


# ========== 4 个接口（全部返回结构化 JSON） ==========

@router.get('/api/futures')
def futures(codes: str = ''):
    """实时行情：/api/futures?codes=nf_IF0,hf_OIL,sh600519

    返回：
      {
        "items": [
          {"code":"nf_RB0","name":"螺纹钢","open":...,"high":...,"low":...,
           "price":...,"yestclose":...,"volume":...,"time":...},
          ...
        ]
      }
    """
    if not codes:
        raise HTTPException(status_code=400, detail='缺少 codes 参数')
    items: list[str] = []
    for c in codes.split(','):
        s = c.strip()
        if not s:
            continue
        # 港股（hk 开头）需加 rt_ 前缀才能拿到实时行情，否则约 15 分钟延迟
        if s.lower().startswith('hk'):
            s = 'rt_' + s
        items.append(urllib.parse.quote(s))
    text = sina_get('hq.sinajs.cn', '/list=' + ','.join(items))
    if 'FAILED' in text:
        raise HTTPException(status_code=502, detail='新浪返回 FAILED')
    return JSONResponse({'items': _parse_hq_text(text)})


@router.get('/api/futures/suggest')
def futures_suggest(key: str = ''):
    """期货搜索联想：/api/futures/suggest?key=铜

    返回：{"items":[{"code":"nf_RB0","name":"螺纹钢","market":"国内"}, ...]}
    """
    if not key:
        raise HTTPException(status_code=400, detail='缺少 key 参数')
    path = '/suggest/type=85,86,88&key=' + urllib.parse.quote(key)
    text = sina_get('suggest3.sinajs.cn', path)
    return JSONResponse({'items': _parse_suggest_text(text)})


@router.get('/api/futures/minline')
def futures_minline(symbol: str = ''):
    """国内期货分时：/api/futures/minline?symbol=RB0

    返回：{"symbol":"RB0","data": [[time, price, avg, vol, ...], ...]}
          新浪无数据时 data 为 null。
    """
    if not symbol:
        raise HTTPException(status_code=400, detail='缺少 symbol 参数')
    path = '/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getMinLine?symbol=' + urllib.parse.quote(symbol)
    text = sina_get('stock2.finance.sina.com.cn', path)
    return JSONResponse({'symbol': symbol, 'data': _parse_jsonp_payload(text)})


@router.get('/api/futures/dailykline')
def futures_dailykline(symbol: str = ''):
    """国内期货日K：/api/futures/dailykline?symbol=RB0

    返回：{"symbol":"RB0","data": [[date, open, high, low, close, volume, ...], ...]}
          新浪无数据时 data 为 null。
    """
    if not symbol:
        raise HTTPException(status_code=400, detail='缺少 symbol 参数')
    path = '/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getDailyKLine?symbol=' + urllib.parse.quote(symbol)
    text = sina_get('stock2.finance.sina.com.cn', path)
    return JSONResponse({'symbol': symbol, 'data': _parse_jsonp_payload(text)})