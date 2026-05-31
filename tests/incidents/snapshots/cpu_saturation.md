# narrative
这台主机 CPU 处于饱和状态: mysqld (pid 4242) 正占用 97.5% CPU, 且 1 分钟负载 16.40 已达 4 核的约 4 倍. 建议排查 mysqld 的慢查询或失控线程.

# findings
- [critical] 1-min load 16.40 is >= 2x the 4 available cores (tags: )
- [critical] Process mysqld (pid 4242) is using 97.5% CPU (tags: )

# tokens
input=2700
output=230
