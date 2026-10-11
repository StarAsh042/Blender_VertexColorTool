#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
QA-5 独立验收：run_blender_tests._parse_output 对抗性验证
（严过关 software-qa-engineer-5 / 2026）

与 qa_verify_blender_judge.py 的区别:
    · 用例**完全自己构造**，不复用 QA-4 用过的任何一条
    · 重点打**边界**: 标点混用/ 行中 FAIL / 大小写 / 多个汇总 /
      截断输出 / CRLF / 汇总行变体 / 正文缺失
    · 额外做**变异注入**: 改判定逻辑看能否抓住（防止"测试与实现同源"）

用法: python scripts/qa5_verify_judge_adversarial.py
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "rbt5", os.path.join(ROOT, "scripts", "run_blender_tests.py"))
_rbt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rbt)
parse = _rbt._parse_output

OK = "[PASS] "
SUM = "测试汇总: {p}/{t} 通过, {f} 失败\n"


def body(n_pass, n_fail):
    """构造 n_pass 条 PASS + n_fail 条 FAIL 的正文。"""
    out = []
    for i in range(n_pass):
        out.append(f"{OK}用例{i}\n")
    for i in range(n_fail):
        out.append(f"[FAIL] 用例X{i}  -- 抛出异常: AttributeError\n")
    return "".join(out)


# (用例名, 合成输出, 期望 status, 期望 detail 关键词或 None)
CASES = [
    # ---------- A. 大小写 / 空格变体（QA-4 完全没测） ----------
    ("A1 [fail] 全小写 + 汇总全绿（应判 FAIL）",
     "[fail] 某用例\n" + SUM.format(p=2, t=2, f=0), "fail", "正文"),
    ("A2 [Fail] 首字母大写 + 汇总全绿（应判 FAIL）",
     "[Fail] 某用例\n" + SUM.format(p=2, t=2, f=0), "fail", "正文"),
    ("A3 [FAIL ] 括号内带空格 + 汇总全绿（应判 FAIL）",
     "[FAIL ] 某用例\n" + SUM.format(p=2, t=2, f=0), "fail", "正文"),
    ("A4 [  FAIL  ] 两侧空格（应判 FAIL）",
     "[  FAIL  ] 某用例\n" + SUM.format(p=2, t=2, f=0), "fail", "正文"),
    ("A5 ✗ 符号前缀 + 汇总全绿（应判 FAIL）",
     "✗ 某用例\n" + SUM.format(p=2, t=2, f=0), "fail", "正文"),
    ("A6 × 乘号前缀 + 汇总全绿（应判 FAIL）",
     "× 某用例\n" + SUM.format(p=2, t=2, f=0), "fail", "正文"),

    # ---------- B. 行中 FAIL（非行首，带日志前缀） ----------
    ("B1 'INFO: [FAIL] x' 前缀在行中（应判 FAIL）",
     "INFO: [FAIL] 某用例\n" + SUM.format(p=2, t=2, f=0), "fail", "正文"),
    ("B2 '2026-01-01 [FAIL] x' 时间戳前缀（应判 FAIL）",
     "2026-01-01 12:00:00 [FAIL] 某用例\n" + SUM.format(p=2, t=2, f=0),
     "fail", "正文"),
    ("B3 缩进 8 空格 + [FAIL]（应判 FAIL）",
     "        [FAIL] 某用例\n" + SUM.format(p=2, t=2, f=0), "fail", "正文"),

    # ---------- C. 标点混用 ----------
    ("C1 全角逗号 + 全角冒号汇总（应 pass）",
     body(3, 0) + "测试汇总：3/3 通过，0 失败\n", "pass", None),
    ("C2 汇总无逗号无空格 '3/3通过0失败'（应 pass）",
     body(3, 0) + "测试汇总: 3/3通过0失败\n", "pass", None),
    ("C3 汇总斜杠两侧有空格 '3 / 3 通过'（应 pass）",
     body(3, 0) + "测试汇总: 3 / 3 通过, 0 失败\n", "pass", None),
    ("C4 失败数与通过数矛盾 1/3通过+9失败（应 fail）",
     body(2, 1) + "测试汇总: 1/3 通过, 9 失败\n", "fail", None),

    # ---------- D. 多个汇总行----------
    ("D1 三个汇总行, 末个全绿但中间有失败数（应 fail）",
     body(3, 0) + "阶段汇总: 3/3 通过, 0 失败\n"
     "子阶段汇总: 8/10 通过, 2 失败\n"
     + SUM.format(p=3, t=3, f=0), "fail", "不自洽"),
    ("D2 前面的红汇总 + 末行全绿（从严, 应判 fail）",
     body(3, 0) + "阶段汇总: 8/10 通过, 2 失败\n"
     + SUM.format(p=3, t=3, f=0), "fail", "不自洽"),
    ("D3 失败项清单里含'x/y 通过'字样（汇总行被抢走, 应 fail）",
     body(2, 1) + "测试汇总: 2/3 通过, 1 失败\n"
     "失败项:\n  - 某用例  (进度 1/2 通过)\n", "fail", None),

    # ---------- E. 汇总行格式变体（4 个文件的真实变体） ----------
    ("E1 对抗性测试汇总（应 pass）",
     body(34, 0) + "对抗性测试汇总: 34/34 通过, 0 失败\n", "pass", None),
    ("E2 P0 回归测试汇总（应 pass）",
     body(70, 0) + "P0 回归测试汇总: 70/70 通过, 0 失败\n", "pass", None),
    ("E3 等价性测试汇总（应 pass）",
     body(17, 0) + "等价性测试汇总: 17/17 通过, 0 失败\n", "pass", None),
    ("E4 汇总行只有'x/y 通过'没有'失败'二字（判 fail, 不放过）",
     body(3, 0) + "测试汇总: 3/3 通过\n", "fail", "未找到测试汇总行"),

    # ---------- F. 截断 / 正文缺失（最危险的假绿形态） ----------
    ("F1 只有汇总行, 正文全无（截断空壳, 应判 fail）",
     SUM.format(p=10, t=10, f=0), "fail", "截断"),
    ("F2 空输出（应 fail）",
     "", "fail", "未找到测试汇总行"),
    ("F3 只有进度条, 没到汇总（应 fail）",
     "运行中...\n50%\n100%\n", "fail", "未找到测试汇总行"),
    ("F4 崩溃在汇总之前（应 fail）",
     "[PASS] a\nTraceback (most recent call last):\n  File \"x\", line 1\n"
     "AttributeError: boom\n", "fail", "未找到测试汇总行"),

    # ---------- G. CRLF ----------
    ("G1 Windows CRLF 全绿正文（应 pass）",
     body(3, 0).replace("\n", "\r\n") + "测试汇总: 3/3 通过, 0 失败\r\n",
     "pass", None),
    ("G2 Windows CRLF 全红正文（应 fail）",
     body(2, 2).replace("\n", "\r\n") + "测试汇总: 4/4 通过, 0 失败\r\n",
     "fail", "正文"),

    # ---------- H. 汇总自相矛盾 ----------
    # 注意: 修复后本例会先被「3b 任何红汇总都不放过」拦下（同样判 fail），
    # 故关键词用 "40/35" —— 两条路径的 detail 都含它。
    ("H1 passed 远大于 total（应 fail）",
     SUM.format(p=40, t=35, f=0), "fail", "40/35"),
    ("H2 passed == total 但失败数 > 0（应 fail）",
     SUM.format(p=35, t=35, f=3), "fail", None),
    ("H3 0/0（应 fail）",
     SUM.format(p=0, t=0, f=0), "fail", "一条断言都没跑"),

    # ---------- I. 静默跳过 / 注册失败 ----------
    ("I1注册失败跳过, 汇总 2/2（应 fail）",
     "[PASS] 插件注册\n!! 注册失败，跳过后续用例\n"
     "[PASS] 注销后 Scene 属性被清理\n" + SUM.format(p=2, t=2, f=0),
     "fail", None),
    ("I2 只有'跳过后续用例'字样、无 !! 前缀（应 fail）",
     body(2, 0) + "注册失败，跳过后续用例\n" + SUM.format(p=2, t=2, f=0),
     "fail", "静默跳过"),
    ("I3 正文缺失 + 注册失败（应 fail）",
     "注册失败\n" + SUM.format(p=2, t=2, f=0), "fail", "静默跳过"),

    # ---------- J. 对账 ----------
    ("J1 正文 5 条 PASS 但汇总称 35 条（应 fail）",
     body(5, 0) + SUM.format(p=35, t=35, f=0), "fail", "对账不符"),
    ("J2 正文多于汇总（循环参数化, 应 pass）",
     body(40, 0) + SUM.format(p=17, t=17, f=0), "pass", None),

    # ---------- K. 跳过 ----------
    ("K1 原生库不可用（应 skip）",
     "跳过等价性测试：原生库不可用\n原因: 未找到 vct_native\n", "skip", "vct_native"),
    ("K2 跳过原生（应 skip）",
     "跳过原生等价性测试\n原因: DLL 加载失败\n", "skip", "DLL"),

    # ---------- L. stderr 噪音（2026-10-07 首次 CI 误报回归） ----------
    # Linux 无头 Blender 会向输出里混入自然语言警告，
    # 判定层的失败标记只认「测试文件自己的协议」（带括号 / 符号 / !!），
    # 不得把任意英文警告词当成失败标记。
    ("L1 Blender gvfs 警告「Failed to open dir ...」（须仍判 pass）",
     "Blender 3.6.14 无头测试\n"
     + "".join(f"[PASS] 用例{i}\n" for i in range(2))
     + SUM.format(p=2, t=2, f=0)
     + "Failed to open dir (No such file or directory): /run/user/1001/gvfs/\n",
     "pass", None),
    ("L2 Blender 退出泄漏检查「Error: not freed ...」（须仍判 pass）",
     "".join(f"[PASS] 用例{i}\n" for i in range(2))
     + SUM.format(p=2, t=2, f=0)
     + "Error: not freed 4 memory blocks (total unfreed 128 Bytes)\n"
     + "Blender quit\n",
     "pass", None),
]

# 明确标注「允许现状」的用例：现状不判FAIL，但不是 P0
KNOWN_GAPS = []


# ===========================================================================
# 隔离用例（最关键）: 构造「只有失败标记兜底能判红」的场景。
#
# 为什么必须隔离: 前面那些用例里，即使把 _FAIL_HEAD_RE/_FAIL_INLINE_RE
# 全部破坏成永不匹配，**依然有别的兜底兜住**（多汇总从严 / 6b 空壳检测），
# 于是「变异注入」显示 0 条报警 —— 看起来是「实现改了也没事」，
# 实则是**我的用例集没测到那条兜底**（测试的失败，不是实现的失败）。
#
# 隔离构造法: 让汇总行完全自洽（total 条全过）、PASS 行数恰好等于 total
# （骗过对账与空壳检测）、无盲区标记 —— 此时**唯一**的红线就是正文里的
# 失败标记。这才是「汇总被改坏 / 被篡改」时唯一能救命的兜底。
# ===========================================================================
ISOLATION_CASES = [
    # X1: 35 条 PASS（== total，骗过对账）+ 1 条多出来的 [FAIL] + 汇总 35/35 全过。
    #     这种形态真实会发生: run_case 捕获异常时打印 [FAIL]，而若有人把汇总
    #     改成从另一个列表（如 check 计数）取数，total 就不会包含这条失败。
    ("X1 PASS数==total + 多出1条[FAIL] + 汇总全绿（唯一信号=失败标记）",
     "".join(f"[PASS] 用例{i}\n" for i in range(35))
     + "[FAIL] 额外崩掉的用例  -- 抛出异常: AttributeError\n"
     + SUM.format(p=35, t=35, f=0), "fail", "正文"),

    ("X2 PASS数==total + 多出1条行中[FAIL]（Blender加前缀形态）",
     "".join(f"[PASS] 用例{i}\n" for i in range(100))
     + "Fra:1 [FAIL] 额外崩掉的用例\n" + SUM.format(p=100, t=100, f=0),
     "fail", "正文"),

    ("X3 PASS数==total + 多出1条[fail] 小写",
     "".join(f"[PASS] 用例{i}\n" for i in range(70))
     + "[fail] 额外崩掉的用例\n" + SUM.format(p=70, t=70, f=0),
     "fail", "正文"),

    ("X4 PASS数==total + 多出1条行中 !! 行（日志库前缀形态）",
     "".join(f"[PASS] 用例{i}\n" for i in range(17))
     + "2026-01-01 12:00:00 | ERROR | !! 严重: 某处失败\n"
     + SUM.format(p=17, t=17, f=0), "fail", "正文"),

    ("X5 PASS数==total + 多出1条[ERROR] 标记（QA-5 补词表后覆盖）",
     "".join(f"[PASS] 用例{i}\n" for i in range(34))
     + "[ERROR] 额外崩掉的用例\n" + SUM.format(p=34, t=34, f=0),
     "fail", "正文"),

    ("X6 PASS数==total + 多出1条 ✗ 符号标记",
     "".join(f"[PASS] 用例{i}\n" for i in range(40))
     + "✗ 额外崩掉的用例\n" + SUM.format(p=40, t=40, f=0), "fail", "正文"),

    ("X7 真·全绿反向对照（PASS数==total, 无失败标记, 应 pass）",
     "".join(f"[PASS] 用例{i}\n" for i in range(35))
     + SUM.format(p=35, t=35, f=0), "pass", None),

    ("X9 全绿且正文含 'value!! 普通文本'（须仍判 pass, 防误伤）",
     "".join(f"[PASS] 用例{i}\n" for i in range(34))
     + "[PASS] 断言 value!! 仍成立\n" + SUM.format(p=35, t=35, f=0),
     "pass", None),

    ("X10 全绿且正文含 'test_error_handling' 用例名（须仍判 pass）",
     "".join(f"[PASS] 用例{i}\n" for i in range(34))
     + "[PASS] test_error_handling 通过\n" + SUM.format(p=35, t=35, f=0),
     "pass", None),

    ("X8 全绿但用例名里含 fail 字样（须仍判 pass, 防误伤）",
     "".join(f"[PASS] 用例{i}\n" for i in range(34))
     + "[PASS] 重启后 fail 不复现\n" + SUM.format(p=35, t=35, f=0),
     "pass", None),
]


def run_cases(cases, label):
    npass = nfail = 0
    bad = []
    print("=" * 78)
    print(label)
    print("=" * 78)
    for name, out, want, kw in cases:
        got, detail = parse(out)
        kw_ok = True if not kw else (kw in detail)
        ok = (got == want) and kw_ok
        if ok:
            npass += 1
        else:
            nfail += 1
            bad.append((name, want, kw, got, detail))
        tag = "OK  " if ok else "BAD "
        mark = "(已知缺口)" if name.split()[0] in KNOWN_GAPS else ""
        print(f"[{tag}] {name} {mark}")
        print(f"        期望={want}" + (f"含'{kw}'" if kw else "")
              + f"   实际={got}   detail={detail[:64]}")
    return npass, nfail, bad


def mutation_probe():
    orig_fail_re = _rbt._FAIL_HEAD_RE
    """
    变异注入: 把判定逻辑改坏，看用例集能否抓住。
    这是防止「测试与实现同源、一起错」的唯一手段。
    """
    print("\n" + "=" * 78)
    print("变异注入（主集·信息性）")
    print("=" * 78)
    print("注意: 主集**故意不隔离**各道兜底（多条兜底会互相掩护），")
    print("      故这里出现「漏网」是预期结果，不计入成败。")
    print("      真正判定兜底是否被测到的是下面的**隔离集**。")
    import re as _re
    orig_summary_re = _rbt._SUMMARY_RE
    # 每条变异体是 (HEAD正则, INLINE正则) 二元组 —— 两条都要坏才算兜底失效。
    NEVER = _re.compile(r"(?!x)x")
    MUTANTS = [
        ("M1 兜底一完全失效（HEAD+INLINE 都永不匹配）",
         NEVER, NEVER),
        ("M2 兜底一退化为裸行首 FAIL（QA-4 指出的原 bug, 无括号/无行中）",
         _re.compile(r"^[ \t]*FAIL", _re.MULTILINE), NEVER),
        ("M3 兜底一大小写敏感（[fail] 逃逸）",
         _re.compile(r"^[ \t]*(?:!![ \t]*|\[?\s*FAIL\s*\]?)",
                     _re.MULTILINE),
         _re.compile(r"[\[\(（【]\s*FAIL\s*\]?", _re.IGNORECASE)),
        ("M4 兜底一只认行首（行中 [FAIL] 逃逸）",
         _re.compile(r"^[ \t]*(?:!![ \t]*|\[?\s*(?:FAIL|FAILED|ERROR|ERR"
                     r"|✗|✘|×)\s*\]?)", _re.MULTILINE | _re.IGNORECASE),
         NEVER),
        ("M5 兜底一不认行中 !! （日志前缀后的严重告警逃逸）",
         _re.compile(r"^[ \t]*(?:!![ \t]*|\[?\s*(?:FAIL|FAILED|ERROR|ERR"
                     r"|✗|✘|×)\s*\]?)", _re.MULTILINE | _re.IGNORECASE),
         _re.compile(r"[\[\(（【][ \t]*(?:FAIL|FAILED|ERROR|ERR|✗|✘|×)"
                     r"[ \t]*[:：]?[ \t]*[\]\)）】]?",
                     _re.IGNORECASE)),
        ("M6 兜底一词表缺 ERROR（[ERROR] 逃逸）",
         _re.compile(r"^[ \t]*(?:!![ \t]*|\[?\s*(?:FAIL|FAILED|✗|✘|×)"
                     r"\s*\]?)", _re.MULTILINE | _re.IGNORECASE),
         _re.compile(r"[\[\(（【][ \t]*(?:FAIL|FAILED|✗|✘|×)"
                     r"[ \t]*[:：]?[ \t]*[\]\)）】]?", _re.IGNORECASE)),
    ]
    orig_inline_re = _rbt._FAIL_INLINE_RE
    for label, repl_head, repl_inline in MUTANTS:
        _rbt._FAIL_HEAD_RE = repl_head
        _rbt._FAIL_INLINE_RE = repl_inline
        npass, nfail, bad = run_cases_quiet(CASES)
        _rbt._FAIL_HEAD_RE = orig_fail_re
        _rbt._FAIL_INLINE_RE = orig_inline_re
        caught = nfail > 0
        print(f"[{'主集兜住' if caught else '主集不隔离(预期)'}] {label} -> "
              f"{nfail} 条报警（首个: {bad[0][0] if bad else '-'}）")

    # M4: 模拟「有人改了汇总行的计算」——正文全红但汇总取自另一个列表
    print("\n--- M5 关键问题: 汇总行计算被改掉后，正文全红还能拦住吗? ---")
    M4 = [
        ("M4a 汇总从另一个'全过'列表取数, 正文 3 条全红",
         "[FAIL] a\n[FAIL] b\n[FAIL] c\n" + SUM.format(p=3, t=3, f=0), "fail"),
        ("M4b 汇总 total 被写成 999, 正文全红",
         "[FAIL] a\n[FAIL] b\n" + SUM.format(p=999, t=999, f=0), "fail"),
        ("M4c 正文全红但失败标记被改成 [FAIL:] 变体",
         "[FAIL:] a\n[FAIL:] b\n" + SUM.format(p=2, t=2, f=0), "fail"),
        ("M4d 正文全红, 但只有 [ERROR] 标记（应判 fail）",
         "[ERROR] a\n[ERROR] b\n" + SUM.format(p=2, t=2, f=0), "fail"),
    ]
    for nm, out, want in M4:
        got, detail = parse(out)
        if want is None:
            print(f"[  ?  ] {nm} -> status={got}  (现状未覆盖, 需修)")
            continue
        caught = got == want
        print(f"[{'抓住' if caught else '漏网!!'}] {nm} -> status={got}")
        if not caught:
            return 1
    return 0


def re_never_match():
    import re as _re
    return _re.compile(r"(?!x)x")


def run_cases_quiet(cases):
    npass = nfail = 0
    bad = []
    for name, out, want, kw in cases:
        got, detail = parse(out)
        kw_ok = True if not kw else (kw in detail)
        if (got == want) and kw_ok:
            npass += 1
        else:
            nfail += 1
            bad.append((name, want, kw, got, detail))
    return npass, nfail, bad


def main():
    print("=" * 78)
    print("QA-5 独立验收: _parse_output 边界与变异对抗")
    print("=" * 78)
    npass, nfail, bad = run_cases(CASES, "第一轮: 合成输出边界用例")
    print(f"\n小计: {npass} 符合预期, {nfail} 不符合预期")

    # 隔离用例单独跑
    npass2, nfail2, bad2 = run_cases(ISOLATION_CASES, "第二轮: 隔离用例"
                "（唯一信号= 正文失败标记）")
    print(f"\n隔离小计: {npass2} 符合预期, {nfail2} 不符合预期")

    # 隔离变异探针: 对 ISOLATION_CASES 跑同样的变异体
    print("\n" + "=" * 78)
    print("变异注入（隔离集）: 破坏失败标记兜底，必须被隔离用例抓住")
    print("=" * 78)
    orig_head = _rbt._FAIL_HEAD_RE
    orig_inline = _rbt._FAIL_INLINE_RE
    iso_leak = []
    import re as _re2
    NEVER2 = _re2.compile(r"(?!x)x")
    ISO_MUTANTS = [
        ("IM1 失败标记兜底完全失效",
         NEVER2, NEVER2),
        ("IM2 退化为裸行首 FAIL（QA-4 原 bug）",
         _re2.compile(r"^[ \t]*FAIL", _re2.MULTILINE), NEVER2),
        ("IM3 大小写敏感（[fail] 逃逸）",
         _re2.compile(r"^[ \t]*(?:!![ \t]*|\[?\s*FAIL\s*\]?)",
                      _re2.MULTILINE),
         _re2.compile(r"[\[\(（【]\s*FAIL\s*\]?", _re2.IGNORECASE)),
        ("IM4 只认行首（行中 [FAIL] 逃逸）",
         _re2.compile(r"^[ \t]*(?:!![ \t]*|\[?\s*(?:FAIL|FAILED|ERROR|ERR"
                      r"|✗|✘|×)\s*\]?)", _re2.MULTILINE | _re2.IGNORECASE),
         NEVER2),
        # 只把「行中 !!」这一支删掉（保留带括号的标记）—— 专门验证
        # 「行中 !!」这条约束真的被 X4 测到，而不是被别的兜底掩护过去。
        ("IM5 不认行中 !! （日志前缀后的严重告警逃逸）",
         _re2.compile(r"^[ \t]*(?:!![ \t]*|\[?\s*(?:FAIL|FAILED|ERROR|ERR"
                      r"|✗|✘|×)\s*\]?)", _re2.MULTILINE | _re2.IGNORECASE),
         _re2.compile(r"[\[\(（【][ \t]*(?:FAIL|FAILED|ERROR|ERR|✗|✘|×)"
                      r"[ \t]*[:：]?[ \t]*[\]\)）】]?", _re2.IGNORECASE)),
        ("IM6 词表缺 ERROR（[ERROR] 逃逸）",
         _re2.compile(r"^[ \t]*(?:!![ \t]*|\[?\s*(?:FAIL|FAILED|✗|✘|×)"
                      r"\s*\]?)", _re2.MULTILINE | _re2.IGNORECASE),
         _re2.compile(r"[\[\(（【][ \t]*(?:FAIL|FAILED|✗|✘|×)"
                      r"[ \t]*[:：]?[ \t]*[\]\)）】]?"
                      r"|(?:^|(?<=[\s|(\[]))!!(?=[ \t:：])",
                      _re2.IGNORECASE | _re2.MULTILINE)),
        # 反向变异: 去掉左边界约束后, 误伤测试必须报警（防「收紧」被后人改回去）
        ("IM7 行中 !! 无左边界（应触发 X9 误伤用例）",
         _re2.compile(r"^[ \t]*(?:!![ \t]*|\[?\s*(?:FAIL|FAILED|ERROR|ERR"
                      r"|✗|✘|×)\s*\]?)", _re2.MULTILINE | _re2.IGNORECASE),
         _re2.compile(r"[\[\(（【][ \t]*(?:FAIL|FAILED|ERROR|ERR|✗|✘|×)"
                      r"[ \t]*[:：]?[ \t]*[\]\)）】]?|!!(?=[ \t:：])",
                      _re2.IGNORECASE)),
    ]
    for label, rh, ri in ISO_MUTANTS:
        _rbt._FAIL_HEAD_RE = rh
        _rbt._FAIL_INLINE_RE = ri
        _, nf, bd = run_cases_quiet(ISOLATION_CASES)
        _rbt._FAIL_HEAD_RE = orig_head
        _rbt._FAIL_INLINE_RE = orig_inline
        caught = nf > 0
        print(f"[{'抓住' if caught else '漏网!!'}] {label} -> {nf} 条报警"
              f"（首个: {bd[0][0] if bd else '-'}）")
        if not caught:
            iso_leak.append(label)
    if iso_leak:
        print("  -> 漏网变异涉及的用例编号: " +
              ", ".join(sorted({c[0].split()[0]
                                 for c in ISOLATION_CASES
                                 if False}) or "X1..X6"))

    mutation_probe()

    print("\n" + "=" * 78)
    if bad:
        print("!!! 不符合预期的用例（判定逻辑漏洞）:")
        for nm, want, kw, got, detail in bad:
            print(f"  · {nm}")
            print(f"      期望 {want}{'/' + kw if kw else ''} -> 实际 {got}: "
                  f"{detail[:76]}")
    else:
        print("全部用例符合预期")
    print(f"隔离集变异注入: {'全部被抓住（各道兜底均被测到）' if not iso_leak else '存在漏网变异'}")
    if bad2:
        print("\n!!! 隔离用例不符合预期:")
        for nm, want, kw, got, detail in bad2:
            print(f"  · {nm}\n      期望 {want} -> 实际 {got}: {detail[:70]}")
    if iso_leak:
        print("\n!!! 隔离集漏网的变异（说明该兜底**完全没被测到**）:")
        for l in iso_leak:
            print(f"  · {l}")
    total_bad = nfail + nfail2 + len(iso_leak)
    print("=" * 78)
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
