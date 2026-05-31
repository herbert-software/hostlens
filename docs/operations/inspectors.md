# Inspectors Operations Guide

How Inspector manifests are loaded, validated, listed, and shown in
Hostlens. M1 scope.

## 概述

Inspector 是 Hostlens 的 **巡检 SOT（source of truth）**：每个检查项 = 一份
YAML manifest，Agent 只决定调度哪些 Inspector，不在 prompt 里写死巡检步骤
（见 `CLAUDE.md` §4.2）。

**Manifest 位置**：

- **Builtin**（随包发布，不通过配置覆盖）：
  `src/hostlens/inspectors/builtin/**/*.yaml`
- **用户**：`~/.config/hostlens/inspectors/**/*.yaml`，可通过
  `HOSTLENS_INSPECTORS_SEARCH_PATHS` 环境变量改写为 `:` 分隔的多目录列表
  （Unix `PATH` 风格）。

加载装配通过 `hostlens.inspectors.registry.build_registry_from_search_paths(
user_paths, *, settings)`，返回 `(registry, errors)` 双值：

- Builtin 路径的文件级错误（语法 / 字段 / 注入校验）**直接 raise** —— 仓库
  自带 bug 必须立即暴露；
- 用户路径的文件级错误 **collect 到 `errors`**，不阻塞其他 manifest 加载，
  由 CLI 与 doctor 决定 exit code；
- `duplicate_inspector`（builtin vs builtin / 用户 vs builtin / 用户 vs
  用户）**永远 raise** —— silent skip 会让攻击者在用户路径放同名 manifest
  绕过 builtin 而用户感知不到。

## Manifest 字段速查（M1 子集）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✓ | 全局唯一；正则 `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`（强制至少两段点分命名） |
| `version` | str | ✓ | SemVer 字符串 `^\d+\.\d+\.\d+$` |
| `description` | str | ✓ | 一句话说明 |
| `tags` | list[str] | | 每个 tag 匹配 `^[a-z][a-z0-9_-]*$`；用于 `inspectors list --tag` 筛选 |
| `targets` | list[`local`\|`ssh`] | ✓ | M1 范围恰好 `local` / `ssh`；至少 1 个 |
| `requires_capabilities` | list[str] | | 值必须在 `{shell, file_read, ssh, systemd, docker_cli}` 内 |
| `requires_binaries` | list[str] | | 每个 binary 名匹配 `^[a-zA-Z0-9._-]+$` |
| `requires_files` | list[str] | | 每个路径匹配严格正则 `^/[A-Za-z0-9._/-]+$` + component 级 `.` / `..` 拒绝 |
| `privilege` | `none`\|`sudo`\|`root` | | 默认 `none`；非 `none` 时未 `--allow-privileged` opt-in 会被 runner 标 `requires_unmet` |
| `parameters` | JSON Schema dict | | `type: object` 顶层；string 字段必须含 `pattern` 或 `enum`（见下方"五件套"） |
| `secrets` | list[str] | | 每个 secret 名匹配 `^[A-Z_][A-Z0-9_]*$`（POSIX env 命名） |
| `collect.command` | str | ✓ | Jinja2 模板；`shell-evaluated` |
| `collect.timeout_seconds` | int | | 默认 60；范围 `[1, 300]` |
| `parse.format` | `raw`\|`table`\|`json`\|`kv` | ✓ | M1 恰好这四种 |
| `parse.columns` | list[str] | | `format: table` 必填；`format: raw` 含 `raw_extract_regex` 时必填 |
| `parse.delimiter` | str | | 仅 `format: kv` 时使用，默认 `=` |
| `parse.skip_header_rows` | int | | 仅 `format: table` 时使用，默认 1 |
| `parse.raw_extract_regex` | str\|null | | 仅 `format: raw` 时允许非 None；长度 ≤200 + 静态拒绝 6 类 ReDoS（见下方） |
| `output_schema` | JSON Schema dict | ✓ | `type: object` 顶层 |
| `findings` | list[FindingRule] | | 可空数组 |

**FindingRule 字段**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `for_each` | str\|null | | 形如 `<expr> as <var>`（var 名 `^[a-z_][a-z_0-9]*$`） |
| `when` | str | ✓ | simpleeval 布尔表达式 |
| `severity` | `info`\|`warning`\|`critical` | ✓ | M1 三值集合 |
| `message` | str | ✓ | Python `.format()` 模板；聚合模式禁止引用 `{var.attr}` 形式 |

**M1 不支持的字段**（写了 loader 直接 raise）：`hook` / `sampling_window` /
`artifacts` / `parse.format: sql_result`。这些留给后续提案：

- `parse.format: sql_result` — 留给 M6 PostgreSQL Inspector
- `collect.sampling_window` — 留给 M2.8 incident pack
- `artifacts` — 留给 M3 报告系统
- `hook.py` — 留给 M6 复杂场景（PostgreSQL bloat / TLS 探测等）

## Shell 注入防御五件套

加载时（**不**运行时）静态拒绝注入风险，5 层防御：

1. **字符集约束**：`parameters` 中 `type: string` 字段必须含 `pattern` 或
   `enum`；`type: array` 且 `items.type == "string"` 时 items schema 必须
   同样含 `pattern` 或 `enum`。缺失 → loader raise
   `parameter_missing_charset_constraint`。理由：没字符集的 string 是
   shell 注入向量；强制约束让"manifest 写作阶段"就堵住注入。
2. **强制 quote**：`collect.command` 中 string parameter 必须紧随 `| sh`
   filter；array(string-items) 必须走 `| map('sh') | join(<delim>)`
   filter chain（map 在 join 之前）。loader 用 `jinja2.visitor.NodeVisitor`
   完整遍历 AST（不能只看 `nodes.Name`），覆盖 `default` / `if` /
   `CondExpr` / `Concat` 等所有插值位置。array 缺 `items.type` 声明、items
   是 `object`/`array`、或用 `oneOf/anyOf/allOf` → 拒绝
   `array_parameter_items_type_undetermined`。
3. **Secrets 走 env var**：`secrets:` 列表中的名字只能通过 shell `$VAR_NAME`
   引用（runner 通过 `ExecutionTarget.exec(cmd, env={...})` 注入）；Jinja2
   插值位置（`{{ PGPASSWORD }}`、`{{ env['PGPASSWORD'] }}` 等）出现 secret
   名 → loader raise `secret_inlined_in_command`。理由：Jinja2 插值会把
   secret 落进 cmd string，最终出现在 ps 输出 / shell history / 错误日志
   栈帧里。
4. **`requires_files` 路径严格 allowlist**：字段级正则限定
   `^/[A-Za-z0-9._/-]+$`（仅 ASCII alphanumeric + `._/-`，**禁止** shell
   元字符 `; $ \` ( ) | & < > \n \0`），component 级再拒绝 `.` 与 `..`
   （防穿越）。runner preflight 探测 `[ -r <path> ]` 时仍对路径走
   `shlex.quote(path)` 作为防御纵深的第二道闸。**已知接受风险**（manifest
   作者责任）：路径仍可能指向 `/proc/self/mem`、`/dev/...` 字符设备等敏感
   位置 —— M1 不做白名单 prefix 检查（无业务必要、增加误判面）。
5. **`raw_extract_regex` 静态 ReDoS 拒绝**：长度 ≤ 200 字符 + `re.compile()`
   成功 + 全部命名捕获组 + 静态拒绝 6 类 known-bad 模式（用 `sre_parse.parse`
   走 AST，不是 regex 字面扫描）。runner 包 `asyncio.wait_for(...,
   timeout=1.0)` 仅作为软兜底日志事件，**不**作为主防御（Python `re` 在
   C 层回溯无法被 asyncio / signal 可靠中断）。

### raw_extract_regex 用法

`parse.format: raw` 时可选用 `raw_extract_regex` 提取命名捕获组到顶层
`output` 字段（命名组顺序映射到 `columns`）。

**可接受模式**（在 ≤200 字符内，全部命名组，无 ReDoS pattern）：

| 用途 | 正则 |
|---|---|
| 负载平均值（system.uptime 用） | `load average:\s+(?P<load1>[\d.]+),\s+(?P<load5>[\d.]+),\s+(?P<load15>[\d.]+)` |
| 进程数 | `^Tasks:\s+(?P<total>\d+)\s+total,\s+(?P<running>\d+)\s+running` |
| 单字段提取 | `version:\s+(?P<v>[\d.]+)` |

**拒绝模式**（命中即 raise `pydantic.ValidationError` 含对应 tag）：

| 模式 | 例 | tag |
|---|---|---|
| 嵌套量词 | `(?P<x>(a+)+)` / `(?P<x>(a*)*)` / `(?P<x>(?:a+)+)` | `nested_quantifier` |
| 量词作用于 ASSERT | `(?P<x>(?=a+)+a)` | `quantifier_on_assert` |
| GROUPREF（命名/编号 backref） | `(?P<x>.+)(?P=x)+` / `(?P<x>.+)\1+` | `groupref_forbidden` |
| ATOMIC_GROUP（Python 3.11+） | `(?P<x>(?>a+))` | `atomic_group_forbidden` |
| alternation 前缀子集 | `(?P<x>(a\|aa)+)` / `(?P<x>(a\|ab)+)` | `prefix_subset_alternation` |
| 量词作用于可匹配空串 | `(?P<x>(a?)*)` | `quantifier_on_empty_matchable` |

如果你的需求超出 `raw_extract_regex` 能力范围（比如需要多行 lookbehind、
复杂条件解析），考虑：

- 改用 `parse.format: table` / `json` / `kv` 之一；
- 先 `cut` / `awk` / `jq` 后再 `parse.format: raw` 全文捕获（让 shell 做
  预处理，正则只做最终提取）；
- 等 M6 hook.py 提案落地，用 Python 接管复杂解析。

## Builtin Inspector 列表

M1 随包发布两个 builtin Inspector，用于验证管线 + Demo Path：

| name | targets | 说明 |
|---|---|---|
| `hello.echo` | local / ssh | 跑 `echo hello`，`parse.format: raw`；finding 在 `len(raw) > 0` 时输出 info-level `"hello received: {raw}"`。用于"管线是否打通"的最小测试 |
| `system.uptime` | local / ssh | 跑 `uptime`，`parse.format: raw` + `raw_extract_regex` 提取 1/5/15 分钟负载；两条聚合 finding：`load1 > 4.0` warn，`> 8.0` critical |

`system.uptime` 的 finding 表达式用到 `float(load1)`，因此 DSL 引擎在
`hostlens.inspectors.dsl.evaluate` 已显式注册 `float` / `int` 类型转换函数。

## 4 种 parse format 选型指南

| format | 何时用 | output 形态 |
|---|---|---|
| `raw` | 任意 stdout 想原样保留（`echo` / `cat` / `ping` 等）；可选 `raw_extract_regex` 提取命名组 | `{"raw": <stdout>}` 或 `{<col1>: <val1>, ...}`（regex 模式） |
| `table` | POSIX 表格输出（`ps` / `df` / `netstat`）；按空白拆列 | `{"rows": [{<col>: <val>, ...}, ...]}` |
| `json` | 命令本身输出 JSON（`docker inspect` / `kubectl get -o json` / 你自己的脚本） | `json.loads(stdout)`；顶层必须是 dict |
| `kv` | `key=value` / `key: value` 行式输出（`/proc/meminfo` / `os-release`） | `{<key>: <value>, ...}` |

经验：能用 `kv` 就别用 `raw + regex`；能用 `table` 就别 awk 出 JSON 再走
`json`。format 选错了 manifest 会比该有的复杂 3 倍。

## `inspectors list` / `show` CLI 示例

`list` 默认输出 Rich Table；`--json` 输出按 name 字典序的
`InspectorSummary` JSON 数组（schema 由 M2 锁定，prompt-cache key 稳定）：

```bash
# 列出全部
hostlens inspectors list

# 按 tag 过滤
hostlens inspectors list --tag linux

# 按兼容 target 类型过滤
hostlens inspectors list --target-kind ssh

# 同时过滤（AND）
hostlens inspectors list --tag system --target-kind local

# JSON 输出，按 name 字典序
hostlens inspectors list --json
# 期望（示例）:
# [
#   {"name":"hello.echo","version":"1.0.0","description":"...","tags":["demo","hello"],"compatible_target_kinds":["local","ssh"]},
#   {"name":"system.uptime","version":"1.0.0","description":"...","tags":["linux","performance","system"],"compatible_target_kinds":["local","ssh"]}
# ]
```

`show <name>` 默认 Rich 渲染 manifest 关键字段；`secrets` 字段**只显示名字
列表**，不读 env var；`parameters.<field>.default: "${ENV_VAR}"` 占位符也
不展开：

```bash
hostlens inspectors show hello.echo
hostlens inspectors show hello.echo --json
hostlens inspectors show postgres.bloat.tables   # 即使 PGPASSWORD 已 export，输出只显示 secret 名字
```

**加载错误处理**：`list` 与 `show` 都会把单个 manifest 加载失败的文件
报到 stderr（每行 `path + error kind + 简短 detail`），同时正常加载的
Inspector 仍在 stdout 输出；命令 **exit 1** 退出。

```bash
hostlens inspectors list 2>/dev/null | head    # 只看正常的
hostlens inspectors list >/dev/null            # 只看错误
echo "exit=$?"                                  # 1 表示有加载错误
```

CLI 是只读，允许 root：`sudo hostlens inspectors list` 与
`sudo hostlens inspectors show ...` 都不会被拒绝。

`hostlens doctor` 的 `inspectors` section 给出聚合视图：

```bash
hostlens doctor --json | jq '.inspectors'
# {
#   "status": "ok" | "warn" | "fail",
#   "loaded": <int>,
#   "errors": [{path, kind, detail}],
#   "missing_secrets": [{inspector, secret}]
# }
```

`status` 计算：`errors` 非空 → `fail`；只有 `missing_secrets` 非空 →
`warn`；都空 → `ok`。`fail` 让 doctor 整体 exit 1。

## Secrets 环境变量配置 best practice

Manifest 声明的 `secrets:` 必须在执行 Inspector 之前在 process env 中存在，
runner 通过 `ExecutionTarget.exec(cmd, env={...})` 注入到远端，命令中通过
shell `$VAR_NAME` 引用。**禁止** Jinja2 插值（loader 静态拒绝）。

最简单的本地用法：

```bash
export PGPASSWORD='your-real-password'
hostlens inspect prod-db-01 --inspector postgres.bloat.tables
```

`.envrc`（direnv）风格更适合多 secret 场景：

```bash
# .envrc (not checked in)
export PGPASSWORD="$(security find-generic-password -w -s 'hostlens-pg')"
export TG_BOT_TOKEN="$(security find-generic-password -w -s 'hostlens-tg')"
```

`hostlens doctor` 会列出每个 Inspector 声明的、当前 env 缺失的 secret：

```bash
hostlens doctor --json | jq '.inspectors.missing_secrets'
# [
#   {"inspector": "postgres.bloat.tables", "secret": "PGPASSWORD"}
# ]
```

缺失只是 `warn`（不影响 doctor exit code），但运行该 Inspector 时
runner 会返回 `InspectorResult(status="requires_unmet",
missing=["env:PGPASSWORD"])`。

### SSH 远端 `AcceptEnv` 配置

OpenSSH 默认只放行 `AcceptEnv LANG LC_*`，其他名字的 env 会被远端 sshd
silently drop。给 Inspector 用的 secret 推荐用 `HOSTLENS_` 前缀，并把下
面加入远端 `/etc/ssh/sshd_config`：

```text
AcceptEnv HOSTLENS_*
```

详见 [docs/operations/targets.md](targets.md#ssh-remote-acceptenv-配置)。

**永远不要**把 secret 拼进命令字符串（`export PGPASSWORD=value; psql ...`），
那样 secret 会进入远端 `ps` 输出与 shell history。

## `collect.sampling_window` 时窗采集

适合「过去 N 分钟的错误数」这类需要时间窗口的采集（如 `log.tail.error_burst`）。

```yaml
collect:
  command: |
    count=$(journalctl --since "{{ window_start }}" --until "{{ window_end }}" -p err --no-pager -q 2>/dev/null | wc -l)
    printf 'error_count=%s\nwindow_seconds=%s\n' "$count" "{{ window_seconds }}"
  sampling_window:
    duration_seconds: 300
```

声明 `sampling_window` 后，runner 基于**可注入时钟**计算并注入三个变量到
**命令渲染上下文**与 **Finding DSL 求值上下文**：

| 变量 | 类型 | 含义 |
|---|---|---|
| `window_start` | str | `now - duration_seconds`，`YYYY-MM-DD HH:MM:SS`（UTC） |
| `window_end` | str | `now`，同格式 |
| `window_seconds` | int | 等于 `duration_seconds` |

约定与注意：

- 时间格式刻意用 `YYYY-MM-DD HH:MM:SS`（UTC）而非带 `T`/时区偏移的 ISO 形式 ——
  journalctl `--since/--until` 对前者解析稳定。命令统一假定 Linux 目标。
- `window_start` / `window_end` / `window_seconds` 是**保留注入变量名**：manifest
  的 `parameters` 若声明同名字段，loader 拒绝加载。
- 省略 `sampling_window` 时三个变量都不注入，行为与既有 Inspector 完全一致。
- runner 的时钟可注入（默认真实 UTC）；测试 / 回放注入固定时钟，使渲染命令逐字节
  稳定 —— 这是 `ReplayTarget` 能精确匹配窗口命令的前提。

## 离线回放：ReplayTarget

`ReplayTarget` 是执行层的回放目标（LLM 层 `PlaybackBackend` 的对称物），按渲染后的
命令字符串匹配 fixture 中预录的 `ExecResult`，让 Inspector 在 CI 上无需真实故障主机
即可走完整 `target → collect → parse → findings` 路径。

`targets.yaml` 用 `type: replay` 接入：

```yaml
version: "1"
targets:
  - name: incident-host
    type: replay
    fixture: ./tests/fixtures/incident_pack/cpu_saturation.json
```

fixture JSON 结构：

```json
{
  "impersonate": "local",
  "capabilities": ["shell"],
  "commands": [
    {"cmd": "command -v ps", "stdout": "/usr/bin/ps\n", "exit_code": 0},
    {"cmd": "<完整渲染后的主命令>", "stdout": "<故障态输出>", "exit_code": 0}
  ],
  "files": {}
}
```

要点：

- `impersonate`（`local`/`ssh`，默认 `local`）决定运行时 `.type`，使 runner preflight
  的 `target.type in manifest.targets` 透明通过 —— 不新增 target 枚举。
- `commands[]` **必须**预录全部 preflight 探测命令（`command -v <binary>`）与渲染后的
  主命令；命令按「逐行 rstrip 后 SHA256」匹配。
- 未命中即抛 `ReplayMiss`（继承 `HostlensError` 而非 `TargetError`），并记入
  `target.misses` —— 绝不回落真实 shell。
- 只读，无写路径，不受 EUID==0 写约束。

incident-pack 的 8 个场景就用「ReplayTarget fixture + PlaybackBackend cassette + 冻结
时钟」做离线确定性回放，详见 [tests/incidents/README.md](../../tests/incidents/README.md)。
