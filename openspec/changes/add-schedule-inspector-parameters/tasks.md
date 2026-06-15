## 1. schedule-manifest schema

- [x] 1.1 `scheduler/schema.py`:`ScheduleManifest` 新增 `inspector_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)`,字段处注释说明内层不在调度层强类型化(裁判在 inspector `parameters` schema)、`Any` 为有意豁免
- [x] 1.2 测试 `tests/scheduler/test_schema*.py`:省略 → `{}`;dict-of-dict 正常解析;**标量** value(string)→ `ValidationError`(精确锚,不声称内层被校验);top-level `extra="forbid"` 对未知字段仍 fail(回归)

## 2. resolve_inspector_set 迁址(解 loader 重依赖 + 分层)

- [x] 2.1 把 `resolve_inspector_set` 从 `orchestration/deterministic.py` 迁到 `inspectors/health.py`(与 `DEFAULT_HEALTH_INSPECTORS` 同住、纯名集逻辑无 orchestration 依赖);`deterministic.py` **显式** `from hostlens.inspectors.health import resolve_inspector_set` 并放进 `__all__`(只写 `__all__` 不真 import 会让 `from ...deterministic import resolve_inspector_set` 报错)
- [x] 2.2 测试:断言 `hostlens.orchestration.deterministic.resolve_inspector_set is hostlens.inspectors.health.resolve_inspector_set`(**对象身份**,防未来「清理未用 import」误删 re-export 静默断 `tests/orchestration/test_deterministic_collection.py:46` 的旧路径 import);行为 `None→DEFAULT_HEALTH_INSPECTORS`、否则 verbatim 不变

## 3. 抽公共参数校验 helper(双门同管线)

- [x] 3.1 从 `inspectors/runner.py` 抽出公共 `coerce_and_validate_parameters(params, manifest) -> dict[str, Any]`:**第一行** `params = dict(params or {})`(归一化 `None`/falsy,两个 caller 都传原始 params),随后 `_apply_schema_defaults` + `_coerce_parameters` + `jsonschema.validate`。异常契约 = `(jsonschema.ValidationError, jsonschema.exceptions.SchemaError)` 两类(畸形 inspector schema raise 后者)。`InspectorRunner.run` 改调它,**保留**原对**两类**异常的处理(runner.py:248 `ValidationError`→`parameter_validation_failed`、:265 `SchemaError`→`parameter_schema_invalid` 的 `status="exception"` 不变);**helper 只覆盖有参分支**(`manifest.parameters is not None`)——refactor 必须**保留 runner.py:242 的无条件 `effective_parameters = dict(parameters or {})`**:有参分支用 helper 返回值覆盖它,无参分支沿用 :242 的值(保「无参 inspector 跳过校验」的字节级旧行为、决策 6),不得把 helper 调用 gate 进 `if` 后让无参分支的 `effective_parameters` 未定义
- [x] 3.2 测试:`coerce_and_validate_parameters` 对 `required`+`default` 字段省略时 **接受**(默认注入后 validate 过)——锚定 loader 与 runner 接受集相等、不退回 raw-validate 的方向反转洞;另测畸形 schema → raise `SchemaError`(锚定 helper 异常契约含两类)

## 4. loader 注入 InspectorRegistry + 五道 fail-loud 校验(全 ConfigError)

- [x] 4.1 `scheduler/loader.py`:`load_schedules` 新增 `inspector_registry: InspectorRegistry` 形参。接线(review 的 wiring undercount,逐项落实):
  - `cli/schedule.py`:改 helper `_load_manifests_or_exit(settings, target_registry)` 签名 + **4 处** call site 传 inspector registry——`list_cmd`(:328) / `status_cmd`(:526) **新建** registry;`trigger_cmd`(:361) / `_serve`(:433，run+daemon 共用) **已建**、只穿现有(`run_cmd`/`daemon_cmd` 不直接调 loader、委托 `_serve`,**勿**在它们里加冗余 build);`_build_inspector_registry` 已存在
  - `cli/doctor.py`:`_check_schedules` 新增建 inspector registry;**并把 inspector-registry-build 的失败纳入其 except**(现元组 `(ConfigError, TargetError, ValidationError, yaml.YAMLError)` 不含 `InspectorError`,坏 builtin → 致命 `InspectorError` 会崩 doctor,违反「doctor 不得因坏 manifest 崩」)→ 转 `CheckResult(status="error")` 不外抛
  - `cli/mcp.py`:`serve` 已在 `:202` 建 inspector registry,穿进 `_build_management_deps` 的 `load_manifests=lambda` 闭包
- [x] 4.2 校验 step 1 mode 适用性:`mode != "deterministic"` 且 `inspector_parameters` 非空 → `ConfigError`(含文件名,指明仅 deterministic 生效)
- [x] 4.3 校验 step 2 key 归属:deterministic 时 `key ∉ resolve_inspector_set(manifest.inspectors)`(从 `inspectors.health` import)→ `ConfigError`(含文件名 + 越界 key);显式 `inspectors:` 集权威、不与默认集取并
- [x] 4.4 校验 step 3 inspector 已注册:`registry.get(key)` **包 `try/except InspectorError`** → 翻译成 `ConfigError`(含文件名 + key);禁止裸 `InspectorError` 逃出
- [x] 4.5 校验 step 4 无参 inspector(loader 独有生产门):`manifest.parameters is None` 且 `params` 非空 → `ConfigError`(不接受参数);`{}` 不触发。step 4/5 **仅**对 step 4.4 成功取回的 manifest 跑(短路,未取回不访问 `manifest.parameters`)
- [x] 4.6 校验 step 5 参数值:对有 `parameters` 的 inspector 调 `coerce_and_validate_parameters`(task 3.1 同一 helper)→ 捕获 **`jsonschema.ValidationError` 与 `jsonschema.exceptions.SchemaError` 两类** 翻译成 `ConfigError`(只接前者会让 `SchemaError` 裸泄漏崩 CLI/doctor);**禁止**退回 raw-validate(否则 loader≠runner)
- [x] 4.7 测试 `tests/scheduler/test_loader*.py`(注入 fake/real inspector registry):agent+非空→ConfigError;deterministic+越界 key→ConfigError(含 key);deterministic+显式 inspectors+key 不在显式集→ConfigError;**deterministic+显式 inspectors 含未注册名作 param key→ConfigError(非裸 InspectorError)**;deterministic+无参 inspector key+非空 params→ConfigError;deterministic+typo 参数键(net.listening_ports)→ConfigError(加载期 ValidationError);**deterministic+畸形 parameters schema 的 fake inspector→ConfigError(非裸 SchemaError)**;deterministic+无参 inspector+`{}`→pass;deterministic+key 在集且参数合法→pass;deterministic+空→pass
- [x] 4.8 测试 `tests/cli/test_doctor*.py`:doctor 的 schedule 检查在 inspector registry build 出错时返 `CheckResult(status="error")`、**不崩**(回归「doctor 不因坏 manifest 崩」)
- [x] 4.9 (消息精度,非阻塞)`build_registry_from_search_paths(...).errors` 被丢弃 → 被引用的 user inspector「加载失败」会经 step 3 当「未注册」报。要么把 build `errors` 传入 loader 让 `ConfigError` 区分「未注册 vs 加载失败」,要么在 `ConfigError` 文案显式接受该合并(fail-loud 本身正确、仅消息精度);二选一并在实现注明

## 5. deterministic 参数透传 + 运行期兜底

- [x] 5.1 `orchestration/deterministic.py`:`run_deterministic_inspection` 新增 keyword `inspector_parameters: dict[str, dict[str, Any]] | None = None`(**非** `Mapping`——`InspectorRunner.run` 形参是 `dict[str, Any] | None`,须可直接赋值);`_bounded` 内 `params = (inspector_parameters or {}).get(manifest.name)` 传 `runner.run(parameters=params)`,替换 `parameters=None`
- [x] 5.2 `orchestration/deterministic.py`:`run_deterministic_pipeline` 新增同名 keyword 并直传
- [x] 5.3 `scheduler/runner.py`:`_run_job` 调 `run_deterministic_pipeline` 时传 `inspector_parameters=manifest.inspector_parameters`
- [x] 5.4 测试 `tests/orchestration/test_deterministic*.py`:命中→`runner.run` 收声明 dict(spy/fake runner);未命中→`None`;present-but-`{}`→`{}`;无参 inspector+`{}`→`{}` 无害 no-op;pipeline→inspection 直链不丢;空→全 `None`(行为不变锚)

## 6. net.listening_ports inspector

- [x] 6.1 `inspectors/builtin/net/listening_ports.yaml`:`parameters.properties` 加 `allowed_processes: {type: array, items: {type: string, pattern: "^[A-Za-z0-9._@-]+$"}, default: []}`(`additionalProperties:false` 不变);更新 `description`:进程名豁免语义 + 进程名空(非特权拿不到他人 socket)仍按未豁免处理的保守边界 + **pattern 的 charset 限制**(含 `:`/括号的罕见 `comm` 名如 `postgres:main` 不可加入 allowlist,by-design)
- [x] 6.2 同文件:finding `when` 改为 `p.wildcard == True and p.port not in allowed_ports and p.process not in allowed_processes`;`version` `1.0.0 → 1.1.0`
- [x] 6.3 测试(registry build):manifest 加载后 `registry.errors == []`(pattern 满足 authoring-contract、防 string-array 缺 pattern 致 build 崩)
- [x] 6.4 测试(finding/fixture 层,安全语义显式锚):wildcard + process ∈ allowed_processes → **不**产 finding;wildcard + process ∉ → 产 warning;wildcard + port ∈ allowed_ports(原路径)→ 仍不产;process 为空串 + 非空 allowed_processes → 产 warning(保守边界锚);**既有** `tests/inspectors/test_os_net.py::test_listening_ports_unexpected_detected` / `::test_listening_ports_ok_no_findings` 不变绿(默认 `allowed_processes=[]` 保旧行为,采集 shell 未改、fixture 稳定)

## 7. Demo Path 与文档

- [x] 7.1 **不**在仓内 `schedules/` 提交 live manifest（会因 target 未注册 → `schedule list`/`doctor checks.schedules` 在 fresh checkout 崩,见 loader target-membership 校验）。Demo Path 作为 **docs 示例**(`docs/` 下 fenced YAML + 注明 `target add` 前置)或 `*.yaml.example`(不被 `schedules/` 扫描)交付,proposal 的 Demo Path 措辞同步
- [x] 7.2 docs:`docs/` 下 schedule manifest 字段说明补 `inspector_parameters`(deterministic-only + key 须在 inspector 集 + 须是已注册的有参 inspector + value 由 inspector schema 经双门同管线在加载期裁定)

## 8. 收尾

- [x] 8.1 全量 `pytest tests/`(非子集)+ `mypy --strict` + `ruff` 绿;确认未顶动既有 cassette/snapshot(本提案不改 inspector message,不应有 finding-id churn——`when` 仅加合取项,message 模板未动)
- [x] 8.2 `openspec-cn validate add-schedule-inspector-parameters --strict` 绿;temp 副本实测 `archive` 不报错(防归档期 rebuild 校验返工)
