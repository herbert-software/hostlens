## 新增需求

### 需求:net 域必须增量补 TLS chain validity 探针

本套件**必须**在 net 域既有探针之上、按 `TODO.md` §M6 覆盖矩阵补一个 **TLS chain validity** 单元格:新增纯 shell inspector `net.tls.chain_validity`,用 `openssl s_client` 验证端点的完整证书链能否被系统信任库验证通过(`Verify return code`),覆盖既有 `net.tls.cert_expiry`(只看 `notAfter` 剩余天数)看不见的「缺中间 CA / 链顺序错 / 自签 / 不受信根」类握手失败。

**非退出门槛、纯矩阵补格**:net 域已达 §M6「每域 ≥3」退出条件,本需求**不**为达覆盖门槛,而为补矩阵明确列出的 chain validity 探针。与 `net.tls.cert_expiry` **职责互补、可重叠**(过期证书 `openssl verify` 返 code 10,两者会同时报告,是交叉印证非冲突),**禁止**合并或改动既有 inspector。

**追加式冻结 cohort**:本需求是 os-shell 套件的**追加**需求,**禁止** MODIFY wave-1 或 security/pkg cohort 的既有需求;各 cohort 自冻结、互不回溯(与套件既有 cohort 冻结纪律一致)。具体 inspector 的 input/output 行为契约遵守 `inspector-authoring-contract`,本需求只约束套件层的矩阵覆盖与质量门。

该 inspector **必须**仅含零外部服务依赖的 OS shell 探针(只做出站 TLS 握手 + 本机信任库验证),**禁止**依赖外部服务客户端或语言运行时。

#### 场景:inspector 干净注册并勾上矩阵

- **当** 套件实现完成、运行 `build_registry_from_search_paths([], settings=Settings())`
- **那么** `net.tls.chain_validity` **必须**以其声明 `name` 出现在 registry 中,且 registry `errors == []`
- **且** `TODO.md` §M6 覆盖矩阵 net 域 TLS chain validity 单元格**必须**被勾上

#### 场景:链不可信时检出 critical finding

- **当** collector 拿到的 openssl stdout 含证书(`BEGIN CERTIFICATE`)且 `Verify return code: N (reason)` 的 `N != "0"`(如 `20 unable to get local issuer certificate` 缺中间 CA、`19 self-signed certificate in certificate chain` 自签链)
- **那么** 该 inspector **必须**产出一条 `severity=critical` 的 finding(finding DSL 用**字符串**比较 `verify_code != '0'`——`raw` 解析捕获恒为 str,整数比会让 `"0" != 0` 反转成有效链误报),message **必须**带上 `verify_code` 与 `reason`(供 Agent 后续关联分级),`status=ok`(成功采集到「链不可信」这一事实)
- **且** 本变更 snapshot 测试**必须**含至少一份「链断」finding-trigger fixture 断言此检出

#### 场景:端点不通 / 无证书时 fail-loud 不把连不上当链有效

- **当** `openssl s_client -connect` 因端点不可达 / 握手超时而失败(stdout 无证书),**或**端点是「纯 TCP 非 TLS 端口 / 半握手 / 无 peer 证书」而 openssl **仍打印 `Verify return code: 0`**(verify 0 = 没验出错,非「验过链」)
- **那么** 该 inspector **必须**以 `status=exception` 呈现:`raw_extract_regex` **必须**要求证书 PEM 标记(`-----BEGIN CERTIFICATE-----`)在 `Verify return code` 行**之前**出现,无证书文本 → regex 非 match → `{verify_code: null}` → `output_schema`(`required` + `type: string`)拒 null → exception。**禁止**伪造 `status=ok` 或 `verify_code="0"`(把「连不上 / 无证书 / 非 TLS」误判为「链有效」的**关键假阴防护**);`output_schema` 的 `required` + 非 null 类型**禁止**松动(松了会让无证书路径变 silent ok)
- **且** 本变更 snapshot 测试**必须**含两份 fixture 各一:①「空 stdout / 不可达」②「`no peer certificate available` + `Verify return code: 0` 但**无 PEM 标记**」,均断言 `status=exception`;**因守门在 parser regex(offline `re.search` 会执行),此 B3 假阴防护 offline 即可证**,无需依赖真机(区别于 SNI case 分支那类 collector-shell 逻辑)

#### 场景:参数安全进 shell(两道防线)

- **当** 评估 `endpoint` 参数注入面
- **那么** `endpoint` **必须**同时受**两道防线**约束:① schema `pattern`(`^[A-Za-z0-9._-]+:[0-9]{1,5}$`)挂在 JSON-Schema `properties` 下(`parameters` 用 `type: object` 包裹风格,**禁止**扁平写法——扁平会令 `pattern` 静默失效)、② collector 内 `{{ endpoint | sh }}`(shlex.quote)
- **且** 本变更 snapshot 测试**必须**含一份命令串级断言:捕获的主命令对 `endpoint` 经 `| sh` 引用(确保即便 pattern 漏改也有 shlex.quote 兜底)

#### 场景:verify code 解析跨 openssl 实现稳定

- **当** 该 inspector 解析 `Verify return code` 行
- **那么** **必须**用 `parse.format: raw` + `raw_extract_regex` **正则捕获数字**(非字段位置锚),以跨 OpenSSL 3.x 与 LibreSSL(macOS local target 自带)稳定取值;collector **禁止**在 shell 内 `printf` 拼 JSON(reason 文本未转义会破 JSON → 合法链断 finding 被误判成 exception)
- **且** `parse` 块**必须**声明 `columns: [verify_code, reason]`(`raw` + `raw_extract_regex` 强制要求非空 `columns`,且 named-group 数须 == `len(columns)`;漏写 → manifest load 即崩、registry errors≠[]),`output_schema` 的 `verify_code`/`reason` **必须** `type: string`(`raw` 捕获恒为 str,写 `integer` → jsonschema 拒 → 每次运行都 exception)
- **且** 本变更 snapshot 测试**必须**含 OpenSSL 3.x 与 LibreSSL **两份** stdout 样本 fixture 回归解析

#### 场景:SNI 镜像真实客户端行为(命令串级锁 + 真机验证)

- **当** 该 inspector 对 `host:port` 端点构造 `openssl s_client` 命令
- **那么** collector **必须**从 endpoint 切出 `host`,**仅当 `host` 为 hostname(含字母)时**发 `-servername "$host"`、**纯 IPv4 字面量端点跳过 SNI**(镜像真实客户端:浏览器访问 IP 不发 SNI;且 RFC 6066 禁 IP-SNI,发了部分 TLS 栈会 abort 握手致假阳)
- **且** 本变更 snapshot 测试**必须**含命令串级断言锁住 **SNI case 分支结构**:捕获命令同时含 hostname 臂 `*[A-Za-z]*) sni="-servername $host"` 与非 hostname(IPv4)臂 `sni=""`,且 `$sni` 串入 `openssl s_client`。**注意**:SNI 选择用 shell `case`(design D7,非 Jinja),故 hostname 与 IPv4 端点渲染出的 collect 命令串**字节相同**(两臂都在模板里,发不发 SNI 由 `case` 在 shell **运行时**决定)——`_CaptureTarget` 不跑 collector shell,offline **无法**用「命令串含/不含 `-servername`」区分两者。**SNI case 分支的执行正确性**(IPv4 端点实际跳 SNI 仍验对默认链)因此**只命令串级锁分支结构**,端到端正确性须真机 Demo Path(公网有效链 + 缺中间 CA + 一个 IP 端点)验证——offline fixture **不**声称锁 collector shell 执行正确性(见本变更 tasks.md 偏离登记)
