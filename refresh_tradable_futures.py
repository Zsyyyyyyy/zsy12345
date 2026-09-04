#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh_tradable_futures.py —— 刷新国内期货「真实合约」字典（幂等 upsert）

不再手工维护品种 + 可交割月份，而是每天从新浪拉取当前挂牌的全部真实合约
（如 nf_RB2701、nf_AU2612），网页「新增持仓」只显示这些真实存在的合约。

数据来源（零第三方依赖，仅标准库）：
  1) 品种 → node 映射：
     http://vip.stock.finance.sina.com.cn/quotes_service/view/js/qihuohangqing.js
     返回 ARRFUTURESNODES = { czce:[[中文名, node, ...], ...], dce:..., shfe:..., cffex:..., gfex:... }
  2) 某品种全部挂牌合约行情：
     https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQFuturesData
       ?page=1&sort=position&asc=0&node=<node>&base=futures
     返回 [{symbol:"RB2701", name:"螺纹钢2701", ...}, ...]

只保留「具体合约」（symbol 形如 字母+4位年月，如 RB2701），
跳过连续/主力合约（RB0）。品种乘数（multiplier）按内置 MULTIPLIERS 字典填充，
少数新上市品种（如铂/钯）multiplier 暂为 None，前端可手动补。

用法：
    venv/bin/python refresh_tradable_futures.py            # 拉取 + upsert
    venv/bin/python refresh_tradable_futures.py --dry-run  # 只看不写
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select as sa_select
from app.core.database import SessionLocal, engine, Base
from app.models import TradableFuture

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
REFERER = 'http://finance.sina.com.cn/'
TIMEOUT = 15
SLEEP = 0.3  # 请求间隔，避免新浪封 IP

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

# ========== 品种乘数字典：underlying -> (multiplier, tick_size) ==========
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
    "EB": (5, 1),      # 苯乙烯
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

# 连续合约判定：symbol 形如 RB0 / IF0 / SC0（字母 + 单个 0）
_CONTINUOUS_RE = re.compile(r'^[A-Za-z]+0$')
# 具体合约判定：字母 + 4位年月，如 RB2701 / IF2609
_CONTRACT_RE = re.compile(r'^([A-Za-z]+)(\d{4})$')


def http_get(url: str, enc: str = 'utf-8') -> str:
    req = urllib.request.Request(url, headers={
        'Referer': REFERER, 'User-Agent': UA, 'Accept': '*/*',
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode(enc, errors='replace')


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


def upsert(db, code: str, symbol: str, name: str, underlying: str,
           underlying_name: str, exchange: str, multiplier: float | None,
           tick_size: float | None, dry_run: bool) -> str:
    """插入或更新一个合约。返回 inserted/updated/unchanged。"""
    existing = db.scalar(sa_select(TradableFuture).where(TradableFuture.code == code))
    if existing is None:
        if not dry_run:
            db.add(TradableFuture(
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


def main():
    parser = argparse.ArgumentParser(description='刷新国内期货真实合约字典')
    parser.add_argument('--dry-run', action='store_true', help='只看不写')
    args = parser.parse_args()

    if not args.dry_run:
        Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        nodes = fetch_nodes()
        print(f'品种 node 数：{len(nodes)}')
        seen_codes: set[str] = set()
        inserted = updated = unchanged = skipped = failed = 0

        for cn_name, node, exchange in nodes:
            try:
                contracts = fetch_contracts(node)
            except Exception as e:
                print(f'  ✗ {cn_name:8} node={node:12} 拉取失败：{e}')
                failed += 1
                continue

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
                action = upsert(db, code, symbol, name, underlying, cn_name,
                                exchange, mult, tick, args.dry_run)
                if action == 'inserted':
                    inserted += 1
                elif action == 'updated':
                    updated += 1
                else:
                    unchanged += 1
            time.sleep(SLEEP)

        # 把本次没再出现的旧合约标记 is_active=False（合约到期下架）
        deactivated = 0
        if not args.dry_run:
            all_rows = db.scalars(sa_select(TradableFuture)).all()
            for row in all_rows:
                if row.is_active and row.code not in seen_codes:
                    row.is_active = False
                    deactivated += 1

        print('-' * 60)
        print(f'新增 {inserted} / 更新 {updated} / 未变 {unchanged} / '
              f'跳过(连续等) {skipped} / 拉取失败品种 {failed} / 下架 {deactivated}')
        if not args.dry_run:
            db.commit()
            active = len(db.scalars(
                sa_select(TradableFuture).where(TradableFuture.is_active.is_(True))
            ).all())
            print(f'✅ 完成：当前可交易合约 {active} 条')
        else:
            print('[DRY-RUN] 未提交')
    finally:
        db.close()


if __name__ == '__main__':
    main()
