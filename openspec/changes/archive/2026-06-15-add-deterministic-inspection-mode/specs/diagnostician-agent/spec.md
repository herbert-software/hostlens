## 新增需求

### 需求:诊断师装配必须支持 narrate-only 变体（仅 correlate_findings、禁再巡检 / 选 target）

除既有「全装配」（`register_diagnostician_tools` 装出 `correlate_findings` + `request_more_inspection` + `list_inspectors` 三件,见 §需求:`request_more_inspection` 必须复用 `InspectorRunner` 执行、暴露 status、target 固定、不暴露 target 发现 的「诊断师注册表不含 list_targets」场景）外,**必须**额外提供一条 **narrate-only 装配路径**（新函数,或现有装配函数的新参数),供确定性巡检模式（见 `deterministic-inspection-mode` 能力）的「LLM 只对已采集结果写根因叙述」场景使用。该路径**必须**:

- **只注册 `correlate_findings`**（复用既有 `_build_correlate_findings_spec` 工厂,不另造结构化输出通道）。
- **禁止注册 `request_more_inspection`**——结构上让 narrate-only 的 LLM 拿不到再跑 inspector 的能力。
- **禁止注册 `list_inspectors`**——narrate-only 不需要发现可补查的巡检项。
- **禁止注册 `list_targets`**（与全装配同铁律,§7 最小能力)。

理由:确定性巡检的覆盖在采集阶段已固定（逐 target 跑固定集）,诊断阶段**仅**做根因叙述;若装出 `request_more_inspection`,LLM 可在 narrate 阶段追加巡检 / 漫游,破坏「覆盖确定 + token 有界」的确定性契约。既有全装配（三件）需求**不变**——agent 模式诊断师仍需 `request_more_inspection` 在证据不足时补查。

#### 场景:narrate-only 装配的注册表只含 correlate_findings

- **当** 检视 narrate-only 装配路径装出的工具注册表
- **那么** **必须**仅含 `correlate_findings`,**禁止**含 `request_more_inspection`、`list_inspectors`、`list_targets`

#### 场景:全装配路径不受影响

- **当** 检视既有全装配 `register_diagnostician_tools` 装出的注册表
- **那么** **必须**仍含 `correlate_findings` / `request_more_inspection` / `list_inspectors`（既有行为不变,**禁止**含 `list_targets`）
