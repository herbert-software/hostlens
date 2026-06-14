## 新增需求

### 需求:FindingRule message 必须是简短中文标签 + 注入关键数据，禁空指针 / 禁纯英文长句

每个 FindingRule 的 `message` **必须**:

- **(a)** 用**简短中文标签**描述问题类别（如「systemd 失败服务」「磁盘使用率超阈值」「内存不足」）。
- **(b)** 对**有可变数据**的发现,经 `str.format` 用 `{field}` **注入 collector 输出的关键数据**(哪个单元 / 什么值 / 什么阈值);被注入的 `{field}` **必须**是 `output_schema` 保证存在的字段(`required` 或有容错默认),避免 `KeyError`。
- **(c)** 被 `{field}` 注入的字段**必须渲染成干净人读串**——`str.format` 对数组 / 对象类输出会吐 **Python repr**(如 array-of-objects `[{'unit': 'foo.service'}]`),严禁直接注入。**collector 应额外 emit 一个串或已 join 的字段**(如把 `failed: [{unit:...}]` 旁配一个 `failed_names: "foo.service, bar.service"`),message 注入那个干净串字段、而非 raw object 数组。
- **(d)** **禁止** `see X for details` 这类**不含实际数据的空指针**——发现必须自带具体指向,不必跳别处查。
- **(e)** **禁止**纯英文长句叙述。

目的:报告里每条发现**自带具体指向 + 中文**。叙述性的根因分析归 Diagnostician（中文,见 diagnostician-agent),finding message 只做「简短中文标签 + 数据」。

**契约记录(finding id churn)**:`message` 是 `compute_finding_id(inspector_name, inspector_version, message)` 的输入(severity 被刻意排除以支持 `changed_severity`)。因此**改写 message 会改变同一问题的 finding id**——批量重写 message 的那次升级,**首跑 regression diff 会把旧 id 一次性报成 `resolved`、新 id 报成 `added`**(同一真实问题 id 被重置,**非真实状态变化**)。这是 message 改写的**已知一次性副作用**,认可且记录在案;作者改写 message 时须知晓,运维侧首跑 diff 的这次 resolved+added 噪声应被解读为 id 重置而非问题消失 / 新增。

#### 场景:message 注入干净串而非空指针 / repr
- **当** 一个列出 failed 单元的 inspector(输出 `failed: [{unit:...}]` 数组,并旁配 collector emit 的已 join 串字段 `failed_names`),其 FindingRule 在 `len(failed) > 0` 时触发
- **那么** 其 `message` **必须**形如 `"systemd 失败服务：{failed_names}"`(注入 `"foo.service, bar.service"` 类干净人读串),**禁止**注入 `{failed}` raw 数组(`str.format` 会吐 `[{'unit': 'foo.service'}]` repr),**也禁止**形如 `"One or more systemd units are in the failed state (see failed for details)"`

#### 场景:契约由 crosscheck 机审防漂移(静态检查,有边界)
- **当** 遍历所有内置 inspector 的 FindingRule `message`
- **那么** **必须**有测试断言:无 `see .* for details` 类空指针 pattern、含中文字符、且对有非平凡 `output_schema` 字段的 inspector 其 message 含 `{...}` 注入
- **且** crosscheck 是**静态**检查(**不**实例化 collector、**不**跑命令),其能力**仅限**上述「pattern + 中文 + 有数据者含 `{...}`」三项;它**不能**验证被注入的 `{field}` 在运行时是否真实存在、也**不能**验证注入值渲染是否干净(无 repr 泄漏)——那两项靠 collector 单测 + 真机 demo 兜,本契约不得宣称 crosscheck 覆盖运行时正确性

#### 场景:message 改写导致 finding id 一次性重置
- **当** 批量改写内置 inspector 的 FindingRule `message`(中文化 + 注入数据)并升级
- **那么** 升级后**首跑** regression diff **必须**被理解为:同一真实问题的 finding id 因 `message` 变更而重置——旧 id `resolved` + 新 id `added` 各一次,**非真实状态变化**;此一次性 churn 是 message 改写的已知且认可的副作用
