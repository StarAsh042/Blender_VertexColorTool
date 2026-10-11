#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
QA 独立验收脚本（software-qa-engineer-4 临时产物，验收后删除）

用途: 用**合成输出**验证 scripts/run_blender_tests.py 的 _parse_output 判定逻辑。
本机没有 Blender，无法真跑；判定逻辑是防「假绿」的关键关卡，
故必须对其做逐条对抗性验证。

用法: python scripts/qa_verify_blender_judge.py
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "rbt", os.path.join(ROOT, "scripts", "run_blender_tests.py"))
_rbt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rbt)
parse = _rbt._parse_output

# (用例名, 合成输出, 期望status, 期望detail关键词或None)
CASES = [
    ("C1 正常全通过（PASS 行数与汇总 total 自洽）",
     "".join(f"[PASS] 用例{i}\n" for i in range(35))
     + "测试汇总: 35/35 通过, 0 失败\n",
     "pass", None),

    ("C2 汇总有失败数",
     "[FAIL] 某用例\n测试汇总: 30/35 通过, 5 失败\n",
     "fail", "30/35"),

    ("C3 汇总称0失败但正文有 FAIL 行（真·FAIL 前缀）",
     "FAIL  某用例\n测试汇总: 35/35 通过, 0 失败\n",
     "fail", "以正文为准"),

    ("C3b 汇总称0失败但正文是 [FAIL] 前缀（真实测试文件的格式!）",
     "[FAIL] 某用例  -- detail\n测试汇总: 35/35 通过, 0 失败\n",
     "fail", "以正文为准"),

    ("C4 0/0 通过（一条都没跑）",
     "测试汇总: 0/0 通过, 0 失败\n",
     "fail", "一条断言都没跑"),

    ("C5 崩溃无汇总行",
     "Traceback (most recent call last):\n  File \"x.py\", line 1\nRuntimeError: boom\n",
     "fail", "未找到测试汇总行"),

    ("C6 Blender 启动失败，输出为空",
     "",
     "fail", "未找到测试汇总行"),

    ("C7 原生库不可用（自述跳过）",
     "跳过原生等价性测试\n原因: 未找到原生库vct_native\n",
     "skip", "未找到原生库"),

    ("C8 !! 前缀的 FAIL 行",
     "!! 某处失败\n测试汇总: 10/10 通过, 0 失败\n",
     "fail", "以正文为准"),

    ("C9 passed<total 但失败数写 0（汇总本身写错）",
     "测试汇总: 34/35 通过, 0 失败\n",
     "fail", "34/35"),

    ("C10 passed>total（汇总自相矛盾，应判 fail）",
     "测试汇总: 40/35 通过, 0 失败\n",
     "fail", "自相矛盾"),

    ("C11 注册失败 -> 后续用例被跳过，只跑了2条却全过",
     "[PASS] 插件注册\n!! 注册失败，跳过后续用例\n"
     "[PASS] 注销后 Scene 属性被清理\n测试汇总: 2/2 通过, 0 失败\n",
     "fail", "失败标记"),  # 注册失败导致后续用例被静默跳过

    # QA-5 更新: 本例原为「只有汇总行、无正文」，期望 pass。
    # 但那其实是「输出被截断成只剩汇总」的形态 —— 汇总声称35 条断言跑过,
    # 正文却一条 [PASS] 都没有, 无法自证。run_blender_tests.py 已加
    # 兜底六之补（空壳检测）判fail，故期望改为 fail。
    # 真正「35 条全绿」请用 C1（含 35 条 [PASS] 正文）。
    ("C12 中文逗号汇总（只有汇总行、无正文= 截断空壳, 应判 fail）",
     "测试汇总: 35/35 通过，0 失败\n",
     "fail", "截断"),

    ("C12b 中文逗号汇总 + 完整 35 条正文（应判 pass）",
     "".join(f"[PASS] 用例{i}\n" for i in range(35))
     + "测试汇总: 35/35 通过，0 失败\n",
     "pass", None),

    ("C13 多个汇总行，取最后一个",
     "阶段汇总: 5/5 通过, 0 失败\n测试汇总: 30/35 通过, 5 失败\n",
     "fail", "30/35"),
]

# P0 级专项: 插件注册失败导致 6 个用例静默跳过 —— 汇总却显示全通过
REGISTRATION_BLIND = (
    "[PASS] 插件注册\n"
    "!! 注册失败，跳过后续用例\n"
    "[PASS] 注销后 Scene 属性被清理\n"
    "测试汇总: 2/2 通过, 0 失败\n"
)

# 真正触发「插件 100% 不可用」的形态: 全红但汇总数字被写成全过
ALL_RED_BUT_SUMMARY_GREEN = (
    "[FAIL] P0-1 聚类匹配不崩溃  -- 抛出异常: AttributeError\n"
    "[FAIL] P0-2 颜色复制正确性  -- 抛出异常: AttributeError\n"
    "[FAIL] 非聚类匹配  -- 抛出异常: AttributeError\n"
    "测试汇总: 3/3 通过, 0 失败\n"
)


def main():
    print("=" * 78)
    print("QA 独立验收：run_blender_tests._parse_output 判定逻辑对抗性验证")
    print("=" * 78)

    npass = nfail = 0
    problems = []
    for name, out, want, want_kw in CASES:
        got, detail = parse(out)
        kw_ok = True
        if want_kw:
            kw_ok = want_kw in detail
        ok = (got == want) and kw_ok
        flag = "OK  " if ok else "BAD "
        if ok:
            npass += 1
        else:
            nfail += 1
            problems.append((name, want, want_kw, got, detail))
        print(f"[{flag}] {name}")
        print(f"        期望={want}" + (f" (含'{want_kw}')" if want_kw else "")
              + f"  实际={got}  detail={detail[:70]}")

    print("\n" + "=" * 78)
    print("P0 专项：历史创伤复现场景")
    print("=" * 78)
    for nm, out in [("注册失败+6 用例静默跳过", REGISTRATION_BLIND),
                    ("全红但汇总被写成全过", ALL_RED_BUT_SUMMARY_GREEN)]:
        got, detail = parse(out)
        verdict = "正确(判FAIL)" if got == "fail" else "!! 假绿(判PASS) !!"
        print(f"  {nm}\n    -> status={got}  {verdict}")
        print(f"    detail={detail[:90]}")
        if got != "fail":
            nfail += 1
            problems.append((nm, "fail", None, got, detail))
        else:
            npass += 1

    print("\n" + "=" * 78)
    print(f"合计: {npass} 符合预期, {nfail} 不符合预期")
    if problems:
        print("\n!!! 不符合预期的用例（判定逻辑漏洞）:")
        for nm, want, kw, got, detail in problems:
            print(f"  · {nm}")
            print(f"      期望 {want}{'/'+kw if kw else ''} -> 实际 {got}: {detail[:80]}")
    print("=" * 78)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
