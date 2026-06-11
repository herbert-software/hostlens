## 1. 模型定义

- [x] 1.1 新建 `src/hostlens/remediation/models.py`，定义 `RemediationStep`（字段 `description: str` / `precheck_cmd: _NonEmptyCmd | None` / `forward_cmd: _NonEmptyCmd` / `rollback_cmd: _NonEmptyCmd | None` / `verify_cmd: _NonEmptyCmd` / `risk_level: RiskLevel`，**全部必填无默认值**），`model_config = ConfigDict(extra="forbid", frozen=True)`
- [x] 1.2 `_NonEmptyCmd = Annotated[str, StringConstraints(min_length=1)]`；共享 `_is_blank_equivalent`（剔除 `Cf`/`Zs`/`Cc` 后判空）；`_reject_blank_command` 字段校验器（`forward_cmd`/`verify_cmd`/`precheck_cmd`/`rollback_cmd`）经 `_is_blank_equivalent` 拒纯空白/纯不可见（raise `command_must_not_be_blank`）且**不修改值**
- [x] 1.3 在 models docstring 标注命令字段是 untrusted 命令串、消费方（P2 audit/P3 卡片）渲染前自行脱敏；并声明「P2 必须经 `load_json` 加载」
- [x] 1.4 定义 `RemediationPlan`（`finding_id: _NonEmptyCmd` / `target_name: _NonEmptyCmd` / `rationale: str` / `steps: list[RemediationStep] = Field(min_length=1)` / `estimated_duration_seconds: StrictInt = Field(ge=0)`），`extra="forbid"` + `frozen=True`；`_reject_blank_binding` 校验器剔除 `Cf`/`Zs`/`Cc` 不可见字符后 `strip()==""` 即拒（`binding_field_must_not_be_blank`）

## 2. 校验不变量与解析入口

- [x] 2.1 `RemediationStep._validate_risk_invariants`（`@model_validator(mode="after")`）：`risk_level=="high"` ⟹ `precheck_cmd is not None`，违反 raise，消息含 token `high_requires_precheck`
- [x] 2.2 同一 validator：`rollback_cmd is None` ⟹ `risk_level=="high"`，违反 raise，消息含 token `rollback_none_requires_high`
- [x] 2.3 `RemediationPlan.load_json(data)` 类方法：`json.loads(..., object_pairs_hook=_reject_duplicate_keys)` 拒重复键（raise `duplicate_json_key: ...`）后 `model_validate`；`model_validate_json` 不拒重复键，故 P2 必须用 `load_json`

## 3. 单元测试（`tests/remediation/test_models.py` + `__init__.py`）

- [x] 3.1 新建 `tests/remediation/__init__.py`（必须：兄弟测试目录均有，缺它 CI console pytest 崩）+ `test_models.py`，`_step()`/`_plan()` helper
- [x] 3.2 命令字段：空串/纯空白/`\t`/纯不可见（`​`/`﻿`）拒（参数化 forward/verify + precheck/rollback，非空例 pin token `command_must_not_be_blank`）；`"  echo hi  "` 保留首尾空白
- [x] 3.3 precheck/rollback `None` 合法（不变量满足时）；description/rationale 空串合法、`None`/`123` 拒（`string_type`）
- [x] 3.4 不变量：`high` 缺 precheck 拒（token 断言 `"high_requires_precheck" in str(exc)`）；`rollback=None` 非 high（low+medium）拒（token）；`errors()[0]["type"]=="value_error"`；字段级错误并存时短路（只报 `string_too_short`、token 不出现）
- [x] 3.5 缺任何字段拒（`type=="missing"`）；extra 字段拒；非法 risk_level 拒；frozen 赋值拒
- [x] 3.6 RemediationPlan：合法 plan step 顺序；绑定字段空/纯空白/`\t`/`​`/`﻿` 拒、`" disk-full "` 保留；空 steps 拒；`StrictInt` 拒 `5.0`/`"5"`/`True`/`False`/`-1`、接受 `0`/`5`
- [x] 3.7 嵌套 dict 强制路径：`model_validate({...steps:[dict]})` 合法成功；dict 含 extra 拒；dict `high` 缺 precheck 被 `high_requires_precheck` 拒
- [x] 3.8 JSON 往返：含 `rollback_cmd=None` high step，`model_dump_json` 产 `"rollback_cmd":null`、`model_validate_json` 重建 `== 原对象`（`__eq__`）
- [x] 3.9 `load_json`：含重复键 payload raise（`match="duplicate_json_key"`）；干净 payload 返回 `== 原对象`

## 4. 收尾验收

- [x] 4.1 `mypy --strict src/hostlens/remediation/models.py` 0 错误
- [x] 4.2 `pytest tests/remediation/test_models.py -v` 全绿（72 passed）
- [x] 4.3 跑 proposal.md「Demo Path」`python -c` 片段，确认合法 plan 打印 JSON、两个独立反例各打印 `正确拒绝: ValidationError`、重复键 `load_json` 打印拒绝
- [x] 4.4 `openspec-cn validate add-remediation-plan-schema --strict` 通过
