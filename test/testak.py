import akshare as ak


futures_zh_spot_df = ak.futures_zh_spot(symbol='V2705, P2205, B2201, M2205', market="CF", adjust='0')
print(futures_zh_spot_df)
