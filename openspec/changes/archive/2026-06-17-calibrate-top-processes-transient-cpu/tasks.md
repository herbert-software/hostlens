## 1. Inspector manifest 改动

- [x] 1.1 改 `src/hostlens/inspectors/builtin/linux/cpu_top_processes.yaml` 的 `collect.command` 为 `ps -eo pid,pcpu,pmem,etimes,comm --sort=-pcpu --no-headers | head -n 10`,collect 注释写明 **procps-only**(busybox 不支持 `-eo`/`etimes`,fail-loud)。验收:`grep etimes` 命中 collect 行。
- [x] 1.2 `parse.columns` 改为 `[pid, cpu_pct, mem_pct, etimes, comm]`(列序与 `-eo` 一致);`output_schema.properties` 增 `etimes: { type: string }`。验收:`openspec-cn`/manifest load 不报错。
- [x] 1.3 新增 `parameters` 块:`min_etimes`(`type: number`,`exclusiveMinimum: 0`,`default: 10`),注释说明 SLO 取舍(10 非 60)。验收:`min_etimes` 出现在 manifest。
- [x] 1.4 两条 finding 的 `when` 前置 age 闸:`int(p.etimes) >= min_etimes and float(p.cpu_pct) >= 90.0`(critical)、`int(p.etimes) >= min_etimes and float(p.cpu_pct) >= 70.0 and float(p.cpu_pct) < 90.0`(warning);`message` 不变(下标语法、英文)。bump `version` → `1.1.0`。验收:两条 `when` 均含 `int(p.etimes) >= min_etimes`。
- [x] 1.5 (review 引入)**fail-loud 守卫**:`output_schema.rows` 加 `minItems: 1`——0 行(不支持平台 `ps` 报错 stdout 空、退出码被 `| head` 掩盖、runner 不 gate 主命令退出码)→ schema 校验失败 → `status=exception`,而非静默 `status=ok` 假绿。配套 §2.5 回归测试。验收:空 stdout → `status=exception`(实测 `output_schema_mismatch: [] should be non-empty`)。

## 2. Offline 测试(D-7 _CaptureTarget,双锚 + 命令串级锁)

- [x] 2.1 新建 `tests/inspectors/test_cpu_top_processes_collector.py`,镜像 `test_system_load_avg_collector.py` 结构(真 `InspectorRunner` + `_CaptureTarget` 应答 `command -v` 探针 + 手编 table stdout + 命令串字节级断言)。`_CaptureTarget` **内联定义**(load_avg 范式即内联在测试文件,无 sibling helper——勿写 `from inspectors._...`)。验收:文件存在、无对不存在 helper 的导入。
- [x] 2.2 锚 1(伪影不告警):喂一行 `cpu_pct>=90` 但 `etimes < min_etimes`(如 `33948 100 0.5 1 journalctl`)→ 断言 **零 finding**。
- [x] 2.3 锚 2(持续才告警):喂 `etimes >= min_etimes` 且 `cpu_pct>=90`(如 `4242 97.5 12.3 86400 mysqld`)→ 断言 **1 条 critical**;再喂 `[70,90)` → 断言 warning。
- [x] 2.4 命令串级锁:断言捕获主命令含 `ps -eo pid,pcpu,pmem,etimes,comm`(字段序固定)。验收:`pytest tests/inspectors/test_cpu_top_processes_collector.py -q` 全绿。
- [x] 2.5 (review 引入)fail-loud 回归锚:喂空 stdout → 断言 `status == "exception"`(minItems:1 兜底,非静默 ok)。

## 3. 下游 ripple(改 collect 列数的连带影响)

- [x] 3.1 **incident 场景重生成(必改,且只改源 + 重生成,禁止手改 generator-owned fixture)**:`test_cpu_saturation.py` 经 ReplayTarget 回放**已提交的** `src/hostlens/demo/scenarios/cpu_saturation/fixture.json`(按精确命令串匹配)、不读 `_scenarios.py`(后者只是 `_generate.py` 的源)。步骤:(a) 改 `tests/incidents/_scenarios.py:90` 的 `cpu_saturation` 场景 `main_stdout` 从 4 列补到 5 列(加 `etimes`),mysqld 给长跑值保 critical(如 `4242 97.5 12.3 86400 mysqld\n4310 64.2 3.1 3600 python3\n1180 12.0 1.0 7200 nginx\n`——只 mysqld 过 cpu 阈值,etimes 须 ≥`min_etimes` 否则 finding 消失致 message 变、cassette miss);(b) 重生成:`HOSTLENS_GENERATE_INCIDENTS=1 HOSTLENS_GENERATE_ONLY=cpu_saturation pytest tests/incidents/_generate.py -q`(无需 API key,RecordingBackend 包 scripted FakeBackend);(c) cassette 提交门:`python scripts/cassette_lint.py`(exit 0);(d) 提交重生成的 generator-owned 工件 `src/hostlens/demo/scenarios/cpu_saturation/{fixture.json,cassette.jsonl}` + `tests/incidents/snapshots/cpu_saturation.md`。验收:`pytest tests/incidents/test_cpu_saturation.py -q` 绿且 `ReplayTarget.misses == []`。(message 不变 → cassette request-key 不变,实测留 cassette 不动仍 hit,但走标准重生成命令即可。)
- [x] 3.2 跑 manifest schema / builtin 加载 / 容器 cohort 守卫 / manifest-load 消费方 / i18n backlog 守卫,确认新增 parameter/列不破既有断言:`pytest tests/inspectors/test_schema.py tests/inspectors/test_builtin_inspectors.py tests/inspectors/test_docker_target_cohort_guard.py tests/inspectors/test_health_default_set.py tests/inspectors/test_incident_pack_manifests.py tests/inspectors/test_finding_message_i18n_crosscheck.py -q`。
- [x] 3.3 **version bump 1.0.0→1.1.0 的 finding-id drift(必改)**:`tests/demo/snapshots/cpu_saturation.md:11` 是手维护的 derived oracle(**无 generator**),含 `Supporting findings: 6010bd422fab42a1`(1.0.0 id),须**手工** find-replace 为 `996c44992db27b32`(1.1.0 id;message `Process mysqld (pid 4242) is using 97.5% CPU` 不变,已用 `compute_finding_id` 算实)。验收:`pytest tests/demo/ -q` 绿。
- [x] 3.4 兜底全仓 ripple:grep 其余引用 `linux.cpu.top_processes` 的 fixture/快照(`tests/incidents/`、`tests/reporting/`、`tests/scheduler/`、`tests/orchestration/`),逐一确认——已知 `test_deterministic_pipeline.py` 的 `compute_finding_id(..., "1.0.0", "warm")` 处仅 `assert len(fid)==16`(version 解耦、**不破**;行号随文件漂移,grep `compute_finding_id` 定位),其余多为 name-only 引用。验收:`pytest tests/incidents/ tests/reporting/ tests/scheduler/ tests/orchestration/ -q` 绿。

## 4. spec validate + 全量回归 + 收尾

- [x] 4.1 `openspec-cn validate calibrate-top-processes-transient-cpu --strict` 通过;并在 temp 副本实测 `openspec-cn archive`(rebuild 校验,ADDED 需求中文标题)不中止。
- [x] 4.2 **全量** `pytest -q`(非子集——遵 memory:scoped 改动靠编排者全量兜 ripple)+ `mypy --strict` + `ruff` 全绿。
- [ ] 4.3 真机 Demo Path(procps Linux / local target):`hostlens inspect linux.cpu.top_processes --json` 核对输出含 `etimes` 列;`yes > /dev/null &` 后立即采集验 `etimes<10` 不告警、存活 >10s 后告警。
- [x] 4.4 commit 到 `feat/calibrate-top-processes-transient-cpu`,跑对抗性 review-loop,APPROVE/CLEAR 后开 PR → CI 绿 → squash merge。(实现 review-loop 2 轮 APPROVE-DEGRADED;PR #116 已 squash merge)
