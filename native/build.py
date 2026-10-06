#!/usr/bin/env python3
"""
构建原生加速库（C++ → 纯 C ABI 动态库）

用法:
    python native/build.py              # 构建 + 自检
    python native/build.py --check      # 只检查现有产物

编译器优先级:
    1. Zig（`pip install ziglang`）—— 无需管理员权限，各平台一致，推荐
    2. 系统 g++ / clang++（若已装 MinGW-w64 或 LLVM）

产物:
    native/bin/vct_native.dll     (Windows)
    native/bin/libvct_native.so   (Linux)
    native/bin/libvct_native.dylib(macOS)

设计说明:
    产物是**纯 C ABI 动态库**而非 CPython 扩展，因此不受 Python 版本约束
    （Blender 3.4 用 Python 3.10，4.x 用 3.11+，同一个 DLL 都能加载）。
    库缺失时插件会自动回退纯 Python，不影响可用性。
"""

import argparse
import ctypes
import os
import shutil
import subprocess
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIVE_DIR = os.path.join(PROJECT_ROOT, "native")
SOURCE = os.path.join(NATIVE_DIR, "vct_native.cpp")
OUTPUT_DIR = os.path.join(NATIVE_DIR, "bin")

EXPECTED_API_VERSION = 1


def library_filename():
    if sys.platform.startswith("win"):
        return "vct_native.dll"
    if sys.platform == "darwin":
        return "libvct_native.dylib"
    return "libvct_native.so"


def find_compiler():
    """
    返回 (描述, 命令前缀列表) 或 (None, None)。

    命令前缀之后会追加编译参数与源文件。
    """
    # 1) Zig —— 通过 python -m ziglang 调用（pip 安装，无需管理员）
    try:
        import ziglang  # noqa: F401

        # 优先直接用包内可执行文件，避免 -m 入口在不同版本上的差异
        package_dir = os.path.dirname(os.path.abspath(ziglang.__file__))
        for name in ("zig.exe", "zig"):
            candidate = os.path.join(package_dir, name)
            if os.path.isfile(candidate):
                return f"Zig ({candidate})", [candidate, "c++"]
        return "Zig (python -m ziglang)", [sys.executable, "-m", "ziglang", "c++"]
    except ImportError:
        pass

    # 2) 系统 zig
    zig = shutil.which("zig")
    if zig:
        return f"Zig ({zig})", [zig, "c++"]

    # 3) g++ / clang++
    for name in ("g++", "clang++"):
        path = shutil.which(name)
        if path:
            return f"{name} ({path})", [path]

    return None, None


def build_command(compiler_prefix, output_path):
    """组装编译命令"""
    args = list(compiler_prefix)

    # -O3 优化；刻意不加 -ffast-math，避免改变浮点语义导致与 Python 版结果不一致
    args += ["-O3", "-std=c++17", "-shared", "-fPIC", "-fvisibility=hidden"]

    if sys.platform.startswith("win"):
        # 显式指定目标三元组，避免默认目标与预期不符
        args += ["-target", "x86_64-windows-gnu"]

    # 注意: 不要传 `-static`。它与 `-shared` 冲突，会让产物变成 ar 静态库
    # （开头为 "!<arch>" 而非 "MZ"），加载时报 WinError 193。

    args += ["-I", NATIVE_DIR, SOURCE, "-o", output_path]
    return args


def inspect_binary(path):
    """
    检查产物是否为合法的 x64 DLL，并列出它依赖的外部 DLL。

    依赖非系统 DLL（如 libstdc++-6.dll）会导致用户机器上加载失败，
    因此这里主动暴露出来。
    """
    with open(path, "rb") as handle:
        data = handle.read()

    if data[:2] != b"MZ":
        preview = data[:16]
        hint = ""
        if preview.startswith(b"!<arch>"):
            hint = "（这是 ar 静态库，说明 -shared 未生效或被 -static 覆盖）"
        print(f"✗ 产物不是 PE 文件，开头为 {preview!r} {hint}")
        return 1

    import struct

    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        print("✗ PE 签名缺失")
        return 1

    machine = struct.unpack_from("<H", data, e_lfanew + 4)[0]
    characteristics = struct.unpack_from("<H", data, e_lfanew + 22)[0]
    machine_name = {0x8664: "x86-64", 0xAA64: "ARM64", 0x14C: "i386"}.get(machine, "未知")

    print(f"  架构: {machine_name} (0x{machine:x})")
    print(f"  类型: {'DLL' if characteristics & 0x2000 else '非 DLL'}")

    if machine != 0x8664:
        print("✗ 架构不是 x86-64，Blender x64 无法加载")
        return 1
    if not (characteristics & 0x2000):
        print("✗ 产物不是 DLL")
        return 1

    # 粗查导入表中出现的 DLL 名（够用即可，不解析完整导入表）
    import re
    own_name = os.path.basename(path).lower()
    found = sorted(set(
        name.decode("ascii") for name in
        re.findall(rb"[A-Za-z0-9_\-\.]{3,40}\.dll", data)
    ))

    # 系统库前缀（统一大写后比较，避免大小写不匹配导致误报）
    # 说明: api-ms-win-crt-* 属于 Windows 通用 CRT (UCRT)，
    # 自 Windows 10 起为系统组件，Blender 本身也要求 Win10+，因此视为系统库。
    system_prefixes = (
        "KERNEL32", "API-MS-", "VCRUNTIME", "UCRTBASE", "MSVCRT",
        "ADVAPI32", "USER32", "NTDLL", "BCRYPT", "RPCRT4", "OLE32",
        "SHELL32", "GDI32", "COMBASE", "WINMM", "PSAPI", "DBGHELP",
    )
    external = [
        name for name in found
        if name.lower() != own_name
        and not name.upper().startswith(system_prefixes)
    ]
    if external:
        print(f"  ⚠ 依赖非系统 DLL: {external}")
        print("    用户机器可能缺少这些库，建议改为静态链接或避免 STL")
        return 1

    print("  依赖: 仅系统库（含 Windows 通用 CRT）")
    return 0


def run_build():
    description, prefix = find_compiler()
    if not prefix:
        print("✗ 未找到 C++ 编译器。")
        print("  推荐: pip install ziglang    （约 94MB，无需管理员权限）")
        print("  或安装 MinGW-w64 / LLVM 后重试。")
        return 1

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, library_filename())

    print(f"编译器: {description}")
    print(f"源文件: {SOURCE}")
    print(f"产物  : {output_path}")

    command = build_command(prefix, output_path)
    print(f"命令  : {' '.join(command[:3])} ...")

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=600
        )
    except subprocess.TimeoutExpired:
        print("✗ 编译超时")
        return 1
    except OSError as exc:
        print(f"✗ 无法执行编译器: {exc}")
        return 1

    if result.returncode != 0:
        print("✗ 编译失败")
        print(result.stdout[-3000:])
        print(result.stderr[-3000:])
        return 1

    if result.stderr.strip():
        # 警告也打印出来，便于发现问题
        print("编译警告:")
        print(result.stderr[-1500:])

    size_kb = os.path.getsize(output_path) / 1024
    print(f"✓ 编译成功（{size_kb:.1f} KB）")
    return 0


def verify_library():
    """加载产物并调用自检，确认 ABI 可用"""
    output_path = os.path.join(OUTPUT_DIR, library_filename())
    if not os.path.isfile(output_path):
        print(f"✗ 产物不存在: {output_path}")
        return 1

    print("检查产物格式与依赖:")
    if inspect_binary(output_path) != 0:
        return 1

    if sys.platform.startswith("win") and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(OUTPUT_DIR)
        except OSError:
            pass

    try:
        lib = ctypes.CDLL(output_path)
        version = lib.vct_api_version()
        if version != EXPECTED_API_VERSION:
            print(f"✗ ABI 版本不匹配: 库={version}, 期望={EXPECTED_API_VERSION}")
            return 1

        rc = lib.vct_self_test()
        if rc != 0:
            print(f"✗ 自检未通过（返回码 {rc}）")
            return 1

        print(f"✓ 自检通过（ABI v{version}）")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 加载/自检失败: {exc}")
        return 1


def main():
    parser = argparse.ArgumentParser(description="构建原生加速库")
    parser.add_argument("--check", action="store_true", help="只检查现有产物")
    parser.add_argument("--no-verify", action="store_true", help="跳过构建后自检")
    options = parser.parse_args()

    if options.check:
        return verify_library()

    rc = run_build()
    if rc != 0:
        return rc

    if options.no_verify:
        return 0
    return verify_library()


if __name__ == "__main__":
    sys.exit(main())
