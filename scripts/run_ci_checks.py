#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
CI 快速检查统一入口（无需 Blender）

用途:
    把「语法检查 + 5 个核心逻辑验证脚本 + 真 numpy 通道」串成一个命令，
    供 GitHub Actions Job 1 使用。这些检查全部不依赖 bpy，
    因此在 CI 上秒级完成，无需安装 Blender。

检查项（任一失败即整体失败）:
    1. 语法检查       python -m compileall（全部源码）
    2. P0 断言可证伪性 verify_p0_assertions.py
    3. 聚类等价性     verify_cluster_equivalence.py
    4. 匹配等价性     verify_match_equivalence.py
    5. 法线约束       verify_normal_constraint.py
    6. 取色距离上限   verify_pick_distance.py
    7. 真 numpy 通道  verify_normal_constraint.py --real-numpy  ★需本机装有 numpy

各脚本的退出码约定:
    0  = 全部通过
    非0 = 有断言失败或脚本内部错误
    3  = **因环境缺失而跳过**（仅 --real-numpy 会用到）

★关于退出码 3（本项目最核心的诚实性要求）:
    「跳过」**绝不等于**「通过」。上一轮的教训是
    「CI 5/5 全绿，但插件在默认设置下 100% 不可用」，
    根因是测试替身比真实环境更宽容、把唯一会崩的组合消掉了。
    因此本脚本把「跳过」单列为一个状态：
      - 默认（--require-numpy）：退出码 3 直接算**失败**，
        因为 CI 装了 numpy 却没跑成数值路径，属于环境/接线出了问题，
        必须暴露而不是静默放过；
      - 加 --allow-skip：允许降级为「跳过」，但汇总里会显式标注，
        且**不计入通过数**——让「6/7 通过 + 1 项跳过」，
        而不是伪装成 7/7。

设计说明:
    之所以能在无 Blender 环境运行，是因为验证脚本用
    「按文件路径加载源码 + 注入 bpy/mathutils 替身」的方式绕开
    包的 __init__.py（后者会 import bpy）。
    第 6 项刻意**不注入 numpy 替身**（load_ops(real_numpy=True)），
    以便真正执行 core/ 的向量化数值路径。

    Job 2（需 Blender 的 4 个无头测试文件）由 QA 独立维护，不在本脚本范围内。

用法:
    python scripts/run_ci_checks.py
    python scripts/run_ci_checks.py --verbose        # 打印各脚本完整输出
    python scripts/run_ci_checks.py --allow-skip     # 允许无 numpy 时降级为跳过
"""

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))

# 需要语法检查的源码目录与文件
COMPILE_TARGETS = [
    "__init__.py",
    "core",
    "operators",
    "properties",
    "ui",
    "utils",
    "tests",
    "scripts",
]

# 验证脚本：(显示名, 相对路径, 一句话说明)
VERIFY_SCRIPTS = [
    ("P0 断言可证伪性", "verify_p0_assertions.py",
     "确认 P0 修复的每条断言都能被变异打红（防恒真断言）"),
    ("聚类等价性", "verify_cluster_equivalence.py",
     "900 场景：聚类优化前后分组结果必须完全一致（含距离/相似度阈值边界）"),
    ("匹配等价性", "verify_match_equivalence.py",
     "第3步改走原生内核后，匹配结果与原实现一致（含并列最高分）"),
    ("法线约束", "verify_normal_constraint.py",
     "薄壁跨面取色被修正，且非薄壁场景结果不变"),
    ("取色距离上限", "verify_pick_distance.py",
     "超出源范围的顶点不被「拉色」；0=关闭时与该功能上线前逐字一致"),
]

#判定逻辑自身的验收（不需要Blender / numpy，纯字符串解析）。
#
# 为什么必须进 CI: `run_blender_tests.py` 的 `_parse_output` 是
# 「tests/ 4个文件只print 不设退出码」这一事实的**唯一防线**
# —— 它坏了，Blender Job 就会变成永远绿色的空转，而这与本项目
# 栽过的「CI 全绿但插件 100% 不可用」是同一个形状。
# 一个「守门器」本身必须被验证，否则它坏了没人知道。
JUDGE_STEPS = [
    ("判定逻辑验收（基础）", "qa_verify_blender_judge.py",
     "用合成输出验证 _parse_output 的判定：失败标记 / 汇总矛盾 / 静默跳过"),
    ("判定逻辑验收（对抗）", "qa5_verify_judge_adversarial.py",
     "45 条边界用例（含 stderr 噪音回归）+ 6 个变异体：确认每道兜底都真的被测到"),
]

# 与 verify_normal_constraint.py 约定的「环境缺失跳过」退出码。
# 刻意不用 0：0 在 CI 里是绿色，必须能把它和「真的跑过了」区分开。
EXIT_SKIP = 3

# 需要真实 numpy 的检查项：(显示名, 脚本, 参数, 一句话说明)
NUMPY_STEPS = [
    ("真 numpy 通道", "verify_normal_constraint.py", ["--real-numpy"],
     "在真numpy 下执行 _extract_normals/_transform_normals_array/"
     "_normal_dot 的 ndarray 分支（替身路径原理上跑不到这些代码）"),
]


class CheckResult:
    """
    单个检查项的结果。

    三态而非两态: PASS / FAIL / **SKIP**
    「跳过」必须能与「通过」区分开——否则没装 numpy 时
    「数值路径零覆盖」会被记成「通过」，正是本项目上一轮
    栽过的坑（CI 全绿≠ 覆盖到了）。
    """

    def __init__(self, name, ok, elapsed, detail="", skipped=False):
        self.name = name
        self.ok = ok
        self.elapsed = elapsed
        self.detail = detail
        self.skipped = skipped

    @property
    def status(self):
        if self.skipped:
            return "SKIP"
        return "PASS" if self.ok else "FAIL"


def run_step(name, argv, cwd, verbose=False, timeout=600):
    """
    执行一个检查步骤。

    Args:
        name: 显示名
        argv: 命令行参数列表
        cwd: 工作目录
        verbose: 是否打印完整输出
        timeout: 超时秒数

    Returns:
        CheckResult
    """
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        elapsed = time.perf_counter() - t0
        output = (proc.stdout or "") + (proc.stderr or "")
        # 退出码 3 = 环境缺失导致跳过，**不算通过也不算失败**，
        # 由调用方决定是降级为 SKIP 还是升级为 FAIL。
        skipped = proc.returncode == EXIT_SKIP
        ok = proc.returncode == 0
        detail = ""
        if skipped:
            # 从脚本输出里摘出「为什么跳过」，让日志能自解释
            reason = next((ln.strip() for ln in output.splitlines()
                           if "跳过" in ln), "")
            detail = f"环境缺失，跳过。{reason}"[:110]
        elif not ok:
            # 只摘取失败行，避免刷屏
            fails = [ln.strip() for ln in output.splitlines()
                     if ln.strip().startswith(("FAIL", "Traceback", "Error"))
                     or "失败" in ln]
            detail = (fails[0][:90] if fails
                      else f"退出码 {proc.returncode}")
        if verbose:
            print(f"\n----- {name} 完整输出 -----")
            print(output.rstrip())
            print(f"----- 结束（退出码 {proc.returncode}）-----\n")
        return CheckResult(name, ok, elapsed, detail, skipped=skipped)
    except subprocess.TimeoutExpired:
        return CheckResult(name, False, time.perf_counter() - t0,
                           f"超时（>{timeout}s）")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, False, time.perf_counter() - t0,
                           f"{type(exc).__name__}: {exc}")


def main():
    ap = argparse.ArgumentParser(
        description="CI 快速检查（无需 Blender）")
    ap.add_argument("--verbose", action="store_true",
                    help="打印各检查项的完整输出")
    ap.add_argument("--timeout", type=int, default=600,
                    help="单个检查项的超时秒数（默认 600）")
    ap.add_argument("--allow-skip", action="store_true",
                    help="本机无 numpy 时，把真 numpy 通道降级为「跳过」"
                         "而不是失败（默认：视为失败）")
    ap.add_argument("--no-numpy-check", action="store_true",
                    help="完全不跑真 numpy 通道（不推荐；仅供调试对比）")
    args = ap.parse_args()

    total_steps = (1 + len(VERIFY_SCRIPTS) + len(JUDGE_STEPS)
                   + (0 if args.no_numpy_check else len(NUMPY_STEPS)))
    last = total_steps

    print("=" * 70)
    print("CI 快速检查（无需 Blender）")
    print(f"仓库: {ROOT}")
    print(f"Python: {sys.version.split()[0]}")
    print("=" * 70)

    results = []

    # --- 0. 环境能力声明（避免「全绿」看起来覆盖了实际没覆盖的东西）---
    print(f"\n[0/{last}] 环境与覆盖声明...", flush=True)
    np_version = _detect_numpy_version()
    print("      法线约束有**两条**独立通道，二者都跑、且不可互相代替：")
    print("        通道 A（替身路径，无 numpy 也能跑）：")
    print("          numpy 替身**行为对齐真 numpy 的关键语义**，包括：")
    print("            · 对多于 1 元素的数组求真值**抛 ValueError**")
    print("              （这正是上一轮 P0 的根因，替身已能复现并守住它）")
    print("            · 法线以 (N,3) 形态参与，而非 list[Vector]")
    print("          它覆盖的是「无 numpy 机器 / 精简环境」下的行为。")
    if np_version:
        print("        通道 B（真 numpy 通道，本流程会真正执行）：")
        print(f"          检测到 numpy {np_version} —— 将以真 numpy 装载被测模块")
        print("          （load_ops(real_numpy=True)，不注入替身），执行 core/ 里的")
        print("            · _extract_normals / _transform_normals_array /")
        print("              _normal_dot 的 **ndarray 分支**")
        print("            · float64 数值一致性（向量化 vs 逐点，误差 < 1e-12）")
        print("          必要性: 替身没有 .dot、也不是真 float64，")
        print("          这些代码在通道 A 下**一行都不会执行**。")
    else:
        print("        通道 B（真 numpy 通道）：**未检测到 numpy**。")
        if args.no_numpy_check:
            print("          已用 --no-numpy-check 跳过 -> 数值路径本次**零覆盖**。")
        elif args.allow_skip:
            print("          已用 --allow-skip 允许降级 -> 记为「跳过」，")
            print("          **不计入通过数**（不会伪装成全绿）。")
        else:
            print("          未加 --allow-skip -> 本项计为**失败**。")
            print("          理由: 数值路径是本项目最易「假绿」的一段，")
            print("          静默放过就等于回到「CI 全绿但插件不可用」的老路。")
            print("          本地没装 numpy?  pip install numpy")
    print("      Blender 相关用例由 Job 2 覆盖（本入口不含）。")
    print("      已知未覆盖: 源物体**变换**后缓存是否失效（键不含 matrix_world，")
    print("                  见 docs/LIMITATIONS.md 第 12 条）；")
    print("                  UI 交互与性能回归（需 Blender + 人工）。")

    # --- 1. 语法检查 ---
    print(f"\n[1/{last}] 语法检查 (compileall)...", flush=True)
    r = run_step(
        "语法检查",
        [sys.executable, "-m", "compileall", "-q"] + COMPILE_TARGETS,
        cwd=ROOT, verbose=args.verbose, timeout=args.timeout,
    )
    results.append(r)
    print(f"      {r.status}  ({r.elapsed:.1f}s)")

    # --- 2..N. 验证脚本 ---
    for idx, (name, script, desc) in enumerate(VERIFY_SCRIPTS, start=2):
        print(f"\n[{idx}/{last}] {name} - {desc}...", flush=True)
        script_path = os.path.join(SCRIPTS, script)
        if not os.path.isfile(script_path):
            r = CheckResult(name, False, 0.0, f"脚本不存在: {script}")
        else:
            r = run_step(
                name,
                [sys.executable, os.path.abspath(script_path)],
                cwd=SCRIPTS, verbose=args.verbose, timeout=args.timeout,
            )
        results.append(r)
        print(f"      {r.status}  ({r.elapsed:.1f}s)"
              + (f"  {r.detail}" if r.detail else ""))

    # --- 判定逻辑自身的验收（不需要 Blender / numpy）---
    for j, (name, script, desc) in enumerate(JUDGE_STEPS):
        idx = 2 + len(VERIFY_SCRIPTS) + j
        print(f"\n[{idx}/{last}] {name} - {desc}...", flush=True)
        script_path = os.path.join(SCRIPTS, script)
        if not os.path.isfile(script_path):
            r = CheckResult(name, False, 0.0, f"脚本不存在: {script}")
        else:
            r = run_step(
                name,
                [sys.executable, os.path.abspath(script_path)],
                cwd=SCRIPTS, verbose=args.verbose, timeout=args.timeout,
            )
        results.append(r)
        print(f"      {r.status}  ({r.elapsed:.1f}s)"
              + (f"  {r.detail}" if r.detail else ""))

    # --- 最后. 真 numpy 通道（需本机装有 numpy） ---
    if not args.no_numpy_check:
        base = 1 + len(VERIFY_SCRIPTS) + len(JUDGE_STEPS)
        for k, (name, script, argv_extra, desc) in enumerate(NUMPY_STEPS):
            idx = base + k + 1
            print(f"\n[{idx}/{last}] {name} - {desc}...", flush=True)
            script_path = os.path.join(SCRIPTS, script)
            if not os.path.isfile(script_path):
                r = CheckResult(name, False, 0.0, f"脚本不存在: {script}")
            else:
                r = run_step(
                    name,
                    [sys.executable, os.path.abspath(script_path)]
                    + list(argv_extra),
                    cwd=SCRIPTS, verbose=args.verbose,
                    timeout=args.timeout,
                )
                # 退出码 3 = 环境缺失。按 --allow-skip 决定记 SKIP 还是 FAIL。
                if r.skipped and not args.allow_skip:
                    r = CheckResult(
                        name, False, r.elapsed,
                        f"本机无 numpy，数值路径未验证"
                        f"（加 --allow-skip 可降级为跳过，"
                        f"但那等于承认本次零覆盖）", skipped=False)
            results.append(r)
            print(f"      {r.status}  ({r.elapsed:.1f}s)"
                  + (f"  {r.detail}" if r.detail else ""))

    # --- 汇总 ---
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    skipped = sum(1 for r in results if r.skipped)
    failed = sum(1 for r in results if not r.ok and not r.skipped)

    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    for r in results:
        line = f"  {r.status}  {r.name:<18} {r.elapsed:6.1f}s"
        if r.detail:
            line += f"   {r.detail}"
        print(line)
    print("-" * 70)
    summary = f"  {passed}/{total} 通过"
    if failed:
        summary += f"，{failed} 失败"
    if skipped:
        summary += f"，{skipped} 跳过（**未覆盖，不计入通过**）"
    if not failed and not skipped:
        summary += "，全部通过"
    print(summary)
    total_time = sum(r.elapsed for r in results)
    print(f"  总耗时 {total_time:.1f}s")
    if skipped:
        print()
        print("  ⚠ 本次有检查项因环境缺失被**跳过**。以下部分并未验证：")
        for r in results:
            if r.skipped:
                print(f"      - {r.name}：{r.detail}")
    print("=" * 70)

    if failed:
        print("\n失败的检查项：")
        for r in results:
            if not r.ok and not r.skipped:
                print(f"  - {r.name}: {r.detail}")
        print("\n提示: 加 --verbose 查看完整输出")
    return 0 if failed == 0 else 1


def _detect_numpy_version():
    """
    返回本机**真实 numpy** 的版本号；没有则返回 None。

    必须能区分「真numpy」与「同名替身」：判据用 `__version__`，
    因为替身（verify_normal_constraint.py 注入的那个）刻意不带它。
    历史上正是这个遮蔽问题让 --real-numpy 分支崩溃过。
    """
    import importlib
    try:
        mod = importlib.import_module("numpy")
    except ImportError:
        return None
    return getattr(mod, "__version__", None)


if __name__ == "__main__":
    sys.exit(main())
