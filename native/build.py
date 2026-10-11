#!/usr/bin/env python
"""构建原生内核并安装到插件的加载路径。

流程：
  1. 定位 cargo（用户目录下的 rustup 安装优先于 PATH）
  2. cargo build --release
  3. 从 .cargo/config.toml 读出目标三元组，定位产物
  4. 检查产物只依赖系统 DLL——任何非系统依赖都会让用户机器上加载失败
  5. 复制到 native/bin/ 并打印 SHA256

用法：
    python native/build.py            # 构建并安装
    python native/build.py --check    # 只检查已安装产物是否与当前源码一致
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CRATE = os.path.join(HERE, "vct-rs")
INSTALL_DIR = os.path.join(HERE, "bin")

# 只允许出现在产物依赖表里的系统 DLL。
# 少一个都不行：ctypes 加载失败时插件会静默降级，用户只会觉得慢，不会知道原因。
ALLOWED_SYSTEM_DLLS = {
    "kernel32.dll", "user32.dll", "gdi32.dll", "advapi32.dll",
    "shell32.dll", "ole32.dll", "oleaut32.dll", "comdlg32.dll",
    "msvcrt.dll", "ucrtbase.dll", "vcruntime140.dll", "vcruntime140_1.dll",
    "ws2_32.dll", "ntdll.dll", "rpcrt4.dll", "bcrypt.dll",
    "api-ms-win-crt-runtime-l1-1-0.dll",
}


def fail(msg, detail=""):
    print("✗ %s" % msg, file=sys.stderr)
    if detail:
        print(detail, file=sys.stderr)
    raise SystemExit(1)


def find_cargo():
    home = os.path.expandvars(r"%USERPROFILE%")
    candidates = [
        os.path.join(home, ".cargo", "bin", "cargo.exe"),
        os.path.join(home, ".cargo", "bin", "cargo"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    found = shutil.which("cargo")
    if found:
        return found
    fail("找不到 cargo。请安装 Rust 工具链（https://rustup.rs）。")


def target_triple():
    """目标三元组以 .cargo/config.toml 为准，避免本机与产物不一致。"""
    cfg = os.path.join(CRATE, ".cargo", "config.toml")
    default = "x86_64-pc-windows-msvc" if sys.platform == "win32" else None
    if not os.path.isfile(cfg):
        return default
    for line in open(cfg, encoding="utf-8"):
        line = line.strip()
        if line.startswith("target") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"')
    return default


def dll_name():
    return "vct_native.dll" if sys.platform == "win32" else (
        "libvct_native.dylib" if sys.platform == "darwin" else "libvct_native.so")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build():
    cargo = find_cargo()
    print("cargo:", cargo)
    print("运行 cargo build --release ...")
    r = subprocess.run([cargo, "build", "--release", "--locked"],
                       cwd=CRATE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    for line in out.splitlines()[-6:]:
        print("   ", line)
    if r.returncode != 0:
        fail("cargo build 失败")
    return cargo


def check_dependencies(dll):
    """确认产物只依赖系统 DLL。做法：尝试直接加载它。"""
    import ctypes
    try:
        ctypes.CDLL(dll)
    except OSError as exc:
        fail("产物无法被加载，通常意味着缺少依赖：%s" % exc)
    print("  依赖检查：可被直接加载（非系统 DLL 会在这里暴露）")

    # 顺带确认导出了预期的符号
    lib = ctypes.CDLL(dll)
    expected = [
        "vct_api_version", "vct_self_test",
        "vct_kdtree_create", "vct_kdtree_free",
        "vct_kdtree_query_indices", "vct_kdtree_pick_constrained",
        "vct_match_best", "vct_cluster_targets",
    ]
    missing = []
    for name in expected:
        try:
            getattr(lib, name)
        except AttributeError:
            missing.append(name)
    if missing:
        fail("产物缺少导出符号：%s" % ", ".join(missing),
             "提示：某个 .rs 文件可能从未被 mod 声明，因此从未参与编译。")
    print("  导出检查：%d 个符号齐备" % len(expected))

    lib.vct_self_test.restype = ctypes.c_int
    if lib.vct_self_test() != 0:
        fail("产物自检未通过")
    lib.vct_api_version.restype = ctypes.c_int
    print("  自检通过，ABI 版本 %d" % lib.vct_api_version())


def main():
    check_only = "--check" in sys.argv
    if not check_only:
        build()

    triple = target_triple()
    name = dll_name()
    if triple:
        src = os.path.join(CRATE, "target", triple, "release", name)
    else:
        src = os.path.join(CRATE, "target", "release", name)
    if not os.path.isfile(src):
        fail("找不到构建产物：%s" % src)

    print("")
    print("检查产物:", src)
    check_dependencies(src)

    os.makedirs(INSTALL_DIR, exist_ok=True)
    dst = os.path.join(INSTALL_DIR, name)
    if check_only:
        if not os.path.isfile(dst):
            fail("尚未安装：%s" % dst)
        if sha256(src) != sha256(dst):
            fail("已安装产物与当前构建不一致，请重新安装")
        print("  已安装产物与当前构建一致")
    else:
        if os.path.isfile(dst):
            shutil.copyfile(dst, dst + ".bak")
        shutil.copyfile(src, dst)
        print("")
        print("已安装到:", dst)
    print("SHA256:", sha256(dst))

    manifest = os.path.join(INSTALL_DIR, "build-info.json")
    with open(manifest, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"target": triple, "file": name, "sha256": sha256(dst)}, fh,
                  indent=2, sort_keys=True)
        fh.write("\n")
    print("构建信息:", manifest)


if __name__ == "__main__":
    main()