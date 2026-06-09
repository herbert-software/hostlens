# docker-execution-target 规范

## 新增需求

### 需求:`DockerTarget` 必须基于 docker-py 实现且只对已存在容器做只读操作

`hostlens.targets.docker.DockerTarget` 必须：

- `type == "docker"`（class-level 常量 / 只读属性，**不是**构造器参数——构造器签名 `__init__(name: str)` 与 LocalTarget/SSHTarget 一致；容器引用从 `_entry: TargetEntry` 拿，由 `TargetRegistry.register` 注入）
- 结构化满足 `ExecutionTarget` Protocol（见 `execution-target` spec），即恰好提供 `name` / `type` / `capabilities` 属性 + `exec` / `read_file` 异步方法
- 基于 docker-py（`docker` 包）的同步 SDK 实现；因 docker-py 是同步阻塞 SDK，所有阻塞调用（`docker.from_env()` / `client.containers.get(...)` / `container.exec_run(...)` / `container.get_archive(...)`）必须用 `asyncio.to_thread` 包裹，**禁止**在事件循环线程内直接调用阻塞 docker-py 方法（与 §async-first 约定一致）
- 持有 **per-target docker client**：首次需要时按需 `docker.from_env()`（或按 `_entry.docker_host` 构造），后续 `exec` / `read_file` 复用同一 client；**禁止**每次 `exec` 重建 client
- 只对**已存在且 running** 的容器做**只读** `exec` / `read_file`；**禁止**做任何容器生命周期操作（create / start / stop / restart / rm）
- `name` 必须匹配正则 `^[a-z][a-z0-9_\-]{0,63}$`，在构造器内（赋值 `self.name` 前 `re.fullmatch`，不匹配 raise `TargetError(kind="invalid_target_name", target=name)`）enforce，与 SSHTarget/LocalTarget 同一道防线
- `capabilities` 初始值 `{Capability.SHELL, Capability.FILE_READ}`；首次 `exec` 成功后 lazy probe 一次并缓存到实例属性 `_probed_caps`：用 `command -v systemctl` 成功则加 `SYSTEMD`、`command -v docker` 成功则加 `DOCKER_CLI`（容器内有 docker 罕见但允许探到）。**probe 必须用 POSIX `command -v`、不用 `which`**——与 SSHTarget 探测（`command -v <bin>`）严格一致：`which` 非 POSIX，在 distroless / 部分 busybox 镜像里缺失，而 docker target 恰恰大量面对极简镜像，用 `which` 会让 docker 的能力探测比 SSH 更不可靠。`__init__` 内**禁止**做任何 probe（保持构造器纯类属性赋值）。**probe 自身失败时**（`command -v` 的 exec_run 抛异常 / 容器中途消失）：与 SSHTarget 一致——`_probed_caps` 仍设为「已成功探到的子集」（探不到的能力不加），标记为已探不再 re-probe，且**不影响触发本次 probe 的那次 `exec` 的返回值**（probe 是 exec 成功后的旁路增强，失败只意味着能力集偏保守，不让 exec 失败）
- **disabled gate**（继承 `execution-target` spec §`TargetsConfig` disabled 行为约定，对所有 type 生效）：`exec` / `read_file` 必须在**任何 docker 调用之前**（构造 client / `containers.get` / `exec_run` / `get_archive` 之前）检查 `self._entry.enabled`；`enabled is False` 时 raise `TargetError(kind="target_disabled", target=self.name)`，**不构造 client、不连 daemon**（与 LocalTarget/SSHTarget 在 `exec`/`read_file` 顶部调 `_check_enabled()` 同语义）
- **`_entry` 缺失防线**：容器引用从 `self._entry` 拿，由 `TargetRegistry.register` 注入；若 `exec` / `read_file` 在 `_entry is None`（未经 registry 注入的 standalone 构造）时被调用，必须 raise `TargetError(kind="docker_no_entry", target=self.name)`，**不**让 `containers.get(None)` 崩出裸 `TypeError`（与 SSHTarget `ssh_no_entry` 同防线）
- **两道入口防线的顺序**（必须固定，否则 `None.enabled` 抛 `AttributeError`）：`exec` / `read_file` 顶部**先**检查 `_entry is None`（→ `docker_no_entry`），**再**检查 `_entry.enabled`（→ `target_disabled`）；`_entry is None` 时直接 raise `docker_no_entry`，不触碰 `.enabled`

#### 场景:DockerTarget type 为 docker

- **当** 实例化 `DockerTarget(name="x")` 并检查 `target.type`
- **那么** `target.type` 必须为 `"docker"`（类常量，不接受 `type` kwarg）

#### 场景:非法 name 构造 raise invalid_target_name

- **当** 实例化 `DockerTarget(name="Prod-Web")`（含大写）或 `DockerTarget(name="1web")`（数字开头）
- **那么** 必须 raise `TargetError`，kind 为 `"invalid_target_name"`（构造器内 `re.fullmatch` enforce，与 SSHTarget/LocalTarget 同防线）

#### 场景:disabled docker target exec 不触发 daemon

- **当** docker target 的 `_entry.enabled is False`；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"target_disabled"`；且**未**构造 docker client、**未**连接 daemon（可用 mock wrap `docker.from_env` 断言其被调用 0 次）

#### 场景:standalone 构造（无 _entry）exec raise docker_no_entry

- **当** `DockerTarget(name="x")` 未经 `TargetRegistry.register` 注入 `_entry`（`_entry is None`）；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"docker_no_entry"`（**不**崩出裸 `TypeError` / `AttributeError`）

#### 场景:DockerTarget 复用单个 client（单元测试，允许 mock 计数）

- **当** 在**单元测试**（`tests/targets/test_docker_unit.py`，**非** `test_docker_integration.py`）里 mock wrap `docker.from_env`，对同一 `DockerTarget` 实例连续调用 `await target.exec(...)` 3 次
- **那么** `docker.from_env` 必须**只被调用 1 次**（后续复用同一 client）；3 次调用都正常返回 ExecResult
- **注意**：本断言用 mock 计数，**只能**在单元测试做；集成测试（§需求:DockerTarget 集成测试）禁止 mock docker-py，故 client 复用**不**列入集成测试覆盖项（docker-py client 不维持到 daemon 的持久 TCP，无法在集成层用连接数旁证）

#### 场景:DockerTarget capabilities 首次 exec 后才探测

- **当** 构造 `DockerTarget` 后、首次 `exec` 前检查 `target.capabilities`
- **那么** 必须仅含 `{SHELL, FILE_READ}`；首次 `await target.exec(...)` 成功后再检查，`capabilities` 才反映探测结果（容器内有 systemctl 则含 `SYSTEMD`）

### 需求:`DockerTarget.exec` 必须经 exec_run environment 注入 env，禁止拼接，且区分 timed_out 与 exit_code

`DockerTarget.exec(cmd, *, timeout, env=None)` 必须：

- 通过 docker-py `container.exec_run(cmd, environment=env, demux=True)` 执行；`env` dict 经 `environment=` 参数注入到容器内进程
- **禁止**在客户端把 `env` 转换为 `export VAR=val; cmd` 拼到 cmd string（secret 会进容器 process list / shell history / docker 事件日志，与 docs/ARCHITECTURE.md §4 命令渲染安全规则冲突，与 SSHTarget §env 注入 同源约束）
- `cmd` 是 shell-evaluated：必须以 `["/bin/sh", "-c", cmd]` 形式传给 exec_run（保证与 LocalTarget/SSHTarget 的 shell 语义一致，支持管道 / 重定向 / `$VAR` 展开），**不**以裸 token 列表执行。**distroless / 无 `/bin/sh` 的极简容器是非目标**（design Open Questions）：此时 `exec_run` 会抛 `docker.errors.APIError`（"OCI runtime exec failed: … /bin/sh: no such file"），按 §需求:故障分类 归入 `TargetError(kind="exec_failed", target=self.name)`（**不**误归 `docker_unavailable`——daemon 其实正常；`exec_failed` 专指「容器内命令无法启动」，与「daemon/容器不可用」区分，避免误导诊断）
- 返回 `ExecResult`：`exit_code` 取 exec_run 的 `ExitCode`；`stdout` / `stderr` 由 `demux=True` 分离后各自 UTF-8 解码（非 UTF-8 字节用 `errors="replace"`；某一路 demux 为 `None` 时解码为空串 `""`）；`duration_seconds` 记实际耗时。**`ExitCode` 为 `None` 且非超时**（docker-py 在个别 stream 模式下可能返回 None）时填 `ExecResult(exit_code=None, timed_out=False, ...)`——这是 ExecResult 契约允许的「拿不到 exit code 但未超时」合法形态（execution-target spec ExecResult 不变量只禁 `timed_out is True 且 exit_code 非 None`，不禁 `exit_code is None 且 not timed_out`），**禁止**用 `0` 或 `-1` 魔数顶替
- **超时语义**：docker-py `exec_run` 无原生 timeout，必须用外层 `asyncio.wait_for(asyncio.to_thread(...), timeout)` 实现；超时到期时返回 `ExecResult(timed_out=True, exit_code=None, ...)`（满足 execution-target spec ExecResult 不变量 `timed_out is True ⇒ exit_code is None`）；超时后尽力清理 exec（docker exec 进程不在 hostlens 进程组内，残留进程由 docker daemon 负责回收，hostlens 不做进程组 kill——与 SSHTarget「远端进程清理由 sshd 负责」同语义）
- 仅在 transport 级失败（daemon 不可达 / 容器不存在 / 容器非 running）时 raise `TargetError`；命令非零退出 / signal-killed 是**正常** ExecResult（`exit_code` 如实反映），**不** raise

#### 场景:exec 经 environment 注入且不在 cmd string 泄露

- **当** 调用 `await target.exec("echo $MY_VAR", timeout=5, env={"MY_VAR": "x"})`
- **那么** 实现必须调用 `container.exec_run(..., environment={"MY_VAR": "x"})`，传给 exec_run 的 cmd 不含 `export MY_VAR=x` 拼接；stdout 含 `"x"`

#### 场景:exec secret 不出现在 cmd string

- **当** 调用 `await target.exec("ps auxw", timeout=5, env={"SECRET_TOKEN": "abc"})`
- **那么** 实现传给 exec_run 的命令必须严格等于 `["/bin/sh", "-c", "ps auxw"]`（**不**含 `"SECRET_TOKEN"` 或 `"abc"` 子串），secret 仅经 `environment=` 传递

#### 场景:exec 非零退出返回 ExecResult 不 raise

- **当** 调用 `await target.exec("exit 3", timeout=5)`
- **那么** 必须返回 `ExecResult(exit_code=3, timed_out=False, ...)`，**不** raise `TargetError`

#### 场景:exec 超时返回 timed_out 且 exit_code 为 None

- **当** 调用 `await target.exec("sleep 60", timeout=2)` 且容器 running
- **那么** 必须在 ~2s 后返回 `ExecResult(timed_out=True, exit_code=None)`（满足 ExecResult 不变量）；不抛异常

### 需求:`DockerTarget.read_file` 必须经 get_archive 读取，尊重 10MB 上限

`DockerTarget.read_file(path)` 必须：

- **path 预校验（发请求前）**：用 `pathlib.PurePosixPath` **判定**绝对性 / 取 `.parts`（**仅检测，不依赖它折叠 `..`**）；**只接受绝对路径**（必须以 `/` 开头）——相对路径在容器内的 cwd 解析基准不确定（get_archive 相对路径语义依赖容器 WORKDIR），故相对路径一律 raise `TargetError(kind="invalid_path", target=self.name)`；含 NUL 字节 / 换行的路径同样 raise `invalid_path`，**不发起 docker 请求**。**绝对路径含 `..`**（如 `/a/../b`）：实现必须先用 **`posixpath.normpath`**（**不是** `PurePosixPath`——pathlib 纯路径**故意不折叠 `..`**，`str(PurePosixPath("/a/../b/c.txt"))` 原样返回；折叠 `..` 须用 `posixpath.normpath` / `os.path.normpath`）规范化折叠 `..` 后再传给 `get_archive`，折叠结果仍是容器内绝对路径（`/a/..` → `/`、`/../x` → `/x`，get_archive 是容器 namespace 内操作、无宿主逃逸面）；get_archive **不**像 SFTP 做服务端归一，不归一会读到非预期路径
- 文件不存在（`get_archive` 抛 `docker.errors.NotFound`）raise `FileNotFoundError`（标准库异常，不包装）
- **解 tar 与 size 判断的固定顺序**（消除 not_a_file 与 file_too_large 的判定竞争）：`get_archive(path)` 返回 `(stream, stat)`，实现必须按以下顺序：
  1. **逐条迭代 tar 条目定文件性（单遍前向、不预缓存全量）**：tar stream 单向不可 seek，实现**逐条迭代**条目（忽略 PAX / global header 元条目）——target 必须解析为**恰好一个 regular file 条目**；首个**非 regular file** 条目（目录 / 符号链接 / FIFO / 设备 typeflag）立即 raise `TargetError(kind="not_a_file", target=self.name, path=path)`，迭代中**再遇到第二个 regular file** 条目同样 raise `not_a_file`（**不**跟随符号链接、**不**返回 link target 字节）。**实现依赖（get_archive 条目顺序属性，须知悉）**：对单文件路径 `get_archive` 只返回 1 个 regular file 条目；对目录路径其 tar **首条目即目录元条目**（typeflag=DIRTYPE），故 `not_a_file` 在 entry#1 即命中、**无需**为「确认恰好一个」而预读后续或缓存大文件字节。**not_a_file 的判定优先于 size 判定**——多条目超大归档（如 `/etc` 含 >10MB 文件）报 `not_a_file`、**不**报 `file_too_large`（因目录条目先于内含文件，size 累计从不触及那个大文件）
  2. **再对该唯一 regular file 条目判 size**：**超过 10 MB**（`size > 10 * 1024 * 1024`，恰好 10 MB 放行）raise `TargetError(kind="file_too_large", target=self.name, path=path, size=size)`，**不**返回部分内容。边界用 `>`（与 LocalTarget/SSHTarget + execution-target Protocol「超出」严格一致，跨 target 不得漂移）。**累计读取是无条件 backstop**：实现必须**边读该条目字节边累计、累计 `> 10 MB` 立即中止并 raise** `file_too_large`（**无论** `stat` 是否给出 `size`——禁止先把整个 stream 读进内存）；`stat` 给出**可信** size 时，**额外允许**在读取前用它提前 raise（优化，**非**唯一防线——`stat` 缺 size / size 为 None / 实现不信任 stat 时，累计 backstop 仍保证 10MB 上限不被绕过）

#### 场景:read_file 读小文件

- **当** 容器内 `/tmp/hello.txt` 内容为 `b"hello"`；调用 `await target.read_file("/tmp/hello.txt")`
- **那么** 必须返回 `b"hello"`（经 get_archive 解 tar 得到）

#### 场景:read_file 超过 10MB raise

- **当** 容器内 `/tmp/big.bin` 大小为 11 MB；调用 `await target.read_file("/tmp/big.bin")`
- **那么** 必须 raise `TargetError`，kind 为 `"file_too_large"`，含 path 与 size；不返回任何字节

#### 场景:read_file 恰好 10MB 放行

- **当** 容器内 `/tmp/exact.bin` 大小恰好为 `10 * 1024 * 1024` 字节；调用 `await target.read_file("/tmp/exact.bin")`
- **那么** 必须**成功返回**全部字节（边界用 `>` 严格大于，与 LocalTarget/SSHTarget 一致），**不** raise `file_too_large`

#### 场景:read_file 路径指向目录或符号链接 raise not_a_file

- **当** path 指向容器内的**目录**（如 `/etc`），或指向**符号链接**（tar typeflag 非 regular file）；调用 `await target.read_file(path)`
- **那么** 必须 raise `TargetError`，kind 为 `"not_a_file"`，含 path（**不**跟随符号链接、**不**返回目录/link target 内容）

#### 场景:read_file 多条目超大归档优先报 not_a_file

- **当** path 指向**目录**且该目录含一个 >10 MB 的文件（get_archive 返回多条目 tar，其中某 regular file >10MB）；调用 `await target.read_file(path)`
- **那么** 必须 raise `TargetError`，kind 为 `"not_a_file"`（文件性判定优先于 size 判定），**不**报 `"file_too_large"`

#### 场景:read_file 相对路径 raise invalid_path

- **当** 调用 `await target.read_file("tmp/x")`（相对路径，不以 `/` 开头）
- **那么** 必须 raise `TargetError`，kind 为 `"invalid_path"`；不发起 docker 请求

#### 场景:read_file 绝对路径含 `..` 规范化后读取

- **当** 调用 `await target.read_file("/a/../b/c.txt")`，容器内 `/b/c.txt` 存在
- **那么** 实现必须先用 `posixpath.normpath` 折叠为 `/b/c.txt` 再传 `get_archive`，成功返回 `/b/c.txt` 内容（不把未归一的 `/a/../b/c.txt` 直接交给 get_archive；**不**用 `PurePosixPath` 折叠——它不折叠 `..`）

#### 场景:read_file 不存在 raise FileNotFoundError

- **当** 调用 `await target.read_file("/nonexistent")` 容器内无此文件
- **那么** 必须 raise `FileNotFoundError`（不是 `TargetError`）

#### 场景:read_file 路径含 NUL 字节 raise

- **当** 调用 `await target.read_file("/tmp/x\x00.txt")`
- **那么** 必须 raise `TargetError`，kind 为 `"invalid_path"`；不发起 docker 请求

#### 场景:read_file 路径含换行 raise invalid_path

- **当** 调用 `await target.read_file("/tmp/x\n.txt")`（含换行符）
- **那么** 必须 raise `TargetError`，kind 为 `"invalid_path"`；不发起 docker 请求

### 需求:`DockerTarget` 必须把 docker SDK / daemon / 容器层故障分类为 TargetError

`DockerTarget` 在以下故障场景必须 raise 带明确 `kind` 的 `TargetError`（只在 transport 边界 raise，不吞异常）：

- **docker-py SDK 未安装**（用户未装 `[docker]` extra）：DockerTarget 构造或首次使用时 import `docker` 失败 → raise `TargetError(kind="docker_sdk_unavailable", target=self.name)`，错误消息必须含修复提示 `pip install "hostlens[docker]"`；**禁止**让裸 `ImportError` / `ModuleNotFoundError` 冒泡
- **docker daemon 不可达 / socket 权限不足**：`docker.from_env()` 或 API 调用抛 `docker.errors.DockerException`（含 `APIError` 连接类）→ raise `TargetError(kind="docker_unavailable", target=self.name)`
- **目标容器不存在**：`client.containers.get(ref)` 抛 `docker.errors.NotFound` → raise `TargetError(kind="container_not_found", target=self.name)`
- **目标容器存在但非 running**（`status != "running"`，含 exited / paused / created / restarting / dead / removing）→ raise `TargetError(kind="container_not_running", target=self.name)`，含容器当前 status（规则是 `status != "running"` 全覆盖，不漏任何非 running 状态；粒度上把 paused/dead 等归同一 kind 是有意简化，诊断细节由 status 字段保留）
- **容器内命令无法启动**（`/bin/sh` 不存在等 OCI runtime exec 失败）→ raise `TargetError(kind="exec_failed", target=self.name)`，**不**归 `docker_unavailable`（daemon/容器正常，仅命令启动失败）
- 异常包装前必须经既有 `scrub_exception_message`（来自 agent-tool-adapter spec，签名 `(text: str) -> str`，接**已 str 化的消息**）清洗：实现必须把 docker 异常**显式提取为字符串**后再喂给 scrub（如取 `exc.explanation` / `str(exc)` 的结果），与其它 target 的异常包装路径一致，脱敏其中**偶然夹带的**用户 home 路径 / IP / 凭据特征（复用既有 `_SCRUB_PATTERNS`，覆盖 `/Users/`、`/home/`、IPv4/IPv6、`*_KEY=`、`Bearer`、`sk-` 等）
- **关于 docker socket 路径**（明确不脱敏，避免假承诺）：docker 默认本机 socket 路径 `unix:///var/run/docker.sock` 是**公开的非密路径**（每台装了 docker 的机器都一样，不含凭据），既有 `scrub_exception_message` **不**针对它脱敏，本 spec **也不要求**脱敏它——它出现在 `docker_unavailable` 错误里是**可接受的、非敏感的**诊断信息。`docker_host` 已被配置层限制为只允许本机 `unix://` socket（见 `execution-target` spec §`TargetsConfig` Docker 字段；远程 TCP/TLS 端点在加载期就被 `docker_host_remote_not_supported` 拒绝），故 docker 错误里**不会**出现含主机名/IP 的远程端点凭据。**禁止**在 spec / 实现里声称「scrub 会脱敏 socket 路径」——那会让实现者依赖一个 `scrub_exception_message` 实际不做的脱敏（经验证：`scrub_exception_message("unix:///var/run/docker.sock")` 原样返回）；若未来要脱敏 socket 路径，须单独对 `agent-tool-adapter` spec 起 MODIFY 提案，不在本提案范围

> docker-py 是 optional-dep：模块顶层 `import docker` 必须容错（`try/except ImportError` 置标志位，或延迟到方法内 import），让未装 `[docker]` 的环境**仍能 import `hostlens.targets.docker`**（用于 mypy / registry 分支注册），只在**实际构造或使用** DockerTarget 时才 raise `docker_sdk_unavailable`。

#### 场景:docker SDK 未安装 raise 带安装提示

- **当** 环境未安装 `docker` 包；构造 `DockerTarget` 并首次 `exec`
- **那么** 必须 raise `TargetError`，kind 为 `"docker_sdk_unavailable"`，消息含 `pip install "hostlens[docker]"`；**不** raise 裸 `ImportError`

#### 场景:容器不存在 raise container_not_found

- **当** `_entry.container` 指向不存在的容器名；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"container_not_found"`，含 target name

#### 场景:容器存在但已停止 raise container_not_running

- **当** 目标容器 `status == "exited"`；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"container_not_running"`，含当前 status

#### 场景:daemon 不可达 raise docker_unavailable

- **当** docker daemon 未运行（socket 连接被拒）；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"docker_unavailable"`（不是裸 `DockerException`）

#### 场景:无 /bin/sh 的容器 exec raise exec_failed

- **当** 目标是无 `/bin/sh` 的 distroless 容器（running 正常）；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"exec_failed"`（**不**是 `docker_unavailable`——daemon 与容器都正常，仅命令无法启动）

#### 场景:transport 异常经 scrub 脱敏偶然夹带的凭据

- **当** docker 异常消息里偶然夹带了用户 home 路径（如 `/Users/alice/...`）或凭据特征（如 `Bearer xxx`）；该异常被包装为 `TargetError`
- **那么** 最终 `TargetError.__str__` 与 structlog 输出中该 home 路径 / 凭据子串必须被 `scrub_exception_message` 脱敏；保留 target name + kind
- **且**（明确的非脱敏断言）docker 默认 socket 路径 `unix:///var/run/docker.sock` 作为公开非密路径**允许**出现在错误里，本场景**不**要求脱敏它（避免对 `scrub_exception_message` 实际不做的脱敏做出假断言）

### 需求:DockerTarget 集成测试必须用真实 docker 容器，无 daemon 时 skip

`tests/targets/test_docker_integration.py` 必须：

- 用真实 docker 容器跑（如 `python -m docker` 起一个 `alpine` 容器），**禁止** mock docker-py——不仅 `mock.patch("docker.from_env")` / `mock.patch("hostlens.targets.docker.docker")`，也包括 bare `patch("docker...")`（`from unittest.mock import patch`）/ `mocker.patch(...)` / `monkeypatch.setattr(docker, ...)` / `patch.object(docker, ...)` 等任意把 docker SDK 替换掉的写法，与 SSHTarget 用真实 sshd 容器的约定一致（CLAUDE.md §6）
- 用 `@pytest.mark.docker_integration` 标记；测试会话 fixture 检测 docker daemon 是否可达，不可达时 `pytest.skip("docker daemon unavailable")`（CI 无 docker 时整组 skip，不 fail）
- 覆盖：成功 exec / 非零 exit / 超时取消（断言 `timed_out=True`、`exit_code=None` 的**返回值**）/ env 经 environment 注入 + secret 不进 cmd string / `get_archive` read_file 小文件 / read_file 恰好 10MB 放行 / read_file 超过 10MB raise / read_file 目录或符号链接 raise not_a_file / read_file 相对路径 raise invalid_path / read_file 不存在 raise FileNotFoundError / 容器不存在 raise container_not_found / 容器已停止 raise container_not_running / capabilities lazy probe（首次 exec 前仅 `{SHELL, FILE_READ}`、exec 后反映探测）
- **不列入集成覆盖（有意排除，附理由）**：① **client 复用单次构造**——需 mock 计数，归单元测试（见 §需求:DockerTarget 复用单个 client 场景）；② **超时后线程/进程释放**——`asyncio.wait_for` 取消协程后，`to_thread` 包裹的阻塞 `exec_run` 仍在后台线程跑到容器内进程结束，「线程池 worker 何时释放」在测试内不可证（同步 SDK + to_thread 的固有限制，design 已承认）；集成测试**只**断言超时的**返回值**（timed_out=True / exit_code=None），**不**声称验证了线程释放，避免伪验收
- 容器 cold start 用 session-scoped fixture 复用；测试用例之间独立（独立临时文件 / 路径避免共享状态泄漏）
- DockerEntry 配置解析 + registry docker 分支的**单元测试**（`tests/targets/test_docker_config.py`）**不需要** daemon，必须在无 docker 的 CI 上也能跑过

#### 场景:集成测试通过真实容器跑 echo

- **当** 跑 `pytest tests/targets/test_docker_integration.py::test_exec_echo`（docker daemon 可达）
- **那么** 必须在真实容器内跑 `echo hostlens-probe`，断言 `ExecResult.stdout` 含 `"hostlens-probe"`

#### 场景:无 docker daemon 时集成测试 skip 不 fail

- **当** CI 环境无 docker daemon；跑 `pytest tests/targets/test_docker_integration.py`
- **那么** 整组测试必须 `skip`（reason 含 `docker daemon unavailable`），**不** fail / error

#### 场景:不允许 mock docker-py

- **当** 检查 `tests/targets/test_docker_integration.py` 文件内容
- **那么** 必须**不含**对 docker SDK 的任何 mock：grep 须用「`patch` / `mocker.patch` / `monkeypatch.setattr` / `patch.object` 任一调用 + 同行出现 `docker` 子串」的宽匹配（既覆盖 `mock.patch("docker.from_env")`、bare `patch("docker...")`，也覆盖 `patch("hostlens.targets.docker.docker")` / `monkeypatch.setattr(docker, ...)` —— 与本需求体禁止的全部写法一致，不留前缀绕过缝隙）（集成测试必须走真实 docker API）

#### 场景:配置解析单元测试无需 daemon

- **当** 在无 docker daemon 的环境跑 `pytest tests/targets/test_docker_config.py`
- **那么** 全部通过（DockerEntry 解析 + registry docker 分支构造仅校验配置层 / 类型，不触发 daemon 连接）
