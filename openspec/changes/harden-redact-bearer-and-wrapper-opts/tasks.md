## 1. #7 Bearer 引号感知

- [x] 1.1 `src/hostlens/core/redact.py` 把 `_BEARER_HEADER` 的 value `(\S+)` 换为与 `_SHELL_WORD` 逐字节同构的引号感知片段 `((?:[^\s"']+|"(?:\\.|[^"\\])*"?|'[^']*'?)+)`（保留 `(?i)\bBearer\s+` 前缀）。验收：`_BEARER_HEADER.pattern` 的 value 组与 `_SHELL_WORD.pattern` 内层一致
- [x] 1.2 把 `_BEARER_HEADER` 的 sub 从 `_mask` 改 `_mask_glued_value`（lambda `f"Bearer {_mask_glued_value(m.group(1))}"`）。验收：`Bearer "my token val"` → `Bearer ****`（不漏 `val`）；`Authorization: Bearer eyJabc...` 无空格 token 输出与改前**逐字节相同**
- [x] 1.3 在 `tests/core/test_redact.py` 补 Bearer 引号值用例：`Bearer "a b c"` 断言不漏尾 + 幂等；无空格 Bearer token 断言输出不变（回归锚）；加进 `test_idempotent` 参数化

## 2. #4 补全 wrapper 取值选项表

- [x] 2.1 `src/hostlens/core/redact.py` 给 `_WRAPPER_VALUE_OPTS` 补常用带值选项（仅**已知带值**项，如 `sudo` 补 `-C/-T`、`docker` 补已缺漏项、`ssh` 补常用、`env` 补 `-u` 等）。验收：补的每项都是真带值选项（man page 核对），跳过方向安全侧
- [x] 2.2 在 `tests/core/test_redact.py` 补 wrapper 选项穿透用例（如 `sudo -C 3 mysql -psecret` → 脱敏）；负例锁定 `echo mysql -psec` head 不误判、`mysql -p mydatabase` 不动

## 3. 文档 + 验收 + 归档

- [x] 3.1 `docs/OPERABILITY.md §7.2` 微调：Bearer 现覆盖引号含空格值（与 key=value 同）
- [x] 3.2 在 temp 副本干跑 `openspec-cn archive harden-redact-bearer-and-wrapper-opts`，确认 RENAME + MODIFY R1 rebuild 校验过（FROM/TO 标题与既有逐字匹配、中文标题、场景 4-井号）
- [x] 3.3 跑 Demo Path + `mypy --strict src/hostlens/core/redact.py` 干净 + 全量 `pytest -q` 绿（确认无空格 Bearer/既有 4 类零回归、无新 ReDoS）+ `openspec-cn validate harden-redact-bearer-and-wrapper-opts --strict` 通过
