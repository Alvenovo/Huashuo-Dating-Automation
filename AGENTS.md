# 项目规则：华硕大厅客户端 UI 自动化

## 知识库指针

当任务涉及控件定位、页面结构、跑法命令、提权、历史决策或写任何断言时，先读项目根目录 `项目知识库/00-index.md`，再按索引读相关文档；普通单文件修改不必全量读。

当前任务进度、环境现状、下一步看根目录《会话交接.md》。

## 硬红线

- **密码 / 测试号 / 微软邮箱只走环境变量**（`HALL_TEST_USER` / `HALL_TEST_PASSWORD` / `HALL_TEST_NEW_PASSWORD` / `HALL_MS_USER`），不落盘、不进 git、不写进任何文件。`config.local.yaml` 已在 `.gitignore`。
- **绝不点这些按钮**：同步页「全部安装」`InstallAllBtn` 及任何条目安装、更新页 `UpdateAllBtn`、任何非夹具条目——列表里全是 QQ/微信/WPS 这类日常软件，点下去就真装一机器。
- **办公机不要 `--allow-install`**；破坏性用例（`destructive` 标记）必须门禁 + 提权才跑。
- 本机 `python` 不在 PATH，一律 `.venv\Scripts\python.exe`，带 `PYTHONUTF8=1`。
- 内部安全工具（CheckAppV/SignAppsV/signcheck_v2）和受检安装包**不进公开仓库**，只放 `Desktop/Test/华硕大厅/fixtures/`，committed 的 `config.yaml` 对应块留空。

## 交接与知识库分工

- 《会话交接.md》只放**会变的**：当前进度、环境现状、下一步、未提交/未 push 清单。
- `项目知识库/` 放**稳定的**：控件图、坑、跑法、决策。同一条事实只留一个权威来源。
- 一次性过程叙事（第几轮复跑、耗时基线、commit 号）归 commit message，两边都不留。
