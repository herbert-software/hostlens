## 为什么

net 域已有 `net.tls.cert_expiry`（证书**过期时间**检查），但 §M6 矩阵的 TLS 域还列了 **chain validity**（证书**链有效性**）——过期只是 TLS 故障的一种，更常见的生产事故是「叶子证书没过期，但中间 CA 证书缺失 / 链顺序错 / 自签未受信」导致客户端握手失败。`tls.cert_expiry` 看 `notAfter`，看不出链断。本提案补 `net.tls.chain_validity`：用 `openssl s_client` 验证完整证书链能否被系统信任库验证通过。

这是 net 域的**增量补强**（非新空白域）——net 域已 6 个 inspector、不受「每域 ≥3」退出条件约束，本提案只补这一个矩阵单元格。

## 变更内容

- 新增 **1 个 net 域 builtin inspector** `net.tls.chain_validity`（文件 `builtin/net/tls_chain_validity.yaml`），与既有 `net.tls.cert_expiry` 同域并列、**职责互补（过期 vs 链有效性，可重叠）**——过期证书 `openssl verify` 返回 code 10，会被两者同时报告，是交叉印证而非冲突（见 design.md D6）。
- 参数 `endpoint`（`host:port`，JSON-Schema `properties` 下 `pattern` 收紧）。collector 用 `openssl s_client -connect <endpoint>`（**默认即验证，不传 `-verify` 深度**——`-verify N` 是深度上限非开关，深度值还会把合法长链误判 code 22；见 design.md D3）解析 `Verify return code`：`"0"`=链有效，非 `"0"`=链有问题（附 verify 错误码与人读原因，如 `20 unable to get local issuer certificate` 缺中间 CA / `19 self-signed certificate in certificate chain` 自签链 / `18 self-signed certificate` 自签叶 / `10 certificate has expired` 过期）。
- **B3 假阴防护**：openssl 对「无 peer cert / 纯 TCP 非 TLS / 半握手」也会打 `Verify return code: 0`（verify 0 = 没验出错，非「验过链」）；故 `raw_extract_regex` **要求先匹配证书 PEM 标记（`-----BEGIN CERTIFICATE-----`）再读 verify 行**——无证书文本不匹配 → `{verify_code: null}` → output_schema 拒 → exception，不当链有效。守门在 **parser regex**（offline `re.search` 会执行），故 B3 **offline 可证**（见 design.md D9）。
- 纯 YAML（`raw` parse + `raw_extract_regex` 抽 `Verify return code: N (reason)`，**带 `columns: [verify_code, reason]`**——`raw` 格式带 regex 必填 `columns` 且 named-group 数须等于 `len(columns)`；**captured 字段恒为 str**，故 `output_schema` 与 finding DSL 全按字符串处理；**不在 shell 里 `printf` 拼 JSON**，见 design.md D4）；fixture 录**手写/录制的 openssl stdout 样本（不打真实网络）**：①有效链 stdout（`verify_code="0"` 且含证书）→ ok；②链断 stdout（缺中间 CA / 自签）→ finding；③无证书 / 空 stdout → `status=exception`。**fixture 必录 OpenSSL 3.x + LibreSSL 两份**（macOS local target 必撞 LibreSSL）。

### 完整 manifest 示例（`net.tls.chain_validity`）

> 注：string 参数走**两道防线**——`pattern` 挂进 `properties`（收值域）+ collector `{{ x | sh }}`（shlex.quote 兜底）。扁平 `parameters` 写法会让 pattern 静默失效（见 design.md D1/D2）。

```yaml
name: net.tls.chain_validity
version: 1.0.0
description: >
  Verify a TLS endpoint's full certificate chain validates against the
  system trust store (not just expiry). Surfaces missing intermediate CA,
  wrong chain order, self-signed, or untrusted-root failures that
  net.tls.cert_expiry (notAfter-only) cannot see.
tags: [net, tls, security]
targets: [local, ssh]
requires_capabilities: [shell]
requires_binaries: [openssl]

parameters:
  type: object
  required: [endpoint]
  properties:
    endpoint:
      type: string
      pattern: "^[A-Za-z0-9._-]+:[0-9]{1,5}$"   # host:port, blocks shell injection
  additionalProperties: false

collect:
  # s_client 默认验证并打印 server cert PEM + "Verify return code: N (reason)" (不传 -verify 深度)。
  # SNI 从 endpoint 的 host 切出; 仅 hostname 发 SNI, 纯 IPv4 跳过 (RFC 6066 禁 IP-SNI)。
  # 安全: endpoint 经 pattern 收值域 + `| sh`; $host/$sni 是 shell 内派生, 其安全
  #   **仅靠 endpoint pattern** (`| sh` 不覆盖派生变量, 故 pattern 是这条路径的唯一防线)。
  # collector 只透传 openssl 整段文本, B3 守门下沉到 parser regex (见下)。
  command: |
    host=$(printf '%s' {{ endpoint | sh }} | cut -d: -f1)
    case "$host" in
      *[A-Za-z]*) sni="-servername $host" ;;   # hostname → 发 SNI
      *)          sni="" ;;                     # 纯 IPv4 → 不发
    esac
    echo | openssl s_client -connect {{ endpoint | sh }} $sni 2>&1
  timeout_seconds: 10

parse:
  format: raw
  # raw + raw_extract_regex 必带 columns (schema.py:354), named group 数 == len(columns)
  #   (schema.py:422)。raw 捕获恒为 str (parsers/raw.py:45), 故 output_schema/DSL 按 str 处理。
  columns: [verify_code, reason]
  # B3 假阴防护 (核心): openssl 对「无 peer cert / 非 TLS 端口 / 半握手」也打 "Verify return code: 0"
  #   (verify 0 = 没验出错, 非「验过链」)。故 regex **要求先见证书 PEM 标记再读 verify 行** ——
  #   无证书文本不匹配 → parse_raw 返 {verify_code: null} (raw.py:44) → output_schema 拒 null
  #   → status=exception。守门在 parser (offline re.search 会跑), 故 B3 offline 可证。
  raw_extract_regex: '-----BEGIN CERTIFICATE-----[\s\S]*?Verify return code:\s*(?P<verify_code>\d+)\s*\((?P<reason>[^)]*)\)'

output_schema:
  type: object
  properties:
    verify_code: { type: string }   # raw 捕获恒为字符串 (非 integer); 非 match 时为 null
    reason: { type: string }
  required: [verify_code]            # null 不满足 required+string → 无证书路径落 exception
  additionalProperties: false

findings:
  # aggregate 模式: message 只能用裸 {verify_code}/{reason} (schema 禁 {var.attr})。
  # output_schema 先于 findings 校验, 故 findings 只在 verify_code 为合法 str 时求值;
  #   字符串比较 "0" = 验证通过 → 不触发 (B2: 整数比会让 "0" != 0 == True 误报 critical)。
  - when: "verify_code != '0'"
    severity: critical
    message: "TLS 证书链验证失败 (code {verify_code}: {reason})"
```

## 功能 (Capabilities)

### 新增功能
- 无新 capability。`net.tls.chain_validity` 属 net 域 OS-shell 快照，归既有 `os-shell-inspector-suite` 套件契约管辖。

### 修改功能
- `os-shell-inspector-suite`: **追加** 一条小需求 —— net 域**增量**补 `net.tls.chain_validity`，遵守套件既有质量门（纯 YAML + ReplayTarget fixture 含 finding-trigger 与 unreachable + snapshot + 矩阵勾选 + 零新基础设施）。**追加式冻结 cohort**，不 MODIFY 既有 wave 需求。此需求明确：net 域已达 ≥3，本增量不为达退出门槛、而为补矩阵列出的 chain validity 探针。

## 影响

- **新增代码**：`builtin/net/tls_chain_validity.yaml`（1 个）。
- **新增测试**：1 个 inspector 的 snapshot + fixture（`tests/inspectors/fixtures/os_net/`，追加到既有 net 域 recorder/测试）；扩 loader / capability-gate 断言。
- **文档**：勾选 `TODO.md` §M6 矩阵 TLS 域 chain validity 单元格。
- **对外契约影响**：schema 不变；registry 扩 1 个；Agent 工具数组不变；不涉及 MCP/Notifier/Schedule/CLI。
- **依赖**：不新增 Python 依赖。`openssl` 由 preflight 探测，缺失 → `requires_unmet`。

## 非目标（Non-Goals）

- **不替代 `net.tls.cert_expiry`** —— 二者并存、**职责互补可重叠**（过期 vs 链；过期证书两者都报，是交叉印证）；不合并、不改既有 inspector。
- **不做 OCSP/CRL 吊销检查、不做 cipher/protocol 合规扫描** —— 只验链能否被系统信任库验证通过；吊销/合规留后续。
- **不自带 CA bundle** —— 用目标系统信任库（`openssl` 默认），不内置证书集。
- **不改 schema/capability/parse-format**。

## Failure Modes

1. **端点不通 / 握手超时 / 无证书 / 非 TLS 端口** → openssl 输出**无证书 PEM 标记**（或为空）→ `raw_extract_regex`（要求 `-----BEGIN CERTIFICATE-----` 在前）非 match → `{verify_code: null}`（`raw.py:44`）→ `output_schema`（`required` + `type: string`）拒 null → `status=exception`（不把「连不上 / 无证书 / 非 TLS 但 openssl 打 code 0」当「链有效」）。**机理**:runner 不看命令 `exit_code`；`parse_raw` 非 match **不报错**、返 null，exception 实际来自 **output_schema 拒 null**，非「解析失败」。故 `output_schema` 的 `required: [verify_code]` + 非 null 类型是这条 fail-loud 的承重件,**禁止松动**（松了无证书路径变 silent ok = B3 回归）；fixture 必须断言此 exception（B3/M3）。
2. **无 openssl** → preflight `requires_binaries` → `requires_unmet` skip。
3. **`endpoint` 注入** → **两道防线**:`pattern` 挂进 `properties` 收值域 + collector `{{ endpoint | sh }}`（shlex.quote）。扁平 `parameters` 会让 pattern 静默失效,故必须 JSON-Schema 包裹风格（design.md D1/D2）。
4. **openssl 版本输出格式漂移**（`Verify return code` 行格式,尤其 macOS local 撞 LibreSSL）→ `raw_extract_regex` **正则捕获数字**（不靠字段位置）；fixture 必录 **OpenSSL 3.x + LibreSSL 两份**样本回归。
5. **fixture 漂移** → 录制文件由录制器产出（禁手改），openssl stdout 样本内容由作者编写注入（`_CaptureTarget` 约定，**不打真实网络**）；replay 锁不住 collector shell 真机正确性 → 靠 Demo Path 真机覆盖。

## Operational Limits

- **并发**：不引入新并发；`collect.timeout_seconds` ≤10s（含握手 RTT）。
- **内存**：collector 透传 s_client 整段输出（含证书链文本）给 parser，但**解析后只保留 verify_code + reason 两个字段**，不入库整个证书链文本。

## Security & Secrets

- **不引入新密钥**：只做出站 TLS 握手验证，无凭据。
- **攻击面**：`endpoint` 经 `pattern`（挂 `properties`）收值域 + collector `{{ endpoint | sh }}` 兜底,两道防线;SNI 的 `$host`/`$sni` 是 shell 内从 endpoint 派生，其安全**仅靠 endpoint pattern**——`| sh` 不覆盖派生变量,故 pattern 是这条派生路径的唯一防线(design.md D2/D7 已声明)；不开端口、纯出站只读握手。
- **脱敏**：verify code + reason 无 PII；reason 文本是 openssl 标准错误串，无敏感数据。

## Cost / Quota Impact

- **零 LLM token**：采集层。`list_inspectors` +1 项。

## Demo Path

```bash
# 1) replay 锁解析+DSL (录制/手写 stdout, 不打网络)
pytest tests/inspectors/ -k "tls_chain" -v
# 验证点: net.tls.chain_validity 加载 (errors==[]); 有效链 stdout(含证书+verify_code="0") → ok;
#   缺中间 CA stdout(verify_code="20" unable to get local issuer) → critical finding;
#   无证书 stdout("no peer certificate available"+verify_code="0") → status=exception (B3 假阴防护);
#   空 stdout → status=exception (非把连不上当链有效)
hostlens inspectors list | grep tls.chain_validity

# 2) 真机覆盖 collector shell + B3 (replay 锁不住的部分)
hostlens inspect net.tls.chain_validity --param endpoint=untrusted-root.badssl.com:443  # → critical (链不受信)
hostlens inspect net.tls.chain_validity --param endpoint=example.com:443                # → ok (hostname 发 SNI)
hostlens inspect net.tls.chain_validity --param endpoint=1.1.1.1:443                     # → ok (IP 不发 SNI, 验默认链)
hostlens inspect net.tls.chain_validity --param endpoint=<纯TCP非TLS端口, 如 SSH 22>     # → exception (B3: 无证书不当链有效)
```
