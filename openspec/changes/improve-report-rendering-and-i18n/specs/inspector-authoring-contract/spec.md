## 新增需求

### 需求:FindingRule message 必须是简短中文标签 + 注入关键数据，禁空指针 / 禁纯英文长句

每个 FindingRule 的 `message` **必须**:

- **(a)** 用**简短中文标签**描述问题类别（如「systemd 失败服务」「磁盘使用率超阈值」「内存不足」）。
- **(b)** 对**有可变数据**的发现,经 `str.format` 用 `{field}` **注入 collector 输出的关键数据**(哪个单元 / 什么值 / 什么阈值);被注入的 `{field}` **必须**是 `output_schema` 保证存在的字段(`required` 或有容错默认),避免 `KeyError`。
- **(c)** **禁止** `see X for details` 这类**不含实际数据的空指针**——发现必须自带具体指向,不必跳别处查。
- **(d)** **禁止**纯英文长句叙述。

目的:报告里每条发现**自带具体指向 + 中文**。叙述性的根因分析归 Diagnostician（中文,见 diagnostician-agent),finding message 只做「简短中文标签 + 数据」。

#### 场景:message 注入数据而非空指针
- **当** 一个列出 failed 单元的 inspector(输出 `failed: [...]`),其 FindingRule 在 `len(failed) > 0` 时触发
- **那么** 其 `message` **必须**形如 `"systemd 失败服务：{failed}"`(注入实际单元名),**禁止**形如 `"One or more systemd units are in the failed state (see failed for details)"`

#### 场景:契约由 crosscheck 机审防漂移
- **当** 遍历所有内置 inspector 的 FindingRule `message`
- **那么** **必须**有测试断言:无 `see .* for details` 类空指针 pattern、含中文字符、且对有非平凡 `output_schema` 字段的 inspector 其 message 含 `{...}` 注入
