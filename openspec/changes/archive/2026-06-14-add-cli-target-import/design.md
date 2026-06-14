## 上下文

`hostlens target add` 单台、无探活;批量迁移几十台不可接受。本提案把「批量纳管」实现为一条四层只读→写流水线。完整背景(用户目标、Codex/架构 review 收敛、首批 = `ts.mac-mini:~/tizi` 6 台、Gate 0 实测)见 [docs/roadmap/target-onboarding.md](../../../docs/roadmap/target-onboarding.md)。

现状关键事实(已核实):
- `targets/config.py` 有 `load_targets_config`(缺省返空、`${VAR}` **read 期展开**)、**无 save**;写路径目前在 `cli/target.py` 里手搓 raw yaml round-trip(`_load_raw_targets_dict` + `yaml.safe_dump`,**非原子**)。
- `SSHEntry.key_path` / `password` / `passphrase` 全 `Optional[None]` → **cred-less SSH 已可表达**。
- `capabilities` 是 **lazy-probe**(`targets/base.py`,首次 `exec` 才填)。
- 既有写命令 safety:`_refuse_root_for_write(EUID==0)` + 非交互缺 `--yes` 退 1。
- **Gate 0 实测**:asyncssh 直连 Tailscale SSH,tizi 6/6 cred-less 连通(全 Debian13/podman)。

## 目标 / 非目标

**目标:**
- `InventorySource` 可插拔来源抽象(ssh_config + yaml 首发),`TargetProbe` 复用 `ExecutionTarget` 并发探测,`ImportPlan` 可读 diff,`save_targets_config` 原子幂等写。
- 首批 tizi 6 台经 `hostlens target import` 一条命令纳管。
- 写 safety(dry-run/yes/拒 root/非交互)与既有写命令一致。

**非目标:**
- 不碰 MCP(提案 B)、不做 NLSource/CSV、不重构 `target add/remove`、不扩 targets.yaml 存富元数据、不做 docker/k8s 导入。

## 决策

### D-1:三个 capability touch(新 `inventory-source` + `target-import`,改 `execution-target`)
**选择**:来源抽象独立成 `inventory-source`,探测+规划+CLI 成 `target-import`,写盘+命令枚举归既有 `execution-target`(MODIFY,见 D-4 / M-2)。
**理由**:镜像本仓既有「插件抽象 / CLI 命令分离」粒度——`notifier-protocol` 与 `notify-cli-command` 分离、`inspector-plugin-system` 与 `inspect-cli-command` 分离。**措辞校准**:`InventorySource` 是「加一个 Python 实现类 + 显式注册」,精确同构 `notifier-protocol`;`inspector` 的「加一个=加一个文件」主要指加 YAML manifest(无 Python-class 等价),故只作哲学旁证、不并列为同构。
**备选**:❌ 一个 capability 揉所有——失去独立契约展示点,spec 需求过载;❌ 把 `save_targets_config`/命令枚举留 `target-import`——与 `execution-target` 的 `TargetsConfig` 持久化 + 命令集契约割裂(架构 review M-1/M-2)。

### D-2:首批 tizi 走 `ssh_config` source;`yaml` source 是 Hostlens 标准 schema(不是任意第三方 YAML)
**选择**:`ssh_config` source 解析 OpenSSH config 格式 ——`~/tizi/hosts` **就是**标准 ssh config(`Host bwg bandwagon` / `HostName 100.76.213.134` / `AddressFamily inet6`),直接覆盖 6 台。`yaml` source 解析**本提案定义的** Hostlens 标准 inventory schema(每条 `name`/`type`/`host`/凭据引用),**不**解析 tizi 的 `inventory.yml`(其字段是 `tailscale_ipv4`/`public_ipv4` 等非标名)。
**理由**:tizi `inventory.yml` 每台有 5 个地址候选(public_v4/v6、tailscale_v4/name、fqdn),通用 yaml source 无从知道选哪个;而运维真实选择**已编码在 `hosts`**(选了 tailscale 地址)。让 ssh_config source 吃 `hosts` 天然拿到正确连接参数,避免在 yaml source 里硬编码 tizi 字段名或猜地址。`inventory.yml` 的富元数据 enrich 是独立 spec 决策(本提案非目标)。
**备选**:❌ yaml source 支持「字段映射配置」吃任意第三方 YAML——首版过度工程,且地址歧义无通用解。

### D-3:`TargetProbe` 复用 `ExecutionTarget`,先 exec 判**可达性**(capabilities 不落盘);经 `CandidateTarget→TargetEntry` 提升构造单 target
**选择**:(a) `CandidateTarget`(来源层宽松)先**提升**为 `TargetEntry`(经 `LocalEntry`/`SSHEntry` 校验),再经 `build_one_target(entry, settings)` 构造探测 target——`build_one_target` **归 `targets/registry.py`**(与 `build_registry_from_config` 同模块、同「单 entry 构造 + register 注入 `_entry`」SOT;**不**放 `target-import` 的 `probe.py`,否则构造契约被 consumer capability 私有持有、与 D-4「持久化 helper 归 owner」判据自相矛盾;probe.py import 它)。其实现**把单个 `TargetEntry` 包成 `TargetsConfig(targets=[entry])` 复用 `build_registry_from_config(config, settings).get(name)`**:这条路径既 construct **又 register**,而 `register` 才注入 `target._entry`(`registry.py`)。**禁止**裸 `SSHTarget(name=...)` 不 register——`_entry=None` 时首次 `exec` raise `ssh_no_entry`(round-2 review B-NEW-1:否则首批 tizi SSH 全失败)。`build_registry_from_config` 吃整个 config 没错,但**喂它一个 1-entry config** 即复用其 construct+register 全部逻辑、不重抄 `_entry` 注入(早期草稿「不是 build_registry」过度——准确是「不把 `CandidateTarget` 直接喂它,而是提升成 entry 后包 1-entry config 复用」)。提升失败归 `invalid_candidate`(非「防御不可能分支」,是 source/config 两独立契约边界的显式对账,见 spec)。(b) 探测先跑**只读**固定命令(`hostname; uname -srm; cat /etc/os-release; command -v docker podman kubectl`,用 `cat` 不 `source` 远端文件)判 `reachable`,读 `capabilities`/指纹(`fingerprint` 白名单键不含 hostname)。
**理由(纠正前轮错误前提)**:`capabilities` **不是 `TargetEntry` 字段、从不落盘**(`target list`/`test` 每次从 target 实例实时 lazy-probe,见 `targets/base.py`/`cli/target.py`)。故「先 exec」的价值是**判可达性**(exec 成功=reachable→进 `to_add`,失败→`failed_probe`),不是「把准确 capabilities 写进 yaml」(那是伪命题——yaml 无此字段)。capabilities/指纹仅供 `ImportPlan` 展示 + 未来 inspector 选择参考。复用 `ExecutionTarget` 而非另起连接——否则 SSH 凭据 scrub、reconnect backoff、Tailscale 兼容全要重写(Gate 0 已证 asyncssh 走 Tailscale SSH)。
**备选**:❌ 只 TCP ping 不 exec——读不到 reachable 的真实信号(端口开 ≠ 能登能跑);❌ 新写一套探测连接——重复 `targets/ssh.py`;❌ 给 `TargetEntry` 加 `capabilities` 字段持久化——大动作、跨 `execution-target` 契约、与 lazy-probe freshness 冲突,超本提案 scope。

### D-4:`save_targets_config` 归 `execution-target` capability(非 `target-import`),复用 raw round-trip + 原子 0600 写
**选择**:`save_targets_config` 加在 `targets/config.py`、其需求归 **`execution-target` capability(MODIFY delta)**——它与 `load_targets_config` 互逆、共享 `_PLACEHOLDER_ALLOWED_FIELDS` 占位防线、写同一 `targets.yaml`。**serializer 下沉(解循环依赖)**:它复用的 `_load_raw_targets_dict`/`_entry_to_dict` 现物理在 `cli/target.py`;`config.py` 反向 import `cli` 会循环、破坏 loader 层「可从 doctor 安全 import」隔离(round-2 review B-NEW-2 + 架构 BLOCKER-1)。故**把这两个 helper 下沉到 `config.py`**(它们只依赖 `LocalEntry`/`SSHEntry` 模型、不依赖 concrete target,下沉不破隔离),`cli` add/remove 改 import 源(行为零改动)。**凭据 env 透传(解 BLOCKER-2)**:`_entry_to_dict` 从**独立 `password_env`/`passphrase_env` 参数**还原 `${VAR}`(非读 `entry.password`,后者可能是展开后明文——secret 安全),故 `save_targets_config` 入参须带凭据 env 引用(`to_add` 元素 = `(TargetEntry, password_env?, passphrase_env?)`),import 从 `CandidateTarget` 透传。内部:`_load_raw_targets_dict`(不 expand)+ 幂等 upsert + 原子写(`mkstemp(dir=同目录)` + 显式 `fchmod(0600)` + `os.replace`,父目录 `chmod 0700` 收紧既有)。`target-import` 只持「import 调 save 落 to_add」编排。
**理由**:持久化契约割裂(读在 A 写在 B)是 §4.10 边界 smudge;归 `execution-target` 让其有唯一 SOT。serializer 下沉是 BLOCKER-2 的硬约束(不下沉无法实现),顺带把 serializer 放回它本属的持久化层。原子 0600 补既有 `write_text` 非原子 + 无权限控制缺口。
**备选**:❌ `config.py` import `cli`——循环依赖崩 doctor;❌ `config.py` 复制一份 serializer——与「唯一 SOT」「与 add 同形」矛盾、必漂移。

### D-5:连接地址只用清单显式值,绝不对主机名做 DNS 解析
**选择**:ssh_config source 取 `HostName` 字面量;yaml source 取 `host` 字段;**禁止**对 `Host` 别名/主机名调 DNS。
**理由**:tizi `telegrambot` 的系统 DNS 解到 FakeDNS `198.18.x`(Surge 段)——裸解析主机名连到假 IP。清单里的显式地址(tailscale IPv6)才是真相。这是「准确」的第二个实现点。
**备选**:❌ 解析主机名补全地址——在 split-horizon / FakeDNS 环境必错。

### D-6:cred-less SSH 是一等公民(Gate 0 背书)
**选择**:`CandidateTarget` 凭据引用全可空;探测/落盘对「无 key 无 password」的 SSH 条目正常处理(asyncssh 默认 agent/默认 key/Tailscale SSH 握手)。
**理由**:Gate 0 实测 6/6 cred-less 连通,`SSHEntry` 字段已 Optional。`transport:openssh` 逃生舱(roadmap §3 曾担心的最大 scope)**确认不需要**。
**备选**:(无——实测已定)。

### D-7:写 safety 复用既有 helper,`--dry-run` 默认开
**选择**:`target import` 复用 `_refuse_root_for_write` + 非交互缺 `--yes` 退 1;`--dry-run` 默认行为(预览),`--yes` 才落盘。
**理由**:与 `target add` / Remediation 同形(CLAUDE.md §4.5);批量写更需要「先看全量 diff 再整体确认」,正是 dry-run→yes 形态(对比 MCP per-token 审批的反人类,见 roadmap §2)。

### D-8:部分失败默认「探活成功才纳管」+ 逃生舱
**选择**:默认 `--skip-unreachable`(失败进 `failed_probe`,只写成功的);`--include-unreachable` 强制登记(写 `enabled=False`);全失败且非 include → exit 1。
**理由**:把连不上的机器写进配置 = 后续每次巡检报 `requires_unmet` 噪声,违背「准确」。逃生舱登记的条目 `enabled=False`(登记但不激活,不污染巡检),留给「明知暂时下线但想先登记」。

### D-9:dry-run 默认 + `--yes` 落盘,无交互 prompt;`--source` 退出码必须走裸 str 手动校验
**选择**:`--dry-run` 默认(预览 exit 0,不写盘但**仍探活**远端);`--yes` 落盘;import **无逐条 y/N prompt**(批量不可行),故缺 `--yes` 即 dry-run 预览 exit 0——**无「非交互缺 --yes 退 1」分支**(不写盘即满足全局「不默默写盘」)。`--source` **必须**裸 `str` + 命令体手动校验→未知值 `typer.Exit(2)`,**禁** `Choice`/`Enum`(其 `UsageError` 经 `cli/__init__.py` 包装为 **exit 3**,撞「参数错=exit 2」契约,见 [[project_typer_pin_below_026]])。
**理由**:消除前轮 review 抓到的两处矛盾——(a)「dry-run 默认」与「非交互缺 --yes 退 1」只能成立一个;(b) spec 声称 exit 2 但 typer Enum 路径实际 exit 3。dry-run 是「副作用」窄义:对**本地 targets.yaml** 零副作用,对远端仍发只读 SSH exec(探活是 plan 输入)——文案须诚实标注。

### D-10:嗅探分派是新机制,`--source` 优先、多命中报歧义;`ImportPlan` 可序列化为 B 留缝
**选择**:(a) `can_handle(ref)` 内容嗅探是本仓**首个**内容启发式 registry(notifier/inspector 都是显式 type 查表)——`--source` 显式永远优先;缺省嗅探多命中 → raise ambiguous(exit 2),不静默取第一个。(b) `ImportPlan`/`ProbeResult` 必须纯 Pydantic 标量、可 `model_dump_json` round-trip——为 dry-run 产物落盘 + 提案 B `--from-plan` 复用留缝;`TargetProbe` 模块对 `ImportPlan` **零依赖**(probe 产 `list[ProbeResult]`,分类成 plan 是 `import_plan.py` 的事),保 B 可单独复用 probe。
**理由**:嗅探是新分派语义,「镜像 notifier」只在扩展性维度成立、分派维度须自证歧义裁决(架构 review M-3)。`ImportPlan` 不可序列化会断 B 的 `--from-plan` 接缝、逼 B 回头改 A 模型(架构 review M-4);探测↔规划正交保 probe 被 CLI/MCP 两路复用。

## 风险 / 权衡

- **[ssh_config 解析复杂度]** OpenSSH config 有 `Match`/通配 `Host`/`Include` 嵌套 → **缓解**:首版只解析**显式** `Host`(非通配 pattern)+ 一层 `Include`(路径限 `~/.ssh/` 树内防任意文件读);`Match` 块与通配 Host 跳过并 log(不静默)。tizi `hosts` 全是显式 Host,首批不受影响。
- **[name 跨层缺口]** ssh_config 任意 `Host` 别名 vs `_NAME_PATTERN` → **缓解**:source 层确定性规范化 + 撞名/不可规范化 raise `ConfigError`,plan 展示 `原始→派生` 映射;畸形别名在来源层被挡,不让 `ValidationError` 在落盘期崩整批。
- **[TargetProbe 先 exec 的副作用]** 对生产机跑命令 → **缓解**:探测命令**纯只读固定字面量**(不插值 inventory 字段,无注入面),无状态变更;与 `target test` 既有探活同类。
- **[并发探活握手风暴]** 几十台同时连 → **缓解**:信号量限流(默认 ≤10,`--concurrency` 可调)+ per-host timeout 失败隔离。
- **[与 `target add` 写逻辑漂移]** 两条写路径(import 用 `save_targets_config`、add 手搓) → **缓解**:`save_targets_config` 复用同款 `_load_raw_targets_dict` raw round-trip + `_entry_to_dict` 序列化;`execution-target` spec 已加「与 `target add` 输出逐字段同形」场景作可测对齐(非仅 design 口头)。收敛为单 writer 是登记的后续 refactor。

## Migration Plan

- 纯增量:新增 `targets/inventory/`、`targets/probe.py`、`targets/import_plan.py`、`config.save_targets_config`、`cli/target.py` 加 `import` 子命令。无数据迁移、无 schema 破坏。
- `targets.yaml` 产出条目与 `target add` 同形,既有 loader/registry 零改动即可读。
- 回滚:移除 `import` 子命令 + 新模块即回到 M1 单台 add;`save_targets_config` 无调用方时为 dead code,不影响既有写命令。

## Open Questions

- ~~**yaml source 标准 schema**~~ → **已定死进 `inventory-source` spec**(前轮 review:schema 未定→场景循环不可测,不能留 OQ)。
- **ssh_config `Include` 解析深度**:首版一层 `Include`(覆盖 `~/tizi/hosts` 被 `~/.ssh/config` Include 的真实用法)是否够?多层嵌套 / `Match` 留后续(spec 已钉一层 + 路径限 `~/.ssh/` 树内)。
- **inventory.yml 富元数据 enrich**(role/provider/region → target tags + 驱动 inspector 选择)→ 本提案非目标,独立 spec(roadmap §7.4)。
- **`hosts` + `inventory.yml` 显式合并模式**(连接以 ssh_config 为权威、YAML 富化)→ roadmap §7.5,独立决策。
