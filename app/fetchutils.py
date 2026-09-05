#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetchutils.py —— 抓数公共底层（非接口文件，realtime/history/维护脚本共用）

新浪 HTTP 工具 + JSONP 剥壳 + 品种/合约常量与乘数字典 + 日K URL。
"""
import json
import re
import socket
import urllib.request

from fastapi import HTTPException

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
REFERER = 'http://finance.sina.com.cn/'
TIMEOUT = 15     # 单次请求超时（秒）
SLEEP = 0.3      # 批量抓取请求间隔，避免新浪封 IP

# =====================================================================
# 一、HTTP 工具（公共：三处旧代码各写一遍的 urlopen/UA/Referer 收拢于此）
# =====================================================================



def http_get(url: str, enc: str = 'utf-8', timeout: int = TIMEOUT) -> str:
    """带 Referer/UA 的 GET，返回按 enc 解码后的文本。失败抛 HTTPException。"""
    req = urllib.request.Request(url, headers={
        'Referer': REFERER,
        'User-Agent': UA,
        'Accept': '*/*',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buf = resp.read()
    except socket.timeout:
        raise HTTPException(status_code=504, detail='请求超时')
    except Exception as e:
        raise HTTPException(status_code=502, detail='代理请求失败: ' + str(e))
    return buf.decode(enc, errors='replace')


def sina_get(host: str, path: str, enc: str = 'gb18030') -> str:
    """转发新浪 https 接口（hq.sinajs.cn 等）。新浪默认 GB18030 编码。"""
    return http_get('https://' + host + path, enc=enc)


# JSONP `var xxx=<payload>;` —— 服务端剥壳，转纯 JSON 数组/对象
_JSONP_VAR_RE = re.compile(r'var\s+[A-Za-z_]\w*\s*=\s*(.*?)\s*;?\s*$', re.DOTALL)


def parse_jsonp(text: str):
    """从 `var t=(...);` 文本中提取 JSON。失败或为 null 返回 None。

    新浪 JSONP 实际是 `var t=(<JSON>);` —— 外层圆括号是 JS 表达式分组，
    不是合法 JSON。需要剥掉再喂给 json.loads。
    """
    s = (text or '').strip()
    if not s:
        return None
    m = _JSONP_VAR_RE.search(s)
    if not m:
        return None
    payload = m.group(1).strip()
    if not payload or payload == 'null':
        return None
    # 剥外层 JS 表达式括号：`var t=([[...]]);` -> `[[...]]`
    if payload.startswith('(') and payload.endswith(')'):
        payload = payload[1:-1].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


# =====================================================================
# 二、实时行情 / 联想 解析（新浪原始格式 -> 统一 JSON 结构）
# =====================================================================

# hq.sinajs.cn 单行：var hq_str_<code>="f1,f2,...,fN";

EXCHANGE_MAP = {
    'czce': 'CZCE',   # 郑州商品交易所
    'dce': 'DCE',     # 大连商品交易所
    'shfe': 'SHFE',   # 上海期货交易所（含上海国际能源 INE 品种）
    'cffex': 'CFFEX', # 中国金融期货交易所（股指/国债）
    'gfex': 'GFEX',   # 广州期货交易所
}

NODE_LIST_URL = 'http://vip.stock.finance.sina.com.cn/quotes_service/view/js/qihuohangqing.js'
CONTRACT_URL = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
                'Market_Center.getHQFuturesData?page=1&sort=position&asc=0&node={node}&base=futures')

# 品种乘数字典：underlying -> (multiplier, tick_size)
# 每点价值（合约乘数，元/点）。新品种（铂/钯）暂 None，前端可手动补。
MULTIPLIERS: dict[str, tuple[float | None, float | None]] = {
    # ---- SHFE 上期所（含 INE） ----
    "RB": (10, 1),     # 螺纹钢 10吨/手
    "HC": (10, 1),     # 热轧卷板
    "WR": (10, 1),     # 线材
    "CU": (5, 10),     # 铜
    "AL": (5, 5),      # 铝
    "ZN": (5, 5),      # 锌
    "PB": (5, 5),      # 铅
    "SN": (1, 10),     # 锡
    "NI": (1, 10),     # 镍
    "AU": (1000, 0.02),  # 黄金 1000克/手
    "AG": (15, 1),     # 白银 15千克/手
    "RU": (10, 5),     # 橡胶
    "BU": (10, 1),     # 沥青
    "FU": (10, 1),     # 燃料油
    "SP": (10, 2),     # 纸浆
    "SS": (5, 5),      # 不锈钢
    "AO": (20, 1),     # 氧化铝
    "NR": (10, 5),     # 20号胶
    "LU": (10, 1),     # 低硫燃料油
    "BC": (5, 10),     # 国际铜
    "BR": (5, 5),      # 丁二烯橡胶
    "EC": (50, 0.1),   # 集运指数 50元/点
    "AD": (5, 5),      # 铸造铝合金
    "OP": (5, 1),      # 胶版印刷纸
    "SC": (1000, 0.1), # 原油(INE) 1000桶/手
    # ---- DCE 大商所 ----
    "M": (10, 1),      # 豆粕
    "A": (10, 1),      # 豆一
    "B": (10, 1),      # 豆二
    "Y": (10, 2),      # 豆油
    "P": (10, 2),      # 棕榈油
    "C": (10, 1),      # 玉米
    "CS": (10, 1),     # 玉米淀粉
    "JD": (10, 1),     # 鸡蛋 5吨/手(元/500kg)
    "LH": (16, 5),     # 生猪 16吨/手
    "I": (100, 0.5),   # 铁矿石 100吨/手
    "J": (100, 0.5),   # 焦炭
    "JM": (60, 0.5),   # 焦煤
    "L": (5, 1),       # 塑料(LLDPE)
    "PP": (5, 1),      # 聚丙烯
    "V": (5, 1),       # PVC
    "EG": (10, 1),     # 乙二醇
    "EB": (10, 1),     # 苯乙烯
    "PG": (20, 1),     # LPG
    "RR": (10, 1),     # 粳米
    "FB": (500, 0.05), # 纤维板 500张/手
    "BB": (500, 0.05), # 胶合板 500张/手
    "LG": (90, 0.5),   # 原木 90立方米/手
    "BZ": (5, 1),      # 纯苯
    # ---- CZCE 郑商所 ----
    "CF": (5, 5),      # 棉花
    "CY": (5, 5),      # 棉纱
    "AP": (10, 1),     # 苹果
    "CJ": (5, 5),      # 红枣 5吨/手
    "RM": (10, 1),     # 菜粕
    "OI": (10, 2),     # 菜油
    "RS": (10, 1),     # 菜籽
    "TA": (5, 2),      # PTA
    "MA": (10, 1),     # 甲醇
    "UR": (20, 1),     # 尿素
    "SA": (20, 1),     # 纯碱
    "SR": (10, 1),     # 白糖
    "SF": (5, 2),      # 硅铁
    "SM": (5, 2),      # 锰硅
    "FG": (20, 1),     # 玻璃
    "SH": (30, 1),     # 烧碱
    "PF": (5, 2),      # 短纤
    "PK": (5, 2),      # 花生
    "PX": (5, 2),      # 二甲苯
    "PR": (15, 2),     # 瓶片
    "PL": (5, 1),      # 丙烯
    "WH": (20, 1),     # 强麦
    "JR": (20, 1),     # 粳稻
    "RI": (20, 1),     # 早籼稻
    "LR": (20, 1),     # 晚籼稻
    "ZC": (100, 0.2),  # 动力煤
    # ---- CFFEX 中金所 ----
    "IF": (300, 0.2),     # 沪深300
    "IH": (300, 0.2),     # 上证50
    "IC": (200, 0.2),     # 中证500
    "IM": (200, 0.2),     # 中证1000
    "TF": (10000, 0.005), # 5年期国债
    "T": (10000, 0.005),  # 10年期国债
    "TS": (20000, 0.005), # 2年期国债
    # ---- GFEX 广期所 ----
    "SI": (5, 5),      # 工业硅
    "LC": (1, 50),     # 碳酸锂
    "PS": (3, 5),      # 多晶硅
    "PT": (None, None), # 铂（新上市，规格待确认）
    "PD": (None, None), # 钯（新上市，规格待确认）
}

# 具体合约判定：字母 + 4位年月，如 RB2701 / IF2609
_CONTRACT_RE = re.compile(r'^([A-Za-z]+)(\d{4})$')



KLINE_URL = ('https://stock2.finance.sina.com.cn/futures/api/jsonp.php/'
             'var%20t=/InnerFuturesNewService.getDailyKLine?symbol={symbol}')
BATCH_SIZE = 500  # 每批 upsert 行数



