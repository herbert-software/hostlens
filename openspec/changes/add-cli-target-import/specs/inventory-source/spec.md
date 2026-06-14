## 新增需求

### 需求:`InventorySource` 必须定义纯解析、零网络 IO 的来源接口

系统必须提供 `InventorySource` Protocol,把「一种清单来源」抽象成可插拔单元(在**扩展性**维度镜像 `notifier-protocol`:加一个来源 = 新增一个实现类 + 显式注册;**分派语义不同**——见嗅探需求)。接口必须含:

- `name: str` —— 注册到 source registry 的 key
- `can_handle(ref: str) -> bool` —— 按内容/后缀嗅探该 ref 是否归本 source
- `parse(ref: str) -> list[CandidateTarget]` —— 把 ref 解析成候选列表

`parse` **必须**纯解析、**禁止**发起任何网络连接(探活属 `TargetProbe`,与解析正交);**禁止**对主机名做 DNS 解析(只取清单里的显式地址字面量)。解析失败(语法畸形)必须 raise `ConfigError`,**禁止**因「连不上」而失败。

#### 场景:parse 合法来源返回候选列表
- **当** 一个合法的来源文件被 `InventorySource.parse(ref)` 解析
- **那么** 返回 `list[CandidateTarget]`,每项含连接所需字段,无网络 IO、无 DNS 解析发生

#### 场景:parse 离线可跑
- **当** 在无网络环境调 `parse`
- **那么** 必须正常返回候选列表(解析不依赖目标主机可达)

#### 场景:parse 畸形输入抛 ConfigError
- **当** 来源文件语法畸形(YAML 语法错 / ssh_config 不可解析)
- **那么** 必须 raise `ConfigError`(语法错语义,非连接错),不返回部分结果

### 需求:`CandidateTarget` 必须是未验证候选、禁止明文密钥、且 name 派生必须满足 `_NAME_PATTERN`

系统必须定义 `CandidateTarget`(Pydantic v2,`extra="forbid"`):未经校验/探测的纳管意图,区别于已校验的 `TargetEntry`(避免来源层污染配置层契约)。字段必须含 `name` / `type`(**`Literal["local","ssh"]`**——首版 import 只产这两类,Pydantic 在 parse 期即拒 docker/k8s 等) / 连接字段(`host`/`user`/`port`)/ 凭据**引用**(`password_env` / `passphrase_env` / `key_path`)/ 来源元数据。**禁止**含任何明文密钥字段。

**name 派生契约**(挡 `_NAME_PATTERN` 跨层缺口):`CandidateTarget.name` 最终要提升为受 `^[a-z][a-z0-9_-]{0,63}$`(`targets/config.py:_NAME_PATTERN`)约束的 `TargetEntry.name`。来源的原始标识(ssh_config `Host` 别名 / yaml dict-key)**可能违反**该 pattern(含大写 / 点 / 数字开头 / 超 64 字符)。故 source **必须**在产出 `CandidateTarget` 时把原始标识**确定性规范化**为合法 name:小写化 → 非法字符(含点)替换为 `-` → 折叠连续 `-` → 去掉前导非 `[a-z]` 字符 → 截断 64;规范化后仍为空 / 不匹配 pattern → raise `ConfigError(kind="invalid_target_name", ...)` 指明原始标识。规范化产生的 `原始标识 → 派生 name` 映射必须可被上层(`ImportPlan`)展示。同一批内两个不同候选规范化到**同一 name** → raise `ConfigError(kind="ambiguous_target_name", ...)`(不静默二选一)。

#### 场景:候选只带凭据引用不带明文
- **当** 构造一个 SSH 候选
- **那么** 凭据以 `*_env`(环境变量名)或 `key_path` 引用形式存在,模型**无** `password` 明文字段

#### 场景:合法标识直接成 name
- **当** ssh_config `Host bwg bandwagon` / `HostName 100.76.213.134`(别名已合法)
- **那么** `CandidateTarget.name` 为 canonical 别名(如 `bandwagon`),无需改写

#### 场景:非法标识规范化并在 plan 展示映射
- **当** 来源标识为 `Web-Prod.example`(含大写 + 点)
- **那么** 规范化为 `web-prod-example`,且 `原始标识 → 派生 name` 映射可被 `ImportPlan` 展示;**禁止**让非法 name 走到 Pydantic `TargetEntry` 才 `ValidationError`

#### 场景:不可规范化的标识被拒
- **当** 来源标识规范化后为空(如纯符号 `***`)
- **那么** raise `ConfigError(kind="invalid_target_name")` 指明原始标识,该候选不产出

#### 场景:规范化撞名被拒不静默
- **当** 两个不同来源条目规范化到同一 name
- **那么** raise `ConfigError(kind="ambiguous_target_name")`,**禁止**静默丢弃其一

### 需求:来源含明文密钥必须 fail-closed 拒绝(单一策略,不映射)

来源文件出现明文密码 / passphrase **字段名**(`password`/`passphrase`)时(按字段名判定,**非**对其他字段值嗅探「像不像密码」),`InventorySource.parse` **必须** raise `ConfigError(kind="plaintext_secret_forbidden", field=<字段名>)`,提示用户改用 `${VAR}` env 引用,**绝不**把明文读入 `CandidateTarget` 或任何中间态。`ConfigError` 的 `field` 只取**字段名**(`"password"`),**禁止**把明文 value 放进 `ConfigError` 的 `field`/`extra`/任何字段(`ConfigError.__str__` 会渲染 `extra` 的 `key=value`,误传 value 即经异常 `__str__` 泄露明文)。**禁止**「自动映射成 env 名」分支(env 名无从推断,且映射会让明文经中间变量 / 日志 / plan 渲染泄露;首批 tizi cred-less、ssh_config 本无明文 password 字段、yaml 标准 schema 不设明文字段,映射分支无真实驱动场景)。

#### 场景:明文密码被 fail-closed 拒
- **当** yaml 来源出现 `password: hunter2`(明文)
- **那么** raise `ConfigError(kind="plaintext_secret_forbidden")`,明文**不**进入 `CandidateTarget` / 不进日志 / 不进任何渲染

### 需求:source registry 必须显式装配、`--source` 显式优先、嗅探多命中必须报歧义

系统必须提供显式装配函数 `register_default_sources(registry)`(对齐 `register_default_tools` 模式,**禁止** module-level 单例)。分派语义(本仓首个内容嗅探 registry,与 `notifier`/`inspector` 的显式 type 查表**不同构**,故须显式定义冲突裁决):CLI `--source` 显式指定时**永远优先**、跳过嗅探;缺省嗅探时按各 source `can_handle` 选中;**多个 source `can_handle` 同时命中 → raise 结构化 ambiguous 错误(CLI 映射 exit 2),禁止静默取第一个**;无 source 命中 → 结构化「未知来源」错误(exit 2)。

**两 source 的 `can_handle` 启发式(钉死,保首批主用例 `~/tizi/hosts` 无后缀文件唯一命中 ssh_config)**:
- `ssh_config.can_handle(ref)` = 文件名匹配 `*config` 或 basename == `hosts`(覆盖 `~/.ssh/config`、`~/tizi/hosts`)**或** 首若干非空非注释行含 OpenSSH 指令(`Host `/`HostName `/`Match ` 前缀,大小写不敏感)。
- `yaml.can_handle(ref)` = 扩展名 `.yml`/`.yaml` **且** `yaml.safe_load` 顶层是 mapping(dict)。
- **首批对账**:`~/tizi/hosts`(无 `.yml` 扩展,basename=`hosts` + 含 `Host ` 行)→ 仅 ssh_config 命中(yaml 因无 `.yml` 扩展不命中),唯一命中、可测;`.yml` inventory → 仅 yaml 命中。两者交集仅在「`.yml` 文件内容恰含 `Host ` 行」等罕见情形,届时多命中报 ambiguous(已有场景)。

#### 场景:--source 显式优先跳过嗅探
- **当** 传入 `--source ssh_config <path>`
- **那么** 直接用 `ssh_config` source,不跑任何 `can_handle` 嗅探

#### 场景:嗅探唯一命中
- **当** 缺省传入一个 `~/.ssh/config` 路径
- **那么** registry 经 `can_handle` 选中 `ssh_config` source

#### 场景:嗅探多命中报歧义
- **当** 缺省传入一个 `.yml` 文件,而 `yaml` 与某未来 source 的 `can_handle` 同时返 True
- **那么** raise 结构化 ambiguous 错误(CLI exit 2),提示用 `--source` 显式指定,**禁止**静默取第一个

#### 场景:无 source 命中返回结构化错误
- **当** 传入一个无任何 source `can_handle` 的 ref
- **那么** 返回结构化「未知来源」错误(CLI exit 2),**禁止**裸异常透传

### 需求:`ssh_config` source 必须解析 OpenSSH config、用显式地址不靠 DNS、约束 `Include` 路径与 `IdentityFile`

系统必须提供 `ssh_config` source,解析 OpenSSH client config 格式:`Host`(别名)/ `HostName`(连接地址)/ `User` / `Port`(非整数 → `ConfigError(kind="invalid_ssh_config")`,不裸 `ValueError`)/ `IdentityFile` / `AddressFamily`。**实现注**:为兑现下面的 `Include` 解析语义 + 路径边界 + `Match`/通配跳过 + 不回显内容,**须手写 line parser**;**不得**整体委托 `asyncssh.config.SSHClientConfig`(它为连接期设计、自动解析 `Include`/`Match`/通配,无法在 SDK 层施加路径边界与 realpath 校验,会静默绕过 `include_path_escape` 安全门)。约束:

- **显式地址**:连接地址取 `HostName` 字面量,**禁止**对 `Host` 别名做 DNS 解析(防撞 FakeDNS / split-horizon)。**`HostName` 缺失**时(`Host` 块无 `HostName`):连接地址取 canonical `Host` token 字面量(OpenSSH fallback 语义,asyncssh 连接期再解析),**仍不**由本 source 主动 DNS 解析。
- **多别名**:取一个 canonical name 经上文 name 派生契约规范化。
- **`Include` 解析语义(OpenSSH 兼容)**:`~` 展开;**相对路径锚到 `~/.ssh/`**(OpenSSH user-config 规则,**非**进程 CWD);**glob 展开**(`Include ~/.ssh/config.d/*` 展开为每个匹配文件;无匹配视为空、不报错)。首版支持**一层** `Include`。
- **`Host *` 全局默认 + 通配/Match 跳过**:`Host *`(精确通配全集)块的指令作为**全局默认**应用到每个 host(`User`/`Port`/`IdentityFile` 等;**host 专属指令优先**,不建模完整 first-match 顺序);其他通配 `Host`(`*.x` / `?`)与 `Match` 块**跳过并 log**(不静默吞、不报错——需 pattern 匹配,首版不实现)。
- **`User` 缺省**:`Host` 块无 `User` 且无 `Host *` 默认时,提升期 `User` 缺省取**本机当前用户名**(`getpass.getuser()`,OpenSSH 语义),**禁**空字符串(空 user 会断连)。
- **`Include` 路径边界 + TOCTOU(realpath + O_NOFOLLOW)**:ssh_config 是操作者可信文件(同 `target add` 信任级)。解析出的每个路径(pattern 自身 + 每个 glob 匹配)的 `os.path.realpath` 必须落在**用户 home 树** ∪ **symlink-resolve 后的 `~/.ssh` 树**内——**放行**文档化的 `Include ~/tizi/hosts`(home 内、`~/.ssh` 外)与 `~/.ssh -> ~/dotfiles/ssh` dotfiles symlink,**拒绝** `Include /etc/shadow`(两树皆外)。边界用 `os.path.commonpath([root, realpath]) == root`(任一 root 命中即放行;`ValueError` 视为不命中);**对 pattern 自身先查**(越界路径不论存在与否都拒,因 realpath 解析既有前缀)。实际读取必须 `os.open(path, O_RDONLY | O_NOFOLLOW)`(拒最后一跳 symlink,关 TOCTOU 窗口)。越界 / O_NOFOLLOW 失败 → `ConfigError(kind="include_path_escape")`;异常文本**禁止**回显文件内容,只报路径 kind。
- **`IdentityFile` 仅作引用**:parse 阶段把 `IdentityFile` 透传为 `key_path` 引用,**禁止**读私钥内容 / `open` / `stat` 该路径;`~` 展开 + `${VAR}` fail-closed 拒绝见 key_path 需求。
- **`AddressFamily`**:`SSHEntry` 无 `address_family` 字段,故该指令**不落盘**;IPv6 寻址意图由 `HostName` 的 IPv6 字面量本身承载,spec 不声称「保留 AddressFamily 指令」。

#### 场景:多别名取 canonical name + HostName
- **当** 解析 `Host bwg bandwagon` + `HostName 100.76.213.134`
- **那么** 产出 `CandidateTarget`,`name` 为规范化 canonical 别名、`host` 为 `100.76.213.134`(取自 HostName,非对别名解析)

#### 场景:HostName 缺失取 Host token 字面量不 DNS 解析
- **当** 某 `Host gw` 块无 `HostName` 行
- **那么** `CandidateTarget.host` 取 `gw` 字面量(OpenSSH fallback,asyncssh 连接期解析),本 source **不**主动 DNS 解析该别名

#### 场景:IPv6 字面量 HostName 保留(AddressFamily 指令丢弃)
- **当** 解析 `HostName fd7a:115c::6874` + `AddressFamily inet6`
- **那么** `CandidateTarget.host` 为该 IPv6 字面量;`AddressFamily` 指令不落盘(SSHEntry 无对应字段)

#### 场景:Include 相对路径锚到 ~/.ssh
- **当** ssh_config(在 `~/.ssh/config`)含 `Include config.d/hosts`(相对路径)
- **那么** 锚到 `~/.ssh/config.d/hosts`(**非** 进程 CWD)解析其 Host 块

#### 场景:Include glob 展开
- **当** ssh_config 含 `Include ~/.ssh/config.d/*`,目录下有 `a.conf`/`b.conf`
- **那么** glob 展开为两文件,各自的 Host 块都被纳入

#### 场景:Include home 内、~/.ssh 外的路径放行(tizi 模式)
- **当** `~/.ssh/config` 含 `Include ~/tizi/hosts`(在 home 树内、`~/.ssh` 外)
- **那么** 经 home 树边界放行,解析 `~/tizi/hosts` 的 Host 块(兑现 tizi README 文档化用法)

#### 场景:Include symlink 逃逸经 realpath 被拒
- **当** ssh_config 含 `Include ~/.ssh/evil`,而 `~/.ssh/evil` 是指向 home 树外文件的 symlink
- **那么** 经 `os.path.realpath` + `commonpath` 校验判越界 → raise `ConfigError(kind="include_path_escape")`,异常文本**禁止**含被包含文件内容

#### 场景:Include 绝对路径越界被拒
- **当** ssh_config 含 `Include /etc/shadow`(home 树 ∪ ~/.ssh 树皆外)
- **那么** raise `ConfigError(kind="include_path_escape")`(不论 `/etc/shadow` 是否存在),不回显内容

#### 场景:Host * 提供全局默认、host 专属优先
- **当** ssh_config 含 `Host *`(`User globaluser` / `Port 2200`)+ `Host foo`(仅 HostName)+ `Host bar`(HostName + `User specific`)
- **那么** `foo` 取默认 `user=globaluser`/`port=2200`;`bar` 的 `user=specific`(host 专属优先)、`port=2200`(默认补缺)

#### 场景:缺 User 缺省取本机用户名非空
- **当** 一个 ssh 候选无 `User` 且无 `Host *` 默认
- **那么** 提升出的 `SSHEntry.user` 为 `getpass.getuser()`(本机当前用户名),**非**空字符串

### 需求:`key_path` / `IdentityFile` 必须仅展开 `~`、对 `${VAR}` fail-closed 拒绝

source **必须**对 `IdentityFile`/`key_path` **仅展开 `~`**(`os.path.expanduser`),**任何 `${VAR}`(整串或部分)一律 raise `ConfigError(kind="key_path_placeholder_forbidden", ...)`、禁止 `expandvars`**。背景:`targets/config.py:_PLACEHOLDER_ALLOWED_FIELDS` 仅允许 `password`/`passphrase` 含 `${VAR}`,`key_path` 是非 secret 字段;loader 的占位校验用 `_PLACEHOLDER_PATTERN.fullmatch`——**整串** `key_path: ${KEY_FILE}` 被 `env_placeholder_not_allowed_here` 拒,**部分** `key_path: ${KEY_DIR}/k.pem` 不 fullmatch → 静默放行为无效字面串。理由:`key_path` 落盘是**明文 value**(不享 `${VAR}` 占位保全);若 source 层 `expandvars`,会把整个 env 值(可能是 `/run/user/.../secrets/...` 等敏感路径)走私进明文落盘的 `key_path` —— fail-closed 拒绝比展开安全。parse 期**禁止** `open`/`stat` 该路径(呼应 IdentityFile 仅作引用)。

#### 场景:IdentityFile 的 ~ 展开为绝对路径
- **当** ssh_config `IdentityFile ~/.ssh/id_ed25519`
- **那么** `CandidateTarget.key_path` 为展开后的绝对路径(如 `/Users/x/.ssh/id_ed25519`),不含 `~`

#### 场景:key_path 含 `${VAR}` 被 fail-closed 拒
- **当** `IdentityFile ${KEY_DIR}/k.pem`(整串或部分占位)
- **那么** raise `ConfigError(kind="key_path_placeholder_forbidden")`,**不** `expandvars`(防 env 值走私进明文落盘的 key_path),**禁止**把含 `${VAR}` 的 key_path 落进候选

### 需求:`yaml` source 必须解析 Hostlens 标准 inventory schema(schema 在本 spec 钉死)

系统必须提供 `yaml` source,解析 **Hostlens 标准 inventory schema**(本 spec 定义,非任意第三方格式)。最小 schema:

- 顶层可选 `defaults`(dict)—— 字段**按目标 `type` 的合法字段集过滤后**并入每个主机条目(条目显式字段优先)。**禁**无脑并入:`defaults: {user: root}` 对 `local` 条目须**跳过** `user`(`LocalEntry` `extra="forbid"` 无 user,无脑并入会让整批 local 条目提升 `ValidationError`→`invalid_candidate`,误伤常见用法)——只把 defaults 中属该条目 type 允许字段集的键并入。
- 顶层其余 key 为**分组键**(任意名,如 `hosts_proxy`),值为「主机条目 dict」(key = 主机标识,经 name 派生契约规范化为 `CandidateTarget.name`)。
- 每个主机条目:必含 `type`(**`Literal["local","ssh"]`**,其他值如 `docker`/`k8s` → `ConfigError`,首版 import 只产 local/ssh);`ssh` 条目**必含** `host`,可选 `user`/`port`/`password_env`/`passphrase_env`/`key_path`;`local` 条目字段集 = `{type}`(对齐 `LocalEntry`,**无** host/user)。
- 凭据引用字段名固定为 `password_env` / `passphrase_env` / `key_path`;**`password_env`/`passphrase_env` 的值必须匹配 `^[A-Z_][A-Z0-9_]*$`(`_ENV_VAR_NAME_PATTERN`,复用 `_validate_env_var_name`)**,否则 raise `ConfigError(kind="invalid_env_var_name")`——否则 `password_env: "lower case"` 会落盘成 `${lower case}`、loader fullmatch 失败静默存字面、asyncssh 拿字面当密码。**无**明文 `password` 字段(出现即触发明文 fail-closed 拒绝)。

任意第三方 YAML(字段名不符本 schema,如 tizi `inventory.yml` 的 `tailscale_ipv4` 等)**不在** `yaml` source 处理范围;`ssh` 条目缺 `host` 等必填字段 → raise `ConfigError`(指明缺失字段 + 提示「若这是 OpenSSH config 请用 `--source ssh_config`」),**禁止**静默吞或产出连接字段为空的候选。

#### 场景:standard yaml + defaults 按 type 过滤合并
- **当** 解析 `defaults: {user: root}` + `hosts_proxy: {web1: {type: ssh, host: 10.0.0.1}}` + `hosts_local: {l1: {type: local}}`
- **那么** `web1` 并入 `user=root`;`l1` **跳过** `user`(local 无此字段)→ `CandidateTarget(name="l1", type="local")` 不因 defaults 进 `invalid_candidate`

#### 场景:非法 password_env 值被拒
- **当** yaml 条目 `password_env: "lower case"`(不匹配 `^[A-Z_][A-Z0-9_]*$`)
- **那么** raise `ConfigError(kind="invalid_env_var_name")`,不落盘成无效 `${...}` 占位

#### 场景:type 非 local/ssh 被拒
- **当** yaml 条目 `type: docker`
- **那么** raise `ConfigError`(首版 import 只产 local/ssh)

#### 场景:local 条目仅需 type
- **当** 解析 `hosts_local: {demo-localhost: {type: local}}`
- **那么** 产出 `CandidateTarget(name="demo-localhost", type="local")`,**不**要求 host(对齐 `LocalEntry`)

#### 场景:ssh 条目缺 host 报错并指向 ssh_config
- **当** 解析一个 `ssh` 条目缺 `host`(或整个文件是非标 inventory,如 tizi `inventory.yml`)
- **那么** raise `ConfigError`,指明缺失字段 + 提示「若这是 OpenSSH config 请用 `--source ssh_config`」,**禁止**静默吞
