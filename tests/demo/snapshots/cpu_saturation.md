综合 top 进程与系统负载两个信号, 根因指向 mysqld 失控占用 CPU. 建议优先排查其慢查询.

## Findings
- critical: Process mysqld (pid 4242) is using 97.5% CPU
- critical: 1-min load 16.40 is >= 2x the 4 available cores

## 根因假设

### mysqld (pid 4242) 失控占用 CPU 是本次饱和的根因: 单进程 97.5% CPU 叠加 1 分钟负载 16.40 远超 4 核容量.
- **Confidence:** high
- **Supporting findings:** 6010bd422fab42a1
- **Suggested actions:**
  - 排查 mysqld 慢查询日志, 定位失控线程或全表扫描.
  - 必要时限流或重启 mysqld, 并观察负载是否回落.

status=ok tokens_in=6300 tokens_out=500
