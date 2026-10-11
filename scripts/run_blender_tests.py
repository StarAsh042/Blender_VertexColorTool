#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Blender 无头测试执行器（CI Job 2 入口）

为什么需要这个包装器（**这是本项目最关键的一条CI 教训**）:
    `tests/` 下的 4 个 Blender 测试文件**只打印汇总、从不用退出码表态**——
    它们的 `main()` 结尾没有 `sys.exit(1)`。
    Blender 的 `--python` 模式在脚本正常跑完时一律返回 0，
    于是「就算 30 条断言全红，CI 依然是绿的」。
    这正是上一轮「CI 5/5 全绿，但插件 100% 不可用」的同一个形状:
    **一个不表达失败的信号，被当成了通过的证据。**

    本包装器因此自己解析 stdout 里的汇总行，反推成败:
      · 有FAIL 行 / 失败数 > 0  -> 退出码 1
      · 找不到汇总行（脚本崩了/提前 return）-> 退出码 1（不放过）
      · 明确打印「跳过」-> 退出码 3（SKIP，≠ 通过）
      · 全部通过 -> 退出码 0

    退出码约定（与 verify_normal_constraint.py 一致）:
      0 = 通过；1 = 失败或结果不可信；3 = 因环境缺失跳过

用法:
    python scripts/run_blender_tests.py --blender <路径> [测试文件...]
    python scripts/run_blender_tests.py --blender <路径> --list
    python scripts/run_blender_tests.py --blender <路径> --allow-skip

    # 不传 --blender 时按「PATH 里找 blender」处理
    python scripts/run_blender_tests.py
"""

import argparse
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIP = 3

# 默认的 4 个无头测试（按「注册 -> 基础 -> 对抗 -> 原生 -> P0」排序）
DEFAULT_TESTS = [
    ("blender_smoke_test.py", "插件注册 + 冒烟"),
    ("test_p0_fixes.py", "P0 回归（数据安全 / 崩溃防护）"),
    ("qa_adversarial_test.py", "对抗性测试（边界与回归）"),
    ("test_native_equivalence.py", "原生内核等价性（无原生库时整体跳过）"),
]

# 汇总行形如「测试汇总: 35/35 通过, 0 失败」
# 中间可能夹不同的前缀（测试汇总 / 对抗性测试汇总 / P0 回归测试汇总 …），
# 故只匹配「X/Y 通过」这个稳定片段。
_SUMMARY_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*通过[,，]?\s*(\d+)?\s*失败")

# 汇总行候选必须是「整行」，不能是失败明细列表里的一项。
# 形如「  - 某用例  (进度 1/2 通过)」的行不是汇总行——
# 否则明细文字会把数字抢走当权威（QA-5 实测: 该行被判成了汇总）。
_SUMMARY_BULLET_RE = re.compile(r"^\s*[-*·]\s")

# 逐条断言行的格式。**必须与测试文件实际打印的格式对齐**——
# tests/ 下 4 个文件的 check() 都是 `print(f"[{status}] {name}")`，
# 即真正落地的格式是 `[FAIL] xxx`（带方括号）。
#
# 历史教训（QA-4 在验收中发现）: 本正则原先只匹配行首裸 `FAIL`，
# 而真实输出全是 `[FAIL] `，导致「以正文为准」那道兜底**从未生效**——
# 与本项目栽过的「CI 全绿但插件不可用」完全同形。
#
# QA-5 独立复核又发现 5 处残留逃逸（均已用**隔离用例**实测复现——
# 即「汇总全绿 + PASS 行数==total」时，只有失败标记能判红的那种场景）:
#   1) 大小写: `[fail]` / `[Fail]` 逃逸（有人把 status 改成小写即失效）
#   2) 行中位置: `INFO: [FAIL] x` / `Fra:1 [FAIL] x` 逃逸
#      —— Blender 会给自己的输出加 `Fra:N` 前缀，日志库也会加前缀
#   3) 符号标记: 只认✗ ×，不认 ✘
#   4) 行中`!!`: `2026-01-01 | ERROR | !! 严重` 逃逸（!! 只在行首才算）
#   5) 标记词表: `[ERROR]` 完全不在词表内，逃逸
# 故拆成「行首形态」+「行中形态」两条，按位或计数。
# 行首形态。**带括号的词标记**（`[FAIL]` / `【ERROR】` …）大小写不敏感；
# **裸词只保留大写 `FAIL` 且后跟空白/冒号**（测试协议词，整词边界）：
# 2026-10-07 首次真实 CI 实测，Linux 无头 Blender 会向 stderr 打印
# 「Failed to open dir (No such file or directory): /run/user/1001/gvfs/」
# 这类**自然语言警告**，旧的宽松写法（行首裸 FAIL/FAILED/ERROR、无边界、
# 忽略大小写）把它们全当成失败标记，三个全绿套件全部被误判为红。
# 裸词收窄到 `FAIL(?=[ \t:：])` 后：协议形态（C3「FAIL  某用例」）仍被兜住，
# 自然语言警告（Failed… / Error: not freed…）不再误伤。
# `!!` 与 ✗/✘/× 符号保持原语义（无括号也足够无歧义）。
_FAIL_HEAD_RE = re.compile(
    r"^[ \t]*(?:"
    r"!![ \t]*"
    r"|\[[ \t]*(?:FAIL|FAILED|ERROR|ERR|✗|✘|×)[ \t]*\]"
    r"|【[ \t]*(?:FAIL|FAILED|ERROR|ERR|✗|✘|×)[ \t]*】"
    r"|[✗✘×]"
    r"|FAIL(?=[ \t:：])"
    r")",
    re.MULTILINE | re.IGNORECASE)

# 行中形态: 带括号的标记（`[FAIL]` / `【FAIL】` / `(ERROR)` …），
# 或行中出现的 `!!`（日志前缀后的严重告警）。
# 括号限定是为了避免误伤「用例名里含 fail 字样」的 PASS 行
# （tests/ 下 4 个文件实测无此写法，此处仍保守处理）。
# 行中 `!!` 额外要求**左边界**（行首或空白/分隔符），
# 避免 `value!! x` 这类普通文本被当成失败标记。
_FAIL_INLINE_RE = re.compile(
    r"[\[\(（【][ \t]*(?:FAIL|FAILED|ERROR|ERR|✗|✘|×)[ \t]*[:：]?[ \t]*[\]\)）】]?"
    r"|(?:^|(?<=[\s|(\[]))!!(?=[ \t:：])",
    re.IGNORECASE | re.MULTILINE)

# 逐条 PASS 行（用于「条数对账」：PASS + FAIL 应与汇总的 total 相符）
_PASS_LINE_RE = re.compile(r"^[ \t]*\[[ \t]*PASS[ \t]*\]", re.MULTILINE
                           | re.IGNORECASE)

# 测试文件用来表示「注册失败，后续用例被静默跳过」的行。
# 该情形下汇总只会显示已跑的那几条（例如 2/2 通过），
# 看起来全绿，实际 6 个用例**根本没验证**——必须单独识别。
_BLIND_MARKERS = ("注册失败", "跳过后续用例")


def _parse_output(output):
    """
    从测试输出里判定成败。

    Returns:
        (status, detail)
        status ∈ {"pass", "fail", "skip"}
    """
    # 1) 显式跳过优先（如「原生库不可用」）
    if "跳过等价性测试" in output or "跳过原生" in output:
        reason = ""
        for line in output.splitlines():
            if line.strip().startswith("原因:"):
                reason = line.strip()[3:].strip()
                break
        return "skip", reason or "脚本自述跳过（通常是原生库不可用）"

    # 2) 汇总行：唯一的权威结论
    #    只认「整行都是汇总」的行—— 失败明细列表里的
    #    「  - 某用例  (进度 1/2 通过)」不是汇总行（QA-5 实测会抢数字）。
    summary_matches = [
        m for line in output.splitlines()
        if not _SUMMARY_BULLET_RE.match(line)
        for m in _SUMMARY_RE.findall(line)
    ]
    if not summary_matches:
        # 3) 找不到汇总 = 脚本没跑到汇总处（崩了/ 提前 return /
        #    Blender 启动失败）。这**不能**当通过——
        #    「没有失败的证据」不等于「成功的证据」。
        tail = [ln.strip() for ln in output.splitlines() if ln.strip()][-3:]
        return "fail", ("未找到测试汇总行，脚本可能异常终止。"
                        + (f" 末行: {' | '.join(tail)[:100]}" if tail else ""))

    passed, total, failed = summary_matches[-1]
    passed, total = int(passed), int(total)
    failed = int(failed) if failed else 0

    # 3b) 多个汇总行时，**任何一行**显示有失败都不放过。
    #QA-5 实测的逃逸: 3 行汇总里中间那行是 8/10 通过、2 失败，
    #     末行是 3/3 通过 —— 原实现只取末行，把中间的失败吞掉了。
    #     真实的分阶段测试文件完全可能长这样，故一律从严。
    for p_i, t_i, f_i in summary_matches:
        p_i, t_i = int(p_i), int(t_i)
        f_i = int(f_i) if f_i else 0
        if p_i > t_i:
            return "fail", (f"汇总自相矛盾: {p_i}/{t_i} 通过"
                            f"（通过数大于总数，汇总行本身不可信）")
        if f_i > 0 or p_i < t_i:
            return "fail", (f"存在不自洽的汇总行「{p_i}/{t_i} 通过, {f_i} 失败」"
                            f"（共 {len(summary_matches)} 行汇总，"
                            f"末行为 {passed}/{total}）—— 不放过任何一个红汇总")

    # 4) 兜底一：正文出现任何失败标记 -> 失败（以正文为准）
    #    覆盖「汇总数字写错/ 被篡改」的情形。
    #    注意: 失败标记的**格式**必须与测试文件实际打印的一致
    #    （是 `[FAIL] xxx` 而非裸 `FAIL`），否则这条兜底形同虚设。
    #行首形态 + 行中形态都要算（Blender 会给自己的行加前缀）。
    n_fail_lines = (len(_FAIL_HEAD_RE.findall(output))
                    + len(_FAIL_INLINE_RE.findall(output)))
    if n_fail_lines:
        return "fail", (f"汇总称 {passed}/{total} 通过、{failed} 失败，"
                        f"但正文有 {n_fail_lines} 条失败标记（以正文为准）")

    # 5) 兜底二：注册失败导致后续用例被**静默跳过**
    #    smoke 测试注册失败时会打印该行并跳过 6 个用例，
    #    此时汇总只显示已跑过的 2/2 通过 —— 看起来全绿，实则大面积未验证。
    for marker in _BLIND_MARKERS:
        if marker in output:
            return "fail", (f"检测到「{marker}」：部分用例被静默跳过，"
                            f"汇总 {passed}/{total} 只反映已执行的用例，"
                            f"不可视为全部通过")

    # 6) 兜底三：逐条 PASS 行数与汇总 total 对账
    #    测试文件里每条 check() 都会打印一行，理论上 PASS 行数应等于 total。
    #    对不上说明有断言没被执行到（或汇总被写错），一律判失败。
    #    这一条能抓住「汇总只统计了部分用例」这类最难察觉的假绿。
    #
    #    注意: 有些测试的 check() 会被循环调用（如参数化矩阵），
    #    此时 PASS 行数会**多于** total，故只在「少于」时告警——
    #    多出来的行数只是同一条断言被执行多次，不代表漏跑。
    n_pass_lines = len(_PASS_LINE_RE.findall(output))
    if n_pass_lines and n_pass_lines < total:
        return "fail", (f"对账不符：正文只有 {n_pass_lines} 条 [PASS] 行，"
                        f"但汇总称共 {total} 条 —— "
                        f"差 {total - n_pass_lines} 条未被验证")

    #6b) 兜底三之补: 汇总说 N条全过，正文却**一条断言行都没有**。
    #     这说明输出被截断（只剩汇总行）或断言根本没执行，
    #     属于「无法自证」—— 一律判失败，不接受「只有汇总行」的空壳。
    #     QA-5 实测: 改动前「测试汇总: 10/10 通过, 0 失败」孤零零一行会判pass。
    if total > 0 and n_pass_lines == 0 and n_fail_lines == 0:
        return "fail", (f"正文没有任何逐条断言行（[PASS]/[FAIL] 均为 0 条），"
                        f"但汇总称 {total} 条全通过 —— 输出可能被截断，"
                        f"无法自证这些断言真的跑过")

    # 7) 汇总自身的自洽性
    if passed > total:
        return "fail", (f"汇总自相矛盾: {passed}/{total} 通过"
                        f"（通过数大于总数，汇总行本身不可信）")
    if failed > 0 or passed < total:
        return "fail", f"{passed}/{total} 通过，{failed} 失败"
    if total == 0:
        return "fail", "汇总为 0/0 —— 一条断言都没跑，不能算通过"
    return "pass", f"{passed}/{total} 通过"


def _find_blender(explicit):
    """定位 Blender 可执行文件；找不到返回 None。"""
    if explicit:
        return explicit
    exe = "blender.exe" if os.name == "nt" else "blender"
    from shutil import which
    found = which(exe) or which("blender")
    if found:
        return found
    # 常见安装位置兜底
    cands = []
    if os.name == "nt":
        for base in (r"C:\Program Files\Blender Foundation",
                     os.path.expandvars(r"%LOCALAPPDATA%\Programs")):
            if os.path.isdir(base):
                for root, dirs, _ in os.walk(base):
                    for d in list(dirs):
                        if d.lower().startswith("blender"):
                            cands.append(os.path.join(root, d, exe))
                    break
    else:
        cands = ["/usr/bin/blender", "/usr/local/bin/blender",
                 "/snap/bin/blender", os.path.expanduser("~/blender/blender")]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def run_one(blender, script, timeout=900, verbose=False):
    """跑一个测试文件，返回 (status, detail, elapsed)。"""
    path = os.path.join(TESTS, script)
    if not os.path.isfile(path):
        return "fail", f"测试文件不存在: {script}", 0.0
    argv = [blender, "--background", "--factory-startup",
            "--python-exit-code", "1", "--python", path]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, cwd=ROOT, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "fail", f"超时（>{timeout}s）", time.perf_counter() - t0
    except Exception as exc:  # noqa: BLE001
        return "fail", f"{type(exc).__name__}: {exc}", time.perf_counter() - t0
    elapsed = time.perf_counter() - t0
    output = (proc.stdout or "") + (proc.stderr or "")
    status, detail = _parse_output(output)
    if verbose:
        print(f"\n----- {script} 完整输出 -----")
        print(output.rstrip())
        print(f"----- 结束（Blender 退出码 {proc.returncode}，判定 {status}）-----\n")
    return status, detail, elapsed


def main():
    ap = argparse.ArgumentParser(
        description="Blender 无头测试执行器（强制把断言结果转成退出码）")
    ap.add_argument("tests", nargs="*", help="要跑的文件名（默认全部 4 个）")
    ap.add_argument("--blender", help="Blender 可执行文件路径")
    ap.add_argument("--timeout", type=int, default=900, help="单文件超时秒数")
    ap.add_argument("--verbose", action="store_true", help="打印完整输出")
    ap.add_argument("--allow-skip", action="store_true",
                    help="允许「跳过」不算失败（默认：跳过即失败）")
    ap.add_argument("--list", action="store_true", help="只列出将执行的文件")
    args = ap.parse_args()

    if args.list:
        for s, d in DEFAULT_TESTS:
            print(f"  {s:<30} {d}")
        return 0

    items = ([(os.path.basename(t), "") for t in args.tests]
             if args.tests else DEFAULT_TESTS)

    print("=" * 70)
    print("Blender 无头测试（强制退出码）")
    print("=" * 70)
    blender = _find_blender(args.blender)
    if not blender:
        print("!! 找不到 Blender 可执行文件。")
        print("   本地请用 --blender <路径> 指定，")
        print("   或设置环境变量 BLENDER_EXE。")
        print("   注意: 这些用例覆盖 bpy 交互路径，**无法用替身替代**，")
        print("   所以本项不能降级为「跳过」——它必须真的跑。")
        return EXIT_FAIL
    print(f"Blender: {blender}")

    try:
        ver = subprocess.run([blender, "--version"], capture_output=True,
                             text=True, timeout=120,
                             encoding="utf-8", errors="replace")
        print(f"版本: {(ver.stdout or ver.stderr).strip().splitlines()[0]}")
    except Exception as exc:  # noqa: BLE001
        print(f"!! 无法执行 Blender --version: {type(exc).__name__}: {exc}")
        return EXIT_FAIL

    print("=" * 70)
    rows = []
    for script, desc in items:
        print(f"\n>>> {script}  {desc}")
        status, detail, elapsed = run_one(
            blender, script, timeout=args.timeout, verbose=args.verbose)
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[status]
        print(f"    {mark}  ({elapsed:.1f}s)  {detail}")
        rows.append((script, status, detail, elapsed))

    npass = sum(1 for _, s, _, _ in rows if s == "pass")
    nskip = sum(1 for _, s, _, _ in rows if s == "skip")
    nfail = sum(1 for _, s, _, _ in rows if s == "fail")

    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    for script, status, detail, elapsed in rows:
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[status]
        print(f"  {mark}  {script:<30} {elapsed:6.1f}s   {detail}")
    print("-" * 70)
    print(f"  {npass}/{len(rows)} 通过"
          + (f"，{nfail} 失败" if nfail else "")
          + (f"，{nskip} 跳过（**未覆盖**）" if nskip else ""))
    if nskip:
        print()
        print("  ⚠ 跳过的文件**没有被验证**，其覆盖的路径本次是盲区：")
        for script, status, detail, elapsed in rows:
            if status == "skip":
                print(f"      - {script}: {detail}")
    print("=" * 70)

    if nfail:
        return EXIT_FAIL
    if nskip and not args.allow_skip:
        print("\n失败原因：有文件被跳过而未加 --allow-skip"
              "（跳过 = 未验证，不等于通过）")
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
