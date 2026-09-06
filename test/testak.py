import akshare as ak


# futures_zh_spot_df = ak.futures_zh_spot(symbol='V2705, P2205, B2201, M2205', market="CF", adjust='0')
# print(futures_zh_spot_df)


# import akshare as ak

get_futures_daily_df = ak.get_futures_daily(start_date="20260701", end_date="20260716", market="DCE")
print(get_futures_daily_df)

