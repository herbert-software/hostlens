# service-inspector-contract 规范（delta）

> 目的：`postgres.bloat_tables` 迁移至全合规（`HOSTLENS_POSTGRES_PASSWORD` secret + 原生 `PGPASSWORD` remap + `PGCONNECT_TIMEOUT=5` + 列表 top-N 截断 + total 计数 + 补 finding-trigger 轨），从祖父条款移除。**这是最后一个 pre-spike seed** —— 移除后祖父化 seed 列表**归零**、祖父条款**闭合**（无在册祖父化 inspector）。三处需求里对 `postgres.bloat_tables` 的「漂移/祖父化」点名收口为「全部 pre-spike seed 已迁移」。需求标题不变、无 RENAMED。

## 修改需求

### 需求:本契约管辖范围与既有 seed 祖父化

本 `service-inspector-contract` 的**全部需求**(连接注入安全 / secret / 失败分类 / 超时输出 / 跨 target 无分叉 / 双轨 fixture / 单实例边界)**仅管辖本 spike 起新增或迁移**的 service inspector;作为新立(ADDED)契约,它**向前生效**、**不回溯绑定**已归档的 pre-spike inspector。曾有两个 pre-spike 既有 seed(`redis.slowlog`、`postgres.bloat_tables`)被祖父化;此二者**均已由独立 follow-up 迁移至全合规**——`redis.slowlog`(secret 改 `HOSTLENS_REDIS_PASSWORD` + remap 到 `REDISCLI_AUTH`、补 default-阈值 semantic-abnormal fixture 轨)与 `postgres.bloat_tables`(secret 改 `HOSTLENS_POSTGRES_PASSWORD` + remap 到原生 `PGPASSWORD`、补 `PGCONNECT_TIMEOUT=5`、列表输出截断 top-N + total 计数、补 finding-trigger 轨)现**均受本契约管辖**、不再祖父化。**至此祖父化 seed 列表归零、祖父条款闭合**:**无任何在册祖父化 inspector**。本需求作为「契约向前生效、不回溯绑定已归档 pre-spike inspector」的**管辖范围原则**继续生效,但**不再豁免任何具体 inspector**——日后若审计发现某 pre-spike inspector 与本契约漂移,**必须**经独立 follow-up 迁移合规,**禁止**新立祖父化豁免。

#### 场景:契约不回溯绑定已归档 pre-spike inspector

- **当** 审计某 inspector 对本契约各需求的合规性
- **那么** 仅**本 spike 起新增或迁移**的 service inspector 须满足本契约 MUST;已迁移的 `redis.slowlog`、`postgres.bloat_tables` 现受本契约管辖须满足全部 MUST;祖父化 seed 列表已归零、**无在册祖父化 inspector**,**禁止**据「契约不回溯绑定」为任何在册 inspector 新立祖父化豁免

#### 场景:redis.slowlog 迁移后受契约管辖

- **当** 审计 `redis.slowlog` 对本契约 secret / argv / 双轨 fixture 需求的合规性
- **那么** 它**必须**满足全部 MUST:secret 声明为 `HOSTLENS_REDIS_PASSWORD` 并在 collector 内 remap 到 `REDISCLI_AUTH`、命令串**禁止**含 `-a ` 明文密码 flag、**必须**附 default-阈值下触发 finding 的 semantic-abnormal fixture;**禁止**再将其按祖父化豁免

#### 场景:postgres.bloat_tables 迁移后受契约管辖

- **当** 审计 `postgres.bloat_tables` 对本契约 secret / 超时 / 输出规模 / 双轨 fixture 需求的合规性
- **那么** 它**必须**满足全部 MUST:secret 声明为 `HOSTLENS_POSTGRES_PASSWORD` 并在 collector 内 remap 到 client 原生 `PGPASSWORD`、client 连接超时 `PGCONNECT_TIMEOUT=5` **必须**小于 `collect.timeout_seconds`、列表形态输出 **必须**经 `max_results` 参数截断为 top-N 并附 `total_tables` total 计数标量、**必须**附 default-阈值下触发 finding 的 semantic-abnormal fixture(`bloated.json`)与降阈值触发的 finding-trigger 轨;**禁止**再将其按祖父化豁免

### 需求:service inspector 的 secret 必须经 env 注入且从不进命令字符串

（适用范围见首条「本契约管辖范围与既有 seed 祖父化」需求:下述 secret 规则对**本契约管辖**的 inspector 为 MUST;两个 pre-spike seed `redis.slowlog`、`postgres.bloat_tables` 均已迁移、现受本契约管辖,祖父化 seed 列表已归零、无在册豁免。）

service inspector 的连接凭据(密码 / token)**必须**经 manifest `secrets` 字段声明、由 runner 经 `env=secrets_env` 注入。声明的 secret 名**必须**用 `HOSTLENS_` 前缀(如 `HOSTLENS_REDIS_PASSWORD` / `HOSTLENS_MYSQL_PWD` / `HOSTLENS_POSTGRES_PASSWORD`)——这对齐既有 `ssh-execution-target` 契约(其 spec 规定 SSH secret 投递走 `AcceptEnv HOSTLENS_*` + `HOSTLENS_` 前缀变量名),使 secret 能跨 SSH 到达远端。collector 内**必须**把该 `HOSTLENS_` 变量 **remap** 到 client 原生 env 鉴权通道(`redis-cli` 读 `REDISCLI_AUTH`、`mysql` 读 `MYSQL_PWD`、`psql` 读 `PGPASSWORD`),使凭据**不进** `argv`。**禁止**把凭据经 `{{ }}` 渲染进命令字符串;**禁止**以会进 `argv`(全局 `ps` 可见)的命令行明文密码参数(如 `mysql -p<pwd>` / `redis-cli -a <pwd>`)传递。本 spike **不**引入凭据文件(`--defaults-extra-file` 等)或其它新 secret 机制;client **无**原生 env 鉴权通道(如 `curl` 的 bearer token)的 secret 机制留对应 wave 定(本 spike 两探针的 client 均有原生 env 通道),届时仍**禁** `argv` 明文。

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

**secret 跨 SSH 走既有契约的 `HOSTLENS_` 路径**:runner 的 SSH target 经 AsyncSSH `conn.run(env=)` 传 env(命令字符串绝不改写),该路径受远端 sshd `AcceptEnv` 约束(默认仅 `LANG`/`LC_*`)。既有 `ssh-execution-target` 契约已定 SSH secret 投递路径 = `HOSTLENS_` 前缀变量名 + 远端 `AcceptEnv HOSTLENS_*`;本契约的 secret 需求**遵循**之(secret 声明 `HOSTLENS_*`、collector remap)。故需 secret 的 inspector 在 SSH 上的运行**前提**是远端配 `AcceptEnv HOSTLENS_*`;本契约**不**声称在未配 AcceptEnv 的默认 sshd 下透明跨 SSH。非 secret 行为由 runner 对 target 的统一 dispatch 结构性等价。**注**:两个 pre-spike seed `redis.slowlog`(用 `HOSTLENS_REDIS_PASSWORD`)、`postgres.bloat_tables`(用 `HOSTLENS_POSTGRES_PASSWORD`)均已迁移合规,祖父化 seed 列表已归零、无在册 `HOSTLENS_`-命名漂移项。

#### 场景:manifest 无 target 分叉逻辑

- **当** 检视某 service inspector 的 manifest 与 collector 命令
- **那么** 其连接参数传入、secret 引用、失败处理**必须**不含按 `target.type` 分叉的分支;**禁止**为某一 target 特设旁路

#### 场景:secret inspector 在 SSH 上遵循 HOSTLENS_ + AcceptEnv 路径

- **当** 某需 secret 的 service inspector 跑在 ssh target 上
- **那么** 其 secret **必须**以 `HOSTLENS_` 前缀声明,且其到达远端的**前提**是远端 sshd 配 `AcceptEnv HOSTLENS_*`;该前提**必须**被文档式声明(manifest 注释 / 运行文档),**禁止**默认它在未配 AcceptEnv 的 sshd 下自动成立
