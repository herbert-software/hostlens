# inspector-plugin-system 规范增量

## MODIFIED Requirements

### 需求:`InspectorManifest` Pydantic 模型必须严格 conform M1 字段集

`hostlens.inspectors.schema.InspectorManifest` 必须是 Pydantic v2 模型，含**恰好**以下顶层字段（不多不少；`model_config = ConfigDict(extra="forbid", frozen=True)`）：

- 标识：
  - `name: str`：全局唯一；正则 `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`（点分命名空间，如 `linux.cpu.top_processes`；**强制最少两段**——`.` 之前与之后都必须有内容；以小写字母开头）
  - `version: str`：SemVer 字符串；正则 `^\d+\.\d+\.\d+$`（M1 阶段强制 SemVer，与 ToolSpec.version 的 opaque 语义不同——manifest 是用户写的，强 SemVer 帮助 schema 兼容判定）
  - `description: str`：min_length=1
- 兼容性：
  - `tags: list[str] = []`：用于 `inspectors list --tag` 筛选；每个 tag 匹配 `^[a-z][a-z0-9_-]*$`
  - `targets: list[Literal["local", "ssh", "docker"]]`：取值域**恰好** `local` / `ssh` / `docker`；至少 1 个、可任意组合；与 `ExecutionTarget.type` Literal 对齐的子集。`docker` 由「放开 Inspector 的 Docker target 支持」提案放开（DockerTarget 已落地，见 `docker-execution-target` spec）；**`k8s` / `kubernetes` 仍不在取值域内**——KubernetesTarget 尚未实现，放开会造成「manifest 声明了没有实现的 target 类型」的不一致，待后续 KubernetesTarget 提案再扩展。声明 `docker` 的 inspector 必须满足 `inspector-authoring-contract` spec §需求:容器适用性 的容器语义判据。
  - `requires_capabilities: list[str] = []`：值必须在 `Capability` Enum 的小写 value 集合（M1 = `{"shell", "file_read", "ssh", "systemd", "docker_cli"}`）内；loader 校验未知值 raise
  - `requires_binaries: list[str] = []`：每个 binary 名匹配 `^[a-zA-Z0-9._-]+$`（防注入）
  - `requires_files: list[str] = []`：每个路径必须匹配严格正则 `^/[A-Za-z0-9._/-]+$`（POSIX 绝对路径 + 严格 ASCII 字符集，**禁止** shell 元字符 `; $ \` ( ) | & < > \n \0` 等）**且**在 Pydantic `field_validator` 中做 path-component 级二次校验：拆分 `path.split("/")` 后任何 component 等于 `"."` 或 `".."` → raise（防止父目录穿越，保证路径是规范化的绝对路径）；理由：runner preflight 探测时会把 path 拼到 `[ -r <path> ]` shell 求值，宽松字符集会构成命令注入向量；防御纵深由 (a) 此字段级正则在 manifest 加载时拒绝 + (b) component 级 `..` 拒绝 + (c) runner 在拼 shell 命令前**仍**用 `shlex.quote(path)` 包路径**三重保证**。**已知接受风险**（manifest 作者责任，不在 loader 范围）：路径仍可能指向 `/proc/self/mem` 等伪文件系统、`/dev/...` 字符设备等敏感位置——M1 不做白名单 prefix 检查（无业务必要、增加误判面）；docs/operations/inspectors.md 中记为「已知接受风险」
  - `privilege: Literal["none", "sudo", "root"] = "none"`：M1 runner 对 `privilege != "none"` 在未 `allow_privileged` opt-in 时返回 `requires_unmet`；M1 范围**不**实现 sudo 调用集成
- 参数化：
  - `parameters: dict[str, Any] | None = None`：JSON Schema dict；如非 None 必须 conform JSON Schema draft 2020-12；`type: object` 顶层（其他顶层类型 reject）
  - `secrets: list[str] = []`：每个 secret 名匹配 `^[A-Z_][A-Z0-9_]*$`（POSIX env var 命名）
- 采集：
  - `collect: CollectSpec`：嵌套模型；详见下一需求块
- 解析：
  - `parse: ParseSpec`：嵌套模型；详见下一需求块
- 输出与判定：
  - `output_schema: dict[str, Any]`：JSON Schema dict；`type: object` 顶层；非 None
  - `findings: list[FindingRule]`：可空（空列表表示该 Inspector 只采集不判定，仅返回 output）

**M1 范围禁用字段（出现 → loader raise `manifest_validation_error`）**：`hook` / `sampling_window` / `artifacts` / 任何 manifest 顶层未列出的字段。

#### 场景:Manifest 字段集严格

- **当** 用 `extra` 字段（如 `priority: high`）的 yaml 加载 manifest
- **那么** 必须 raise `pydantic.ValidationError`，错误信息含 `extra fields not permitted` + 字段名

#### 场景:name 正则强制点分命名

- **当** 试图加载 `name: simple_name` 的 manifest（缺少点分）
- **那么** 必须 raise `pydantic.ValidationError`，错误信息含 name 正则

#### 场景:name 接受多级点分

- **当** 加载 `name: linux.cpu.top_processes` 的 manifest
- **那么** 必须成功

#### 场景:version 强制 SemVer

- **当** 试图加载 `version: latest` 或 `version: 1` 或 `version: v1.0` 的 manifest
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:targets 必须非空且仅含允许值

- **当** 试图加载 `targets: []`（空）或 `targets: [kubernetes]` 或 `targets: [k8s]` 的 manifest
- **那么** 必须 raise `pydantic.ValidationError`（空列表违反 `min_length=1`；`kubernetes`/`k8s` 不在 Literal 取值域内——KubernetesTarget 未实现）

#### 场景:targets 接受 docker

- **当** 加载 `targets: [docker]` 或 `targets: [local, docker]` 或 `targets: [local, ssh, docker]` 的 manifest
- **那么** 必须成功（`docker` 已在 Literal 取值域内；DockerTarget 已实现）

#### 场景:requires_files 含 shell 元字符被拒绝

- **当** 试图加载 `requires_files: ["/tmp/x; curl evil.com"]` 或 `requires_files: ["/etc/$(whoami)"]` 或 `requires_files: ["/path with space"]` 的 manifest
- **那么** 必须 raise `pydantic.ValidationError`（字段级正则 `^/[A-Za-z0-9._/-]+$` 在 manifest 加载阶段拒绝；防御纵深的第一道闸）

#### 场景:requires_files 含 NUL 字节被拒绝

- **当** 试图加载 `requires_files: ["/tmp/x  "]`（含 NUL 字节）
- **那么** 必须 raise `pydantic.ValidationError`（正则字符集已限定 ASCII alphanumeric + `._/-`，NUL 不在其中）

#### 场景:requires_files 含 .. 父目录穿越被拒绝

- **当** 试图加载 `requires_files: ["/etc/../passwd"]` 或 `requires_files: ["/a/b/../c"]`
- **那么** 必须 raise `pydantic.ValidationError`（component 级 `..` 校验拒绝；防穿越是 manifest 安全契约的一部分）

#### 场景:requires_files 含 . 单点 component 被拒绝

- **当** 试图加载 `requires_files: ["/etc/./passwd"]`
- **那么** 必须 raise `pydantic.ValidationError`（要求路径已规范化）

#### 场景:M1 禁用字段被拒绝

- **当** 试图加载含顶层 `hook: hook.py` 或 `artifacts: [...]` 或 `collect.sampling_window: ...` 的 manifest
- **那么** 必须 raise `pydantic.ValidationError`（与 extra="forbid" 行为一致）

#### 场景:Manifest 实例不可变

- **当** 已加载的 `manifest` 试图赋值 `manifest.name = "x"`
- **那么** 必须 raise `pydantic.ValidationError`（frozen=True）
