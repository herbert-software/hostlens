## 1. Inspector manifest（builtin/net/）

- [x] 1.1 新增 `builtin/net/tls_chain_validity.yaml`（`name: net.tls.chain_validity`，三段式对齐 sibling `net.tls.cert_expiry`，design.md D-命名）：
  - `parameters` **用 `type: object` 包裹风格**（`required: [endpoint]` / `properties.endpoint` 带 `pattern: "^[A-Za-z0-9._-]+:[0-9]{1,5}$"` / `additionalProperties: false`）——**禁止扁平写法**（扁平令 pattern 静默失效，design.md D1）。
  - `requires_capabilities: [shell]` / `requires_binaries: [openssl]`（无 `privilege`，默认 none）。
  - `collect.command`：`host` 从 endpoint 切出；`case "$host" in *[A-Za-z]*) sni="-servername $host";; *) sni="";; esac`（hostname 发 SNI、纯 IPv4 跳过，design.md D7）；`echo | openssl s_client -connect {{ endpoint | sh }} $sni 2>&1`（**`| sh` 第二道防线**，design.md D2；`$host`/`$sni` 派生路径安全仅靠 pattern，`| sh` 不覆盖，design.md D2 边界/M6；**不传 `-verify` 深度**——是深度非开关且引入 code22 假阳，design.md D3；**不用 `-verify_return_error`**——会把链断推向 exception，design.md D3）；collector 只透传整段 openssl 文本（**不在 shell 拼 JSON、无 grep 守门**——B3 守门下沉到 parser regex，design.md D4/D9）；`timeout_seconds: 10`。
  - `parse`：`format: raw` + **`columns: [verify_code, reason]`**（raw+regex 强制非空 columns 且 named-group 数 == len(columns)，漏写 → load 崩，design.md D4 B1）+ `raw_extract_regex: '-----BEGIN CERTIFICATE-----[\s\S]*?Verify return code:\s*(?P<verify_code>\d+)\s*\((?P<reason>[^)]*)\)'`（**要求先见证书 PEM 标记再读 verify 行**——无证书文本不匹配 → null → exception，B3 假阴防护下沉 parser、offline 可证，design.md D9；过 ReDoS 四层门 schema.py:389，长度 101<200）。
  - `output_schema`：`verify_code: string`（required；**非 integer**——raw 捕获恒为 str，integer 致每次 exception，design.md D4 B2）+ `reason: string`（optional）+ `additionalProperties: false`。
  - `findings`：aggregate 模式（单 endpoint、无 `for_each`）`when: "verify_code != '0'"`（**字符串比较**——`!= 0` 会让 `"0" != 0` 反转误报，design.md D4 B2）→ `severity: critical`（单档不分级，design.md D8）；message 用裸 `{verify_code}`/`{reason}`（aggregate 禁 `{var.attr}`，design.md D5；统一模板，code 区分靠 `{reason}` 自带，**不**另开 code 分支，design.md D6/M4）。
  - 验收：snapshot 含 ①链断 finding-trigger（含证书+`verify_code="20"` → critical）②有效链（含证书+`verify_code="0"` → ok）③无证书（`no peer certificate available`+`verify_code="0"`）→ exception ④空 stdout / 不可达 → exception（③④均非把连不上/无证书当链有效）。

## 2. Fixture 录制与 snapshot 测试（tests/inspectors/）

- [x] 2.1 用 fixture 录制器产出 replay fixture（`tests/inspectors/fixtures/os_net/`，追加到既有 net 域 recorder `_record_os_net.py` / 测试 `test_os_net.py`，**禁手改录制文件**；openssl stdout 样本内容由作者编写注入，`_CaptureTarget` 约定、**不打真实网络**）：
  - 有效链 stdout（**含 `-----BEGIN CERTIFICATE-----` 段** + `Verify return code: 0 (ok)`）→ ok
  - 链断 stdout（含证书 + `Verify return code: 20 (unable to get local issuer certificate)`）→ critical finding
  - **无证书 stdout**（`no peer certificate available` + `Verify return code: 0 (ok)`，**无 BEGIN CERTIFICATE PEM 标记**）→ `status=exception`（**B3 假阴防护，offline 真证**：fixture 录的文本经 `parse_raw` 的 `re.search` 走真实 regex，regex 因缺 PEM 标记不匹配 → null → output_schema 拒 → exception；这条**不靠真机**，因守门在 parser 不在 collector shell，design.md D9）
  - 空 stdout（端点不可达）→ `status=exception`
  - **OpenSSL 3.x 与 LibreSSL 两份**措辞样本（macOS local target 必撞 LibreSSL，design.md R1）回归 `raw_extract_regex`（含「有效链」与「链断」各覆盖两实现措辞）。
- [x] 2.2 命令串级断言（offline 锁不住 collector shell 执行，靠捕获的命令串锁）：
  - `endpoint` 经 `| sh` 引用（即便 pattern 漏改也有 shlex.quote 兜底）。
  - SNI case 分支**结构**命令串锁:捕获命令同时含 hostname 臂 `*[A-Za-z]*) sni="-servername $host"` 与 IPv4 臂 `sni=""`、`$sni` 串入 openssl（hostname 与 IPv4 命令串字节相同,发不发 SNI 由 case 运行时定，见下偏离）。
    > **偏离（见返回 JSON issues）**：design.md D7 选择 shell `case` 分支（而非 Jinja `{% if %}`）做 SNI 选择，故 hostname 与 IPv4 两端点渲染出的 collect 命令串**字节相同**——`-servername` 作为 `case` 分支静态模板文本，两者都含，IPv4 端点命令**无法**「不含 `-servername`」。`_CaptureTarget` 不跑 collector shell，case 分支运行时才决定发不发 SNI。改为命令串级锁 SNI case 分支**结构**（hostname 臂 `*[A-Za-z]*) sni="-servername $host"` 与 IPv4 臂 `sni=""` 均在、`$sni` 串入 openssl、endpoint 经 `| sh` 正确穿入），IPv4 实际跳 SNI 的端到端正确性由真机 Demo Path（§2.3 偏离登记 / §4.2）覆盖。
- [x] 2.3 snapshot 测试 + 同进程全量回归不破坏既有（`pytest tests/inspectors/ -k tls_chain -v`）。

  > **review 偏离登记（D-7 架构下「collector shell 逻辑只命令串级锁、不行为级执行」）**：`_CaptureTarget` 对整条 `collect.command` 返回作者编的 stdout、**不跑真 shell**，故以下 **collector-shell** 正确性点只由命令串捕获 + 真机 Demo Path 锁定、**不**由 offline snapshot 行为级验证，与 wave-1 / security-pkg cohort 同构：
  >
  > - **SNI case 分支执行**（`*[A-Za-z]*` 判 hostname vs IPv4）：fixture 录的是 collector 最终输出,case 分支不跑——命令串级锁（2.2）+ 真机 Demo Path 验证「IP 端点不发 SNI 仍验对默认链」。
  > - **真机 openssl 输出格式**：offline 录的是作者编的 OpenSSL/LibreSSL stdout 样本（2.1），锁住「给定这段文本，regex 抽对 verify_code/reason + DSL 判定对」；但**真机 openssl 实际打印的格式**（含 PEM 段位置、verify 行措辞）仍须 Demo Path 覆盖。
  >
  > **明确不在偏离之列（offline 真证，区别于上面）**：
  > - **B3 假阴防护**（无证书 / 非 TLS 但 openssl 打 code 0 → exception）守门在 **parser regex**（`-----BEGIN CERTIFICATE-----` 前置要求），`parse_raw` 的 `re.search` 在 offline **会执行** → 无证书 fixture 走真实 regex 落 exception，**offline 即锁死**，不靠真机（design.md D9）。这是把 B3 从「collector-shell 测不了」迁出的关键设计选择。
  > - **B1/B2**（columns 必填 / str 类型 / 字符串 DSL）由 load + parse + output_schema 校验在 offline 全程执行，offline 真证。
  >
  > manifest/test 注释措辞**不**夸大 offline fixture 的锁定范围；也**不**把 offline 真证的 B1/B2/B3 错记成「须真机」。

## 3. 套件契约与注册

- [x] 3.1 扩 loader / capability-gate 断言：`net.tls.chain_validity` 干净注册（`build_registry_from_search_paths([], settings=Settings())` 的 `errors == []`、以声明 name 出现）；若 suite 有冻结计数 / cohort 清单 meta-guard，同步 +1。
- [x] 3.2 确认零对外契约变更：schema/capability/parse 枚举不变；Agent 工具数组不变；不涉及 MCP/Notifier/Schedule/CLI。

## 4. 文档与收尾

- [x] 4.1 勾选 `TODO.md` §M6 覆盖矩阵 net 域 TLS chain validity 单元格。
- [x] 4.2 真机 Demo Path（带真实 openssl 的 host，覆盖 offline 锁不住的 collector shell）：
  ```bash
  hostlens inspect net.tls.chain_validity --param endpoint=example.com:443                # → ok (hostname 发 SNI)
  hostlens inspect net.tls.chain_validity --param endpoint=untrusted-root.badssl.com:443  # → critical (链不受信)
  hostlens inspect net.tls.chain_validity --param endpoint=1.1.1.1:443                     # → ok (IP 端点不发 SNI, 验默认链)
  ```
- [x] 4.3 全量验收：`pytest tests/inspectors/ -v` 全绿；`hostlens inspectors list | grep tls.chain_validity` 列出；冻结计数对齐。
