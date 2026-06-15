# narrative
这台主机 CPU 处于饱和状态: mysqld (pid 4242) 正占用 97.5% CPU, 且持续负载 (5 分钟 12.10, 15 分钟 8.00) 已达 4 核容量的 2 倍以上, 说明过载已持续数分钟而非瞬时尖峰. 建议排查 mysqld 的慢查询或失控线程.

# findings
- [critical] Process mysqld (pid 4242) is using 97.5% CPU (tags: )
- [critical] sustained load (5-min 12.10, 15-min 8.00) is critically high for 4 cores (tags: )

# tokens
input=2700
output=230
