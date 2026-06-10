# service-inspector-contract 规范（delta）

> 目的：`redis.slowlog` 已迁移至全合规（HOSTLENS_ secret + REDISCLI_AUTH remap + 双轨 fixture），从祖父条款移除；pre-spike 祖父化 seed 仅剩 `postgres.bloat_tables`。需求标题不变、无 RENAMED。

## 修改需求

### 需求:本契约管辖范围与既有 seed 祖父化

本 `service-inspector-contract` 的**全部需求**(连接注入安全 / secret / 失败分类 / 超时输出 / 跨 target 无分叉 / 双轨 fixture / 单实例边界)**仅管辖本 spike 起新增或迁移**的 service inspector;作为新立(ADDED)契约,它**向前生效**、**不回溯绑定**已归档的 pre-spike inspector。曾有两个 pre-spike 既有 seed(`redis.slowlog`、`postgres.bloat_tables`)被祖父化;其中 **`redis.slowlog` 已由独立 follow-up 迁移至全合规**(secret 改 `HOSTLENS_REDIS_PASSWORD` + remap 到 `REDISCLI_AUTH`、补 default-阈值 semantic-abnormal fixture 轨),现**受本契约管辖**、不再祖父化。仅剩**一个 pre-spike 既有 seed**——`postgres.bloat_tables`——是**已祖父化的 legacy**:它在 secret 命名(用非 `HOSTLENS_` 名)、双轨 fixture(仅单轨 finding-trigger、缺 default-阈值 semantic-abnormal)等处与本契约各需求**漂移**,但这是**已登记的已知不合规项**,**必须**由独立 follow-up 迁移使其合规——**不视为本契约的内部矛盾**。

#### 场景:契约不回溯绑定既有 seed

- **当** 审计某 inspector 对本契约各需求的合规性
- **那么** 仅**本 spike 起新增或迁移**的 service inspector 须满足本契约 MUST;已迁移的 `redis.slowlog` 现受本契约管辖须满足 MUST;仅剩的 pre-spike seed(`postgres.bloat_tables`)按祖父化处理、其漂移由独立 follow-up 迁移消解,**禁止**据此判本契约自相矛盾

#### 场景:redis.slowlog 迁移后受契约管辖

- **当** 审计 `redis.slowlog` 对本契约 secret / argv / 双轨 fixture 需求的合规性
- **那么** 它**必须**满足全部 MUST:secret 声明为 `HOSTLENS_REDIS_PASSWORD` 并在 collector 内 remap 到 `REDISCLI_AUTH`、命令串**禁止**含 `-a ` 明文密码 flag、**必须**附 default-阈值下触发 finding 的 semantic-abnormal fixture;**禁止**再将其按祖父化豁免

### 需求:service inspector 的 secret 必须经 env 注入且从不进命令字符串

（适用范围见首条「本契约管辖范围与既有 seed 祖父化」需求:下述 secret 规则对**本契约管辖**的 inspector 为 MUST;已迁移的 `redis.slowlog` 现受本契约管辖;仅剩的 pre-spike seed `postgres.bloat_tables` 的 secret 漂移祖父化、由独立 follow-up 迁移。）

service inspector 的连接凭据(密码 / token)**必须**经 manifest `secrets` 字段声明、由 runner 经 `env=secrets_env` 注入。声明的 secret 名**必须**用 `HOSTLENS_` 前缀(如 `HOSTLENS_REDIS_PASSWORD` / `HOSTLENS_MYSQL_PWD`)——这对齐既有 `ssh-execution-target` 契约(其 spec 规定 SSH secret 投递走 `AcceptEnv HOSTLENS_*` + `HOSTLENS_` 前缀变量名),使 secret 能跨 SSH 到达远端。collector 内**必须**把该 `HOSTLENS_` 变量 **remap** 到 client 原生 env 鉴权通道(`redis-cli` 读 `REDISCLI_AUTH`、`mysql` 读 `MYSQL_PWD`、`psql` 读 `PGPASSWORD`),使凭据**不进** `argv`。**禁止**把凭据经 `{{ }}` 渲染进命令字符串;**禁止**以会进 `argv`(全局 `ps` 可见)的命令行明文密码参数(如 `mysql -p<pwd>` / `redis-cli -a <pwd>`)传递。本 spike **不**引入凭据文件(`--defaults-extra-file` 等)或其它新 secret 机制;client **无**原生 env 鉴权通道(如 `curl` 的 bearer token)的 secret 机制留对应 wave 定(本 spike 两探针的 client 均有原生 env 通道),届时仍**禁** `argv` 明文。

凡 manifest 声明了某 secret,runner preflight 即要求该 env **存在**于环境(按 `name in os.environ` 判定);**无鉴权**实例(如无密码 Redis)需显式导出**空串**(`HOSTLENS_REDIS_PASSWORD=`)使 preflight 通过,collector 内再按 `[ -n "$VAR" ]` 分流有/无鉴权——空串"存在"即满足声明前提,与"完全不设 env"(→ `requires_unmet`,见失败分类)区分。

**SSH 投递**:runner 的 SSH target 经 AsyncSSH `conn.run(env=)` 传 env(命令字符串绝不改写),该路径**受远端 sshd `AcceptEnv` 约束**(默认仅 `LANG`/`LC_*`)。故 secret 用 `HOSTLENS_` 前缀 + 远端配 `AcceptEnv HOSTLENS_*` 是其跨 SSH 到达的**前提**(既有 ssh 契约已定的路径);本契约**不**声称在默认(未配 AcceptEnv)sshd 下透明跨 SSH。

#### 场景:凭据经 HOSTLENS_ 声明 remap 到 client 原生 env 通道

- **当** 某 service inspector 需要连接凭据
- **那么** 该凭据**必须**以 `HOSTLENS_` 前缀经 `secrets` 声明、并在 collector 内 remap 到 client 原生 env 鉴权通道;**禁止**出现 `{{ password }}` 插值,**禁止** `-p<pwd>` / `-a <pwd>` 等会进 `argv` 的命令行明文密码

#### 场景:声明 secret 即强制其 env 存在

- **当** 某 manifest 声明了 `secrets: [X]` 但环境未设 `X`(连空串都没有)
- **那么** runner preflight **必须**标 `status=requires_unmet`(与缺 client 二进制并列),collector 不执行;无鉴权实例**必须**显式导出空串 `X=` 才能跑

#### 场景:回显的凭据不落 fixture

- **当** 录制 fixture 时 client 把凭据回显进 stdout/stderr(如连接错误带连接串)
- **那么** 产出 fixture 的 stdout/stderr 中**禁止**出现明文凭据;录制器**必须**在写盘前脱敏

### 需求:service inspector 跨 local 与 SSH target 无分叉(secret 投递有 SSH 前提)

service inspector **必须**对 `local` 与 `ssh` target 用**同一** manifest、**同一** collector 命令文本、**同一** secret 声明,**禁止**在 manifest / collector 内出现按 target 类型分叉的连接参数约定或失败处理逻辑(无 target-specific 旁路)。该「无分叉」是**可经代码检视机械核验**的属性(检 manifest 无 target 条件分支),CI 在 local 上验证非 secret 行为。

**secret 跨 SSH 走既有契约的 `HOSTLENS_` 路径**:runner 的 SSH target 经 AsyncSSH `conn.run(env=)` 传 env(命令字符串绝不改写),该路径受远端 sshd `AcceptEnv` 约束(默认仅 `LANG`/`LC_*`)。既有 `ssh-execution-target` 契约已定 SSH secret 投递路径 = `HOSTLENS_` 前缀变量名 + 远端 `AcceptEnv HOSTLENS_*`;本契约的 secret 需求**遵循**之(secret 声明 `HOSTLENS_*`、collector remap)。故需 secret 的 inspector 在 SSH 上的运行**前提**是远端配 `AcceptEnv HOSTLENS_*`;本契约**不**声称在未配 AcceptEnv 的默认 sshd 下透明跨 SSH。非 secret 行为由 runner 对 target 的统一 dispatch 结构性等价。**注**:已迁移的 `redis.slowlog` 现用 `HOSTLENS_REDIS_PASSWORD`、合规;仅剩的 pre-spike seed `postgres.bloat_tables` 用**非** `HOSTLENS_` 名、与该契约漂移,迁移是独立 follow-up,不在本 spike 范围。

#### 场景:manifest 无 target 分叉逻辑

- **当** 检视某 service inspector 的 manifest 与 collector 命令
- **那么** 其连接参数传入、secret 引用、失败处理**必须**不含按 `target.type` 分叉的分支;**禁止**为某一 target 特设旁路

#### 场景:secret inspector 在 SSH 上遵循 HOSTLENS_ + AcceptEnv 路径

- **当** 某需 secret 的 service inspector 跑在 ssh target 上
- **那么** 其 secret **必须**以 `HOSTLENS_` 前缀声明,且其到达远端的**前提**是远端 sshd 配 `AcceptEnv HOSTLENS_*`;该前提**必须**被文档式声明(manifest 注释 / 运行文档),**禁止**默认它在未配 AcceptEnv 的 sshd 下自动成立
