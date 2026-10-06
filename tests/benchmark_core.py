"""
核心运算基准测试（原生 vs Python A/B 对照）

用途:
    量化 C++ 原生内核相对纯 Python 实现的实际加速比，
    并给出各环节的耗时分解，作为优化是否达标的依据。

运行:
    "<Blender安装路径>/blender.exe" --background --factory-startup \
        --python tests/benchmark_core.py

可调规模（环境变量）:
    VCT_BENCH_VERTS=20000     复制基准的顶点数
    VCT_BENCH_TARGETS=10      复制基准的目标物体数
    VCT_BENCH_OBJECTS=600     匹配基准的物体数（源=目标）
"""

import os
import sys
import time

import bpy
import numpy as np

VERTS = int(os.environ.get("VCT_BENCH_VERTS", "20000"))
TARGETS = int(os.environ.get("VCT_BENCH_TARGETS", "10"))
OBJECTS = int(os.environ.get("VCT_BENCH_OBJECTS", "600"))


def timed(func, repeat=3):
    """执行 repeat 次取最小值（减少调度抖动影响）"""
    best = None
    result = None
    for _ in range(repeat):
        start = time.perf_counter()
        result = func()
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    return best, result


def fmt(seconds):
    return f"{seconds * 1000:>10.1f} ms"


def ratio(python_time, native_time):
    if native_time <= 0:
        return "—"
    return f"{python_time / native_time:.1f}x"


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh)


def make_cloud(name, count, color, collection, rng=None):
    """点云（POINT 域颜色，无需面）"""
    if rng is None:
        positions = [(float(i % 200), float(i // 200), 0.0) for i in range(count)]
    else:
        positions = rng.uniform(-50, 50, size=(count, 3)).astype(np.float32).tolist()

    mesh = bpy.data.meshes.new(f"{name}_mesh")
    mesh.from_pydata(positions, [], [])
    mesh.update()

    attr = mesh.color_attributes.new(name="Color", type='FLOAT_COLOR', domain='POINT')
    flat = np.tile(np.array(color, dtype=np.float32), count)
    attr.data.foreach_set("color", flat)

    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


# ---------------------------------------------------------------------------
# [1] 顶点色复制 A/B
# ---------------------------------------------------------------------------

def bench_copy():
    from Blender_VertexColorTool.core.cache import VertexColorCache
    from Blender_VertexColorTool.core.vertex_color_ops import (
        copy_vertex_colors_between_objects,
    )

    print(f"\n[1] 顶点色复制：1 个源物体 → {TARGETS} 个目标物体（各 {VERTS:,} 顶点）")
    print("-" * 78)

    vc_tool = bpy.context.scene.vertex_color_tool

    def setup():
        clear_scene()
        coll = bpy.data.collections.new("BenchCopy")
        bpy.context.scene.collection.children.link(coll)
        rng = np.random.default_rng(2026)
        source = make_cloud("bench_src", VERTS, (1, 0, 0, 1), coll, rng)
        targets = [
            make_cloud(f"bench_tgt_{i}", VERTS, (1, 1, 1, 1), coll, rng)
            for i in range(TARGETS)
        ]
        return source, targets

    def run(use_native):
        source, targets = setup()
        vc_tool.use_active_vcol = True
        vc_tool.use_kdtree = True
        vc_tool.use_cache = True          # 缓存开启（真实使用场景）
        vc_tool.use_native_accel = use_native
        VertexColorCache.clear_cache()

        # 预热：构建 KDTree / 缓存
        copy_vertex_colors_between_objects(source, targets[0], vc_tool=vc_tool)

        def _run():
            VertexColorCache.clear_cache()   # 每次都从冷启动测量，保证公平
            ok = True
            for target in targets:
                ok = copy_vertex_colors_between_objects(source, target, vc_tool=vc_tool) and ok
            return ok

        return timed(_run, repeat=3)

    py_time, py_ok = run(False)
    nat_time, nat_ok = run(True)
    vc_tool.use_native_accel = True

    print(f"  {'实现':<28}{'耗时':>14}{'':>6}")
    print(f"  {'Python 实现':<28}{fmt(py_time):>14}")
    print(f"  {'原生 C++ 内核':<28}{fmt(nat_time):>14}")
    print(f"  {'加速比':<28}{ratio(py_time, nat_time):>14}")
    print(f"  结果正确: Python={py_ok} 原生={nat_ok}")
    return py_time, nat_time


# ---------------------------------------------------------------------------
# [2] 物体匹配 A/B
# ---------------------------------------------------------------------------

def bench_matching():
    from Blender_VertexColorTool.core.matching import (
        get_object_features, find_best_matches,
    )
    from Blender_VertexColorTool.core.cache import VertexColorCache

    total_pairs = OBJECTS * OBJECTS
    print(f"\n[2] 物体匹配：{OBJECTS} 源 × {OBJECTS} 目标 = {total_pairs:,} 次比较")
    print("-" * 78)

    clear_scene()
    coll = bpy.data.collections.new("BenchMatch")
    bpy.context.scene.collection.children.link(coll)

    rng = np.random.default_rng(77)
    sources = [make_cloud(f"m_src_{i}", 8, (1, 0, 0, 1), coll, rng) for i in range(OBJECTS)]
    targets = [make_cloud(f"m_tgt_{i}", 8, (1, 1, 1, 1), coll, rng) for i in range(OBJECTS)]

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.distance_threshold = 100000.0   # 放宽，确保进入完整计算分支
    vc_tool.match_similarity_threshold = 0.0
    vc_tool.use_native_accel = True

    source_feats = {o.name: get_object_features(o) for o in sources}
    target_feats = {o.name: get_object_features(o) for o in targets}

    def run(use_native):
        vc_tool.use_native_accel = use_native

        def _run():
            return find_best_matches(sources, source_feats, targets, target_feats, vc_tool)

        return timed(_run, repeat=3)

    py_time, py_result = run(False)
    nat_time, nat_result = run(True)
    vc_tool.use_native_accel = True

    # 结果一致性核对
    mismatch = sum(
        1 for a, b in zip(py_result, nat_result)
        if (a[0] is None) != (b[0] is None)
        or (a[0] is not None and a[0].name != b[0].name)
        or abs(a[1] - b[1]) > 1e-5
    )

    print(f"  {'Python 实现':<28}{fmt(py_time):>14}")
    print(f"  {'原生 C++ 内核（多线程）':<28}{fmt(nat_time):>14}")
    print(f"  {'加速比':<28}{ratio(py_time, nat_time):>14}")
    print(f"  结果一致: {'是' if mismatch == 0 else f'否（{mismatch} 项不一致）'}")
    return py_time, nat_time, mismatch


# ---------------------------------------------------------------------------
# [3] 大规模匹配（原生内核的线程扩展性）
# ---------------------------------------------------------------------------

def bench_matching_large():
    from Blender_VertexColorTool.core.matching import (
        get_object_features, find_best_matches,
    )

    count = 2000
    print(f"\n[3] 大规模匹配：{count} 源 × {count} 目标 = {count * count:,} 次比较（仅原生）")
    print("-" * 78)

    clear_scene()
    coll = bpy.data.collections.new("BenchLarge")
    bpy.context.scene.collection.children.link(coll)

    rng = np.random.default_rng(1234)
    sources = [make_cloud(f"L_src_{i}", 8, (1, 0, 0, 1), coll, rng) for i in range(count)]
    targets = [make_cloud(f"L_tgt_{i}", 8, (1, 1, 1, 1), coll, rng) for i in range(count)]

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.distance_threshold = 100000.0
    vc_tool.match_similarity_threshold = 0.0
    vc_tool.use_native_accel = True

    source_feats = {o.name: get_object_features(o) for o in sources}
    target_feats = {o.name: get_object_features(o) for o in targets}

    nat_time, _ = timed(
        lambda: find_best_matches(sources, source_feats, targets, target_feats, vc_tool),
        repeat=3,
    )
    print(f"  {'原生 C++ 内核':<28}{fmt(nat_time):>14}")
    print(f"  每秒比较次数: {count * count / nat_time / 1e6:.1f} M/s")
    return nat_time


def main():
    addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package_name = os.path.basename(addon_dir)
    parent_dir = os.path.dirname(addon_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    import importlib
    addon = importlib.import_module(package_name)
    addon.register()

    from Blender_VertexColorTool.core import native_backend

    print("=" * 78)
    print(f"核心运算基准测试  (Blender {bpy.app.version_string}, numpy {np.__version__})")
    print(f"原生后端: {native_backend.status()}")
    print("=" * 78)

    if not native_backend.is_available():
        print("!! 原生库不可用，仅能测量 Python 路径。请先运行: python native/build.py")
        addon.unregister()
        return

    try:
        copy_py, copy_nat = bench_copy()
        match_py, match_nat, mismatch = bench_matching()
        bench_matching_large()

        print("\n" + "=" * 78)
        print("结论摘要")
        print("=" * 78)
        print(f"  顶点色复制  : {copy_py * 1000:>8.0f} ms → {copy_nat * 1000:>8.0f} ms"
              f"   ({ratio(copy_py, copy_nat)} 加速)")
        print(f"  物体匹配    : {match_py * 1000:>8.0f} ms → {match_nat * 1000:>8.0f} ms"
              f"   ({ratio(match_py, match_nat)} 加速)")
        print(f"  结果一致性  : {'通过' if mismatch == 0 else '存在差异'}")
        print("=" * 78)
    finally:
        addon.unregister()


if __name__ == "__main__":
    main()
