#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data.py —— 全站「抓取数据」统一入口（所有对外输出均为 JSON）

把原本散在多处的抓新浪数据代码收进这一个文件：
  1) 行情代理 4 个 HTTP 接口（实时/联想/分时/日K）——原 app/routers/futures.py
  2) futures_base 合约库刷新（refresh_tradable_futures.py 薄壳脚本的逻辑）
  3) 日级历史行情入库（fetch_daily_history.py 薄壳脚本的逻辑）

对外接口一览（全部返回 JSON）：
  GET  /api/futures?codes=...                       实时行情（公开）
  GET  /api/futures/suggest?key=...                 搜索联想（公开）
  GET  /api/futures/minline?symbol=...              国内期货分时（公开）
  GET  /api/futures/dailykline?symbol=...           国内期货日K（公开）
  POST /api/futures/refresh-contracts               刷新 futures_base 在市合约（需登录，后台任务）
  POST /api/futures/fetch-history                   抓取日级历史行情入库（需登录，后台任务）
  GET  /api/futures/jobs/{job_id}                   查询后台任务进度/结果（需登录）

refresh-contracts 只做两件事：① 新浪当前挂牌中表里没有的新合约补进去；
② 本次成功抓取到的交易所里、没再出现的在市合约置 is_active=0（到期下架）。
已退市历史合约（更早年份）不进 refresh 维护，由 build_futures_base_history.py
手动探测补录。

抓新浪日K历史全量约 10 分钟，POST 只负责「开任务」立即返回 job_id；
进度与结果通过 GET /api/futures/jobs/{job_id} 轮询（内存态，服务重启即丢失）。

零第三方依赖，仅用 Python 标准库（urllib、re、json、threading）。
端点用同步 def 声明，FastAPI 会自动把同步端点丢进线程池执行，不阻塞事件循环；
耗时抓取任务再单独开守护线程跑，HTTP 层立即返回。
"""
import json
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.security import get_current_user
from app.models import FuturesBase, FuturesDailyBar, User

router = APIRouter(tags=["futures-data"])

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


# =====================================================================
# 三、futures_base 合约库刷新（原 refresh_tradable_futures.py 的逻辑）
# =====================================================================

# 交易所分组 -> 统一代码
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


def fetch_nodes() -> list[tuple[str, str, str]]:
    """拉取品种 node 映射。返回 [(中文名, node, exchange), ...]。"""
    js = http_get(NODE_LIST_URL, enc='gb2312')
    out: list[tuple[str, str, str]] = []
    for exch_key in ('czce', 'dce', 'shfe', 'cffex', 'gfex'):
        # 截取该交易所的数组段
        i = js.find(exch_key + ' :')
        if i < 0:
            i = js.find(exch_key + ':')
        if i < 0:
            continue
        seg = js[i:]
        seg = seg[seg.find('['):]
        depth = 0
        end = None
        for idx, ch in enumerate(seg):
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    end = idx
                    break
        arr = seg[:end + 1] if end is not None else seg
        for name, node in re.findall(r"\['([^']+)',\s*'([^']+)'\s*,", arr):
            if node.endswith('_qh'):
                out.append((name, node, EXCHANGE_MAP[exch_key]))
    return out


def fetch_contracts(node: str) -> list[dict]:
    """拉取某品种全部挂牌合约。返回原始 dict 列表。"""
    return json.loads(http_get(CONTRACT_URL.format(node=node)))


def _upsert_contract(db: Session, code: str, symbol: str, name: str, underlying: str,
                     underlying_name: str, exchange: str, multiplier: float | None,
                     tick_size: float | None, dry_run: bool) -> str:
    """插入或更新一个合约。返回 inserted/updated/unchanged。"""
    existing = db.scalar(select(FuturesBase).where(FuturesBase.code == code))
    if existing is None:
        if not dry_run:
            db.add(FuturesBase(
                code=code, symbol=symbol, name=name, underlying=underlying,
                underlying_name=underlying_name, exchange=exchange,
                multiplier=multiplier, tick_size=tick_size, is_active=True,
            ))
        return 'inserted'
    changed = False
    for attr, val in [
        ('symbol', symbol), ('name', name), ('underlying', underlying),
        ('underlying_name', underlying_name), ('exchange', exchange),
        ('multiplier', multiplier), ('tick_size', tick_size),
        ('is_active', True),
    ]:
        if getattr(existing, attr) != val:
            if not dry_run:
                setattr(existing, attr, val)
            changed = True
    return 'updated' if changed else 'unchanged'


def refresh_contracts(db: Session, dry_run: bool = False, log=None) -> dict:
    """刷新 futures_base 在市合约（新浪当前挂牌，幂等 upsert）。

    只做两件事：
      ① 把当前挂牌、表里还没有的新合约补进去（is_active=1）；
      ② 把「本次成功抓取到的交易所」里没再出现的在市合约置 is_active=0
         （到期下架）。某个交易所本次拉取失败时不下架其合约，避免误杀。
    已退市的历史合约（更早年份）不在此维护，由 build_futures_base_history.py 补录。

    log 为可调用对象（接收一行文本）或 None（静默，供 --json / 后台任务用）。
    返回统计 dict（可直接 JSON 序列化）。
    """
    def say(msg: str) -> None:
        if log is not None:
            log(msg)

    nodes = fetch_nodes()
    say(f'品种 node 数：{len(nodes)}')
    seen_codes: set[str] = set()
    scanned_exchanges: set[str] = set()
    inserted = updated = unchanged = skipped = failed = 0

    for cn_name, node, exchange in nodes:
        try:
            contracts = fetch_contracts(node)
        except Exception as e:
            say(f'  ✗ {cn_name:8} node={node:12} 拉取失败：{e}')
            failed += 1
            continue
        scanned_exchanges.add(exchange)

        for c in contracts:
            symbol = (c.get('symbol') or '').upper()
            m = _CONTRACT_RE.match(symbol)
            if not m:
                # 连续合约（RB0）或异常 symbol，跳过
                skipped += 1
                continue
            underlying = m.group(1)
            code = 'nf_' + symbol
            seen_codes.add(code)
            name = c.get('name') or ''
            # 新浪把「10 月合约」（如 AU2610）的 name 误标为「连续」，
            # 修正为「品种中文名 + 年月」
            if name.endswith('连续'):
                name = f'{cn_name}{m.group(2)}'
            mult, tick = MULTIPLIERS.get(underlying, (None, None))
            action = _upsert_contract(db, code, symbol, name, underlying, cn_name,
                                      exchange, mult, tick, dry_run)
            if action == 'inserted':
                inserted += 1
            elif action == 'updated':
                updated += 1
            else:
                unchanged += 1
        time.sleep(SLEEP)

    # 把「本次成功抓取到的交易所」里没再出现的在市合约置 is_active=0（到期下架）
    deactivated = 0
    active: int | None = None
    if not dry_run:
        all_rows = db.scalars(select(FuturesBase)).all()
        for row in all_rows:
            if (row.is_active and row.code not in seen_codes
                    and row.exchange in scanned_exchanges):
                row.is_active = False
                deactivated += 1
        db.commit()
        active = len(db.scalars(
            select(FuturesBase).where(FuturesBase.is_active.is_(True))
        ).all())

    say('-' * 60)
    say(f'新增 {inserted} / 更新 {updated} / 未变 {unchanged} / '
        f'跳过(连续等) {skipped} / 拉取失败品种 {failed} / 下架 {deactivated}')
    if not dry_run:
        say(f'✅ 完成：当前可交易合约 {active} 条')
    else:
        say('[DRY-RUN] 未提交')
    return {
        'dry_run': dry_run, 'nodes': len(nodes),
        'inserted': inserted, 'updated': updated, 'unchanged': unchanged,
        'skipped': skipped, 'failed': failed, 'deactivated': deactivated,
        'active': active,
    }


# =====================================================================
# 四、日级历史行情入库（原 fetch_daily_history.py 的逻辑）
# =====================================================================

KLINE_URL = ('https://stock2.finance.sina.com.cn/futures/api/jsonp.php/'
             'var%20t=/InnerFuturesNewService.getDailyKLine?symbol={symbol}')
BATCH_SIZE = 500  # 每批 upsert 行数


def contract_month_of(symbol: str) -> date | None:
    """从合约代码解析所属交割月份。RB2701 → 2027-01-01；无法解析返回 None。

    仅支持当前通行的 4 位年月编码；郑商所 2019 年前的 3 位老编码（如 AP901）不适用。
    """
    m = _CONTRACT_RE.match(symbol.upper())
    if not m:
        return None
    yymm = m.group(2)
    year, month = 2000 + int(yymm[:2]), int(yymm[2:])
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def _to_float(s) -> float | None:
    try:
        v = float(s)
        return v if v == v else None  # 过滤 NaN
    except (TypeError, ValueError):
        return None


def _to_int(s) -> int | None:
    f = _to_float(s)
    return None if f is None else int(f)


def _parse_kline_rows(symbol: str, data: list[dict]) -> list[dict]:
    """新浪日K原始行 -> 表行。过滤无日期/无价格的脏行。"""
    month = contract_month_of(symbol)
    rows = []
    for d in data:
        day = (d.get('d') or '').strip()
        try:
            trade_date = date.fromisoformat(day)
        except ValueError:
            continue
        close = _to_float(d.get('c'))
        if close is None or close <= 0:
            continue  # 无成交的老合约日K可能是全 0 占位
        rows.append({
            'symbol': symbol,
            'trade_date': trade_date,
            'contract_month': month,
            'open_price': _to_float(d.get('o')),
            'high': _to_float(d.get('h')),
            'low': _to_float(d.get('l')),
            'close': close,
            'volume': _to_int(d.get('v')),
            'open_interest': _to_int(d.get('p')),
            # 结算价：0 视为缺失（早期数据新浪填 0）
            'settlement': (_to_float(d.get('s')) or None),
        })
    return rows


def _upsert_daily_bars(db: Session, rows: list[dict], dry_run: bool) -> int:
    """MySQL 批量 upsert（ON DUPLICATE KEY UPDATE）。返回写入行数。"""
    if not rows or dry_run:
        return len(rows) if dry_run else 0
    written = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        stmt = mysql_insert(FuturesDailyBar).values(batch)
        stmt = stmt.on_duplicate_key_update(
            contract_month=stmt.inserted.contract_month,
            open_price=stmt.inserted.open_price,
            high=stmt.inserted.high,
            low=stmt.inserted.low,
            close=stmt.inserted.close,
            volume=stmt.inserted.volume,
            open_interest=stmt.inserted.open_interest,
            settlement=stmt.inserted.settlement,
        )
        db.execute(stmt)
        written += len(batch)
    db.commit()
    return written


def _last_date_of(db: Session, symbol: str) -> date | None:
    return db.scalar(
        select(func.max(FuturesDailyBar.trade_date))
        .where(FuturesDailyBar.symbol == symbol)
    )


def _fetch_symbol(db: Session, symbol: str, dry_run: bool, full_refresh: bool) -> tuple[int, int, str]:
    """抓一个 symbol 的日K并入库。返回 (新增+更新行数, 总行数, 状态)。"""
    raw = http_get(KLINE_URL.format(symbol=urllib.parse.quote(symbol)), enc='utf-8')
    data = parse_jsonp(raw)
    if not isinstance(data, list):
        return 0, 0, 'no-data'
    rows = _parse_kline_rows(symbol, data)
    if not rows:
        return 0, 0, 'no-data'

    # 增量：只 upsert 本地最大交易日及之后的行（覆盖当日盘中写入的半根K）
    if not full_refresh:
        last = _last_date_of(db, symbol)
        if last is not None:
            rows = [r for r in rows if r['trade_date'] >= last]
    written = _upsert_daily_bars(db, rows, dry_run)
    return written, len(rows), 'ok'


def run_fetch_history(db: Session, symbols: list[str] | None = None, active_only: bool = True,
                      limit: int = 0, full_refresh: bool = False, sleep_s: float = 0.3,
                      dry_run: bool = False, log=None) -> dict:
    """抓取国内期货具体合约日级历史行情入库（幂等、可增量）。

    symbols 为 None 时默认取 futures_base 表全部具体合约（含已下架）。
    log 为可调用对象或 None（静默，供 --json / 后台任务用）。返回统计 dict。
    """
    def say(msg: str) -> None:
        if log is not None:
            log(msg)

    # ---- 组装 symbol 清单 ----
    if symbols is None:
        q = (select(FuturesBase.symbol, FuturesBase.name, FuturesBase.exchange)
             .order_by(FuturesBase.symbol))
        if active_only:
            q = q.where(FuturesBase.is_active.is_(True))
        contract_rows = db.execute(q).all()
        if not contract_rows:
            msg = ('⚠ futures_base 表为空，请先运行：'
                   'venv/bin/python refresh_tradable_futures.py')
            say(msg)
            return {'ok': False, 'reason': 'futures_base 表为空，请先刷新合约清单', 'written': 0}
        if limit:
            contract_rows = contract_rows[:limit]
        symbols = [r.symbol for r in contract_rows]
        say(f'合约数：{len(contract_rows)}')
        for r in contract_rows[:10]:
            say(f'  {r.symbol:10} {r.name} [{r.exchange}]')
        if len(contract_rows) > 10:
            say(f'  ... 等共 {len(contract_rows)} 个')

    total_written = total_rows = ok = no_data = failed = 0
    t0 = time.time()
    for i, symbol in enumerate(symbols, 1):
        try:
            written, nrows, status = _fetch_symbol(db, symbol, dry_run, full_refresh)
            total_written += written
            total_rows += nrows
            if status == 'ok':
                ok += 1
            else:
                no_data += 1
            say(f'[{i}/{len(symbols)}] {symbol:10} {status:8} 抓到 {nrows:5} 行，写入 {written:5} 行')
        except Exception as e:
            failed += 1
            say(f'[{i}/{len(symbols)}] {symbol:10} ✗ 失败：{e}')
        time.sleep(sleep_s)

    say('-' * 60)
    say(f'✅ 完成：成功 {ok} / 无数据 {no_data} / 失败 {failed}，'
        f'共写入 {total_written} 行，耗时 {time.time() - t0:.1f}s')
    if dry_run:
        say('[DRY-RUN] 未写库')
    return {
        'ok': True, 'symbols': len(symbols), 'succeeded': ok, 'no_data': no_data,
        'failed': failed, 'total_rows': total_rows, 'total_written': total_written,
        'elapsed_sec': round(time.time() - t0, 1), 'dry_run': dry_run,
    }


# =====================================================================
# 五、后台任务（耗时抓取通过 POST 开任务立即返回 job_id，进度轮询）
# =====================================================================

JOBS: dict[str, dict] = {}
_BJ_TZ = timezone(timedelta(hours=8))
_JOB_LOG_CAP = 300  # 每个任务保留最近多少行日志（防内存膨胀）


def _now_bj() -> str:
    return datetime.now(_BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')


def _append_job_log(record: dict, msg: str) -> None:
    record['lines'].append(msg)
    if len(record['lines']) > _JOB_LOG_CAP:
        record['lines'] = record['lines'][-_JOB_LOG_CAP:]


def _run_job(job_id: str) -> None:
    """后台线程：执行任务并回写结果。注意用独立 Session，勿复用请求的 db。"""
    record = JOBS.get(job_id)
    db = SessionLocal()
    try:
        kind, params = record['kind'], record['params']
        log = lambda m: _append_job_log(record, m)  # noqa: E731
        if kind == 'refresh_contracts':
            result = refresh_contracts(db, dry_run=params.get('dry_run', False), log=log)
        elif kind == 'fetch_history':
            result = run_fetch_history(
                db,
                symbols=params.get('symbols'),
                active_only=params.get('active_only', True),
                limit=params.get('limit', 0),
                full_refresh=params.get('full_refresh', False),
                sleep_s=params.get('sleep', SLEEP),
                dry_run=params.get('dry_run', False),
                log=log,
            )
        else:
            raise ValueError('未知任务类型: ' + str(kind))
        record['result'] = result
        record['status'] = 'done'
    except Exception as e:
        record['status'] = 'error'
        record['error'] = f'{type(e).__name__}: {e}'
    finally:
        record['finished_at'] = _now_bj()
        db.close()


def _start_job(kind: str, params: dict) -> str:
    """登记并启动后台任务，返回 job_id。"""
    job_id = uuid.uuid4().hex[:12]
    record = {
        'job_id': job_id,
        'kind': kind,
        'status': 'running',
        'created_at': _now_bj(),
        'finished_at': None,
        'lines': [],
        'result': None,
        'error': None,
        'params': params,
    }
    JOBS[job_id] = record
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return job_id


# =====================================================================
# 六、HTTP 端点
# =====================================================================

# ---------- 实时行情 / 联想 / 分时 / 日K（公开，纯代理转发，输出 JSON） ----------

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


# ---------- 触发抓取任务（需登录） ----------

@router.post('/api/futures/refresh-contracts')
def api_refresh_contracts(payload: dict | None = None, user: User = Depends(get_current_user)):
    """刷新期货合约库在市合约（新浪当前挂牌，幂等）。

    只添加新挂牌合约 + 把本次成功抓取交易所里没再出现的在市合约置 is_active=0；
    历史退市合约由 build_futures_base_history.py 另行补录。

    body 可选：{"dry_run": false}（true 只试跑不写库）。
    立即返回：{"ok": true, "job_id": "..."}，用 GET /api/futures/jobs/{job_id} 查进度。
    """
    dry_run = bool((payload or {}).get('dry_run'))
    job_id = _start_job('refresh_contracts', {'dry_run': dry_run})
    return {'ok': True, 'job_id': job_id, 'status': 'running'}


@router.post('/api/futures/fetch-history')
def api_fetch_history(payload: dict | None = None, user: User = Depends(get_current_user)):
    """抓取国内期货日级历史行情入库（幂等、可增量，全量约 10 分钟）。

    body 全部可选：
      {"symbols": "RB2701,RB0" | ["RB2701","RB0"],   # 缺省=futures_base 全部具体合约
       "active_only": true,     # 只抓在市合约（缺省清单模式默认 true）
       "limit": 0,              # 只处理前 N 个合约（试跑用）
       "full_refresh": false,   # 忽略增量起点全量 upsert
       "sleep": 0.3,            # 请求间隔秒数
       "dry_run": false}
    立即返回：{"ok": true, "job_id": "..."}，用 GET /api/futures/jobs/{job_id} 查进度。
    """
    p = payload or {}
    symbols = p.get('symbols')
    if isinstance(symbols, str):
        symbols = [s.strip().upper() for s in symbols.split(',') if s.strip()]
    job_id = _start_job('fetch_history', {
        'symbols': symbols,
        'active_only': bool(p.get('active_only', True)),
        'limit': int(p.get('limit', 0) or 0),
        'full_refresh': bool(p.get('full_refresh', False)),
        'sleep': float(p.get('sleep', SLEEP) or SLEEP),
        'dry_run': bool(p.get('dry_run', False)),
    })
    return {'ok': True, 'job_id': job_id, 'status': 'running'}


@router.get('/api/futures/jobs/{job_id}')
def api_job_status(job_id: str, user: User = Depends(get_current_user)):
    """查询后台抓取任务状态。

    返回：{"job_id":..., "kind":..., "status": "running|done|error",
          "created_at":..., "finished_at":..., "lines":[...最近日志], "result":..., "error":...}
    任务记录在内存中，服务重启后不可查（返回 404）。
    """
    record = JOBS.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail='任务不存在或服务已重启（任务记录在内存）')
    return {k: record[k] for k in (
        'job_id', 'kind', 'status', 'created_at', 'finished_at', 'lines', 'result', 'error'
    )}
