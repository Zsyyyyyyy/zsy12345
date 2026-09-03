#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期货行情代理路由 —— 整合进 FastAPI 主后端。

原独立服务 modules/futures/server.py 的转发逻辑原样搬到这里：
  - 补 Referer（新浪 hq.sinajs.cn 严格校验 Referer，否则 403）
  - GB18030 -> UTF-8 转码
  - 港股代码自动加 rt_ 前缀拿实时行情

前端 modules/futures/public/index.html 本来就用相对路径 /api/... 调接口，
因此后端提供同名接口后，前端无需任何改动，同源直连。

零第三方依赖，仅用 Python 标准库（urllib）。端点用同步 def 声明，
FastAPI 会自动把同步端点丢进线程池执行，不阻塞事件循环。
"""
import socket
import urllib.request
import urllib.parse

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
REFERER = 'http://finance.sina.com.cn/'
TIMEOUT = 15

router = APIRouter(tags=["futures"])


def sina_get(host, path):
    """转发新浪接口，返回 (utf8文本, 错误信息)。错误信息为 None 表示成功。"""
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
        return None, '__TIMEOUT__'
    except Exception as e:
        return None, str(e)

    try:
        text = buf.decode('gb18030')  # 新浪返回 GB18030
    except Exception:
        text = buf.decode('utf-8', errors='replace')
    return text, None


def _proxy(host, path):
    """把新浪的原始响应透传给前端（纯文本，前端自行解析）。"""
    text, err = sina_get(host, path)
    if err == '__TIMEOUT__':
        raise HTTPException(status_code=504, detail='请求超时')
    if err:
        raise HTTPException(status_code=502, detail='代理请求失败: ' + err)
    return Response(content=text, media_type='text/plain; charset=utf-8')


@router.get('/api/futures')
def futures(codes: str = ''):
    """实时行情：/api/futures?codes=nf_IF0,hf_OIL,sh600519"""
    if not codes:
        raise HTTPException(status_code=400, detail='缺少 codes 参数')
    items = []
    for c in codes.split(','):
        s = c.strip()
        if not s:
            continue
        # 港股（hk 开头）需加 rt_ 前缀才能拿到实时行情，否则约 15 分钟延迟
        if s.lower().startswith('hk'):
            s = 'rt_' + s
        items.append(urllib.parse.quote(s))
    return _proxy('hq.sinajs.cn', '/list=' + ','.join(items))


@router.get('/api/futures/suggest')
def futures_suggest(key: str = ''):
    """期货搜索联想：/api/futures/suggest?key=铜"""
    if not key:
        raise HTTPException(status_code=400, detail='缺少 key 参数')
    path = '/suggest/type=85,86,88&key=' + urllib.parse.quote(key)
    return _proxy('suggest3.sinajs.cn', path)


@router.get('/api/futures/minline')
def futures_minline(symbol: str = ''):
    """国内期货分时：/api/futures/minline?symbol=RB0"""
    if not symbol:
        raise HTTPException(status_code=400, detail='缺少 symbol 参数')
    path = '/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getMinLine?symbol=' + urllib.parse.quote(symbol)
    return _proxy('stock2.finance.sina.com.cn', path)


@router.get('/api/futures/dailykline')
def futures_dailykline(symbol: str = ''):
    """国内期货日K：/api/futures/dailykline?symbol=RB0"""
    if not symbol:
        raise HTTPException(status_code=400, detail='缺少 symbol 参数')
    path = '/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getDailyKLine?symbol=' + urllib.parse.quote(symbol)
    return _proxy('stock2.finance.sina.com.cn', path)
