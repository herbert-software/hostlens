## 新增需求

### 需求:wave-2b 必须覆盖归档时冻结的累积/时间窗口服务单元格

wave-2b cohort **必须**覆盖「**须持续运行的 workload / 时间窗口累积 / 非确定性时序**」这一采集风险类的 service inspector——其 semantic-abnormal 异常态**不能**经有界、确定性 setup 后即时采到,而**必须**依赖采样窗口内**持续运行**的 workload(如长查询采样时仍在跑)或时间窗口内**累积的真实事件/流量**(如慢查询日志、5xx 流量)才能命中。本 cohort 的具体 inspector(以本变更**归档时冻结**的 `proposal.md` / `tasks.md` 清单为准)**必须**全部以其声明 `name` 干净注册且 registry `errors == []`。

本需求是对 `service-inspector-suite` 的 `新增需求`(ADDED)sibling,**引用**套件已冻结的公共质量门(守 `service-inspector-contract` / 守作者契约且输出键区分 / 附 ReplayTarget fixture 与可证检出 snapshot / 禁引入新基础设施),**不**重述其细则;**禁止** `MODIFY` 已归档的 wave-2a 冻结覆盖需求、**禁止**改写或扩写 wave-2a 清单。

切片判据(reviewer 判定门,与 wave-2a 覆盖需求互补、非机械门):某 service inspector 的 semantic-abnormal 录制**若**依赖采样窗口内**持续运行**的 workload、**时间窗口累积**或**非确定性时序**,则属 wave-2b(**禁止**回流 wave-2a);仅「确定性即时快照(含采样时刻持有的固定资源)可录」者属 wave-2a。**关键约束(确定性录制)**:wave-2b inspector 的窗口/持续态聚合**必须**在**采样时刻**于**目标机内**算成**最终标量**并冻结进 collector 输出,`ReplayTarget` 回放时原样返回该已冻结标量;**禁止**回吐需在回放时按 `now()` 重聚合的原始带时间戳明细(否则 `now` 漂移使回放非确定,违反「离线回放确定性出结果」公共需求)。

#### 场景:wave-2b 冻结清单全部干净注册

- **当** wave-2b 实现完成、运行 `build_registry_from_search_paths([], settings=Settings())`
- **那么** 本变更 proposal/tasks 列出的每个 wave-2b inspector(以**归档时冻结**清单为准;后续 wave 另立 change 不回溯改本 spec)**必须**以其声明 `name` 出现在 registry 中,且 registry `errors == []`

#### 场景:窗口/持续态聚合在采样时坍缩成标量、回放确定

- **当** 某 wave-2b inspector 采集依赖时间窗口/持续 workload 的异常态
- **那么** 其窗口聚合(计数 / 派生率 / 最长时长)**必须**在采样时刻于目标机内算成最终标量、冻结进输出 JSON,`ReplayTarget` 回放**必须**原样返回该冻结标量并产出与快照一致的确定性结果;**禁止** collector 回吐需在回放时按 `now()` 重聚合的原始带时间戳明细

#### 场景:semantic-abnormal 须真造持续/累积异常而非低阈值凑

- **当** 评估某 wave-2b inspector(其 `findings` 非空)的 semantic-abnormal fixture
- **那么** 该 fixture **必须**对**真实**的持续/累积异常态录制(真实持续长查询 / 真实累积慢查询 / 真实 5xx 流量),且 snapshot 断言其在 manifest **默认阈值**下产出预期 severity + message;**禁止**以「健康态 + 人为低阈值」的 finding-trigger fixture 冒充(沿用 `service-inspector-contract` 双轨 fixture 硬条款)
