#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trial_tqsdk_depth.py —— 试跑：验证 TqSdk 免费账号「日线历史」能回溯多深

只探测不写库：对每个合约拉日K，过滤无效行后打印最早/最晚一根 K 与根数。
重点看退市老合约（DCE.i1909 / DCE.i1409 / SHFE.cu1101…）能否拿到上市以来日线。

凭据：项目根目录 tq_creds.txt（第一行快期账号，第二行密码），或 TQ_USER/TQ_PASS。
用法：
    venv/bin/python trial_tqsdk_depth.py                 # 默认合约清单
    venv/bin/python trial_tqsdk_depth.py DCE.i1409       # 指定单个合约
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CRED_FILE = Path(__file__).resolve().parent / 'tq_creds.txt'

DEFAULT_SYMBOLS = [
    'SHFE.rb2610',     # 当前在市（对照）
    'DCE.i1909',       # 2019-09 退市，新浪也有 —— 对照
    'DCE.i1409',       # 2014-09 退市，新浪没有 —— 重点
    'SHFE.cu1101',     # 2011 老合约
    'KQ.m@SHFE.rb',    # 螺纹钢主力连续（参照，应到 2009）
]


def load_creds() -> tuple[str, str]:
    if CRED_FILE.exists():
        lines = [ln.strip() for ln in CRED_FILE.read_text(encoding='utf-8').splitlines() if ln.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]
    user = os.getenv('TQ_USER', '')
    pwd = os.getenv('TQ_PASS', '')
    if user and pwd:
        return user, pwd
    print('❌ 找不到凭据：请建 tq_creds.txt（第一行账号，第二行密码）或设 TQ_USER/TQ_PASS')
    sys.exit(2)


def variants(symbol: str) -> list[str]:
    out = [symbol]
    if '.' in symbol and not symbol.startswith('KQ.'):
        ex, inst = symbol.split('.', 1)
        for cand in (inst.upper(), inst.lower()):
            s2 = f'{ex}.{cand}'
            if s2 not in out:
                out.append(s2)
    return out


def fetch_valid(api, k, budget: float = 120.0):
    """等日K下载稳定，返回 (DataFrame有效行, 根数)；超时返回当前部分数据。"""
    import pandas as pd
    deadline = time.time() + budget
    prev = 0
    stable = 0.0
    last_report = 0.0
    while time.time() < deadline:
        api_wait = min(5.0, max(0.1, deadline - time.time()))
        api.wait_update(deadline=time.time() + api_wait)
        df = k.df if hasattr(k, 'df') else k
        if df is None or len(df) == 0:
            continue
        try:
            valid = df[df['datetime'] > 0]
        except Exception:
            valid = df
        n = len(valid)
        if n != prev:
            prev = n
            stable = 0.0
        else:
            stable += api_wait
        now = time.time()
        if now - last_report >= 15:
            last_report = now
            print(f'    …下载中：有效 {n} 根', flush=True)
        if n and (stable >= 12 or n >= int(getattr(k, 'target_len', 0) or 0) or n == len(df)):
            return valid, n
        if n and n >= 8000:  # 已经很多了，当满
            return valid, n
    df = k.df if hasattr(k, 'df') else k
    try:
        valid = df[df['datetime'] > 0]
    except Exception:
        valid = df if df is not None else pd.DataFrame()
    return valid, len(valid)


def main():
    symbols = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_SYMBOLS
    user, pwd = load_creds()

    from tqsdk import TqApi, TqAuth
    import pandas as pd
    print(f'登录天勤（账号: {user}）…', flush=True)
    api = TqApi(auth=TqAuth(user, pwd))
    print('登录成功。开始逐合约拉日K：\n', flush=True)

    for sym in symbols:
        got = None
        for cand in variants(sym):
            try:
                print(f'-- {sym} → 尝试 {cand} …', flush=True)
                k = api.get_kline_serial(cand, 86400, data_length=6000)
                valid, n = fetch_valid(api, k)
                if n:
                    got = (cand, valid)
                    break
                print(f'   [{cand}] 无有效行（可能合约不存在/数据未开放）', flush=True)
            except Exception as e:
                msg = str(e)
                print(f'   [{cand}] 拉取异常：{type(e).__name__}: {msg[:160]}', flush=True)
                # 服务器明确说“合约不存在”时，没必要再试另一种大小写
                if 'non-existent' in msg or '不存在' in msg:
                    break
        if got is None:
            print(f'== {sym}: ❌ 无数据（尝试过 {len(variants(sym))} 种写法）\n', flush=True)
            continue
        cand, valid = got
        try:
            ts = pd.to_datetime(valid['datetime'])
            d0 = ts.iloc[0].strftime('%Y-%m-%d')
            d1 = ts.iloc[-1].strftime('%Y-%m-%d')
            close = float(valid['close'].iloc[0])
            print(f'== {sym} → {cand}: ✅ {len(valid)} 根日K，最早 {d0}，最新 {d1}'
                  f'（首日收盘 {close}）\n', flush=True)
        except Exception as e:
            print(f'== {sym} → {cand}: ✅ 有 {len(valid)} 行，但展示解析失败：{e}\n', flush=True)

    os._exit(0)  # 避免 api.close() 在未完成下载时挂住


if __name__ == '__main__':
    main()
