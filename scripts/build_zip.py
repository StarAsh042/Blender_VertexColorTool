#!/usr/bin/env python3
"""
打包 Blender 插件为可直接安装的 zip。

产物:
    dist/Blender_VertexColorTool-<version>.zip

zip 内部结构（Blender「从文件安装」要求顶层是插件目录）:
    Blender_VertexColorTool/__init__.py
    Blender_VertexColorTool/core/...
    Blender_VertexColorTool/operators/...
    ...

版本号从 __init__.py 的 bl_info["version"] 读取，避免与代码漂移。

用法:
    python scripts/build_zip.py
"""

import ast
import os
import sys
import zipfile

# 插件目录名（zip 内的顶层文件夹名，必须与 Blender 安装后的模块名一致）
PACKAGE_NAME = "Blender_VertexColorTool"

# 需要打进包的内容（相对项目根目录）
INCLUDE = (
    "__init__.py",
    "core",
    "operators",
    "properties",
    "ui",
    "utils",
    "LICENSE",
    # 原生加速库（若已构建）。缺失时插件会自动回退纯 Python，不影响安装。
    "native/bin",
)

# 排除规则
EXCLUDE_DIRS = {
    "__pycache__", ".git", ".github", "dist", "build", "tests",
    ".workbuddy-ai", "deliverables", ".idea", ".vscode", ".venv", "venv",
}
EXCLUDE_EXT = {
    ".pyc", ".pyo", ".pyd", ".blend1",
    # 链接器/调试产物：运行时加载 DLL 只需要 .dll 本身
    ".lib", ".pdb", ".exp", ".ilk",
}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def read_version(project_root):
    """从 __init__.py 的 bl_info 中解析版本号"""
    init_path = os.path.join(project_root, "__init__.py")
    with open(init_path, "r", encoding="utf-8") as handle:
        source = handle.read()

    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "bl_info":
                    version = ast.literal_eval(node.value)["version"]
                    return ".".join(str(part) for part in version)

    raise RuntimeError("未能在 __init__.py 中找到 bl_info['version']")


def collect_files(project_root):
    """收集需要打包的文件，返回 [(绝对路径, zip 内相对路径)]"""
    entries = []

    for item in INCLUDE:
        absolute = os.path.join(project_root, item)

        if not os.path.exists(absolute):
            print(f"  警告: 缺少 {item}，已跳过")
            continue

        if os.path.isfile(absolute):
            entries.append((absolute, item))
            continue

        for current_dir, sub_dirs, files in os.walk(absolute):
            # 就地过滤掉要排除的目录，避免继续深入
            sub_dirs[:] = [d for d in sub_dirs if d not in EXCLUDE_DIRS]

            for filename in files:
                if filename in EXCLUDE_FILES:
                    continue
                if os.path.splitext(filename)[1].lower() in EXCLUDE_EXT:
                    continue

                full_path = os.path.join(current_dir, filename)
                rel_path = os.path.relpath(full_path, project_root)
                entries.append((full_path, rel_path))

    return sorted(entries, key=lambda pair: pair[1])


def build_zip(project_root, version):
    """生成 zip，返回产物路径"""
    dist_dir = os.path.join(project_root, "dist")
    os.makedirs(dist_dir, exist_ok=True)

    zip_name = f"{PACKAGE_NAME}-{version}.zip"
    zip_path = os.path.join(dist_dir, zip_name)

    entries = collect_files(project_root)
    if not entries:
        raise RuntimeError("没有收集到任何文件，请检查 INCLUDE 配置")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for absolute, rel_path in entries:
            # zip 内统一使用正斜杠，并置于插件目录之下
            arcname = f"{PACKAGE_NAME}/{rel_path.replace(os.sep, '/')}"
            archive.write(absolute, arcname)

    return zip_path, entries


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)

    version = read_version(project_root)
    print(f"打包 {PACKAGE_NAME} v{version}")
    print(f"项目根目录: {project_root}")

    zip_path, entries = build_zip(project_root, version)

    size_kb = os.path.getsize(zip_path) / 1024
    print(f"  收录 {len(entries)} 个文件")
    print(f"  产物: {zip_path}  ({size_kb:.1f} KB)")

    # 自检：确认关键文件都在包里
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        required = [
            f"{PACKAGE_NAME}/__init__.py",
            f"{PACKAGE_NAME}/core/cache.py",
            f"{PACKAGE_NAME}/ui/results_panel.py",
            f"{PACKAGE_NAME}/utils/logging_utils.py",
            # 1.1.0 起算子顶层 import 的新模块——漏掉会导致插件
            # 「加载成功」但分析/匹配算子静默缺失（import 失败被吞）
            f"{PACKAGE_NAME}/utils/collection_utils.py",
        ]
        missing = [name for name in required if name not in names]
        if missing:
            print(f"  自检失败，缺少: {missing}")
            return 1

        # 确认没有混入开发文件
        leaked = [
            n for n in names
            if "__pycache__" in n or n.endswith(".pyc") or "/tests/" in n
            or n.startswith(f"{PACKAGE_NAME}/.workbuddy-ai")
        ]
        if leaked:
            print(f"  自检失败，混入开发文件: {leaked[:5]}")
            return 1

    print("  自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
