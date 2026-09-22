"""锁「测试机常亮」的两层实现（机器级 `tools/set_keep_awake.ps1` + 接线）。

## 为什么要单开一个文件

常亮原来只有一层：`hall_auto/awake.py`，**进程级**，只在 pytest 那个进程活着时生效。
多机跑批上线后暴露了它的盲区 —— 节点机是**无人值守**的：

    · 跑批开始之前（人还没敲命令）
    · 两轮任务之间（agent 空转等任务，可能几小时）
    · 跑批结束之后（机器留到第二天接着跑）

这三段里没有任何进程持有常亮，Windows 默认电源方案（显示器 10 分钟关、睡眠 30 分钟）
会让机器**自己睡过去**。睡着的机器连 agent 的轮询线程一起停摆，控制机看不到回执，
现场表现是「投了任务没反应」—— 与「共享盘断了」「agent 崩了」完全同形，
排查方向从一开始就是错的。所以补机器级那层（改电源方案，持久）。

## 这里钉住的四条

1. **可回滚**：改的是系统级设置，没有 `-Restore` 就等于把机器锁死 ——
   原值必须落备份，且**第二次跑不许覆盖备份**（否则原值被 0 盖掉，再也回不去）。
2. **`-Check` 只读**：铺设是幂等的，已经是常亮就不许再写一遍备份/再改一遍设置。
3. **改完必须回读复核**：只看 `powercfg` 的退出码会**假绿** ——
   组策略/第三方电源管理软件能把值顶回去，命令照样 rc=0。
4. **接线不许绕过脚本**：bootstrap 里不许内联 `powercfg` 命令 ——
   绕过脚本就绕过了备份与复核这两道，出问题时没人知道改前是什么值。
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

pytestmark = pytest.mark.unit


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "set_keep_awake.ps1"
BOOTSTRAP_PY = REPO_ROOT / "tools" / "bootstrap_machine.py"
FARM_AGENT_PY = REPO_ROOT / "tools" / "farm_agent.py"
AWAKE_PY = REPO_ROOT / "hall_auto" / "awake.py"


def _load_bootstrap():
    """按路径加载 tools/bootstrap_machine.py（tools/ 不是包，不能直接 import）。"""
    path = BOOTSTRAP_PY
    spec = importlib.util.spec_from_file_location("bootstrap_machine_keep_awake", path)
    assert spec and spec.loader, f"加载失败：{path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bm():
    return _load_bootstrap()


def _fake_run(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------- 脚本本身：可回滚、可预演、只读可查 ----------------


def test_script_exists_and_offers_all_four_modes():
    """`-Check` / `-Restore` / `-DryRun` / `-SkipLid` 四个开关缺一不可。"""
    src = SCRIPT.read_text(encoding="utf-8-sig")
    for flag in ("[switch]$Check", "[switch]$Restore", "[switch]$DryRun", "[switch]$SkipLid"):
        assert flag in src, f"缺开关 {flag}"
    assert "powercfg" in src, "得真去改电源方案"
    assert "退出码" in src, "退出码要写在 .NOTES 里，脚本才好被串进流程"


def test_backup_is_written_before_changes_and_never_overwritten():
    """原值先备份，且**只写第一次**。

    第二次跑时值已经是 0，再写一份备份就把原值盖没了 —— 之后 `-Restore`
    会把机器"还原成永不睡眠"，而使用者以为还原了。这种错没人看得出来。
    """
    src = SCRIPT.read_text(encoding="utf-8-sig")
    assert "power_backup.json" in src, "备份文件名要固定，-Restore 才知道去哪读"
    assert "$env:LOCALAPPDATA" in src, (
        "备份要落在本机 %LOCALAPPDATA% —— 落进仓库会跟着 git 走，多机共用一份是错的"
    )
    assert "备份已存在，沿用" in src, "必须显式处理「备份已存在」这条路径"
    assert src.index("$env:LOCALAPPDATA") < src.index("monitor-timeout-ac"), (
        "备份逻辑必须排在改设置之前"
    )


def test_restore_refuses_to_guess_without_backup():
    """没备份时**不许猜**着还原：猜错方向可能是"改回永不睡眠"，比不还原更糟。"""
    src = SCRIPT.read_text(encoding="utf-8-sig")
    assert "没有备份文件" in src
    assert "不能瞎猜" in src


def test_apply_re_reads_values_instead_of_trusting_exit_code():
    """改完必须回读复核 —— 只看 rc 会被组策略/第三方电源软件顶回去还报成功。"""
    src = SCRIPT.read_text(encoding="utf-8-sig")
    assert "$afterMonitor" in src and "$afterSleep" in src, "要有改完之后的回读"
    assert "复核" in src, "复核结论要打出来，人才知道到底生效没有"


def test_script_has_no_machine_specific_paths():
    """脚本要能在 20 台机器上用：不许出现任何写死的 `C:\\...` 路径。"""
    src = SCRIPT.read_text(encoding="utf-8-sig")
    assert "C:\\" not in src and "C:/" not in src, (
        "脚本里出现了写死的 C 盘路径 —— 换一台机器就可能不存在（口径见 AGENTS.md）"
    )


# ---------------- powercfg 解析：必须取末尾两个索引 ----------------

# 真实 `powercfg /query SCHEME_CURRENT SUB_VIDEO VIDEOIDLE` 的输出（英文系统；
# 中文系统只是标签变中文，`0x` 那几行一模一样）。**样本别删** —— 下面两条用例靠它。
POWERCFG_QUERY_SAMPLE = """\
  Subgroup GUID: 7516b95f-f776-4464-8c53-06167f40cc99  (Display)
    GUID Alias: SUB_VIDEO
    Power Setting GUID: 3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e  (Turn off display after)
      GUID Alias: VIDEOIDLE
      Minimum Possible Setting: 0x00000000
      Maximum Possible Setting: 0xffffffff
      Possible Settings increment: 0x00000001
      Possible Settings units: Seconds
    Current AC Power Setting Index: 0x00000258
    Current DC Power Setting Index: 0x00000078
"""


def _timeouts_from_sample(text: str, *, tail: bool) -> tuple[int, int]:
    """照 `Get-Timeout` 的规则从样本里取 (AC, DC)。`tail=False` 是**出过事的旧写法**。"""
    values = [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{8})", text)]
    assert len(values) >= 2
    return (values[-2], values[-1]) if tail else (values[0], values[1])


def test_query_output_head_two_is_the_false_green_trap():
    """**这条钉的是坑本身**：取前两个 `0x` 会读成「AC=0（永不）/ DC=0xffffffff」。

    `/query` 在「当前 AC/DC 索引」之前先打一段取值范围（最小/最大/增量），
    同样全是 8 位十六进制。取前两个 → **任何机器**都读成"永不睡眠"，
    于是 `-Check` 恒报 `[OK] 已常亮`、bootstrap 第 13 步一个设置都不改。
    2026-09-21 实跑抓出来的，当时脚本就是这么写的。
    """
    assert _timeouts_from_sample(POWERCFG_QUERY_SAMPLE, tail=False) == (0, 0xFFFFFFFF)


def test_query_output_tail_two_is_the_real_index():
    """末尾两个才是当前索引：0x258=600 秒=10 分钟关屏，0x78=120 秒=2 分钟。"""
    assert _timeouts_from_sample(POWERCFG_QUERY_SAMPLE, tail=True) == (600, 120)


def test_get_timeout_reads_the_tail_of_the_matches():
    """脚本里必须**真的**取末尾两个 —— 上面那条只证明规则对，这条证明代码用的是它。

    （枚举型设置如合盖动作没有取值范围段，末尾两个同样是 AC/DC，所以两种设置通用。）
    """
    src = SCRIPT.read_text(encoding="utf-8-sig")
    fn = src[src.index("function Get-Timeout"):src.index("function Get-RegValue")]
    assert "$m.Count - 2" in fn and "$m.Count - 1" in fn, (
        "Get-Timeout 没有取末尾两个索引 —— 会读到取值范围里的最小/最大值，"
        "结果是「任何机器都报已常亮」的假绿（见上一条用例）"
    )
    assert not re.search(r"\$m\[[01]\]", fn), (
        "Get-Timeout 里还有从头部取索引的写法（`$m[0]`/`$m[1]`）—— 那就是踩过的坑"
    )
    assert "最小可能的设置" in fn or "Minimum Possible Setting" in fn, (
        "坑要写在函数注释里，否则下一个人会把「取末尾」当怪癖改回「取前两个」"
    )


def test_get_timeout_refuses_to_guess_when_powercfg_fails():
    """`powercfg` 自己失败时不许拿残缺输出硬猜 —— 猜出来的值一样会被当成"读到了"。"""
    src = SCRIPT.read_text(encoding="utf-8-sig")
    fn = src[src.index("function Get-Timeout"):src.index("function Get-RegValue")]
    assert "$LASTEXITCODE -ne 0" in fn


def test_check_verifies_both_ac_and_dc():
    """AC/DC 都要查：只查 AC 的话，拔了电源（跑电池、DC 仍会息屏）的笔记本会被误判已常亮。"""
    src = SCRIPT.read_text(encoding="utf-8-sig")
    check = src[src.index("# ---------------- -Check"):src.index("# ---------------- -Restore")]
    assert "$monitor.Dc -eq 0" in check and "$sleep.Dc -eq 0" in check


# ---------------- bootstrap 接线 ----------------


def test_bootstrap_has_keep_awake_step_and_opt_out_flag():
    src = BOOTSTRAP_PY.read_text(encoding="utf-8")
    assert "def step_keep_awake" in src
    assert "set_keep_awake.ps1" in src, "要调脚本，不许自己实现一套"
    assert "run(step_keep_awake(args.skip_keep_awake))" in src, "步骤没接进 main 等于没写"
    # ⚠️ 这里**必须**查 argparse 注册，不能只查字符串存在 ——
    # docstring 里提一句 `--skip-keep-awake` 就能满足 "in src"，守卫恒绿。
    # 2026-09-22 真踩：`main()` 读了 `args.skip_keep_awake`，开关却没注册，
    # 于是**每一次** bootstrap 都在最后一步 AttributeError 崩掉。
    assert re.search(r'add_argument\(\s*"--skip-keep-awake"', src), (
        "`--skip-keep-awake` 没真的注册进 argparse —— "
        "只在文档/注释里提一句不算（那样 args.skip_keep_awake 会 AttributeError）"
    )


def _powercfg_invocations(src: str) -> list[str]:
    """从 Python 源码里挑出**真的在调 powercfg** 的字符串字面量（提示文案不算）。

    只认两种命令形态：整条就是命令名、或带 `/子命令` 参数。
    中文句子里夹一个 `powercfg` 词是给人看的提示，不是调用。
    """
    hits = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value.strip().lower()
        if re.fullmatch(r"powercfg(\.exe)?", text) or re.search(r"powercfg(\.exe)?\s+/", text):
            hits.append(node.value.strip())
    return hits


def test_powercfg_detector_catches_calls_but_not_prose():
    """**守卫的守卫**：判据本身要能分辨「调用」和「文案」。

    上面那条用例的判据曾经粗到 `"powercfg" not in src`，被自己的提示文案绊红。
    这里用两段合成源码把边界钉住：真调用必须抓到，中文提示必须放过。
    """
    assert _powercfg_invocations('run(["powercfg", "/change", "standby-timeout-ac", "0"])')
    assert _powercfg_invocations('run("powercfg /query SCHEME_CURRENT")')
    assert not _powercfg_invocations('st.fail("取不到电源设置（powercfg 不可用或输出解析不了）")')
    assert not _powercfg_invocations('"""说明：电源设置走 tools/set_keep_awake.ps1（内部用 powercfg）"""')


def test_bootstrap_never_inlines_powercfg():
    """**核心防线**：bootstrap 里不许真的去调 `powercfg`。

    绕过脚本就绕过了「改前备份」和「改后复核」这两道 ——
    出问题时没人知道改之前是什么值，也没人知道设置到底生效没有。

    **判据是「有没有把它当命令调」，不是「文件里有没有这个词」**。
    早先这里写的是 `assert "powercfg" not in src`，结果被自己的提示文案绊倒：
    `step_keep_awake` 的失败分支要告诉一线「取不到电源设置（powercfg 不可用…）」——
    那句话是对人说的，不是调用，却让守卫恒红。判据粗到会误伤文案的守卫等于没有守卫
    （本项目反复踩的"假守卫"），所以改成走 AST 只看**字符串字面量**里的命令形态。
    """
    offenders = _powercfg_invocations(BOOTSTRAP_PY.read_text(encoding="utf-8"))
    assert not offenders, (
        "bootstrap 里出现了裸 powercfg 调用 —— 电源设置必须走 tools/set_keep_awake.ps1"
        f"（备份 + 复核都在里面，绕过去就等于两道都没有）：{offenders}"
    )


# ---------------- step_keep_awake 的行为（rc 映射） ----------------


def test_step_skips_without_touching_anything(bm):
    """`--skip-keep-awake` 时一个子进程都不起，但要在报告里写明后果。"""
    with mock.patch.object(bm, "_run_text") as run:
        st = bm.step_keep_awake(True)
    run.assert_not_called()
    assert st.ok is True
    assert any("睡" in line for line in st.detail), "跳过也要说明后果，别让人以为是无关项"


def test_step_reports_missing_script_as_failure(bm, tmp_path):
    with mock.patch.object(bm, "KEEP_AWAKE_SCRIPT", tmp_path / "nope.ps1"):
        st = bm.step_keep_awake(False)
    assert st.ok is False


def test_step_leaves_already_awake_machine_alone(bm):
    """`-Check` 说已经是常亮 → 只跑那一次检查，不执行设置（幂等）。"""
    with mock.patch.object(bm, "_run_text", return_value=_fake_run(0, "[OK] 已常亮")) as run:
        st = bm.step_keep_awake(False)
    assert st.ok is True
    assert run.call_count == 1, "已经常亮了还去跑设置，会白写一遍备份"
    assert "-Check" in run.call_args.args[0]


def test_step_applies_when_check_says_not_awake(bm):
    """检查说会睡 → 执行设置；成功时把还原命令写进报告。"""
    calls = [_fake_run(1, "需要设置"), _fake_run(0, "[OK] 本机已常亮")]

    def fake(cmd, **kwargs):
        return calls.pop(0)

    with mock.patch.object(bm, "_run_text", side_effect=fake) as run:
        st = bm.step_keep_awake(False)
    assert st.ok is True
    assert run.call_count == 2
    assert "-Restore" in "\n".join(st.detail), "报告里要给还原命令"


def test_step_fails_when_power_state_unreadable(bm):
    """取不到电源信息必须判 fail —— 无法确认就等于无法排除"半夜睡过去"。"""
    with mock.patch.object(bm, "_run_text", return_value=_fake_run(3, "[FAIL] 取不到")) as run:
        st = bm.step_keep_awake(False)
    assert st.ok is False
    assert run.call_count == 1, "取不到信息时不该再去瞎设一遍"


def test_step_fails_with_admin_hint_when_elevation_needed(bm):
    calls = [_fake_run(1, "需要设置"), _fake_run(2, "[FAIL] 不是管理员")]

    with mock.patch.object(bm, "_run_text", side_effect=lambda cmd, **kw: calls.pop(0)):
        st = bm.step_keep_awake(False)
    assert st.ok is False
    assert any("管理员" in line for line in st.detail), "要指出是权限问题，别让一线往别处查"


# ---------------- 进程级那层：agent 也要持有常亮 ----------------


def test_farm_agent_holds_awake_for_its_lifetime():
    """节点 agent 空转期间必须自己持常亮 —— 它是节点上唯一长期活着的进程。"""
    src = FARM_AGENT_PY.read_text(encoding="utf-8")
    assert "keep_awake_for_process" in src, "agent 没持有常亮，空转等任务时机器会睡过去"
    assert "if not keep_awake_for_process()" in src, "失败要打出来，不许静默"


def test_awake_module_points_to_the_machine_level_script():
    """两层分工要写在模块头：不然"别改电源计划"那句会被读成"机器级也不用"。

    （`awake.py` 里那句针对的是**跑批期间**那一层 —— 进程级更可靠。
    机器级那层补的是没人跑 pytest 的空档，两层互不替代。）
    """
    src = AWAKE_PY.read_text(encoding="utf-8")
    assert "set_keep_awake.ps1" in src, "awake.py 要指向机器级脚本，说清两层分工"
    assert "机器级" in src
