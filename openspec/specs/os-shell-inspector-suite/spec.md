# os-shell-inspector-suite 规范

## 目的

定义 wave-1 OS / Linux shell inspector 套件契约——按域覆盖指定的 OS/Linux 故障域、套件内每个 inspector 为遵守作者契约的纯 YAML、每个附 ReplayTarget fixture 与可证检出的 snapshot 测试、本套件禁止引入新基础设施。
## 需求
### 需求:wave-1 必须按域覆盖指定的 OS/Linux 故障域

本套件**必须**在现有 builtin 基线之上、按 `TODO.md` §M6 覆盖矩阵新增覆盖以下 OS/Linux 故障域的纯 shell inspector：计算 CPU、内存、磁盘/FS、网络、进程、服务管理器与调度器、内核/系统、日志。每个域**必须**至少新增矩阵为该域列出的探针。**遵守 spike D-9**：本需求约束的是**套件层的域覆盖度**，**不**为任一具体 inspector 规定 input/output 行为契约——具体 inspector 清单（名称与采集手法）是**实现**，列在本变更的 `proposal.md` 与 `tasks.md`，由 snapshot 测试验收。

中间件 / 服务域（nginx / mysql / postgres / redis / docker / k8s）**禁止**纳入本套件（留 wave-2）；本套件**仅**含零外部服务依赖的 OS/Linux shell 探针。

#### 场景:清单中的 inspector 全部干净注册

- **当** 套件实现完成、运行 `build_registry_from_search_paths([], settings=Settings())`
- **那么** `proposal.md`/`tasks.md` 列出的每个 wave-1 inspector（共 23 个，以本变更**归档时冻结**的清单为准；后续 wave 另立 change，不回溯改本 spec）**必须**以其声明 `name` 出现在 registry 中，且 registry `errors == []`

#### 场景:每个目标域都有新增探针且勾上矩阵

- **当** 评估 wave-1 是否达成域覆盖
- **那么** 上述 8 个故障域**必须**各自至少新增一个 inspector，且每个落地的 inspector **必须**勾上 `TODO.md` §M6 覆盖矩阵对应单元格；**禁止**以「域内已有一个探针」为由跳过矩阵为该域列出的新增项

#### 场景:中间件与服务域不在本套件范围

- **当** 评估某 inspector 是否属于 wave-1 套件
- **那么** 依赖外部服务（nginx / mysql / postgres / redis / docker / k8s）的 inspector **禁止**纳入本套件（留 wave-2）

### 需求:套件内每个 inspector 必须是遵守作者契约的纯 YAML

本套件内每个 inspector **必须**为纯 YAML manifest，并**遵守** `inspector-authoring-contract` 的全部规则（一切抽取与数值派生在 collector 内、finding 规则只做标量阈值/成员比较、`for_each` 单绑定、输出键防 parameter 遮蔽、命令注入安全三件套、运行前提文档式声明）。本需求**引用**该契约而非重述其细则，以免两份 spec 漂移。**禁止** enable `hook.py`、**禁止**新增 `sql_result` parse format、**禁止**在 finding 表达式里做解析或数值派生。

#### 场景:数值派生与跨行关联在 collector 内

- **当** 某 inspector 需要派生量（如磁盘 IO 利用率、内存 swap 使用率、僵尸进程计数）或需关联多行/多命令输出
- **那么** 该派生/关联**必须**由 collector 命令（shell 算术 / `jq` / 自行 read→sleep→read 双读算差）算出并写入输出 JSON，finding 规则只对已就绪标量做阈值比较；**禁止**在 finding 表达式内现算

#### 场景:输出键命名遵循契约的防遮蔽约定

- **当** 某 inspector 产出结果
- **那么** 若为列表型（配 `for_each`），其可迭代结果集的顶层键**必须**取自 `results` / `items` / `records` 之一；若为聚合型（无 `for_each`），其顶层标量键沿用裸命名（与既有 `system.uptime` / `linux.memory.pressure` 一致）但**必须**不与任一已声明 parameter 同名——两种形态都**禁止**输出键与 parameter 同名（finding 上下文中同名 parameter 会遮蔽 output 键）

#### 场景:参数安全进 shell

- **当** 某 inspector 把调用方参数（如关键进程名列表、日志路径、DNS 待查名）插入 `collect.command`
- **那么** 该参数**必须**经 `| sh`（或数组 `| map('sh')`）引用、且 `parameters` JSON Schema **必须**用 `pattern` 收紧取值域；**禁止**裸 `{{ param }}` 拼进可执行位置

### 需求:套件内每个 inspector 必须附 ReplayTarget fixture 与可证检出的 snapshot 测试

本套件内每个 inspector **必须**附带用 fixture 录制器（`inspector-fixture-recorder`）对真实 Linux host 录制的 `ReplayTarget` 兼容 fixture，以及 snapshot 测试，使其能离线确定性回放出 `InspectorResult`。**禁止**手写 fixture。CI **必须**全程经 `ReplayTarget` 回放，**禁止**在日常 CI 中依赖网络 / 真实主机 / 真实数据源。

为防止 no-op inspector 满足验收，每个 inspector **必须**至少附**一份触发预期 finding 的异常场景 fixture**，其 snapshot **必须**断言该场景产出预期的 finding（severity + message 语义），证明 inspector 真能**检出**目标故障——仅有「干净注册 + happy-path 无 finding」的 snapshot **不满足**验收。

#### 场景:异常场景 snapshot 证明检出能力

- **当** 对某套件 inspector 运行其 snapshot 测试
- **那么** 测试集**必须**含至少一份异常场景 fixture，其 snapshot 断言 inspector 在该场景下产出预期 severity 与 message 语义的 finding；**禁止**只有 happy-path（无 finding）snapshot 就判该 inspector 验收通过

#### 场景:离线回放确定性出结果

- **当** 在任意平台（含 macOS / CI）对某套件 inspector 运行其 snapshot 测试
- **那么** 它**必须**经 `ReplayTarget` 回放录制的 fixture、不触达任何真实主机或网络，并产出与快照一致的确定性 `InspectorResult`

#### 场景:缺少所需二进制时优雅 skip 而非崩溃

- **当** 目标主机缺少某 inspector `requires_binaries` 声明的二进制（如无 `smartctl` / 无 `chronyc`）
- **那么** runner preflight **必须**将该 inspector 标为 `status=requires_unmet` 并 skip、报告中标注，**禁止**报错中断同 run 其它 inspector

### 需求:本套件禁止引入新基础设施

本套件**必须**在现有 schema 字段集内完成，证明纯铺量无需新 infra：**禁止**改动 inspector manifest schema（不增删字段）、**禁止**新增 parse format（仅 raw/table/json/kv）、**禁止**扩 capability enum（现为 `{shell, file_read, ssh, systemd, docker_cli}`）、**禁止**新增 `min_binary_version` 等 schema 字段（窄 scope 版本门仍走文档式声明）、**禁止**新增 Python 运行时依赖。允许使用**现有** schema 字段（含已落地的 `collect.sampling_window`）。

#### 场景:零对外契约变更

- **当** 套件实现完成
- **那么** inspector manifest schema、Agent 可见工具数组（仍只有 `list_inspectors` / `run_inspector`）、parse format 集合、capability enum **必须**全部保持不变；**禁止**因本套件而改动任何对外契约

#### 场景:Linux-only 与版本门用文档式声明

- **当** 某 inspector 依赖 Linux 专有数据源（`/proc`、`/sys`、GNU `date -d`、`journalctl`）或特定工具版本
- **那么** 该前提**必须**在 `description` 与 `tags`（tag 正则 `^[a-z][a-z0-9_-]*$`，禁含 `+`）中文档式声明；**禁止**新增 manifest 字段做机器式版本门（会被 schema `extra="forbid"` 拒）

### 需求:安全基线与包管理域必须按域覆盖（os-shell 后续 wave）

本套件**必须**在 wave-1 既有基线之上、按 `TODO.md` §M6 覆盖矩阵新增覆盖以下两个此前空白（0 inspector）的 OS/Linux 故障域的纯 shell inspector：**安全基线**与**包管理**。每个域**必须**至少新增 3 个 inspector（达 §M6「每域 ≥3」退出条件）。**遵守 spike D-9**：本需求约束的是**套件层的域覆盖度**，**不**为任一具体 inspector 规定 input/output 行为契约——具体 inspector 清单（名称与采集手法）是**实现**，列在本变更的 `proposal.md` 与 `tasks.md`，由 snapshot 测试验收。

**追加式冻结 cohort**：本需求是 os-shell 套件的**追加**需求，**禁止** MODIFY wave-1 的「wave-1 必须按域覆盖」需求；二者 cohort 各自冻结、互不回溯（与 service-inspector-suite 的 cohort 冻结纪律一致）。wave-1 spec 中「中间件/服务域留 wave-2」的注记指的是**服务域**（已由独立的 `service-inspector-suite` capability 承接），与本需求的 security/pkg OS-shell 域**正交**；本需求是 os-shell 套件按 OS 故障域继续铺量的后续 cohort。

本 cohort 的 inspector **必须**仅含零外部服务依赖的 OS/Linux shell 探针（读本机日志/端口/包数据库），**禁止**纳入依赖外部服务（nginx / mysql / postgres / redis / docker / k8s）或语言运行时（JVM / Go）的 inspector。

#### 场景:cohort 清单中的 inspector 全部干净注册

- **当** 套件实现完成、运行 `build_registry_from_search_paths([], settings=Settings())`
- **那么** 本变更 `proposal.md`/`tasks.md` 列出的每个 security/pkg inspector（共 6 个，以本变更**归档时冻结**的清单为准；后续 wave 另立 change，不回溯改本 spec）**必须**以其声明 `name` 出现在 registry 中，且 registry `errors == []`

#### 场景:两个目标域都达 ≥3 覆盖且勾上矩阵

- **当** 评估本 cohort 是否达成域覆盖
- **那么** 安全基线域与包管理域**必须**各自至少新增 3 个 inspector，且每个落地的 inspector **必须**勾上 `TODO.md` §M6 覆盖矩阵对应单元格

#### 场景:安全日志不可达时必须 fail-loud 不假阴

- **当** security inspector（如 `security.failed_logins` / `security.sudo_history`）运行在数据源不可达的目标上（无 journald、journal 因权限不可读、非 systemd 机无 `journalctl`）
- **那么** 该 inspector **必须**以 `status=requires_unmet`（binary 缺失，preflight 拦）或 `status=exception`（数据源读取失败，collector fail-loud `|| exit 1`）呈现，**禁止**伪造 `status=ok` 把「读不到安全日志」误判为「无失败登录 / 无 sudo 活动」（security 域的关键假阴性防护）；collector **禁止**用 `|| true` 掩盖主命令失败
- **且** 本 cohort 的 snapshot 测试**必须**含至少一份「数据源不可达」fixture 断言此非假阴行为

#### 场景:pkg inspector 在采集失败时必须非 ok（无包管理器 或 命令失败）

- **当** pkg inspector（`pkg.pending_updates` / `pkg.security_patches` / `pkg.held_back`）的采集失败——**无论**是「既无 `apt-get` 又无 `dnf`」（collector 内两路 `command -v` 均失败）**还是**「包管理器存在但其主命令失败」（dpkg 锁 / 网络 / 元数据损坏，主命令非零退出）
- **那么** 该 inspector **必须**以 `status=exception`（collector fail-loud `exit 1`）呈现，**禁止**产出 `status=ok` 且计数为 0 的结果（防止「采集失败 → 误判无待升级 / 无安全补丁」的假阴）；collector **禁止**用裸管道 `<主命令> | grep -c`（管道吞主命令退出码 → 假 0），**必须** raw-capture 后判退出码或 `set -o pipefail`
- **且** 本 cohort 的 snapshot 测试**必须**含「无包管理器」与「包管理器存在但主命令失败」两类 fixture 各至少一份断言此行为

#### 场景:security 日志型 inspector 不得因数据源可达但语义错配而假阴

- **当** `security.failed_logins` / `security.sudo_history` 运行在数据源**可达**但与硬编码标识不匹配的目标上（如 RHEL/Fedora/SUSE 家族 sshd 的 systemd unit 名为 `sshd.service` 而非 Debian 的 `ssh.service`），且时窗内**确有**失败登录
- **那么** 该 inspector **禁止**因 unit 名错配而 journalctl 成功返 0 行 → 伪 `status=ok` 计数 0（数据源可达型假阴，fail-loud 不触发，最隐蔽）；collector **必须**同时匹配跨发行版的标识（如 `_SYSTEMD_UNIT=ssh.service _SYSTEMD_UNIT=sshd.service` 多值 OR）
- **且** 本 cohort 的 snapshot 测试**必须**含一份命令串级断言：捕获的 `failed_logins` 主命令同含 `_SYSTEMD_UNIT=ssh.service` 与 `_SYSTEMD_UNIT=sshd.service`（确保 RHEL 家族 sshd.service 不被漏匹配）；**journalctl OR 语义本身**因 D-7 offline 录制（fixture 录 collector 最终 JSON、不跑 journalctl）**只在命令串级锁定**，其「sshd.service 有失败记录 → 检出非 0」的计数边界正确性须在带真实 journald 的 Demo Path 上验证——offline fixture **不**声称锁 OR 执行正确性，下游检出由通用 finding-trigger fixture 证（与下方过滤器场景同构，见本变更 tasks.md 偏离登记）

#### 场景:含过滤逻辑的 pkg inspector 的过滤器正确性须命令串级锁 + 真机验证

- **当** `pkg.security_patches` 的 security 源过滤逻辑（apt 的 security 源 grep / `dnf updateinfo` 过滤）错配（正则写错 / 源名不匹配），可能令「确有补丁」假 0（与 security 日志型「语义错配」假阴同构）
- **那么** 本 cohort snapshot 测试**必须**含一份「post-filter 计数非 0 → 检出 finding」的 finding-trigger fixture，锁住**下游计数 + finding 触发链**；**过滤器 regex 本身**因 D-7 offline 录制（fixture 录 collector 最终 JSON、不跑 shell 过滤器）**只在命令串级锁定**（verbatim 捕获的命令含正确过滤 regex），其**计数边界正确性须在带真实 apt/dnf 的 Demo Path 上验证**——offline fixture **不**声称锁过滤器执行正确性（见本变更 tasks.md 偏离登记）

#### 场景:cohort 内 inspector 不得依赖外部服务或语言运行时

- **当** 评估本 cohort 某 inspector 是否合规
- **那么** 其 `requires_binaries` 与 `collect.command` **禁止**引用外部服务客户端（`nginx` / `mysql` / `redis-cli` / `psql` / `docker` 等）或语言运行时工具（`jstat` / `jcmd` / pprof）——本 cohort **仅**含读本机日志 / 文件权限 / 包数据库的零外部依赖 OS shell 探针

### 需求:net 域必须增量补 TLS chain validity 探针

本套件**必须**在 net 域既有探针之上、按 `TODO.md` §M6 覆盖矩阵补一个 **TLS chain validity** 单元格:新增纯 shell inspector `net.tls.chain_validity`,用 `openssl s_client` 验证端点的完整证书链能否被系统信任库验证通过(`Verify return code`),覆盖既有 `net.tls.cert_expiry`(只看 `notAfter` 剩余天数)看不见的「缺中间 CA / 链顺序错 / 自签 / 不受信根」类握手失败。

**非退出门槛、纯矩阵补格**:net 域已达 §M6「每域 ≥3」退出条件,本需求**不**为达覆盖门槛,而为补矩阵明确列出的 chain validity 探针。与 `net.tls.cert_expiry` **职责互补、可重叠**(过期证书 `openssl verify` 返 code 10,两者会同时报告,是交叉印证非冲突),**禁止**合并或改动既有 inspector。

**追加式冻结 cohort**:本需求是 os-shell 套件的**追加**需求,**禁止** MODIFY wave-1 或 security/pkg cohort 的既有需求;各 cohort 自冻结、互不回溯(与套件既有 cohort 冻结纪律一致)。具体 inspector 的 input/output 行为契约遵守 `inspector-authoring-contract`,本需求只约束套件层的矩阵覆盖与质量门。

该 inspector **必须**仅含零外部服务依赖的 OS shell 探针(只做出站 TLS 握手 + 本机信任库验证),**禁止**依赖外部服务客户端或语言运行时。

#### 场景:inspector 干净注册并勾上矩阵

- **当** 套件实现完成、运行 `build_registry_from_search_paths([], settings=Settings())`
- **那么** `net.tls.chain_validity` **必须**以其声明 `name` 出现在 registry 中,且 registry `errors == []`
- **且** `TODO.md` §M6 覆盖矩阵 net 域 TLS chain validity 单元格**必须**被勾上

#### 场景:链不可信时检出 critical finding

- **当** collector 拿到的 openssl stdout 含证书(`BEGIN CERTIFICATE`)且 `Verify return code: N (reason)` 的 `N != "0"`(如 `20 unable to get local issuer certificate` 缺中间 CA、`19 self-signed certificate in certificate chain` 自签链)
- **那么** 该 inspector **必须**产出一条 `severity=critical` 的 finding(finding DSL 用**字符串**比较 `verify_code != '0'`——`raw` 解析捕获恒为 str,整数比会让 `"0" != 0` 反转成有效链误报),message **必须**带上 `verify_code` 与 `reason`(供 Agent 后续关联分级),`status=ok`(成功采集到「链不可信」这一事实)
- **且** 本变更 snapshot 测试**必须**含至少一份「链断」finding-trigger fixture 断言此检出

#### 场景:端点不通 / 无证书时 fail-loud 不把连不上当链有效

- **当** `openssl s_client -connect` 因端点不可达 / 握手超时而失败(stdout 无证书),**或**端点是「纯 TCP 非 TLS 端口 / 半握手 / 无 peer 证书」而 openssl **仍打印 `Verify return code: 0`**(verify 0 = 没验出错,非「验过链」)
- **那么** 该 inspector **必须**以 `status=exception` 呈现:`raw_extract_regex` **必须**要求证书 PEM 标记(`-----BEGIN CERTIFICATE-----`)在 `Verify return code` 行**之前**出现,无证书文本 → regex 非 match → `{verify_code: null}` → `output_schema`(`required` + `type: string`)拒 null → exception。**禁止**伪造 `status=ok` 或 `verify_code="0"`(把「连不上 / 无证书 / 非 TLS」误判为「链有效」的**关键假阴防护**);`output_schema` 的 `required` + 非 null 类型**禁止**松动(松了会让无证书路径变 silent ok)
- **且** 本变更 snapshot 测试**必须**含两份 fixture 各一:①「空 stdout / 不可达」②「`no peer certificate available` + `Verify return code: 0` 但**无 PEM 标记**」,均断言 `status=exception`;**因守门在 parser regex(offline `re.search` 会执行),此 B3 假阴防护 offline 即可证**,无需依赖真机(区别于 SNI case 分支那类 collector-shell 逻辑)

#### 场景:参数安全进 shell(两道防线)

- **当** 评估 `endpoint` 参数注入面
- **那么** `endpoint` **必须**同时受**两道防线**约束:① schema `pattern`(`^[A-Za-z0-9._-]+:[0-9]{1,5}$`)挂在 JSON-Schema `properties` 下(`parameters` 用 `type: object` 包裹风格,**禁止**扁平写法——扁平会令 `pattern` 静默失效)、② collector 内 `{{ endpoint | sh }}`(shlex.quote)
- **且** 本变更 snapshot 测试**必须**含一份命令串级断言:捕获的主命令对 `endpoint` 经 `| sh` 引用(确保即便 pattern 漏改也有 shlex.quote 兜底)

#### 场景:verify code 解析跨 openssl 实现稳定

- **当** 该 inspector 解析 `Verify return code` 行
- **那么** **必须**用 `parse.format: raw` + `raw_extract_regex` **正则捕获数字**(非字段位置锚),以跨 OpenSSL 3.x 与 LibreSSL(macOS local target 自带)稳定取值;collector **禁止**在 shell 内 `printf` 拼 JSON(reason 文本未转义会破 JSON → 合法链断 finding 被误判成 exception)
- **且** `parse` 块**必须**声明 `columns: [verify_code, reason]`(`raw` + `raw_extract_regex` 强制要求非空 `columns`,且 named-group 数须 == `len(columns)`;漏写 → manifest load 即崩、registry errors≠[]),`output_schema` 的 `verify_code`/`reason` **必须** `type: string`(`raw` 捕获恒为 str,写 `integer` → jsonschema 拒 → 每次运行都 exception)
- **且** 本变更 snapshot 测试**必须**含 OpenSSL 3.x 与 LibreSSL **两份** stdout 样本 fixture 回归解析

#### 场景:SNI 镜像真实客户端行为(命令串级锁 + 真机验证)

- **当** 该 inspector 对 `host:port` 端点构造 `openssl s_client` 命令
- **那么** collector **必须**从 endpoint 切出 `host`,**仅当 `host` 为 hostname(含字母)时**发 `-servername "$host"`、**纯 IPv4 字面量端点跳过 SNI**(镜像真实客户端:浏览器访问 IP 不发 SNI;且 RFC 6066 禁 IP-SNI,发了部分 TLS 栈会 abort 握手致假阳)
- **且** 本变更 snapshot 测试**必须**含命令串级断言锁住 **SNI case 分支结构**:捕获命令同时含 hostname 臂 `*[A-Za-z]*) sni="-servername $host"` 与非 hostname(IPv4)臂 `sni=""`,且 `$sni` 串入 `openssl s_client`。**注意**:SNI 选择用 shell `case`(design D7,非 Jinja),故 hostname 与 IPv4 端点渲染出的 collect 命令串**字节相同**(两臂都在模板里,发不发 SNI 由 `case` 在 shell **运行时**决定)——`_CaptureTarget` 不跑 collector shell,offline **无法**用「命令串含/不含 `-servername`」区分两者。**SNI case 分支的执行正确性**(IPv4 端点实际跳 SNI 仍验对默认链)因此**只命令串级锁分支结构**,端到端正确性须真机 Demo Path(公网有效链 + 缺中间 CA + 一个 IP 端点)验证——offline fixture **不**声称锁 collector shell 执行正确性(见本变更 tasks.md 偏离登记)

### 需求:`linux.system.load_avg` 负载告警必须按持续信号（load5/load15）判定、不以单次 load1 触发

`linux.system.load_avg` 已按 per-core（`load / ncpu`）判定，但当前**门控用 `load1`（单次 1 分钟采样）**，致单核机一次进程突发即误标「严重」（真机 `load1=2.45` 单核 → critical 是已暴露的误报）。**必须**改为用**持续信号** `load5` 与 `load15` 的 per-core 比值门控:

- 告警门控**必须只用 `load5` 与 `load15`**（持续信号），**禁止**让 `load1` 参与任一 finding 的 `when`。`load1` 可保留 output 供展示，但**不得**产生 finding。
- severity 分级:`critical` 当 **`load15/ncpu >= crit_per_core` 且 `load5/ncpu >= crit_per_core`**（持续且高度过载，AND 而非 OR——load5 已回落即视为正在恢复、不 critical）;`warning` 当二者 `>= warn_per_core`（且未达 critical）。两者**必须**为 `parameters`（默认 `warn_per_core=1.0` / `crit_per_core=2.0`，沿用现值，numeric 免 `pattern`），可按机型在 schedule 覆盖。
- 既有 collect（`/proc/loadavg`+`nproc`，`parse: kv`，output 含 `load1/load5/load15/ncpu`）**不变**；仅 findings 的 `when`/`message` 变。message 保持英文（该 inspector 在 i18n `_BACKLOG`，无需迁移 allowlist）。`version` bump → finding-id churn（regression diff 旧 id resolved + 新 id added）。**不动** `system.uptime`（另一个固定阈值 load-avg inspector，非本次范围）。

#### 场景:单次 load1 尖峰不告警（病 2 修复，确定性锚）
- **当** 一台单核主机（`ncpu==1`）`load1` 短暂冲到 `2.45`，但 `load5`/`load15` 正常（如 `0.41`/`0.33`，即 `load15/ncpu==0.33 < warn_per_core`）
- **那么** `linux.system.load_avg` **禁止**产生任何 finding（瞬时尖峰非故障），报告**不得**因此被标 `warning`/`critical`

#### 场景:持续高负载才告警（load5+load15 双门）
- **当** `load15/ncpu` 与 `load5/ncpu` **同时** `>= crit_per_core`（持续过载且此刻仍在过载）
- **那么** **必须**产生 `critical` finding;若二者只达 `warn_per_core` 区间则产 `warning`;`load1` 无论多高都**不**单独决定 severity

### 需求:`linux.systemd.failed_units` 必须携带时间锚（含系统 uptime）并按 oneshot/历史校准 severity

`linux.systemd.failed_units` **必须**为诊断师提供「历史/开机一次性失败」与「近期失败」的区分依据，且**禁止**对**任何** failed 单元一刀切 `critical`（历史 cloud-init 残留把稳定运行数十天的整队标「严重」是已暴露的误判）。

- collect **必须**为每个 failed 单元补 **`Type`**（识别 `oneshot`）与 **失败时刻 `InactiveEnterTimestampMonotonic`**（开机以来微秒，免解析 systemd wall-clock 文本/时区），并补 **系统 `uptime_seconds`**（`/proc/uptime` 首字段，用 **shell 内建** `read -r uptime_seconds _ < /proc/uptime`——**不引入 `cut` 等未声明 binary**；`requires_binaries` 维持 `[systemctl, awk]` 不变）。**禁止**使用 gawk-only 的 `systime()`/`strftime()`/`mktime()`（inspector `targets:[local,ssh]` 会命中 mawk/busybox/BSD awk，致采集崩溃 / 假 `status=exception`）——boot/uptime 一律走 `/proc/uptime` + POSIX。`/proc/uptime` 读取失败 **必须** fail-loud（exit≠0）。per-unit 迭代**必须**用**换行分隔的 `while IFS= read -r unit`**（**禁止** `for unit in $(...)` 的空白分词，否则含空格/转义的单元名会被切碎），并保留既有 awk JSON-escape（unit 名可含 `\`/`"`）。
- 带 `for_each` 的列表输出，可迭代顶层键**必须**取自 `results`/`items`/`records`（套件契约）——本 inspector 用 **`results`**（数组项 `{unit, type, inactive_monotonic_us}`）。output_schema **必须** `required: [uptime_seconds, results]`;**空失败集**仍 **必须** emit 合法顶层对象 `{"uptime_seconds":N,"results":[]}`（`parse_json` 拒顶层数组 / 非对象）。
- **数值字段必须 emit 为裸 JSON 数字（不加引号）**:`uptime_seconds`（`number`）、`inactive_monotonic_us`（`int`）的 awk `printf` **必须**用 `%d`/`%s`-数值形落在引号外（如 `..."inactive_monotonic_us":%d...`）——output_schema 声明 `int`/`number`，若误加引号成字符串则 **jsonschema 校验失败 → `status=exception`**（伪失败，发生在 findings 之前）;只有 `unit`（`string`）走引号 + JSON-escape。
- severity 分级（`for_each: "results as u"`）:**oneshot、失败在开机窗口内、且系统已长跑**（`u.type=='oneshot' and u.inactive_monotonic_us>0 and u.inactive_monotonic_us <= boot_window_seconds*1000000 and uptime_seconds >= min_uptime_seconds`）→ **`warning`**（长跑机的开机一次性历史残留）;其余（非 oneshot、失败晚于开机窗口、**或系统 uptime 不足**、或无失败时刻）→ **`critical`**。`boot_window_seconds`（默认 `180`）与 `min_uptime_seconds`（默认 `3600`）**必须**为 `parameters`。
- **`uptime_seconds >= min_uptime_seconds` 门是必须的**:`InactiveEnterTimestampMonotonic` 是 boot-relative，仅「oneshot 在开机窗口内失败」**不足以**判历史——刚重启的机器上一个本次开机失败的 oneshot 也满足，那是「刚崩」、应 `critical`。须叠加「系统已长跑」才把「长跑机的开机历史残留」与「刚重启后失败」分开。
- message 从单条聚合（`{failed_names}`）改为每单元一条（含 unit + Type）→ **finding 数量 1→N、finding-id churn**（可接受、列入重跑清单）。**message 模板用下标语法** `{u[unit]}` / `{u[type]}`（`str.format` 无法用属性式 `{u.unit}`；属性式 `u.type` 只能在 `when` DSL 表达式里用——与 `docker/containers_restart_loop.yaml` 范式一致：`when: c.restart_count`、`message: {c[name]}`）。该 inspector 是 i18n `_MIGRATED_ALLOWLIST` 唯一成员:**每条 FindingRule（warning + critical 两条）的 message 各**必须保留 ≥1 CJK 字符（`test_message_contains_cjk` 按 FindingRule 参数化、逐条校验）;**只注入**循环变量 `u[...]` 与 `output_schema.properties` 已声明的键(`uptime_seconds`),**禁止**注入 parameter 名(`boot_window_seconds`/`min_uptime_seconds` 不在 output_schema → 触发 `test_injected_fields_are_declared` 的 if-inject-then-declared 失败),也不得引用已删除的 `failed_names`。

#### 场景:长跑机的开机一次性历史残留不拉整队 critical（病 1 修复，确定性锚）
- **当** 一台 `uptime_seconds` 远超 `min_uptime_seconds`（如 up 36 天）的主机有 `cloud-final.service`（`Type=oneshot`）在开机窗口内（`inactive_monotonic_us <= boot_window_seconds*1e6`）失败、无近期失败
- **那么** 该 finding **必须**为 `warning`（**禁止** `critical`），故报告级 `aggregate_severity` 不被这条历史残留拉到「严重」;message **必须**标明其开机一次性/历史性质

#### 场景:刚重启后的开机失败仍 critical（不误降，fresh-reboot 锚）
- **当** 一台 `uptime_seconds < min_uptime_seconds`（刚重启不久）的主机有 `oneshot` 单元在本次开机窗口内失败
- **那么** 该 finding **必须**为 `critical`（**禁止**降为 warning）——低 uptime 下无法判定是历史残留，可能是当前故障

#### 场景:常驻服务失败仍 critical（不误降）
- **当** 一台主机的 `zerotier-one.service`（`Type=notify`/`simple` 等非 oneshot）处于 failed
- **那么** 该 finding **必须**为 `critical`（常驻网络服务 failed 是真问题，即便与其它失败彼此独立——「独立 vs 连锁」是诊断师叙事职责、非 severity）

### 需求:`linux.cpu.top_processes` 必须按进程存活时长（etimes）门控、不以单次 `ps %cpu` 快照触发严重度

`linux.cpu.top_processes` 当前用 `ps -eo pid,pcpu,... | head -n 10` 单次快照,并以 `float(p.cpu_pct) >= 90.0 → critical` 直接定级。但 procps-ng `ps(1)` 的 `%cpu` 定义为 **`cputime/realtime`**(累计 CPU 时间 ÷ 进程**自诞生**的存活时长),**非瞬时利用率**:一个刚 spawn、几乎全程吃满单核活约 1–2s 的短命进程(如 journalctl 扫大 journal——可能正是同轮巡检里别的 inspector 自起的)单次快照即读到 `~100%`,被误标「严重」(真机 `bandwagon` 上 `journalctl pid 33948` 100% CPU、进程随即消失、`load 0.00` 是已暴露的误报)。**必须**叠加「进程已存活足够久」的持续性门控,与 `linux.system.load_avg` 用 `load5/load15` 取代单样本 `load1` 同源:

- collect 命令**必须**加 `etimes` 字段(`ps -eo pid,pcpu,pmem,etimes,comm --sort=-pcpu --no-headers | head -n 10`);`etimes` 是 procps 的「进程存活秒数」(整数)。output_schema **必须**新增 `etimes: { type: string }`(`parse.format: table` 产出恒为 str,DSL 用 `int()` 转换)。`parse.columns` **必须**同步为 `[pid, cpu_pct, mem_pct, etimes, comm]`(列序与 `-eo` 字段序一致)。
- **必须**新增 parameter `min_etimes`(`type: number`,`exclusiveMinimum: 0`,默认 `10`,沿用 `load_avg` 的 numeric-阈值免 `pattern` + `exclusiveMinimum: 0` 校验范式)。`exclusiveMinimum: 0` 拒绝 schedule 把闸覆盖为 `0`/负(否则 `>= min_etimes` 对任意非负 etimes 恒真、门控退化为现状)。默认 `10`(非 60)是 SLO 取舍:既滤掉 `etimes` 0–2s 的一次性进程,又**不**让真正刚跑飞的进程隐身一整分钟伤本 inspector「诊断 CPU saturation」的使命。
- 两条 finding(`critical` ≥90 / `warning` 70–90)的 `when` **必须**前置 age 闸 `int(p.etimes) >= min_etimes`(与 `for_each: "rows as p"` 同作用域)。`message` 沿用下标语法 `{p[comm]}`/`{p[pid]}`/`{p[cpu_pct]}`(`str.format` 无属性式;属性式 `p.cpu_pct`/`p.etimes` 仅 `when` DSL 可用),保持英文(该 inspector 在 i18n `_BACKLOG`,无需迁移 allowlist)。`version` bump → finding-id churn(regression diff 旧 id resolved + 新 id added,可接受)。
- 本 inspector **procps-only**:`etimes`(及既有 `-eo` 自定义字段)在 busybox/Alpine ps 不被支持,**禁止**为兼容引入 `etime`(`[[DD-]hh:]mm:ss`)字符串解析 fallback(过度工程)。collect 注释**必须**写明 procps-only 假设。macOS/BSD `ps %cpu` 是「decaying average over a minute」语义不同,但本 inspector 仅 `targets:[local,ssh]` 的 Linux,机制对目标平台成立,不影响修法。
- **不支持平台必须 fail-loud,禁止静默假绿**:不支持 `etimes` 的 `ps` 报错后,`ps … | head` 的 pipeline 退出码取末段 `head`(恒 0)**掩盖** `ps` 的非零退出,且 runner **不**校验主 collect 命令的退出码(仅校验 timeout / parse / schema / 前置 probe)——故仅靠退出码**无法** fail-loud,会退化成 `status=ok` + 0 finding 的假「全部正常」。因此 `output_schema` 的 `rows` 数组**必须**声明 `minItems: 1`:正常 procps 主机恒有 ≥1 进程(init + 内核线程),`ps … | head` 必出 ≥1 行;**0 行 ⟹ 采集失败/平台不支持**,经 output_schema 校验失败 → runner `status=exception`(`output_schema_mismatch`)。这是 inspector 级 fail-loud 守卫(不改 runner、不改命令)。
- **不动 severity 分级语义**:多核机上「单进程占满 1 核 → critical」的过度归因(按 `ncpu` 归一 / `critical→warning`)是另一独立行为变更,不在本需求范围。本需求只加 age 闸、不改阈值与 severity 映射。

#### 场景:年轻进程的 `%cpu` 伪影不告警（确定性锚）

- **当** `ps` 快照里一个进程 `cpu_pct >= 90`(如 journalctl `100`,`%cpu = cputime/realtime` 对刚起的 CPU-bound 短命进程读数虚高)但 `etimes < min_etimes`(如 `etimes==1`,默认 `min_etimes==10`)
- **那么** `linux.cpu.top_processes` **禁止**为该进程产生任何 finding(瞬时/自起进程非故障),报告**不得**因此被标 `warning`/`critical`

#### 场景:持续占用 CPU 才告警（age 闸 + cpu 阈值双门）

- **当** 一个进程 `etimes >= min_etimes`(已存活足够久,确为持续占用)**且** `cpu_pct >= 90`
- **那么** **必须**产生 `critical` finding;若 `etimes >= min_etimes` 且 `cpu_pct` 在 `[70, 90)` 则产 `warning`;`etimes` 不足 `min_etimes` 的进程**无论 `cpu_pct` 多高**都**不**告警

#### 场景:collect 输出 `etimes` 列且命令串级锁

- **当** 本变更的 snapshot / 命令串级锁测试捕获 `linux.cpu.top_processes` 的主命令
- **那么** 捕获命令**必须**含 `ps -eo pid,pcpu,pmem,etimes,comm`(字段含 `etimes` 且列序固定),`output_schema`/`parse.columns` **必须**含 `etimes`,两条 finding 的 `when` **必须**含 `int(p.etimes) >= min_etimes`

#### 场景:不支持平台的空采集 fail-loud（确定性锚,禁止静默假绿）

- **当** `ps -eo …,etimes,… | head` 在不支持 `etimes` 的 `ps`(busybox/BSD)上报错、stdout 为空(`ps` 非零退出被 `| head` 掩盖,runner 不 gate 主命令退出码),解析得 `rows == []`
- **那么** `linux.cpu.top_processes` **必须** `status == "exception"`(`output_schema` 的 `rows` `minItems: 1` 校验失败 → `output_schema_mismatch`),**禁止** `status == "ok"` + 0 finding 的假「全部正常」;本变更**必须**含一份 offline 回归测试喂空 stdout 断言 `status == "exception"`
