# inspector-plugin-system 规范（delta）

> 目的：`postgres.bloat_tables` 迁移后真实 secret 名为 `HOSTLENS_POSTGRES_PASSWORD`（非 `PGPASSWORD`）。CLI secret 脱敏需求里拿 bloat 当 secret-name 具例的 prose + 场景须同步更新，避免留关于该 inspector 的反事实陈述（与祖父条款闭合一致）。需求标题不变、无 RENAMED；仅示例 secret 名 / inspector 名 typo / env 占位更新，**脱敏行为契约本身不变**。

## 修改需求

### 需求:CLI `hostlens inspectors show <name>` 必须脱敏 secrets

`hostlens inspectors show <name> [--json]` 必须：

- 找不到 inspector → exit 1 + 错误信息
- 默认输出：Rich 渲染 manifest 字段；`secrets:` 字段**只**显示名字列表（如 `[HOSTLENS_POSTGRES_PASSWORD]`），**禁止**从 `os.environ` 读 secret 值显示
- `parameters` 字段含 `default: "${ENV_VAR}"` 的 default 值时，只显示占位符，**不**展开 env var
- `--json`：输出 `InspectorManifest.model_dump()`（含 secrets 字段名列表，**不**含值）；schema 稳定（snapshot 测试覆盖）
- 命令是只读，允许 root

#### 场景:secrets 字段只显示名字

- **当** manifest.secrets=[HOSTLENS_POSTGRES_PASSWORD]，env `HOSTLENS_POSTGRES_PASSWORD=literal-secret-do-not-leak`，跑 `hostlens inspectors show postgres.bloat_tables`
- **那么** 输出含 `HOSTLENS_POSTGRES_PASSWORD` 但**不**含 `literal-secret-do-not-leak`

#### 场景:--json schema 稳定

- **当** 跑 `hostlens inspectors show hello.echo --json`
- **那么** 输出 conform `InspectorManifest.model_json_schema()`

#### 场景:不存在的 name exit 1

- **当** 跑 `hostlens inspectors show does.not.exist`
- **那么** exit code 1，stderr 含 `inspector_not_found`
