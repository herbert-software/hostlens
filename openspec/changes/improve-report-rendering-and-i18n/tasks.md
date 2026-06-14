## 1. 通知模板重做（telegram + lark，立竿见影）

- [ ] 1.1 新 Jinja filters 注册进 telegram + lark env:`sev_label`(critical→严重)、`conf_label`(high→高)、`coverage`(从 `meta.inspectors_used` 算 `ok/total · skipped`)、`fmt_time`、`dedup`(同 inspector_name+message)、`sort_sev`(critical<warning<info)。
- [ ] 1.2 `telegram/report.md.j2` 重做:抬头(非 intent)/ 覆盖行 / 根因分析置顶(+`↳` suggested_actions)/ 发现(去重+排序+来源)/ 健康态 / 多 target 分节。
- [ ] 1.3 `lark/report.card.j2` 同构重做(卡片形态)。
- [ ] 1.4 测试:两通道渲染快照覆盖 8 场景(抬头非 intent、覆盖行、根因置顶、去重、按 severity 排序、带来源、健康态、多 target 分节);MarkdownV2 转义不回归。

## 2. finding message 具体化 + 中文契约

- [ ] 2.1 crosscheck 测试(机审):遍历所有内置 inspector 的 FindingRule `message`,断言 (a) 无 `see .* for details` 类空指针、(b) 含中文、(c) 有非平凡 output 字段者含 `{...}` 注入。
- [ ] 2.2 systematic 改写 ~72 个 inspector 的 `message` 为「简短中文标签 + `{field}` 注入数据」:先 `linux/systemd_failed_units.yaml`(`see failed for details` → `systemd 失败服务：{failed}`)做样板,再按域(计算/内存/磁盘/网络/服务…)分批(可多 PR)。
- [ ] 2.3 既有 service-inspector / fixture crosscheck 硬编码结构若含 message 断言,同步更新([[project_service_inspector_crosscheck_frozen_structures]])。

## 3. 中文根因叙述

- [ ] 3.1 Diagnostician 系统提示加「`description` / `suggested_actions` 必须简体中文」约束(写进系统提示**常量**,保 byte-stable + prompt cache 命中)。
- [ ] 3.2 测试:cassette 回放确认产出的 `description` / `suggested_actions` 为中文;`confidence` 仍枚举。

## 4. 文档与收尾

- [ ] 4.1 docs:inspector-authoring `message` 规约(中文标签 + 注入数据 + 禁空指针)+ 报告渲染示例(本提案 prototype 的渲染)。
- [ ] 4.2 升级说明:message 改写改变 `compute_finding_id`(含 message)→ 升级后**首跑 regression diff 有一次性 `resolved` + `added`**(同一问题 id 重置),非真实变化,文档点明。
- [ ] 4.3 ts.mac-mini:模板 + message + 中文叙述生效后,重跑 `schedule trigger` 看真实新报告(替换本提案的 prototype)。
- [ ] 4.4 `openspec-cn validate --strict` + temp 副本实测 archive + feature branch `feat/improve-report-rendering-and-i18n` + PR + CI 绿 + 对抗性 review;merge 后归档。
