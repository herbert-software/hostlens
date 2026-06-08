## 为什么

**wave-3 占位 scaffold(尚未启动实施)**。`add-replication-lag-inspectors`(wave-2c)的 postgres 录制验证门(task 5.1 / 决策 W-6)**撑破**:副本侧 `now()-pg_last_xact_replay_timestamp()` + idle-guard 不能干净归一成 `replication-inspector-contract` 的 apply_lag 三元组——

- **漏洞①(实证)**:receiver 断开后 `pg_last_wal_receive_lsn()==pg_last_wal_replay_lsn()` 仍为 TRUE,idle-guard 捏造 `lag=0/健康`,而 standby 实际任意滞后;唯一揭穿信号是 `pg_stat_wal_receiver.status='streaming'`(另一系统视图)。
- **漏洞②(实证)**:standalone `pg_last_wal_receive_lsn()` 返回 NULL。
- 副本侧无 mysql `Seconds_Behind_Source` 式单值秒数等价物;诚实稳健的 postgres apply-lag 是**主库侧** `pg_stat_replication.replay_lag`(契约前向规则的主库侧聚合)。

完整证据见 `openspec/changes/archive/.../add-replication-lag-inspectors/design.md`「门裁定」表。

## 变更内容

- 起独立 `postgres.replication_lag` 复制 inspector,**优先评估主库侧 `pg_stat_replication.replay_lag` 路径**(而非已撑破的副本侧 timestamp idle-guard);若走副本侧则必须补 `pg_stat_wal_receiver` 健康检查 + source-currency 信号。
- 纳入 `test_replication_contract_crosscheck.py` 的 `_REPLICATION_MANIFESTS` 枚举,复制 cohort 计数守卫由 **2 → 3**。
- 覆盖矩阵 postgres 单元格由 `deferred` 冻结为 `delivered`(语义类 `apply_lag`)。

## 非目标 (Non-Goals)

- ❌ 不重复已撑破的副本侧 timestamp idle-guard 单路径方案。
- ❌ 不在本占位内实施——这是 wave-2c 门裁定留下的 scaffold,实施前须按 OpenSpec 正式补齐 design/specs/tasks。
