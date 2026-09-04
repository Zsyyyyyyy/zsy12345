-- 回滚 20260903_01：删掉 direction 列并恢复唯一约束
--
-- 注意：删列属于 Contract 阶段的高危操作。执行前必须确认已无旧版本代码在读这些列，
--       否则回滚后旧代码会立刻报 Unknown column。
--       回滚会丢失 direction 列的全部数据，不可恢复。

-- 恢复唯一约束前先去重，否则存在同品种多条持仓时会建索引失败。
-- 保留每个 (user_id, code) 下 id 最小的一条，其余先删。
DELETE p FROM positions p
  JOIN (
    SELECT user_id, code, MIN(id) AS keep_id
    FROM positions
    GROUP BY user_id, code
    HAVING COUNT(*) > 1
  ) d ON p.user_id = d.user_id AND p.code = d.code AND p.id > d.keep_id;

ALTER TABLE positions
  ADD UNIQUE INDEX uq_positions_user_code (user_id, code),
  ALGORITHM=INPLACE, LOCK=NONE;

ALTER TABLE positions
  DROP COLUMN direction,
  ALGORITHM=INSTANT;

ALTER TABLE settlements
  DROP COLUMN direction,
  ALGORITHM=INSTANT;
