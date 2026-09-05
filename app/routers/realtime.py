#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realtime.py —— 实时行情接口（从网页抓取，新浪源），全部返回 JSON

范围仅限「A股证券(sh/sz/bj，含沪深指数) + 国内期货(nf_)」，
海外期货(hf_)、港股(hk) 已下线，报价与联想均不再返回。

  GET /api/futures            实时报价（公开）
  GET /api/futures/suggest    搜索联想（公开，仅 A股 + 国内期货）
  GET /api/futures/minline    当日分时（公开）
  GET /api/futures/dailykline 日K图数据（公开，实时向新浪取）

历史行情 / 品种信息等「读数据库」接口见 history.py。
"""
import re
import urllib.parse

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.fetchutils import parse_jsonp, sina_get

router = APIRouter(tags=["realtime"])

_HQ_LINE_RE = re.compile(r'var\s+hq_str_([A-Za-z0-9_$.]+?)="(.*?)"\s*;?', re.DOTALL)

# 股指期货/国债期货识别
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
    # 海外期货(hf_)/港股(hk/rt_hk)已下线：不在此解析，返回 None 即被跳过
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
    """解析 suggest3.sinajs.cn 返回的 `var suggest_value="..."` 文本。

    包装层：只保留 A股证券（市场 11，代码自带 sh/sz/bj 前缀）与
    国内期货（市场 85/88 → nf_）；海外期货/现货（市场 86）、基金、
    债券等其他市场一律丢弃。
    """
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
        if len(a) < 5:
            continue
        market = a[1]
        raw = (a[3] or '').strip()
        if not raw:
            continue
        if market in ('85', '88'):
            # 国内期货：cu0 -> nf_CU0
            code = raw.upper()
            final_code = code if code.startswith('NF_') else 'nf_' + code
            mk = '期货'
        elif market == '11':
            # A股证券/指数：600519(sh600519) / 000300(sh000300)，个别条目带 sh 前缀
            code = raw.lower()
            if re.fullmatch(r'(sh|sz|bj)\d{6}', code):
                final_code = code
            elif re.fullmatch(r'\d{6}', code):
                # 兜底：没带交易所前缀时按首位数推断 6/5->沪、0/3->深、其余->北
                head = code[0]
                final_code = ('sh' if head in '56' else 'sz' if head in '03' else 'bj') + code
            else:
                continue
            mk = 'A股'
        else:
            # 海外期货(86)/基金/债券等：全部丢弃
            continue
        if final_code in seen:
            continue
        seen.add(final_code)
        # a[4] 通常是规范全称（期货 a[0] 同 a[4]；指数条目 a[0] 可能只是代码）
        name = (a[4] or a[0] or '').strip() or final_code
        out.append({'code': final_code, 'name': name, 'market': mk})
    return out[:20]


# =====================================================================
# 三、futures_base 合约库刷新（原 refresh_tradable_futures.py 的逻辑）
# =====================================================================

# 交易所分组 -> 统一代码

@router.get('/api/futures')
def futures(codes: str = ''):
    """实时行情：/api/futures?codes=nf_IF0,sh600519,sh000300

    仅支持 A股证券(sh/sz/bj) 与国内期货(nf_)；海外期货(hf_)/港股(hk)
    不在支持范围，传入也会被跳过、不出现在 items 里。

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
        items.append(urllib.parse.quote(s))
    text = sina_get('hq.sinajs.cn', '/list=' + ','.join(items))
    if 'FAILED' in text:
        raise HTTPException(status_code=502, detail='新浪返回 FAILED')
    return JSONResponse({'items': _parse_hq_text(text)})


@router.get('/api/futures/suggest')
def futures_suggest(key: str = ''):
    """搜索联想（仅 A股 + 国内期货）：/api/futures/suggest?key=黄金

    服务端只请求 11(A股证券)/85/88(国内期货) 三类市场，并把结果再次
    过滤，保证不会返回海外期货(hf_)/港股(hk) 等代码。

    返回：{"items":[{"code":"sh600519","name":"贵州茅台","market":"A股"},
                   {"code":"nf_AU0","name":"黄金连续","market":"期货"}, ...]}
    """
    if not key:
        raise HTTPException(status_code=400, detail='缺少 key 参数')
    path = '/suggest/type=11,85,88&key=' + urllib.parse.quote(key)
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
    path = ('/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getMinLine'
            '?symbol=' + urllib.parse.quote(symbol))
    text = sina_get('stock2.finance.sina.com.cn', path)
    return JSONResponse({'symbol': symbol, 'data': parse_jsonp(text)})


@router.get('/api/futures/dailykline')
def futures_dailykline(symbol: str = ''):
    """国内期货日K：/api/futures/dailykline?symbol=RB0

    返回：{"symbol":"RB0","data": [[date, open, high, low, close, volume, ...], ...]}
          新浪无数据时 data 为 null。
    """
    if not symbol:
        raise HTTPException(status_code=400, detail='缺少 symbol 参数')
    path = ('/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getDailyKLine'
            '?symbol=' + urllib.parse.quote(symbol))
    text = sina_get('stock2.finance.sina.com.cn', path)
    return JSONResponse({'symbol': symbol, 'data': parse_jsonp(text)})



