综合 top 进程与持续负载两个信号, 根因指向 mysqld 失控占用 CPU. 持续负载 (5/15 分钟均值) 高于核心数, 排除了瞬时尖峰的可能. 建议优先排查其慢查询.

## Findings
- critical: Process mysqld (pid 4242) is using 97.5% CPU
- critical: sustained load (5-min 12.10, 15-min 8.00) is critically high for 4 cores

## 根因假设

### mysqld (pid 4242) 失控占用 CPU 是本次饱和的根因: 单进程 97.5% CPU 叠加持续负载 (5 分钟 12.10, 15 分钟 8.00) 远超 4 核容量, 过载已持续数分钟.
- **Confidence:** high
- **Supporting findings:** 6010bd422fab42a1
- **Suggested actions:**
  - 排查 mysqld 慢查询日志, 定位失控线程或全表扫描.
  - 必要时限流或重启 mysqld, 并观察负载是否回落.

status=ok tokens_in=6300 tokens_out=500
