## 1. Feature branch 与脚手架

- [x] 1.1 从最新 `main` 切 feature branch：`git checkout main && git pull && git checkout -b feat/add-report-data-model`；验收：`git status` 显示干净工作区，`git rev-parse --abbrev-ref HEAD` 返回 `feat/add-report-data-model`
- [x] 1.2 创建包目录与 `__init__.py`（零副作用）：`src/hostlens/reporting/__init__.py` 仅 export `Severity` / `Evidence` / `Finding` / `Report` / `render_markdown` / `render_json`；验收：`python -c "import hostlens.reporting"` 不触发任何 IO（pytest 测试在任务 7 验收）

## 2. `hostlens.reporting.models` Pydantic 模型

- [x] 2.1 实现 `Severity` Literal（`hostlens/reporting/models.py`）：`Severity = Literal["info", "warning", "critical"]`；验收：`from hostlens.reporting.models import Severity; assert "info" in Severity.__args__`
- [x] 2.2 实现 `Evidence` Pydantic 模型（含 `kind` Literal 4 值 + 10 个 optional 字段 + `truncated: bool = False` + `model_config = ConfigDict(extra="forbid", frozen=True)`）；验收：`mypy --strict src/hostlens/reporting/models.py` 过
- [x] 2.3 实现 `Evidence.model_validator(mode="after")` 强制 kind ↔ 字段子集映射（4 行映射表）；验收：手写一个矩阵测试 `tests/reporting/test_evidence_validator.py` 覆盖 4 kind × （合法字段集 / 缺必填 / 含禁止字段）至少 12 个 case 全过
- [x] 2.4 实现 `Finding` Pydantic 模型（严格 4 字段：`severity: Severity` / `message: str` 含 min_length=1 / `evidence: list[Evidence] = []` / `tags: list[str] = []`；`extra="forbid", frozen=True`）；验收：`tests/reporting/test_finding_model.py` 覆盖 spec §需求:`Finding` 的 9 个场景全过（含 tags 默认空 + tags 接受字符串列表 + tags 拒绝非字符串）
- [x] 2.5 实现 `Report` Pydantic 模型（9 字段：`report_id: UUID` / `schema_version: Literal["1.0"]` / `intent: str | None = None` / `target_name: str` min_length=1 / `inspector_results: list[InspectorResult]` min_length=1 / `findings: list[Finding] = []` / `started_at: datetime` / `finished_at: datetime` / `metadata: dict[str, str] = {}`）+ `model_validator` 强制 `finished_at >= started_at`；**循环导入处理**：模块顶部用 `from __future__ import annotations` + `if TYPE_CHECKING: from hostlens.inspectors.result import InspectorResult`；`InspectorResult` 引用走字符串前向 ref；forward ref 解析放到 `inspectors/result.py` 末尾的 `model_rebuild()`（task 5.4）；验收：`tests/reporting/test_report_model.py` 覆盖 spec §需求:`Report` 的 8 个场景全过
- [x] 2.6 实现 `Report.from_inspector_results(target_name, inspector_results, *, intent=None, started_at, finished_at, metadata=None) -> Report` classmethod 工厂：自动 `uuid4()` / 锁定 `schema_version="1.0"` / 机械 flatten findings / `metadata=None` 时填 `{}` 给 Report / 空 inspector_results raise ValueError；验收：`tests/reporting/test_report_factory.py` 覆盖 spec §需求:`Report.from_inspector_results` 的 6 个场景全过 + 额外断言 metadata 默认行为

## 3. `hostlens.reporting.render_markdown` 渲染器

- [x] 3.1 实现 `render(report: Report) -> str` 主函数骨架（标题 + meta 表 + 4 个 section）；验收：`render(report)` 返回非空 str，含 `# Hostlens Inspection Report`
- [x] 3.2 实现 meta 表渲染（2 列 GFM 表格：Field | Value）；intent=None 时显示 `—`；时间戳用 ISO 8601 格式；duration_seconds 显示 2 位小数；验收：snapshot 测试 `tests/reporting/test_render_markdown_meta.py` 通过
- [x] 3.3 实现 `## Summary` 章节（按 severity 分组数量统计 + `_No findings._` 空列表回退）；验收：`tests/reporting/test_render_markdown_summary.py` 覆盖空/非空两个场景
- [x] 3.4 实现 `## Findings` 章节（按 severity 倒序 critical → warning → info；finding 标题 `### [{SEVERITY}] {message}`；evidence 非空时渲染 `<details>` 折叠块 + per-evidence sub-table；evidence 空时不渲染 details）；验收：spec §需求:`render_markdown.render` 的 3 个相关场景（倒序 / 空 details / 非空 details）测试全过
- [x] 3.5 实现 `## Inspector Results` 附录章节（含 name / version / status / target_name / duration_seconds / error / output JSON 围栏代码块）；验收：spec 的"InspectorResult.status != 'ok' 时显式提示"场景测试过
- [x] 3.6 实现控制字符 escape 工具函数 `_escape_control_chars(s: str) -> str`（保留 `\n` `\t`；其他 `\x00-\x1f` `\x7f` 转字面量 `\xXX`）；应用到 evidence 的 stdout/stderr/excerpt/command 与 InspectorResult.error；验收：spec 的"控制字符被转义"场景测试过
- [x] 3.7 验证 env var 不被展开（渲染器**不** 调 `os.path.expandvars` 等）；验收：spec 的"env var 不被展开"场景测试过
- [x] 3.8 验证单文件 ≤ 200 行（含 imports + docstring）；验收：`wc -l src/hostlens/reporting/render_markdown.py` 输出 ≤ 200
- [x] 3.9 性能验证（M1 范围）：单 Inspector + 0 finding 渲染 < 50ms；验收：`tests/reporting/test_render_markdown_perf.py` 用 `pytest-benchmark` 或简单 timeit 断言

## 4. `hostlens.core.redact` 集成（脱敏边界依赖，**必须先于 §4.5 渲染器实现**）

- [x] 4.1 检查 `src/hostlens/core/` 下是否已有 `redact.py`（M0 可能落地或未落地）；验收：`ls src/hostlens/core/redact.py` 或确认不存在
- [x] 4.2 若 `redact.py` 不存在，新增 `src/hostlens/core/redact.py`：实现 `redact_text(s: str) -> str` 函数，正则规则继承 OPERABILITY.md §7.2（`(password|secret|token|api[_-]?key|bearer)\s*[:=]\s*\S+` / `eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+` JWT / `sk-[a-zA-Z0-9-]{20,}`）；脱敏后保留前 4 后 4 字符（如 `sk-abcd...7890`）；验收：单元测试 `tests/core/test_redact.py` 覆盖 5 类规则各至少 2 个 case
- [x] 4.3 若 `redact.py` 已存在，验证其 API 与本提案预期一致（`def redact_text(s: str) -> str`）；若 API 不一致（如返回值非 str / 函数名不同），开 spec amendment 提案而非本提案兼容；验收：函数签名 + 行为与 4.2 等价
- [x] 4.4 实现共享辅助 `hostlens.reporting._redact.redact_report_for_render(report: Report) -> Report`：深拷贝 + 递归把 spec §需求:`render_markdown` / `render_json` 必须在渲染边界对字符串字段过 `core/redact.py` 中列出的所有字段路径过 `redact_text`；**不**修改源对象；验收：`tests/reporting/test_redact_report.py` 覆盖 spec 中 6 个 redaction 场景

## 4.5 `hostlens.reporting.render_json` 渲染器（**依赖 §4** redact 已就绪）

- [x] 4.5.1 实现 `render(report: Report) -> str`：内部先调 `redact_report_for_render(report)` 得到脱敏后的 `Report` 副本 → 再 `redacted.model_dump_json(indent=2, exclude_none=False)`；**不**修改源 Report；验收：spec §需求:`render_json.render` 的 4 个场景测试全过（含 JSON valid / Pydantic round-trip / null 保留 / 缩进）+ tasks §7.8 中 redaction 测试覆盖该路径

## 5. 消除 Finding 双地址（type alias re-export）

- [x] 5.1 修改 `src/hostlens/inspectors/result.py`：删除本地 `Finding` Pydantic 类定义；加 `from hostlens.reporting.models import Finding as Finding`；保留 `__all__ = ["Finding", "InspectorResult", "InspectorStatus"]`；验收：`from hostlens.inspectors.result import Finding as F1; from hostlens.reporting.models import Finding as F2; assert F1 is F2` 过
- [x] 5.2 修改 `src/hostlens/tools/schemas/run_inspector.py`：删除本地 `FindingSummary` Pydantic 类定义；加 `from hostlens.reporting.models import Finding`；定义 `FindingSummary = Finding`；保留 `__all__`；验收：`from hostlens.tools.schemas.run_inspector import FindingSummary; from hostlens.reporting.models import Finding; assert FindingSummary is Finding` 过
- [x] 5.3 **修改 `src/hostlens/tools/default_tools.py` 的 `_run_inspector_handler` 投影逻辑（≈ 第 148-159 行）**：当前实现 `evidence={k: str(v) for k, v in finding.evidence.items()}` 在 BREAKING 后会 raise `AttributeError`（list 没有 .items()）；改为 `findings=list(result.findings)` 直接复用 InspectorResult.findings（因 `FindingSummary = Finding` 同类型）；保留 `result.status != "ok"` 分支的 `findings=[]` 早返回；验收：(a) `pytest tests/tools/test_run_inspector_with_real_registry.py -v` 全绿；(b) 手测 `python -c "from hostlens.tools.default_tools import _run_inspector_handler; ..."` 投影 1 个含 Finding(evidence=[Evidence(...)]) 的 InspectorResult 后 dump 输出不报错
- [x] 5.4 在 `inspectors/result.py` 模块**末尾**追加：`from hostlens.reporting.models import Report as _Report; _Report.model_rebuild(_types_namespace={"InspectorResult": InspectorResult}, force=True)` 完成 Pydantic forward ref 解析（design.md §决策 1 末尾的循环导入处理）；**必须**显式传 `_types_namespace` —— bare `model_rebuild()` 因 TYPE_CHECKING 隔离会 raise `PydanticUndefinedAnnotation`；**必须**传 `force=True` —— Pydantic v2 中如果 `_Report` 在 import reporting.models 时已部分 build（即使 forward ref 未解析），缺 force=True 时 `model_rebuild()` 会直接返回 None 跳过重建（v2 已知行为），导致 forward ref 静默未解析；验收：`python -c "import hostlens.inspectors.result; from hostlens.reporting.models import Report; r = Report.model_fields['inspector_results']; assert r.annotation.__args__[0].__name__ == 'InspectorResult'"` 过
- [x] 5.5 更新 `inspectors/result.py` docstring：去掉"M3 会扩展"的预告（已经在本提案兑现）；改为"Finding SOT 在 hostlens.reporting.models"的指引 + 说明模块末尾 model_rebuild 的目的
- [x] 5.6 更新 `tools/schemas/run_inspector.py` docstring：同上去掉 M3 预告
- [x] 5.7 跑全量已有测试验证零回归：`pytest tests/inspectors/ tests/tools/ -v`；验收：全绿

## 6. `hostlens.cli.inspect` CLI 命令

- [x] 6.1 创建 `src/hostlens/cli/inspect.py`：Typer command 骨架，参数集对齐 spec §需求:`hostlens inspect` 命令必须支持 6 个选项与 1 个位置参数；验收：`hostlens inspect --help` 输出含全部 6 个选项 + 退出码语义 4 行
- [x] 6.2 实现 target 查找：从 `TargetRegistry` 取 target；未找到 → stderr 输出 `target not found:` 前缀 + 提示 + exit 3；验收：spec §需求:`hostlens inspect` 退出码 的"target 未找到退出 3"场景测试过
- [x] 6.3 实现 inspector 查找：从 `InspectorRegistry` 取 manifest；未找到 → stderr `inspector not found:` 前缀 + 提示 + exit 3；验收：spec 同需求"inspector 未找到退出 3"场景过
- [x] 6.4 实现 `--parameters` 双语法解析（`{...}` inline JSON / `@<path>` 文件引用）：解析失败 → exit 3 + stderr `invalid --parameters:` / `failed to read --parameters file:` 前缀；验收：spec §需求:`--parameters` 双语法 的 3 个场景全过
- [x] 6.5 调度 InspectorRunner：异步包 `asyncio.run` + `InspectorRunner(ctx).run(manifest_for_run, target, parameters, allow_privileged=...)`；**`--timeout` 实现**（spec §需求:`hostlens inspect` 命令必须支持 6 个选项与 1 个位置参数 中"`--timeout` 必须经 CollectSpec 重构注入触发 validation"约束）：当 `--timeout` 非 None 时，构造 `from hostlens.inspectors.schema import CollectSpec; new_collect = CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds": cli_timeout}); manifest_for_run = manifest.model_copy(update={"collect": new_collect})`；当 `--timeout is None` 时 `manifest_for_run = manifest`（原引用）；**禁止** 直接 `manifest.collect.model_copy(update={"timeout_seconds": cli_timeout})` —— Pydantic v2 `model_copy(update=...)` 不触发字段 validation；验收：单元测试 `tests/cli/test_inspect_runner_invocation.py` 断言 (a) 不带 `--timeout` 时传给 runner 的 manifest 与原 manifest 是同一引用 (b) 带 `--timeout 5` 时传给 runner 的 manifest.collect.timeout_seconds == 5 且 InspectorRegistry 中的原 manifest 未被修改 (c) monkeypatch 绕过 CLI 上限校验后传 `--timeout 9999` 必须触发 `pydantic.ValidationError`（CollectSpec 防御纵深生效）
- [x] 6.5a `--timeout` 边界校验：CLI 收到 `--timeout` 后立即校验 `1 ≤ value ≤ 300`（与 archived `inspector-plugin-system` spec 中 `CollectSpec.timeout_seconds = Field(ge=1, le=300)` 字段约束严格一致）；不在范围 → exit 3 + stderr `invalid --timeout: must be in [1, 300]`；验收：spec 的 `--timeout 0` / `--timeout 301` / `--timeout 300`（边界接受）3 个场景测试过
- [x] 6.6 构造 Report：`Report.from_inspector_results(target_name, [inspector_result], intent=None, started_at=..., finished_at=...)`；finished_at < started_at 触发的 ValidationError → exit 2 + stderr `internal: report validation failed:`；验收：spec §需求 的"Report finished_at < started_at 退出 2"场景过
- [x] 6.7 实现退出码计算（4 值优先级 3 > 2 > 1 > 0；runner 失败优先于 critical finding）；验收：spec §需求:`hostlens inspect` 退出码 的 9 个状态场景全过（healthy/critical/warning/timeout/target_unreachable/requires_unmet/exception/runner 优先/usage）
- [x] 6.8 实现渲染与输出（`--format md` → `render_markdown.render(report)`；`--format json` → `render_json.render(report)`；`--output FILE` → 写文件 + stdout 静默；缺省 → stdout）；写文件失败 → exit 3 + stderr `failed to write output:`；验收：spec §需求:`hostlens inspect` 必须以 stdout/stderr 分离 的 4 个场景全过
- [x] 6.9 CLI 边界异常包装（**不** 输出 Python traceback；包装为 `internal: <kind>: <msg>` 一行）；验收：故意在 runner 注入 `raise RuntimeError("boom")` 后 stderr **不** 含 `Traceback`
- [x] 6.10 在 `src/hostlens/cli/__init__.py` 注册 `inspect` 子命令：`from hostlens.cli.inspect import inspect_cmd; app.command("inspect")(inspect_cmd)` 或 add_typer 方式；验收：`hostlens --help` 输出含 `inspect` 子命令
- [x] 6.11 **Typer usage exit 改写**：在 `src/hostlens/cli/__init__.py` 顶层入口或 `inspect.py` 的命令函数外层包裹 try-except，**仅**捕获 `click.exceptions.UsageError` / `BadParameter` / `MissingParameter` 改写 exit code 为 3；**禁止** 捕获 `SystemExit(code=0)` —— `--help` / `--version` 必须保持 exit 0（spec §需求:`hostlens inspect` 命令必须支持 6 个选项与 1 个位置参数 中的"Typer 默认 usage exit 转换"约束 + "`--help` 退出码必须为 0（不被 usage 改写误伤）"场景）；验收：(a) 3 个 Typer 默认场景（缺 target / 缺 --inspector / --format html）测试 exit code == 3；(b) `hostlens inspect --help` 测试 exit code == 0

## 7. 测试矩阵与 syrupy snapshot

- [x] 7.1 添加 `syrupy` 到 `[dev]` 依赖（若 pyproject 尚未含）：`pip install -e ".[dev]"` 验证可装；验收：`pytest --version` + `python -c "import syrupy"` 均成功
- [x] 7.2 编写 `tests/reporting/__init__.py` 与 `tests/cli/test_inspect.py` 集成测试 4 个 case（healthy md / healthy json / inspector 不存在 / target 不存在）；驱动真实 InspectorRegistry 装配（hello.echo / system.uptime builtin）+ TargetRegistry + LocalTarget；验收：4 个 case 全过 + `pytest -v tests/cli/test_inspect.py` 全绿
- [x] 7.3 至少 1 个 syrupy snapshot 测试：用自定义 serializer 把 `report_id` → `<UUID>` / timestamp → `<TIMESTAMP>` 后断言 md 字节级输出；首次跑 `pytest --snapshot-update`，提交 snapshot 文件；验收：第二次 `pytest tests/cli/test_inspect.py -k snapshot` 不需要 `--snapshot-update` 且通过
- [x] 7.4 单元测试 `tests/reporting/test_init_no_side_effects.py`：清 `sys.modules["hostlens.reporting"]` 后 `importlib.import_module("hostlens.reporting")`；用 `unittest.mock.patch("builtins.open")` 或 `monkeypatch` 断言 import 期间未触发文件打开（不计 .py / .pyc）；验收：测试过
- [x] 7.5 单元测试 `tests/reporting/test_finding_alias.py` 与 `tests/tools/test_finding_summary_alias.py`：断言 type alias 等价（`F1 is F2`）；验收：两测试过
- [x] 7.6 集成测试：用 BREAKING 后的 Finding 触发 ValidationError —— `Finding(severity="info", message="x", evidence={"a": "b"})` 必须 raise；验收：spec inspector-plugin-system §需求 的"旧 dict 形式 evidence 不再接受"场景过
- [x] 7.7 单元测试 `tests/reporting/test_no_circular_import.py`：分多个独立 subprocess 验证：
  - (a) `python -c "import hostlens.inspectors.result; from hostlens.reporting.models import Report"` 不报错 + exit 0
  - (b) `python -c "import hostlens.reporting; import hostlens.inspectors.result"` 不报错 + exit 0
  - (c) `python -c "import hostlens.inspectors.result; from hostlens.reporting.models import Report; r = Report(...一个合法 Report 构造...); print(r.target_name)"` 不报错 + exit 0
  - (d) **失败路径断言**：`python -c "import hostlens.reporting; from hostlens.reporting.models import Report; Report(report_id=..., schema_version='1.0', target_name='t', inspector_results=[<random object>], started_at=..., finished_at=...)"`（**未** import `hostlens.inspectors.result` 跳过 model_rebuild）必须 exit != 0 且 stderr 含 `PydanticUndefinedAnnotation`（验证当依赖顺序错时报清晰错误而非静默错误）
  - 用 `subprocess.run(..., capture_output=True, text=True)` 跑 clean Python 子进程避免污染；验收：3 个成功子进程 exit 0 + 1 个失败子进程含明确 PydanticUndefinedAnnotation 异常名
- [x] 7.8 单元测试 `tests/reporting/test_redaction_at_render_boundary.py`：构造含 `evidence.stderr="sk-abcdefghijklmnopqrstuvwxyz1234567890"` 的 Report → 调用 `render_markdown.render(report)` 与 `render_json.render(report)` → grep 输出**不** 含完整 API key；验收：spec §需求:`render_markdown` / `render_json` 必须在渲染边界对字符串字段过 `core/redact.py` 的 6 个场景全过
- [x] 7.9 跑全量测试套 + mypy + ruff：`pytest -v && mypy --strict src/hostlens && ruff check src tests && ruff format --check src tests`；验收：全绿

## 8. CLI 退出码与 stdout/stderr 分离的端到端验证

- [x] 8.1 用 `pytest` + `typer.testing.CliRunner` 写端到端测试覆盖 exit code 表（spec §需求:`hostlens inspect` 退出码 4 值）；至少 9 个场景：0 healthy / 1 critical / 0 warning / 2 timeout / 2 target_unreachable / 2 requires_unmet / 2 exception / 2 runner-优先于-critical / 3 target-missing；验收：`pytest tests/cli/test_inspect_exit_codes.py -v` 全绿
- [x] 8.2 端到端 stdout/stderr 分离测试：`CliRunner` 捕获分离 streams；验收：spec §需求 的"缺省输出 stdout"/"--output 写文件且 stdout 不重复"/"错误信息走 stderr"/"不输出 traceback" 4 个场景过

## 9. 已知接受风险与脱敏的文档化

- [x] 9.1 创建 `docs/operations/inspect.md`：记入 Demo Path 10 步 + CLI exit code CI scriptlet 示例 + 已知接受风险（`--output` 静默覆盖 / Report 总字节 > 8MB 时只 warning 不失败 / 脱敏覆盖 OPERABILITY §7.2 默认规则但**不**保证捕获非默认形式的敏感字符串如自定义 token 格式——补充规则需走自定义 regex 配置，由 M5 Notifier 提案设计统一脱敏配置入口）；验收：文档 ≥ 1 个完整 CI scriptlet 示例 + 1 段说明本提案脱敏边界（"OPERABILITY §7.2 默认规则在 render 时强制；非默认规则需扩展配置"）
- [x] 9.2 验证 `evidence.command` 渲染时 env var 不展开（手测 + 单测）：故意在 `Evidence(kind="command_output", command="psql -h $PGHOST", stdout="...")` 后渲染并 grep 输出**不** 含 `os.environ["PGHOST"]` 值；验收：单测过

## 10. Demo Path 验收（端到端可复制粘贴）

- [x] 10.1 创建 `examples/m1-report/README.md`：把 proposal Demo Path 10 步固化为可复制粘贴的 shell 命令；验收：在干净 venv 上按 README 跑完 10 步全过
- [x] 10.1a 创建 `examples/m1-report/inspectors/sleep_timeout.yaml`：M1 完整 manifest，关键字段 = `name: demo.sleep_timeout` / `version: 1.0.0` / description / `tags: [demo, timeout-test]` / `targets: [local]` / `requires_binaries: [sleep]` / `parameters: {type: object, properties: {sleep_seconds: {type: integer, minimum: 1, maximum: 300}}, required: [sleep_seconds]}` (**integer 类型，无 pattern**，对齐 archived `inspector-plugin-system` spec：pattern 仅适用于 string 类型) / `collect.command: "sleep {{ sleep_seconds }}"` (**integer parameter 不走 `| sh` filter**，对齐 archived spec：sh filter 仅对 string parameter 强制) / `collect.timeout_seconds: 60` / `parse.format: raw` / `output_schema: {type: object}` / `findings: []`；用于 Demo Path 第 8 步；验收：`HOSTLENS_INSPECTORS_SEARCH_PATHS=./examples/m1-report/inspectors hostlens inspect local-host --inspector demo.sleep_timeout --parameters '{"sleep_seconds": 30}' --timeout 1` 必须 exit 2 + Report 含 `status: timeout`；**额外验收**：`hostlens doctor --json | jq '.inspectors.errors'` 必须为 `[]`（manifest 加载不报错）
- [x] 10.2 跑 demo 第 4 步：`hostlens inspect local-host --inspector hello.echo` 必须 stdout 输出 md Report 且 exit 0；验收：手测过
- [x] 10.3 跑 demo 第 5 步：`hostlens inspect local-host --inspector hello.echo --format json --output /tmp/hostlens-demo.json`；`jq '.schema_version, .findings[0].severity' /tmp/hostlens-demo.json` 输出 `"1.0"\n"info"`；验收：手测过
- [x] 10.4 跑 demo 第 6 步与 第 7 步（失败路径）：`hostlens inspect local-host --inspector nonexistent.foo; echo "exit=$?"` 必须 exit=3 + stderr 含 `inspector not found:`；同样验证 target 未找到；验收：两个 exit code 表现一致
- [x] 10.5 跑 demo 第 9 步（root 允许）：`sudo hostlens inspect local-host --inspector hello.echo` 必须 exit 0；验收：手测过

## 11. 对抗性 review 与 PR

- [x] 11.1 commit 实现 + spec delta 到 feature branch（**不要** push 到 main）：`git add src/hostlens/reporting src/hostlens/cli/inspect.py src/hostlens/inspectors/result.py src/hostlens/tools/schemas/run_inspector.py tests/ docs/operations/inspect.md examples/m1-report/ openspec/changes/add-report-data-model/`；conventional commit message：`feat(M1.6+M1.7): Report data model + render_markdown/json + hostlens inspect CLI`；验收：`git log --oneline -1` 显示新 commit
- [x] 11.2 跑对抗性 review（CLAUDE.md §5.3）：`/review-loop-codex`（默认）或 `/review-loop`；验收：结论 APPROVE / CLEAR
- [x] 11.3 push feature branch + 开 PR：`git push -u origin feat/add-report-data-model && \gh pr create --base main --title "feat(M1.6+M1.7): add-report-data-model — Report 数据模型 + 渲染器 + hostlens inspect CLI" --body "..."`；PR body 含 spec 引用（`openspec/changes/add-report-data-model/`）+ Demo Path 引用 + 退出码语义 4 行；验收：PR 链接返回 → PR #18 https://github.com/HerbertGao/hostlens/pull/18
- [x] 11.4 等 CI 全绿（mypy + ruff + pytest 全过）；验收：`\gh pr checks <num>` 显示全绿
- [x] 11.5 squash merge：`\gh pr merge <num> --squash --delete-branch`；验收：`git log --oneline` main 上含新 squash commit；feature branch 已删除

## 12. OpenSpec archive

- [ ] 12.1 跑 `/opsx:archive` 归档变更到 `openspec/changes/archive/2026-xx-xx-add-report-data-model/`；同时 specs delta 合到 `openspec/specs/{report-data-model,inspect-cli-command,inspector-plugin-system}/spec.md`；验收：`ls openspec/specs/` 含 10 个 capability spec 目录（含新增 2 个）
- [ ] 12.2 更新 `TODO.md`：M1.6 + M1.7 任务标记 ☑ 完成；M1 进度总览状态切到 ✅ 完成（含新条目"完整 demo path：`hostlens inspect local-host --inspector hello.echo` 端到端可跑通"）；验收：`grep "M1.6\|M1.7\|1.6 报告\|1.7 CLI" TODO.md` 显示完成态
