## 为什么

用户要把**一批已有服务器**(几十台量级)纳入 Hostlens 巡检环境,目前只能用 `hostlens target add` **一台一台敲** CLI——批量迁移场景下不可接受。且单台 `add` 不做任何探活,凭手敲的 host 可能写错(地址选错)、或把一台连不上的机器写进配置,后续每次巡检报 `requires_unmet` 噪声。批量纳管要「准确」,核心是**落盘前验证可达性 + 用清单里的正确地址**——`capabilities` 是运行时 lazy-probe、从不持久化进 `targets.yaml`,故「准确」指的是地址正确 + 可达,不是「把能力写进配置」。

本提案是「服务器批量纳管」路线的**主提案**(取代已废弃的 `add-mcp-write-approval-flow`,详见 [docs/roadmap/target-onboarding.md](../../../docs/roadmap/target-onboarding.md))。经 `/opsx:explore` + Codex/架构 review + Gate 0 实测(asyncssh 直连 Tailscale SSH 6/6 cred-less 通过)收敛:批量纳管的正确载体是 **CLI 批量导入**,审批天然走本地 `--dry-run → --yes`,不碰 M9 远程审批红线([[feedback_ai_no_auto_exec_elevated_risk]])。

## 变更内容

新增 `hostlens target import <inventory>` 命令,把「批量纳管」实现为一条**四层只读→写流水线**(前三层全只读,复杂度前置到 plan/preview,落盘最后一步纯机械——兑现 CLAUDE.md §4.5「写操作前置 plan/preview」):

```
InventorySource (Protocol，加来源=加文件)  →  list[CandidateTarget]   (纯解析，无 IO)
        ↓
TargetProbe (复用 ExecutionTarget)         →  list[ProbeResult]       (并发探活+能力探测+OS指纹)
        ↓
ImportPlan (可读 diff)                      →  to_add / skipped / failed_probe
        ↓  ── --dry-run 停在此（零副作用）── │ ── --yes 继续 ──
save_targets_config (原子+幂等 upsert+${VAR} 保全)  →  ~/.config/hostlens/targets.yaml
```

- **`InventorySource` 插件抽象**(新 capability `inventory-source`):`Protocol` + 显式装配 registry(`register_default_sources`),`parse(ref) -> list[CandidateTarget]` **纯解析、严禁碰网络/DNS**;分派语义是本仓首个**内容嗅探**(`can_handle`,与 notifier/inspector 的显式 type 查表不同构)——`--source` 显式优先、多命中报歧义;首发两个 source 覆盖首批 100%:
  - `ssh_config`:解析 `~/.ssh/config`(及一层 `Include`,路径限 `~/.ssh/` 树内)的 `Host`/`HostName`/`User`/`Port`/`IdentityFile`/`AddressFamily`;`HostName` 取显式地址不靠 DNS;`IdentityFile` 的 `~`/`${VAR}` 在 source 层展开为字面路径(`key_path` 非 secret 字段、不能落 `${VAR}`)
  - `yaml`:解析 **Hostlens 标准 inventory schema**(本提案 spec 钉死字段),**非**任意第三方 YAML(tizi `inventory.yml` 字段非标,走 ssh_config source)
  - `CandidateTarget`:未验证候选(`name`/`host`/`user`/`port`/凭据**引用** + 来源元数据),**绝不含明文密钥**;`name` 由来源标识**规范化**为合法 `_NAME_PATTERN`(`^[a-z][a-z0-9_-]{0,63}$`)、撞名/不可规范化报错
- **`TargetProbe`**(capability `target-import`):复用 `ExecutionTarget`,并发限流探活;`CandidateTarget` 先**提升为 `TargetEntry`**(经 Pydantic 校验),再经 `build_one_target`(把单 entry 包成 1-entry `TargetsConfig` 复用 `build_registry_from_config` → **construct+register 注入 `_entry`**;裸 `SSHTarget(name=...)` 的 `_entry=None` 会让 exec raise `ssh_no_entry`)构造探测 target;提升失败归 `invalid_candidate` 不崩;探测**先触发一次只读 `exec`** 判**可达性**(`reachable`,**非**以 capabilities 非空判),读 `capabilities`/指纹**供 plan 展示**——`capabilities` **不写进 `targets.yaml`**(每次 load 实时 lazy-probe);`ProbeResult` 只持脱敏标量(`error_kind` 闭集枚举、`fingerprint` 白名单键不含 hostname,禁泄 host/凭据)、可 JSON round-trip;采 os 指纹用 `cat /etc/os-release`(不 `source` 远端不可信文件)
- **`ImportPlan`**:纯 Pydantic、可 `model_dump_json` round-trip(为提案 B `--from-plan` 留缝),`to_add` / `skipped`(name 已存在) / `failed_probe` / `invalid_candidate`;渲染禁泄 host/凭据、`to_add` 列出连接地址供落盘前审计;`--dry-run` 渲染后停止
- **`save_targets_config`**(归 `execution-target` capability,见修改功能):原子写(`mkstemp` 同目录 + `os.replace`,文件 `0600`)+ 幂等 upsert + `${VAR}` 占位保全(复用 `_load_raw_targets_dict`,不 expand)+ 复用 `_entry_to_dict` 与 `add` 输出同形
- **`hostlens target import` CLI**:`--source`(裸 str + 手动校验,未知值 exit 2;**非** Choice/Enum——后者 UsageError→exit 3)/ `--dry-run`(**默认**预览 exit 0,不写盘但仍探活远端)/ `--yes`(落盘,无逐条 prompt)/ `--skip-unreachable`(默认)/ `--include-unreachable`(逃生舱,登记 `enabled=False`);**拒 root(EUID==0,落盘路径)**;缺 `--yes` 即 dry-run 预览 exit 0(不写盘即满足「非交互不默默写盘」)

## 功能 (Capabilities)

### 新增功能
- `inventory-source`: 可插拔 inventory 来源抽象——`InventorySource` Protocol + 显式装配 registry + `CandidateTarget` 模型(含 name 规范化 + 明文 fail-closed + key_path 占位展开)+ 内容嗅探分派(`--source` 优先/多命中报歧义)+ 首发 `ssh_config` / `yaml` 两个 source。在**扩展性**维度镜像 `notifier-protocol` 的 Protocol+显式装配模式(分派语义是新引入的内容嗅探,与既有显式 type 查表不同构)。
- `target-import`: 批量纳管流水线——`CandidateTarget→TargetEntry` 提升 + `TargetProbe`(复用 ExecutionTarget,先 exec 判**可达性**、capabilities 仅供 plan 不落盘)+ `ImportPlan`(四分类、可序列化)+ `hostlens target import` CLI(`--dry-run` 默认/`--yes`/`--skip-unreachable`/`--include-unreachable`/拒 root)。

### 修改功能
- `execution-target`: (1) `hostlens target` CLI 命令集枚举从 `add/list/remove/test`(4)扩为含 `import`(5)——既有 spec 把命令集**枚举式**钉死且标「禁止漂移」,新增子命令是对该枚举契约的 MODIFY;(2) 新增 `save_targets_config`(原子/幂等/`${VAR}` 保全/`0600` 权限)——它与 `load_targets_config` 互逆、共享 `_PLACEHOLDER_ALLOWED_FIELDS` 占位防线、写同一 `targets.yaml`,归 `TargetsConfig` 持久化契约的拥有者 `execution-target`(不放 `target-import`,避免「读在 A 写在 B」的契约割裂)。

## 影响

- **代码**:新增 `targets/inventory/`(`base.py` Protocol + registry、`sources/ssh_config.py`、`sources/yaml.py`、`models.py` CandidateTarget)+ `targets/probe.py`(TargetProbe + ProbeResult + `CandidateTarget→TargetEntry` 提升)+ `targets/registry.py` 加 `build_one_target(entry, settings)`(与 `build_registry_from_config` 同模块、同构造 SOT,probe.py import 它)+ `targets/import_plan.py`(ImportPlan + `PendingAdd`/`FailedProbe`/`InvalidCandidate` 具名 model)+ `targets/config.py` 加 `save_targets_config` + `_atomic_write_yaml`(共享原子写,add/remove 也用)+ `cli/target.py` 加 `import` 子命令。
- **行为零改动 / 仅 import 源变**:`ExecutionTarget` Protocol / `load_targets_config` / 既有 `target add/list/remove/test` **行为** / `TargetRegistry` / MCP surface 均不变(本提案完全不碰 MCP——那是提案 B)。**但** `_entry_to_dict` / `_load_raw_targets_dict` 两个序列化 helper 从 `cli/target.py` **下沉到 `targets/config.py`**(否则 `config.py` 的 `save_targets_config` 反向 import `cli` 会循环依赖、破坏 loader 层隔离),`cli/target.py` 的 add/remove 改 import 源——**行为逐字段不变,仅 import 路径变**。
- **契约改动**:`execution-target` spec 的「`hostlens target` 命令集」需求加 `import`(MODIFY delta)+ 新增 `save_targets_config` 需求(ADDED,含 serializer 下沉)。
- **依赖**:不新增第三方依赖(`asyncssh` 已在;ssh_config 解析**须手写 line parser**——**不**整体委托 `asyncssh.config.SSHClientConfig`,它自动解析 `Include`/`Match`/通配,无法在 SDK 层施加 `~/.ssh/` 边界 + realpath 校验,会绕过 `include_path_escape` 安全门;YAML 用既有 `pyyaml`)。
- **CLI 命令数**:`hostlens target` 子命令从 4(add/list/remove/test)→ 5(+import)。

## 非目标 (Non-Goals)

- **不碰 MCP surface**:远程 LLM 驱动 import 是独立提案 B(`add-mcp-target-import-propose`,propose-only)。本提案纯 CLI。
- **不做自然语言来源**(NLSource):留提案 B(它本质是「LLM 把自然语言→CandidateTarget」的前置翻译器)。
- **不做 CSV source**:首批 tizi 是 ssh_config + yaml,CSV 留后续按需。
- **不把 `target add/remove` 收敛为复用 `save_targets_config` 的 entry-list 接口**:本提案**下沉共享 serializer**(`_entry_to_dict`/`_load_raw_targets_dict` → config.py)+ **让 add/remove 末尾写盘也改调共享原子写原语 `_atomic_write_yaml`**(获原子 + `0600`,输出字节不变——否则 import 写 0600 会被下次 add 的裸 `write_text` 抹回 0644),但**不**重写 add/remove 的 raw-dict 变更编排去走 `save_targets_config` 的 entry-list 路径——那属独立 refactor。
- **不扩 `targets.yaml` schema 存富元数据**:`inventory.yml` 的 role/provider/region/notes **本提案不落盘到 target**(只用于探测决策与 plan 展示);target `tags` 扩展是独立 spec 决策。
- **不做 docker/k8s target 导入**:首批全是 SSH target;import 首版只产 `local`/`ssh` 条目。
- **不支持更新已存在 target**:幂等默认 skip 不覆盖;IP/地址变更走 `target remove` + re-import 或手改 yaml(`--update`/`--force` 留后续按需)。
- **不解析任意第三方 YAML**:`yaml` source 只吃 Hostlens 标准 schema;tizi `inventory.yml`(字段非标)走 `ssh_config` source 吃 `~/tizi/hosts`。
- **不做远程审批 / 两段式 token**:审批 = 本地 `--dry-run → --yes`。

## 对外契约影响

- **CLI 命令**:新增 `hostlens target import <inventory> [--source ssh_config|yaml] [--dry-run] [--yes] [--skip-unreachable|--include-unreachable] [--concurrency N] [--json]`(`execution-target` 命令集 MODIFY delta 固化,加入「禁止漂移」命名约定)。退出码:`0` 成功(含 dry-run 预览、`--include-unreachable` 全失败仍登记) / `1` 业务失败(拒 root、`--yes` 落盘但全失败且非 include) / `2` 参数·配置错(未知 source[裸 str 手动校验非 Choice]、inventory 解析失败、`targets.yaml` 损坏)。`--dry-run` 默认(预览 exit 0,不写盘),`--yes` 落盘;缺 `--yes` 即 dry-run 预览(无非交互退 1 分支)。
- **`execution-target` MODIFY**:`hostlens target` 命令枚举 +`import`;新增 `save_targets_config`(原子/幂等/`${VAR}`-保全/`0600`)。
- **新增内部契约**(由 `inventory-source` / `target-import` spec 固化):`InventorySource` Protocol、`CandidateTarget`(name 规范化 + 明文 fail-closed)、`CandidateTarget→TargetEntry` 提升、`ProbeResult`(脱敏标量、可序列化)、`ImportPlan`(四分类、round-trip)schema。
- **Inspector schema / Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest**:**均不变**。
- **`targets.yaml` 格式**:不变(import 产出的条目与 `target add` 写出的同形;`capabilities` 不进 yaml)。

## Failure Modes

1. **inventory 解析失败 / 非法 name / key_path 占位 / 非法 env 名**:`InventorySource.parse` 抛 `ConfigError`(语法错 / `invalid_target_name` / `ambiguous_target_name` / `key_path_placeholder_forbidden` / `invalid_env_var_name` / `plaintext_secret_forbidden` / `include_path_escape`),CLI 映射 **exit 2**,不进入探测。解析与探测正交,离线也能报「语法错」而非「连不上」。
2. **部分主机探活失败**(连不上 / auth 失败):归入 `ImportPlan.failed_probe`;默认 `--skip-unreachable` 只纳管成功的;`--include-unreachable` 逃生舱强制登记(写 `enabled=False`)。`--yes` 下**全部失败**且非 include → exit 1(无可纳管项)。
3. **候选提升失败**(name 仍不合法 / 字段不符 entry schema):归入 `ImportPlan.invalid_candidate`(附脱敏摘要),整批其余继续,**不**抛 `ValidationError` 崩整批。
4. **name 冲突**(候选 name 已在 `targets.yaml`):幂等 upsert 默认归入 `ImportPlan.skipped`(不覆盖),重跑安全。**注**:首版**不支持更新**已存在 target(IP 变更走 `target remove` + re-import 或手改 yaml)——见非目标。
5. **写盘中断**(进程被 kill):原子写(`mkstemp` 同目录 + `os.replace`)保证 `targets.yaml` 要么旧要么新,不留半文件;`${VAR}` 经 raw round-trip(不 expand)防展平成明文([[project_ssh_secret_hostlens_contract_seed_drift]])。

## Operational Limits

- **并发**:`TargetProbe.probe_many` 用 `asyncio.gather` + 信号量限流(默认并发 ≤ 10,可 `--concurrency` 调),避免几十台同时连握手风暴。
- **超时**:每台探测 per-host `connect_timeout`(默认 12s)+ `exec` timeout(默认 12s);单台慢不拖垮整批(独立 task,失败隔离进 `failed_probe`)。
- **内存**:`ImportPlan` 全量在内存,几十台量级可忽略;无大对象。
- **无 LLM 调用**:整条流水线纯 SSH/文件 IO,零 token(见 Cost)。

## Security & Secrets

- **不引入新密钥**。`CandidateTarget` / `ImportPlan` **绝不含明文密钥**——只带凭据**引用**(`*_env` 环境变量名 / `key_path`)。首批 tizi 凭据引用为**空**(外部 Tailscale SSH 认证,`SSHEntry.key_path`/`password` 已 `Optional`)。
- **source 层拒明文(单一 fail-closed)**:来源含明文 `password`/`passphrase` 字段 → `InventorySource.parse` **raise `ConfigError(plaintext_secret_forbidden)`**,明文**绝不**读入候选/中间态/日志。**砍掉「映射成 env 名」分支**(env 名无从推断、且映射让明文经中间变量泄露;首批 cred-less、ssh_config 本无明文字段、yaml 标准 schema 不设明文字段,映射无驱动场景)。
- **`targets.yaml` 文件权限**:`save_targets_config` 写新文件 `0600` + 父目录 `0700`(文件含 host/user/key_path 是横向移动攻击地图,禁 world-readable);临时文件经 `mkstemp(dir=同目录)` 防 symlink/预测路径攻击。
- **探测输出脱敏**:`ProbeResult.error_kind` 是**闭集枚举**(非自由文本异常)、`fingerprint` **白名单键不含 `hostname`**(内网拓扑情报,与 error_kind 同级脱敏)、`ImportPlan.failed_probe`/`invalid_candidate` 渲染禁泄 host IP/`user@host`/traceback(对齐 `_emit_target_error`);探测命令是**固定字面量**(不插值 inventory 字段)、用 `cat /etc/os-release`(不 `source` 远端不可信文件),连接参数经 asyncssh 参数化、无 shell 注入面。
- **`ImportPlan` 落盘 0600**:plan 序列化落盘(dry-run 产物 / 提案 B `--from-plan`)含 `to_add` 明文 host,**必须复用 `save_targets_config` 的 `0600` 原子写**,禁 world-readable 绕过 targets.yaml 的 0600 防线。
- **写拒 root**:`target import` 落盘前 `_refuse_root_for_write(EUID==0)`,防 sudo 制造 root-owned `targets.yaml`。
- **来源信任边界**:`InventorySource` 输入信任级 = 操作者本地文件(Hostlens 不验来源真实性,与 `target add` 同信任级);**`--dry-run` 默认 + `to_add` 完整列出连接地址 = 落盘前地址审计点**(操作者可在 `--yes` 前肉眼核对有无非预期主机——污染的 inventory 指向攻击者主机会在 plan 暴露)。**不靠主机名 DNS 解析**(tizi `telegrambot` 撞 FakeDNS `198.18.x` 教训)——Hostlens 不主动解析 `Host` 别名,只取 `HostName` 字面量(注:`HostName` 若是域名,asyncssh 连接期仍走 OS resolver;防 FakeDNS 应在 inventory 直接写 IP)。`Include` 路径限 `~/.ssh/` 树内,防任意文件读。

## Cost / Quota Impact

- **零 LLM 调用 / 零 token**:`target import` 是纯 SSH 探测 + 文件写,不触发任何 Anthropic API。对配额无影响。
- 不改 prompt caching 策略(根本不调 LLM)。

## Demo Path

无 SSH / 无付费 API 的离线优先路径(用 `local` target 绕开真实 SSH):

```bash
pip install -e ".[dev]"
# 1. 准备一个 yaml inventory fixture（含一个 local 条目，探测走本机、无需 SSH）：
cat > /tmp/demo-inventory.yml <<'EOF'
hosts_local:
  demo-localhost:
    type: local
EOF
# 2. dry-run（默认）预览：解析 → 提升 → 探测 local（触发 exec 判 reachable + 读 capabilities 供展示）→ 渲染 ImportPlan，标注 DRY-RUN 不落盘
hostlens target import /tmp/demo-inventory.yml --source yaml
# 3. 落盘：--yes 写入 ~/.config/hostlens/targets.yaml（原子 0600 + 幂等）
hostlens target import /tmp/demo-inventory.yml --source yaml --yes
# 4. 验证：再跑一次 --yes → demo-localhost 进 skipped（幂等 upsert，不重复加）
hostlens target import /tmp/demo-inventory.yml --source yaml --yes
hostlens target list   # 应见 demo-localhost
# 5. ssh_config source（注意：--dry-run 仍会对 ssh 主机发起只读探活连接，只是不落盘；
#    "离线" 仅指 parse 阶段不连网。对真实 ssh host 此步会 SSH 连接）：
hostlens target import ~/.ssh/config --source ssh_config --dry-run
```

- 单元测试:`InventorySource` 两个 source 的解析(fixture → CandidateTarget,无网络)+ name 规范化(大写/点/数字开头/撞名)+ key_path `~`/`${VAR}` 展开 + 明文 fail-closed + `save_targets_config` 原子/幂等/`${VAR}`-保全/`0600` + `ImportPlan` 四分类 + JSON round-trip。
- 集成测试:`TargetProbe` 对 `local` target 真探测(本机 `exec`,CI 可跑)判 reachable + 读 capabilities;ssh 探测用既有 docker-sshd fixture(复用 `ssh-execution-target` 测试设施),**非 root** 跑通。
