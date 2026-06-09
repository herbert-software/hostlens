# add-docker-target 任务

> Spec 引用：`specs/docker-execution-target/spec.md`（新增）、`specs/execution-target/spec.md`（MODIFY TargetsConfig）。
> Demo Path 见 proposal。每个 OpenSpec change 一个 feature branch `feat/add-docker-target`。

## 1. 依赖与脚手架

- [x] 1.1 `pyproject.toml` 加 `[project.optional-dependencies] docker = ["docker>=7"]`；`dev` 数组追加自引用 `hostlens[docker]`（CI 跑集成测试需要）
- [x] 1.2 本地装 `pip install -e ".[dev,docker]"` 验证 docker-py 可 import（沙箱外）

## 2. 配置层（DockerEntry）

- [x] 2.1 `src/hostlens/targets/config.py`：新增 `DockerEntry(_CommonEntryFields)`，`type: Literal["docker"]`、`container: Annotated[str, Field(min_length=1)]`（必填非空，空串 raise ValidationError）、`docker_host: str | None = None`，`extra="forbid"`
- [x] 2.2 把 `DockerEntry` 加入 discriminated union：`TargetEntry = Annotated[LocalEntry | SSHEntry | ReplayEntry | DockerEntry, Field(discriminator="type")]`
- [x] 2.3 确认 `${...}` 在 `container`/`docker_host` 已被既有字段名 allowlist walker 自动拒绝为 `env_placeholder_not_allowed_here`（占位阶段，`model_validate` 之前）——无需 DockerEntry 额外逻辑，加一条测试确认
- [x] 2.4 `docker_host` scheme 校验（**post-`model_validate` 的 loader 步骤或 `field_validator` raise `ConfigError`，不用 `Field(pattern)`**——它 raise ValidationError 非 ConfigError）：**接受是窄例外、拒绝是默认 catch-all**——仅 `startswith("unix://")`（小写敏感）且 socket 路径非空时接受；**其余一切**（远程 scheme tcp/ssh/http(s)/npipe、无 scheme 裸路径、空 `unix://`、大小写不符 `UNIX://`）→ `ConfigError(kind="docker_host_remote_not_supported", field="docker_host")`。顺序：占位拒绝先于 scheme 校验
- [x] 2.5 单元测试 `tests/targets/test_docker_config.py`（无需 daemon）：type docker 路由到 DockerEntry / container 缺失 raise ValidationError / **container 空串 raise ValidationError** / 多余字段 raise（extra=forbid）/ container 字段集恰好 `{container, docker_host}` / `${...}` 在 container raise `env_placeholder_not_allowed_here` / `docker_host` 拒绝集（`tcp://...`、裸路径 `/var/run/docker.sock`、空 `unix://`、`UNIX://x`、相对 `unix://foo`）均 raise `docker_host_remote_not_supported` / **合法 `docker_host: unix:///var/run/docker.sock` 被接受且保留** / `docker_host: ${X}` 先命中 `env_placeholder_not_allowed_here`（对齐 spec 修改需求场景）

## 3. DockerTarget 实现

- [x] 3.1 `src/hostlens/targets/docker.py`：模块顶层 `try: import docker except ImportError: docker = None`（optional-dep 容错，未装也能 import 模块——D5）
- [x] 3.2 `DockerTarget.__init__(name)`：`re.fullmatch` 校验 name → 不匹配 raise `TargetError(kind="invalid_target_name")`；`type = "docker"` 类常量；不在 `__init__` 做任何 probe / docker 调用（纯类属性赋值）
- [x] 3.3 入口防线（exec/read_file 顶部，任何 docker 调用之前，**顺序固定**）：① **先** `_entry is None` → raise `docker_no_entry`（不触碰 `.enabled` 避免 AttributeError）；② **再** `_entry.enabled is False` → raise `target_disabled`，**不**构造 client / 不连 daemon（对齐 spec 场景:disabled docker target exec 不触发 daemon / standalone 构造 raise docker_no_entry）
- [x] 3.4 lazy docker client：首次需要时 `await asyncio.to_thread(self._build_client)`（`docker.from_env()` 或按 `_entry.docker_host`）并缓存复用；client 构造失败按 D5/错误分类 raise（`docker_sdk_unavailable` / `docker_unavailable`）
- [x] 3.5 容器解析：`await asyncio.to_thread(client.containers.get, ref)`；`NotFound` → `container_not_found`；`status != "running"`（含 exited/paused/created/restarting/dead/removing 全部）→ `container_not_running`（含 status）
- [x] 3.6 `exec(cmd, *, timeout, env)`：`asyncio.wait_for(asyncio.to_thread(container.exec_run, ["/bin/sh","-c",cmd], environment=env, demux=True), timeout)`；超时 → `ExecResult(timed_out=True, exit_code=None)`；正常 → 填 exit_code(ExitCode 为 None 且非超时则 exit_code=None，不顶 0/-1)/stdout/stderr(demux None 路解空串、UTF-8 errors=replace)/duration；非零退出不 raise；OCI exec 失败(/bin/sh 不存在等)→ `exec_failed`（不归 `docker_unavailable`）
- [x] 3.7 env 注入断言：env 仅经 `environment=` 传，cmd 严格为 `["/bin/sh","-c",cmd]`，不拼 `export`（spec 场景:exec secret 不出现在 cmd string）
- [x] 3.8 `read_file(path)` 固定顺序：① 预校验——`PurePosixPath` 仅用于判绝对性/取 parts(不靠它折叠 `..`)、只接受绝对路径(不以 `/` 开头 → `invalid_path`)、NUL/换行 → `invalid_path` 不发请求、绝对路径含 `..` 用 **`posixpath.normpath`** 折叠(`PurePosixPath` 不折叠 `..`！)再 get_archive；② `NotFound` → `FileNotFoundError`；③ **逐条迭代 tar 条目(单遍前向、不预缓存全量)**：忽略 PAX/global header，首个非 regular file(目录/符号链接/FIFO/设备)立即 → `not_a_file`、再遇第二个 regular file 同样 → `not_a_file`(**优先于 size**；依赖 get_archive 顺序属性:目录路径首条目即 DIRTYPE、单文件路径仅 1 条目)；④ **判 size**：累计读取是**无条件 backstop**(无论 stat 有无 size，边读边累计 `>10MB` 立即中止 raise `file_too_large`、禁止先读全量)，stat 给可信 size 时额外允许提前 raise(优化非唯一防线)；边界 `>`(恰好 10MB 放行)
- [x] 3.9 capabilities lazy probe：初始 `{SHELL, FILE_READ}`；首次 exec 成功后 **`command -v systemctl`**(非 `which`,POSIX,distroless 兼容) → `SYSTEMD`、`command -v docker` → `DOCKER_CLI`，缓存到 `_probed_caps`；**probe 自身失败**(command -v exec_run 抛异常)→ `_probed_caps` 设为已探到子集、不 re-probe、不影响本次 exec 返回值
- [x] 3.10 错误包装：从 docker 异常**显式提取字符串**(取 `exc.explanation`/`str(exc)`)后喂 `scrub_exception_message(text)` 清洗**偶然夹带的** home 路径/IP/凭据再包 `TargetError`，只在边界 raise，不吞。**注意**：docker 默认 socket 路径 `unix:///var/run/docker.sock` 是公开非密路径，scrub 不脱敏它、本提案也不要求(不得声称 scrub 会脱敏 socket)；docker_host 已限本机 unix socket，错误里无远程端点凭据

## 4. Registry 接线

- [x] 4.1 `src/hostlens/targets/registry.py`：`build_registry_from_config` 加 `elif entry.type == "docker": target = DockerTarget(name=entry.name)` 分支（mypy narrow 后 register + 注入 `_entry`）
- [x] 4.2 验证 disabled docker target 行为对齐既有约定（注册但 exec 前 raise `target_disabled`）——复用既有逻辑，加一条 registry 单元测试覆盖 docker 分支

## 5. 集成测试（真实容器，无 daemon skip）

- [x] 5.1 单元测试 `tests/targets/test_docker_unit.py`（无需 daemon，**允许 mock**）：mock wrap `docker.from_env`，验 client 复用单次构造（3 次 exec → from_env 调 1 次）；disabled gate → `target_disabled` 且 from_env 调 0 次；`_entry is None` → `docker_no_entry`
- [x] 5.2 集成测试 `tests/targets/test_docker_integration.py`：session fixture 探测 docker daemon，不可达 `pytest.skip`；起 `alpine` 容器（session-scoped 复用），用例间用独立临时文件
- [x] 5.3 集成覆盖：成功 exec / 非零 exit / 超时(只断言 timed_out=True+exit_code=None 返回值，**不**声称验线程释放) / env 经 environment 注入且 secret 不进 cmd / get_archive 读小文件 / read_file 恰好 10MB 放行 / read_file >10MB raise / read_file 目录或符号链接 not_a_file / **read_file 多条目超大归档优先 not_a_file 非 file_too_large** / read_file 相对路径 invalid_path / **read_file 换行路径 invalid_path** / **read_file 绝对路径含 `..` 规范化后读取** / read_file 不存在 FileNotFoundError / 容器不存在 container_not_found / 容器停止 container_not_running / capabilities lazy probe（**client 复用不在集成层、见 5.1 单元**）
- [x] 5.4 加 `docker_integration` marker 到 `pyproject.toml` 的 `[tool.pytest.ini_options] markers`；用 grep 确认 `test_docker_integration.py` 无任意 mock docker 写法：`mock.patch("docker` / bare `patch("docker` / `mocker.patch(...docker` / `monkeypatch.setattr(docker` / `patch.object(docker`（spec 场景:不允许 mock docker-py）

## 6. doctor / CLI 验证（复用既有 type-agnostic 逻辑）

- [x] 6.1 验证 `hostlens target test <docker-target>` 走 echo probe + 输出 capabilities（type-agnostic 逻辑自动覆盖，加一条断言/手测）；确认连通失败时 CLI 如实透传 docker 类 kind（`docker_unavailable`/`container_not_found`/`container_not_running`），无需 MODIFY target test spec（kind 透传是 type-agnostic 的）
- [x] 6.2 验证 `hostlens doctor --json` 的 `targets` section 含 docker target connectivity（type-agnostic 逻辑自动覆盖）；docker socket 安全提示落 `docs/operations/targets.md`（task 7.1），**不**改 doctor 运行时输出

## 7. 文档与收尾

- [x] 7.1 `docs/operations/targets.md`（已存在）补 docker target 配置示例 + socket=宿主 root 等价权限安全提示 + `docker_host` 仅本机 socket 说明 + `[docker]` extra 安装说明；docstring 陈述技术事实「容器内 exec_run environment 直达进程环境，不受 sshd AcceptEnv 过滤」（D3，只写事实不写营销式对比）
- [x] 7.2 `mypy --strict` + `ruff` 全过；跑 `openspec-cn validate --change add-docker-target --strict`
- [x] 7.3 本机 Demo Path 跑通（proposal §Demo Path）：起 alpine 容器 → 配 targets.yaml → `target test`（容器内真 echo，exit0 + capabilities）/ `doctor --json`（connectivity ok、type docker）两腿验证通过；`inspect` 腿因 inspector manifest `targets` 尚不含 docker 报 `requires_unmet`（已记入 proposal Non-Goals，「inspector 巡检容器」是 follow-up，非本 change 范围）
- [ ] 7.4 commit 后跑对抗性 review（`/review-loop-codex`，§5.3：含 src/ 运行时行为 + 安全边界，应 review）→ APPROVE 后开 PR `feat/add-docker-target` → CI 绿 + Copilot/BugBot triage → squash merge
