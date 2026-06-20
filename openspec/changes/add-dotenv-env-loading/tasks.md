## 1. 依赖与加载点

- [x] 1.1 `pyproject.toml` 加 `python-dotenv` runtime 依赖（自问已答:标准库无 .env 解析、pydantic-settings 不导出 os.environ，值得这一依赖）。
- [x] 1.2 在 `hostlens.cli` 根 `@app.callback()`（所有子命令 body 与 `load_settings()` 之前）经 `dotenv_values(dotenv_path=Path(".env"))` + `os.environ.setdefault` 加载 `.env`（不用 `load_dotenv`:同序插值保 Settings 取值不变 + 对 `PYTHON_DOTENV_DISABLED` 免疫）;缺文件 / 不可读 / 目录(≥1.0.1 空 dict / 旧版 OSError)静默跳过、不打印值、不向上递归。

## 2. 测试

- [x] 2.1 新增 CLI 启动加载行为测试，覆盖 spec 四个**行为**场景:`.env` 注入 → `${VAR}`/`os.environ` 可读；`override=False` → export 优先；缺 `.env` 静默零影响;`Settings` 取值不变。
- [x] 2.2 既有 CLI / serve / agent-loop 配置测试隔离仓根 dev `.env`（复用 `_isolate_env`:chdir tmp + delenv），确认干净 CI 不被仓根 `.env` 污染（[[project_tests_must_isolate_dev_env_or_ci_red]]）——即落实 spec「根回调的 cwd 副作用扩大测试隔离义务」契约场景:本变更后须隔离的测试集从「exercise `load_settings()` 者」扩到「任何 exercise 根回调者」。

## 3. 文档与收尾

- [x] 3.1 docs 配置说明:所有密钥 / env 统一放 `.env`;`export` 作为覆盖手段;cwd 语义（从含 `.env` 的目录运行）。
- [x] 3.2 ts.mac-mini 部署收尾:daemon wrapper 里 `source .notifier-secrets.env` 桥接可简化为把密钥放进 `~/hostlens/.env`（那行 source 变冗余可删）。
- [ ] 3.3 `openspec-cn validate add-dotenv-env-loading --strict` 过 + temp 副本实测 archive 不报错；feature branch `feat/add-dotenv-env-loading` + PR + CI 绿 + 对抗性 review;merge 后归档。
