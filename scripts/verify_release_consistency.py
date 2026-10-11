#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
发布一致性校验（无需 Blender，本地可选检查发行包）

背景:
    v1.1.0 发布前曾出现版本号三处不一致——bl_info 是 1.2.0、
    两个算子 docstring 写「v1.3.0 起变更」、CHANGELOG 顶部是 [1.2.0]，
    最后发布时塌缩为 1.1.0。这类问题靠人肉核对迟早再犯。
    本脚本把「版本从一处声明、处处引用」钉成 CI 红线。

检查项:
    1. bl_info（AST 解析 __init__.py）== CHANGELOG 顶部版本节
       （这是 CI 上一定会跑的核心一致性）
    2. 若存在 dist/Blender_VertexColorTool-<版本>.zip（本地构建产物，
       CI 上没有 -> 显式跳过而不是静默）:
       2a. 包内 __init__.py 的 bl_info 版本一致
       2b. 包内 vct_native.dll 的 SHA256 == build_zip.py 记录的
           「已知良好」哈希

退出码:
    0 = 通过；1 = 不一致。dist zip 缺失不判失败（CI 无构建产物属常态），
    但会显式打印跳过说明。
"""

import ast
import hashlib
import io
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"   {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


def read_bl_info_version(init_path):
    """AST 解析 __init__.py 的 bl_info['version']，返回 'x.y.z'"""
    with io.open(init_path, encoding="utf-8") as f:
        source = f.read()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "bl_info":
                    version = ast.literal_eval(node.value)["version"]
                    return ".".join(str(part) for part in version)
    return None


def main():
    print("=" * 76)
    print("发布一致性校验：版本单一来源 + 发行包完整性")
    print("=" * 76)

    # --- 1. bl_info vs CHANGELOG ---
    init_path = os.path.join(ROOT, "__init__.py")
    code_version = read_bl_info_version(init_path)
    check("__init__.py 能解析出 bl_info 版本", code_version is not None,
          f"实际 = {code_version}")

    changelog_path = os.path.join(ROOT, "CHANGELOG.md")
    top_version = None
    with io.open(changelog_path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^## \[(\d+\.\d+\.\d+)\]", line)
            if m:
                top_version = m.group(1)
                break
    check("CHANGELOG 顶部存在版本节", top_version is not None,
          f"实际 = {top_version}")
    if code_version and top_version:
        check("bl_info == CHANGELOG 顶部版本",
              code_version == top_version,
              f"bl_info={code_version}, CHANGELOG=[{top_version}]"
              + ("" if code_version == top_version
                 else "（发布/改版时两处必须同步）"))

    # --- 2. 发行包（存在才查；CI 无 dist 属常态，显式跳过） ---
    zip_path = os.path.join(ROOT, "dist",
                            f"Blender_VertexColorTool-{code_version}.zip")
    if not os.path.isfile(zip_path):
        print("   SKIP  发行包检查（本地可选）-- 未找到 "
              f"{os.path.relpath(zip_path, ROOT)}，"
              "运行 scripts/build_zip.py 后重跑可覆盖此项")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from build_zip import KNOWN_DLL_SHA256

        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            inner_init = f"Blender_VertexColorTool/__init__.py"
            check("发行包内存在插件 __init__.py", inner_init in names)
            if inner_init in names:
                zip_version = read_bl_info_version_from_bytes(
                    archive.read(inner_init))
                check("发行包内 bl_info 版本一致",
                      zip_version == code_version,
                      f"包内={zip_version}, 源码={code_version}")

            dll_name = "Blender_VertexColorTool/native/bin/vct_native.dll"
            if dll_name in names and KNOWN_DLL_SHA256:
                digest = hashlib.sha256(archive.read(dll_name)).hexdigest()
                check("发行包内 vct_native.dll 哈希与记录一致",
                      digest == KNOWN_DLL_SHA256,
                      f"包内={digest[:16]}… 记录={KNOWN_DLL_SHA256[:16]}…"
                      + ("" if digest == KNOWN_DLL_SHA256 else
                         "（若重新编译来自可信来源，更新 build_zip.py 的"
                         " KNOWN_DLL_SHA256）"))
            elif dll_name not in names:
                check("发行包内存在 vct_native.dll", False,
                      "打包自检应已拦截，请检查 build_zip.py required 清单")

    failed = sum(1 for ok in RESULTS if not ok)
    print("\n" + "=" * 76)
    print(f"结果: {len(RESULTS) - failed}/{len(RESULTS)} 通过"
          + ("" if not failed else f"，{failed} 项不一致"))
    print("=" * 76)
    return 0 if not failed else 1


def read_bl_info_version_from_bytes(source_bytes):
    """从字节流（zip 内文件）解析 bl_info 版本"""
    for node in ast.parse(source_bytes.decode("utf-8")).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "bl_info":
                    version = ast.literal_eval(node.value)["version"]
                    return ".".join(str(part) for part in version)
    return None


if __name__ == "__main__":
    sys.exit(main())
