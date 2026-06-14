## 1. manifest schema 与加载

- [ ] 1.1 `ScheduleManifest` 加 `mode: Literal["agent","deterministic"] = "agent"`（默认 agent、向后兼容）。
- [ ] 1.2 loader 的 target 基数校验按 mode:`agent` 恰好 1、`deterministic` ≥1;成员未注册仍 fail-loud。
- [ ] 1.3 测试:无 mode → 默认 agent;agent 多 target fail-loud;deterministic 多 target 加载;单 target 两 mode 均加载。

## 2. 内置健康默认集

- [ ] 2.1 定义 `DEFAULT_HEALTH_INSPECTORS`（覆盖 cpu / 内存 / 磁盘 / inode / 负载 / systemd / 日志 / 网络域，取现有 registry inspector name）。
- [ ] 2.2 测试:`DEFAULT_HEALTH_INSPECTORS` 成员全部存在于 inspector registry（防 curated 集漂移）。

## 3. 确定性采集路径

- [ ] 3.1 `run_deterministic_inspection`:逐 `target × inspector 集` 经 `InspectorRunner` 跑（复用 `run_inspector` 的解析 + capability 门;不满足记 `skipped`）;信号量限流;单项失败隔离;**采集阶段不注入 LLMBackend**（守 §4.2 / ADR-008）。
- [ ] 3.2 inspector 集解析:`deterministic` 无 `inspectors:` → 默认集;有 → 权威集（不叠加）。
- [ ] 3.3 测试:固定集逐 target 跑不漫游（不跑集外 / targets 外）;capability 不满足记 skipped 不计 severity;并发限流;单项失败隔离不崩批。

## 4. narrate-only + 多 target 报告

- [ ] 4.1 采集结果 → `from_inspector_results` 组装**一份**多 target `Report`（findings 跨 target 各带 target 上下文;`report_target_name` 为 fleet 标签）。
- [ ] 4.2 narrate-only Diagnostician:**只注册 `correlate_findings`、禁注册 `request_more_inspection`**;`LLMBackend` 注入 `AgentLoop`（非 ToolContext）。
- [ ] 4.3 测试:narrate-only 无任何能再跑 inspector / 选 target 的工具;多 target 聚合 severity;VCR cassette 回放 narrate LLM。

## 5. runner 路由 + RunStatus 映射

- [ ] 5.1 job body 按 `mode` 路由:`agent` → `run_diagnosis_pipeline`（零改动）;`deterministic` → `run_deterministic_inspection`。
- [ ] 5.2 共享 RunStatus 映射;`deterministic` 全无结果 → `Run(status=failed, error="deterministic inspection produced no inspector results")`,不产 `failed_api_unavailable`。
- [ ] 5.3 测试:agent 行为不变;deterministic 多 target Report 落 `Run(ok/partial)`;全无结果落 `failed`。

## 6. notify、文档、收尾

- [ ] 6.1 多 target 报告经既有 routing / notify 派发（`aggregate_severity` 全队聚合 + `only_if`）+ 测试。
- [ ] 6.2 docs schedule manifest:`mode` / 多 target / 默认健康集说明 + Demo Path（tizi 6 台 deterministic fleet）。
- [ ] 6.3 ts.mac-mini 收尾:`daily-health-fleet.yaml` 改 `mode: deterministic` + `targets: [全 6 台]`;`schedule trigger` 验证逐台确定性覆盖;再 `launchctl load` daemon 上线（**这是用户「先不上、做确定性模式」后的真正上线点**）。
- [ ] 6.4 `openspec-cn validate --strict` + temp 副本实测 archive（含 schedule-manifest / scheduler-engine 的 RENAME + MODIFY rebuild 校验，[[project_openspec_modified_rename_archive]]）+ feature branch + PR + CI 绿 + 对抗性 review;merge 后归档。
