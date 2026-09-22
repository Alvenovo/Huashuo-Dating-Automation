"""把要发给测试同事的文档同步到桌面 —— **同步出来的是「发放版」**。

## 为什么需要这个脚本

桌面那几份是**手工拷的**，仓库一改它就旧，而且**不会报错**。
2026-09-21 真发生过一次：桌面还停在 09-18 的「10 步版」，仓库已经是「12 步版」，
差了整整三轮修复（含 6 个硬阻断）。**手工同步靠记性，一定会漏** —— 而且漏的代价是
测试同事拿着旧手册跑，踩的正是已经修好的坑。

改完手册/清单跑一下这个，或者把它塞进发文档之前的清单里（见 `项目知识库/交付前验收.md`）。

## 发放版：真密码从环境变量来，仓库里只留占位符

要真发出去的那份，里面 `net use \\... /user:hallshare "<共享盘密码>"` 这类命令
**必须填真值**，否则一线照抄会在 `net use` 那步报 401 —— 而那看着像"密码错"，
实际只是文档没填。可密码又**不能进 git**（项目红线，见 `AGENTS.md`）。

所以本脚本的做法是：**仓库里存占位符，同步时用环境变量 `HALL_SHARE_PASSWORD` 现场替换**，
再把结果落到桌面。仓库那份永远是占位版，发出去那份永远是发放版，两边不会串。

    $env:HALL_SHARE_PASSWORD = "<真密码>"     # 值别写进任何文件、别贴进对话
    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py

⚠️ **没设 `HALL_SHARE_PASSWORD` 时不会静默降级成占位版** —— 直接报错退出（rc=1）。
理由和本项目一贯的"假绿比红灯更坏"一样：一份密码写着 `<共享盘密码>` 的文档发出去，
**看起来是好的**，一线要到 `net use` 报 401 才发现，那时半天已经没了。
（只想生成占位版来调试脚本本身：加 `--placeholder`。）

## 同步哪几份

见 `DOCS`。加新文档就往那个列表里加一行 —— **别再去手工拷桌面**。

## 覆盖前留档：只保「人改过的」，不保自己的输出

覆盖已有文件前会把它留一份 `xxx-旧版备份.md`，免得对面手工批注过的东西被无声抹掉。

但**上一版就是本脚本生成的（带「发放版」横幅）时不留档** —— 那种文件里没有手工内容，
留档只会越攒越多：2026-09-22 用户反馈「我要的是新机操作清单，怎么给我生成那么多旧版备份」，
一查桌面 3 份/文档，全是我们自己上一轮的产物。判据见 `_is_our_output()`。

## 用法

    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py
    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py --to "D:\\共享\\手册"
    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py --check   # 只比对，不写
    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py --placeholder

退出码：`0` 全部已是最新（或同步成功）／`1` `--check` 下发现不一致、源文件缺失、
或文档里有凭据占位符却没设 `HALL_SHARE_PASSWORD`。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KB = REPO_ROOT / "项目知识库"

# 要发出去的文档。加一份就往这里加一行。
DOCS: list[Path] = [
    KB / "测试机操作手册.md",
    KB / "新机操作清单.md",
]

# 凭据环境变量。**值不进仓库、不进 git、不进对话。**
PASSWORD_ENV = "HALL_SHARE_PASSWORD"

# 要替换成真值的占位符 —— **精确 token 白名单**，不是"凡是 `<...密码...>` 都换"。
#
# 为什么不图省事用正则扫：文档里有 `<新密码>`，那是"你自己想一个新的高强度密码"的占位
# （`net user hallshare "<新密码>"`，改密流程用），把它换成共享盘密码是**错的**，
# 而且错得很隐蔽 —— 改密那条命令会真的把共享盘密码设成旧密码。
PASSWORD_PLACEHOLDERS: tuple[str, ...] = (
    "<共享盘密码>",
    "<共享账号密码>",
    "<密码>",
)

# 替换后允许**继续留着**的、含「密码」字样的占位符（见上面 `<新密码>` 的说明）。
_ALLOWED_LEFTOVER: tuple[str, ...] = ("<新密码>",)

# 找"看起来像凭据占位符"的东西，用来在替换后自查有没有漏网的。
_CRED_HINT = re.compile(r"<[^<>\n]{0,16}密码[^<>\n]{0,16}>")

# 拆行用。**不用 `str.splitlines()`** —— 它还会在 \v \f \x1c \u2028 等处断行，
# 而 Markdown 正文里出现这些字符虽然罕见，一旦出现就会把文档切碎。
_NEWLINE_RE = re.compile(r"\r\n|\n")

# 插在标题下面那条横幅。**只在真的替换了凭据时加** —— 占位版不加，免得自欺。
_BANNER = (
    "> ⚠️ **发放版（含明文凭据）** —— 由仓库 `项目知识库/{name}` 自动生成，"
    "共享盘密码已填成真值。\n"
    "> **只单独发给操作者**：不要拷进共享盘、不要 commit、不要转发到群里。\n"
    "> 主文档一改这份就旧了 —— 重跑 `tools\\sync_handbook.py` 重新生成，别拿它当权威版本。"
)

# 本脚本生成物的指纹：横幅里这一句出现，就说明那份文件是**我们上一轮写的**。
# 用途见 `_is_our_output()` —— 决定覆盖前要不要留档。（测试里锁着它必须在 `_BANNER` 里。）
_RELEASE_MARK = "发放版（含明文凭据）"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _newline_of(text: str) -> str:
    """这份文档用哪种换行。**必须原样保住**。

    仓库两份文档全是 CRLF。要是同步出去变成 LF，git 会把整篇标成改动，
    评审时真正改的那几行就淹了 —— 和"没改也报改动"一样糟。
    """
    return "\r\n" if "\r\n" in text else "\n"



def default_target(doc: Path) -> Path:
    """默认落点：当前用户桌面。"""
    return Path.home() / "Desktop" / doc.name


def has_credentials(text: str) -> bool:
    """这份文档里有没有待填的凭据占位符。"""
    return any(token in text for token in PASSWORD_PLACEHOLDERS)


def leftover_placeholders(text: str) -> list[str]:
    """替换后还残留的、含「密码」字样的占位符（`<新密码>` 除外）。

    非空 = 有人在文档里新加了一种占位符却没登记进 `PASSWORD_PLACEHOLDERS`。
    那种漏网在发放版里**看不出来** —— 它会原样发出去，一线照抄必失败。
    """
    found = {m.group(0) for m in _CRED_HINT.finditer(text)}
    return sorted(found - set(_ALLOWED_LEFTOVER))


def render_release(text: str, password: str, source_name: str) -> str:
    """把占位符换成真值，并在标题下插一条「发放版」横幅。"""
    nl = _newline_of(text)
    body = text
    for token in PASSWORD_PLACEHOLDERS:
        body = body.replace(token, password)

    lines = _NEWLINE_RE.split(body)
    # 插在 H1 标题之后（第一行）；没有 H1 就插在最前面。
    at = 1 if lines and lines[0].startswith("# ") else 0
    rest = lines[at:]
    while rest and not rest[0].strip():
        rest.pop(0)
    banner = _BANNER.format(name=source_name).split("\n")
    return nl.join(lines[:at] + [""] + banner + [""] + rest)


def build_payload(
    doc: Path, password: str, allow_placeholder: bool
) -> tuple[bytes | None, str, bool]:
    """算出该写出去的字节。返回 `(字节, 原因, 是不是发放版)`；字节为 `None` = 不能同步。

    **全程走字节**：`Path.write_text()` 在 Windows 上会把 `\\n` 翻成 `\\r\\n`，
    于是"写下去再读回来"和"算出来的"不一致 —— 幂等检查会永远报「旧了」。
    """
    raw = doc.read_bytes()
    text = raw.decode("utf-8")

    if not has_credentials(text):
        return raw, "", False

    if not password:
        if allow_placeholder:
            return raw, "", False
        return None, (
            f"{doc.name} 里有待填的凭据占位符，但没设 {PASSWORD_ENV}。\n"
            f"       发出去的文档必须带真值，否则一线照抄 `net use` 会报 401（像密码错，其实是没填）。\n"
            f'       先设：$env:{PASSWORD_ENV} = "<真密码>"（值别写进文件、别贴进对话）\n'
            f"       只想生成占位版（调试脚本用）：加 --placeholder"
        ), False

    rendered = render_release(text, password, doc.name)
    leftover = leftover_placeholders(rendered)
    if leftover:
        return None, (
            f"{doc.name} 替换后仍残留凭据占位符 {leftover} —— "
            f"有人加了新占位符却没登记进 PASSWORD_PLACEHOLDERS，"
            f"照原样发出去一线会照抄失败。先把它加进白名单。"
        ), False
    return rendered.encode("utf-8"), "", True


def _target_for(doc: Path, to_raw: str) -> Path:
    """`--to` 收两种：目录（每份文档放进去）或具体 .md 文件（只允许同步一份）。"""
    raw = (to_raw or "").strip()
    if not raw:
        return default_target(doc)
    given = Path(raw)
    if given.suffix.lower() != ".md":
        return given / doc.name
    if len(DOCS) != 1:
        raise SystemExit(
            f"[FAIL] --to 给到具体 .md 文件时只能同步一份文档，当前配置了 {len(DOCS)} 份："
            f"{[d.name for d in DOCS]}。请给目录。"
        )
    return given


def _backup_path(target: Path) -> Path:
    """覆盖前的留档路径。**已存在就加时间戳** —— 别把上一次的留档也覆盖掉。"""
    plain = target.with_name(f"{target.stem}-旧版备份{target.suffix}")
    if not plain.exists():
        return plain
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return target.with_name(f"{target.stem}-旧版备份-{stamp}{target.suffix}")


def _is_our_output(path: Path) -> bool:
    """这份文件是不是**本脚本上一轮生成的**（带「发放版」横幅）。

    是的话覆盖时**不留档**。留档的意义是保住**人改过的东西** —— 保住自己的输出没有意义，
    只会让桌面越攒越多：2026-09-22 用户反馈「我要的是新机操作清单，怎么给我生成那么多
    旧版备份」，一查桌面 3 份/文档，全是我们自己上一轮的产物。

    横幅插在标题正下方（见 `_BANNER`），所以只读文件头部就够，不用整份读进来。
    """
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    return _RELEASE_MARK in head.decode("utf-8", errors="replace")


def sync_one(doc: Path, to_raw: str, check: bool, password: str, allow_placeholder: bool) -> int:
    """处理一份文档，返回 0 一致/成功，1 旧了、源缺失、或不能生成发放版。"""
    if not doc.is_file():
        print(f"[FAIL] 源文件不存在：{doc}")
        return 1

    payload, problem, is_release = build_payload(doc, password, allow_placeholder)
    if payload is None:
        print(f"[FAIL] 无法生成发放版：{problem}")
        return 1

    target = _target_for(doc, to_raw)
    src_digest = _sha256_bytes(payload)
    src_size = len(payload)
    label = "发放版" if is_release else "文档"

    if not target.exists():
        if check:
            print(f"[旧] 目标不存在：{target}")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        print(f"[新] 已同步{label} -> {target}（{src_size} 字节）")
        return 0

    dst_digest = _sha256_file(target)
    dst_size = target.stat().st_size

    if src_digest == dst_digest:
        print(f"[同] 已是最新：{target}（{src_size} 字节，sha256 一致）")
        return 0

    if check:
        print(
            f"[旧] 与仓库不一致，需要同步：{target}\n"
            f"     仓库版{label} {src_size} 字节 / 目标版 {dst_size} 字节\n"
            f"     跑一次不带 --check 的即可同步"
        )
        return 1

    if _is_our_output(target):
        # 上一版就是本脚本写的 → 里面没有手工内容，不留档（否则每跑一次就多一份，
        # 用户 2026-09-22 反馈的正是这个）。
        target.write_bytes(payload)
        print(
            f"[新] 已同步{label} -> {target}"
            f"（{dst_size} -> {src_size} 字节；上一版是本脚本产物，不留档）"
        )
        return 0

    # 覆盖前先把旧的留一份，万一对面手工批注过还能找回
    backup = _backup_path(target)
    try:
        shutil.copy2(target, backup)
        print(f"[备] 旧版已留档 -> {backup}")
    except OSError as exc:
        print(f"[备] 旧版留档失败（{exc}），继续覆盖")

    target.write_bytes(payload)
    print(f"[新] 已同步{label} -> {target}（{dst_size} -> {src_size} 字节）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="同步要发给测试同事的文档到桌面（发放版）")
    parser.add_argument("--to", default="", help="目标目录或目标文件（默认当前用户桌面）")
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    parser.add_argument(
        "--placeholder",
        action="store_true",
        help=f"故意生成占位版（{PASSWORD_ENV} 未设时的降级路径，只在调试脚本本身时用）",
    )
    args = parser.parse_args()

    password = os.environ.get(PASSWORD_ENV, "")

    worst = 0
    for i, doc in enumerate(DOCS):
        if len(DOCS) > 1:
            print(f"--- {doc.name} ---")
        worst = max(worst, sync_one(doc, args.to, args.check, password, args.placeholder))
        if i + 1 < len(DOCS):
            print()
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
