# 项目规则：华硕大厅客户端 UI 自动化

## 知识库指针

当任务涉及控件定位、页面结构、跑法命令、提权、历史决策或写任何断言时，先读项目根目录 `项目知识库/00-index.md`，再按索引读相关文档；普通单文件修改不必全量读。

当前任务进度、环境现状、下一步看根目录《会话交接.md》。

## 硬红线

- **密码 / 测试号 / 微软邮箱只走环境变量**（`HALL_TEST_USER` / `HALL_TEST_PASSWORD` / `HALL_TEST_NEW_PASSWORD` / `HALL_MS_USER`），不落盘、不进 git、不写进任何文件。`config.local.yaml` 已在 `.gitignore`。
- **绝不点这些按钮**：同步页「全部安装」`InstallAllBtn` 及任何条目安装、更新页 `UpdateAllBtn`、任何非夹具条目——列表里全是 QQ/微信/WPS 这类日常软件，点下去就真装一机器。
- **办公机不要 `--allow-install`**；破坏性用例（`destructive` 标记）必须门禁 + 提权才跑。
  ⚠️ **2026-09-23 起全量档 `--suites all` 含 `install`（卸载重装大厅本体）和 `apps-lifecycle`
  （夹具真装真卸）** —— 它们走提权通道（计划任务 `HallAutoP1`，`hall_auto/elevation.py`）执行，
  节点侧零人工。**在办公机上要跑回归就投具体套件**（`launch,login,settings,…`），**别投 `all`**。
- 本机 `python` 不在 PATH，一律 `.venv\Scripts\python.exe`，带 `PYTHONUTF8=1`。
- 内部安全工具（CheckAppV/SignAppsV/signcheck_v2）和受检安装包**不进公开仓库**，只放 `Desktop/Test/华硕大厅/fixtures/`，committed 的 `config.yaml` 对应块留空。
- **机器绝对路径不进代码**：`*.py` / `*.ps1` 里不许出现**具体用户 profile** 的绝对路径（`C:/Users/<名字>/...`）。
  取本机位置用 `$PSScriptRoot`（PS）、`Path.home()`、`config.local.yaml`（已忽略）或环境变量。
  **唯一例外是 `config.yaml` 的 `installer_dir` 模板值 `C:/Users/ASUS/...`** —— 它是**金丝雀**：
  bootstrap 第 1 步靠 `_foreign_profile_owner` 认出「这台机器没铺过环境 / 整包拷贝带了老机器的 config」，
  换成 `C:/Users/Public/...` 会让这条检查**失效并退回假绿**。**别"清理"它**，`tests/unit/test_repo_hygiene.py` 锁着。
  文档（手册/知识库）与 `tests/` 里的路径只是**举例与夹具**，不受这条约束。

## 交接与知识库分工

- 《会话交接.md》只放**会变的**：当前进度、环境现状、下一步、未提交/未 push 清单。
- `项目知识库/` 放**稳定的**：控件图、坑、跑法、决策。同一条事实只留一个权威来源。
- 一次性过程叙事（第几轮复跑、耗时基线、commit 号）归 commit message，两边都不留。
- `交接归档/` 是**本机专属**（`.gitignore` 已忽略）：老进度快照 + 过期文档副本。clone / 整包拷贝后**不存在**，
  所以《会话交接.md》引用它时必须写明「仅本机」，不许写成"去归档里找"这种 clone 后会落空的承诺。
