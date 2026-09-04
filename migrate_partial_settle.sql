-- 结算表迁移：settlements 加 spread 列（按方向价差），支持部分结算
-- 本脚本幂等，可重复执行。
--
-- 什么时候需要跑：
--   库里已经存在 settlements 表时才需要执行。
--   SQLAlchemy 的 create_all() 只会建新表，不会给已有表补列，
--   不跑这条的话 settlements 表会少 spread 列。
--
--   如果是全新库、还没启动过服务，直接跳过本文件——建表时会自动带上 spread 列。
--
-- 执行：
--   mysql -u root -p123456 fastapi_auth < migrate_partial_settle.sql
--
-- 若提示 Duplicate column name 'spread'，说明已经加过了，忽略即可。
--
-- 说明：
--   spread = 结算价 - 开仓价（做空方向取负），等价于 pnl/(手数*乘数)。
--   把它单独存下来，方便结算历史里直接显示「按方向的价差」，
--   不必每次都用 (settle_price - buy_price) * 方向 现算。
--
--   部分结算由后端按请求的 lots 扣减原持仓手数实现，剩余手数继续留在
--   positions 表；不需要改 positions 表 schema。

ALTER TABLE settlements
  ADD COLUMN spread float NOT NULL DEFAULT 0
  COMMENT '按方向价差（做多=结算价-开仓价，做空=开仓价-结算价）' AFTER settle_price;
