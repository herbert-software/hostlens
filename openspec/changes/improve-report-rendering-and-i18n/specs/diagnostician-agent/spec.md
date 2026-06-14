## 新增需求

### 需求:诊断根因叙述（description / suggested_actions）必须用简体中文

`DiagnosticianAgent` 经 `correlate_findings` 产出的 `RootCauseHypothesis` 的自由文本字段——`description` 与每条 `suggested_actions`——**必须**用**简体中文**书写(面向中文运维)。Diagnostician 的系统提示**必须**显式约束输出语言为简体中文。`confidence`(`low` / `medium` / `high` 枚举)、`supporting_findings`(finding id 引用)等**结构字段不变**;byte-stable 系统提示 + prompt cache 命中约束不变(语言约束写进系统提示常量、不随报告内容变动)。

#### 场景:根因叙述中文
- **当** Diagnostician 对一组 findings 产出 hypothesis
- **那么** 该 hypothesis 的 `description` 与每条 `suggested_actions` **必须**是简体中文;`confidence` 仍为枚举值、`supporting_findings` 仍为 finding id 引用
