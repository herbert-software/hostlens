## 为什么

实测的每日巡检报告质量低，多重问题:

- **标题是整句 intent**:telegram / lark 模板把 `report.intent`（整段巡检意图）当标题。
- **finding 消息空指针 + 纯英文**:FindingRule `message` 是作者手写的**静态英文串、不注入数据**——`linux/systemd_failed_units.yaml:51` 写 `"One or more systemd units are in the failed state (see failed for details)"`,既不说**哪个**单元 failed，又是英文。~72 个 inspector 普遍如此。
- **同条 finding 重复**:模板逐条渲染、不去重。
- **没有根因**:LLM 的根因叙述要么没产出、要么是英文。

面向中文运维的无人值守日报,这些让报告**既看不懂指向、又读着费劲**。

## 变更内容

三块（互相独立、可分阶段实现）:

1. **通知模板重做（telegram + lark 同构）**:
   - **干净抬头**(severity 图标 + `Hostlens 巡检 · <target> · <中文 severity>`),**不**再用 intent 当标题;
   - **覆盖行**(`N/M 项检查 · K 项跳过` + 时间),一眼看全跑没;
   - **根因分析置顶**(中文叙述 + `↳` 可执行处置命令)——人最该看的放最前;
   - **发现**:**渲染时去重**(同 inspector + 同 message)+ **按 severity 排序** + 每条带**来源 inspector**;
   - **健康态**:无 findings 时 `✅ 未发现异常` + 覆盖行(不吵);
   - **多 target**(确定性模式):findings **按主机分节**聚合。

2. **finding 消息「具体指向 + 中文」契约**(inspector-authoring-contract 强化):FindingRule `message` **必须是简短中文标签 + 注入关键数据**(哪个单元 / 什么值 / 什么阈值),**禁止**写 `see X for details` 这类空指针、**禁止**纯英文长句。systematic 过一遍 ~72 个 inspector 的 message。

3. **中文根因叙述**(diagnostician-agent):hypothesis 的 `description` + `suggested_actions` **必须中文**。

**非目标**:

- **不**引入 en / zh 运行时切换(报告即 zh-CN;语言设置 / message catalog 留未来)。
- **不**改 notify 发送 / 签名 / 重试机制(只改渲染)。
- **不**改 `Finding` / `Report` 数据模型结构(去重 / 排序在渲染时做、不动模型)。
- **不**改哪些 inspector 跑(与确定性模式提案正交)。

## 功能 (Capabilities)

### 修改功能

- `notifier-telegram`: 新增「报告渲染结构」需求(抬头 / 覆盖 / 根因优先 / 去重排序 / 来源 / 健康态 / 多 target 分节)。既有 MarkdownV2 转义 + 发送需求不变。
- `notifier-lark`: 同构 card 结构需求。既有签名 + 发送需求不变。
- `inspector-authoring-contract`: 新增「FindingRule message 必须简短中文标签 + 注入关键数据、禁空指针 / 禁纯英文长句」需求。
- `diagnostician-agent`: 新增「根因叙述(description / suggested_actions)必须中文」需求。

## 影响

- **代码**:telegram / lark 模板 + 新 Jinja filters(sev_label / conf_label / coverage / dedup / sort_sev / fmt_time);~72 个 inspector YAML 的 `message` 重写(中文标签 + `.format` 注入数据);diagnostician 系统提示(中文叙述);渲染时 dedup / sort。
- **测试**:模板渲染快照(抬头 / 覆盖 / 根因优先 / 去重 / 排序 / 来源 / 健康态 / 多 target 分节);finding message 注入数据 + 中文 + 禁空指针的 inspector 契约 crosscheck;cassette 回放中文叙述。
- **文档**:inspector-authoring message 规约 + 报告渲染示例(本提案的 prototype 渲染)。
