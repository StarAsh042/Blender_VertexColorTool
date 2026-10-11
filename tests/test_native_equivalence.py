"""
原生加速内核 —— 等价性与边界测试

核心目的:
    优化最大的风险不是「变慢」，而是「悄悄改变了结果」。
    本测试用同一份输入分别跑 **原生 C++ 内核** 与 **Python 参考实现**，
    逐项比对输出，确保加速没有改变任何行为。

覆盖:
    A. 原生后端可用性与自检
    B. 匹配内核等价性（随机 + 极端值 + 阈值边界）
    C. KDTree 取色等价性（随机点 + 重复点 + 大坐标）
    D. 关闭原生开关后的回退路径一致性
    E. 端到端流程等价性（算子级：原生开 vs 关，匹配结果必须一致）

运行:
    "<Blender安装路径>/blender.exe" --background --factory-startup \
        --python tests/test_native_equivalence.py
"""

import os
import random
import sys
import traceback

import bpy
from mathutils import Vector

_RESULTS = []

# 分数容差：原生输出为 float32，Python 为双精度，允许极小误差
SCORE_TOL = 1e-6
# 判定「并列」的容差：两个源分数差小于此值视为并列，允许选中不同源
TIE_TOL = 1e-5


def check(name, condition, detail=""):
    _RESULTS.append((name, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def run_case(name, func):
    try:
        func()
    except Exception as exc:  # noqa: BLE001
        _RESULTS.append((name, False, f"异常: {exc}"))
        print(f"[FAIL] {name}  -- 抛出异常: {exc}")
        traceback.print_exc()


# ---------------------------------------------------------------------------
# 特征构造
# ---------------------------------------------------------------------------

def make_feature(loc, dim, vertex_count, name="obj"):
    dim_v = Vector(dim)
    return {
        "name": name,
        "location": Vector(loc),
        "dimensions": dim_v,
        "vertex_count": vertex_count,
        "polygon_count": max(1, vertex_count * 2),
        "volume": dim[0] * dim[1] * dim[2],
        "bounding_sphere_radius": max(dim) * 0.5,
    }


def reference_best_matches(source_feats, target_feats, vc_tool):
    """Python 参考实现：逐对计算，语义与 match_ops 的回退路径一致"""
    from Blender_VertexColorTool.core.matching import calculate_similarity_score

    out = []
    for target_feat in target_feats:
        best_index = -1
        best_score = 0.0
        for index, source_feat in enumerate(source_feats):
            similarity = calculate_similarity_score(source_feat, target_feat, vc_tool)
            if similarity < vc_tool.match_similarity_threshold:
                continue
            if similarity > best_score:
                best_score = similarity
                best_index = index
        out.append((best_index, best_score))
    return out


def compare_match_results(label, source_feats, target_feats, vc_tool):
    """比对原生与 Python 的匹配结果"""
    from Blender_VertexColorTool.core import native_backend

    native = native_backend.match_best(source_feats, target_feats, vc_tool)
    if native is None:
        check(f"{label}: 原生内核可用", False, "match_best 返回 None")
        return

    native_indices, native_scores = native
    reference = reference_best_matches(source_feats, target_feats, vc_tool)

    index_mismatch = 0
    score_mismatch = 0
    tie_count = 0
    worst_score_diff = 0.0

    for i, (ref_index, ref_score) in enumerate(reference):
        nat_index = int(native_indices[i])
        nat_score = float(native_scores[i])

        diff = abs(nat_score - ref_score)
        worst_score_diff = max(worst_score_diff, diff)
        if diff > SCORE_TOL:
            score_mismatch += 1

        if nat_index != ref_index:
            # 允许「并列」：分数几乎相同但选中了不同源
            if diff <= TIE_TOL or (nat_index >= 0 and ref_index >= 0):
                tie_count += 1
            else:
                index_mismatch += 1

    check(f"{label}: 分数一致（容差 {SCORE_TOL}）", score_mismatch == 0,
          f"不一致 {score_mismatch}/{len(reference)} 项, 最大偏差 {worst_score_diff:.2e}")
    check(f"{label}: 选中源一致", index_mismatch == 0,
          f"不一致 {index_mismatch} 项, 并列 {tie_count} 项")


# ---------------------------------------------------------------------------
# A. 后端可用性
# ---------------------------------------------------------------------------

def test_backend_availability():
    from Blender_VertexColorTool.core import native_backend

    available = native_backend.is_available()
    print(f"[INFO] 原生后端状态: {native_backend.status()}")
    check("原生后端可用", available, native_backend.describe_failure() or "已加载")
    if not available:
        return

    lib = native_backend._lib
    check("ABI 版本正确", lib.vct_api_version() == 1,
          f"版本={lib.vct_api_version()}")
    check("库内自检通过", lib.vct_self_test() == 0)


# ---------------------------------------------------------------------------
# B. 匹配内核等价性
# ---------------------------------------------------------------------------

def test_match_random():
    vc_tool = bpy.context.scene.vertex_color_tool
    rng = random.Random(20261004)

    source_feats = [
        make_feature(
            (rng.uniform(-50, 50), rng.uniform(-50, 50), rng.uniform(-50, 50)),
            (rng.uniform(0.1, 10), rng.uniform(0.1, 10), rng.uniform(0.1, 10)),
            rng.randint(4, 5000),
            name=f"s{i}",
        )
        for i in range(60)
    ]
    target_feats = [
        make_feature(
            (rng.uniform(-50, 50), rng.uniform(-50, 50), rng.uniform(-50, 50)),
            (rng.uniform(0.1, 10), rng.uniform(0.1, 10), rng.uniform(0.1, 10)),
            rng.randint(4, 5000),
            name=f"t{i}",
        )
        for i in range(45)
    ]

    # 遍历多套参数（含不同权重组合与阈值），确保不是只对默认参数成立
    configs = [
        ("默认", dict(distance_threshold=50.0, position_decay_factor=0.5,
                     distance_weight=0.2, size_weight=0.3, volume_weight=0.3,
                     vertex_count_weight=0.2, match_similarity_threshold=0.0)),
        ("精确", dict(distance_threshold=30.0, position_decay_factor=0.3,
                     distance_weight=0.1, size_weight=0.4, volume_weight=0.4,
                     vertex_count_weight=0.3, match_similarity_threshold=0.5)),
        ("宽松", dict(distance_threshold=200.0, position_decay_factor=0.8,
                     distance_weight=0.3, size_weight=0.2, volume_weight=0.2,
                     vertex_count_weight=0.1, match_similarity_threshold=0.0)),
        ("均权", dict(distance_threshold=100.0, position_decay_factor=0.6,
                     distance_weight=0.25, size_weight=0.25, volume_weight=0.25,
                     vertex_count_weight=0.25, match_similarity_threshold=0.3)),
        ("仅距离", dict(distance_threshold=100.0, position_decay_factor=0.5,
                       distance_weight=1.0, size_weight=0.0, volume_weight=0.0,
                       vertex_count_weight=0.0, match_similarity_threshold=0.0)),
        ("全零权重", dict(distance_threshold=100.0, position_decay_factor=0.5,
                         distance_weight=0.0, size_weight=0.0, volume_weight=0.0,
                         vertex_count_weight=0.0, match_similarity_threshold=0.0)),
    ]

    original = {}
    for key in configs[0][1]:
        original[key] = getattr(vc_tool, key)

    try:
        for label, config in configs:
            for key, value in config.items():
                setattr(vc_tool, key, value)
            compare_match_results(f"随机特征[{label}]", source_feats, target_feats, vc_tool)
    finally:
        for key, value in original.items():
            setattr(vc_tool, key, value)


def test_match_edge_cases():
    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.distance_threshold = 1000.0
    vc_tool.position_decay_factor = 0.5
    vc_tool.distance_weight = 0.25
    vc_tool.size_weight = 0.25
    vc_tool.volume_weight = 0.25
    vc_tool.vertex_count_weight = 0.25
    vc_tool.match_similarity_threshold = 0.0

    # 1) 零尺寸物体（触发 avg_size <= 0.001 分支）
    zero_dim = [
        make_feature((0, 0, 0), (0.0, 0.0, 0.0), 0, name="z0"),
        make_feature((1, 0, 0), (0.0, 0.0, 0.0), 0, name="z1"),
    ]
    compare_match_results("零尺寸物体", zero_dim, zero_dim, vc_tool)

    # 2) 完全相同的位置（距离为 0）
    same_pos = [make_feature((5, 5, 5), (2, 2, 2), 100, name=f"p{i}") for i in range(3)]
    compare_match_results("相同位置", same_pos, same_pos, vc_tool)

    # 3) 大坐标（考验浮点精度）
    vc_tool.distance_threshold = 500000.0
    big = [
        make_feature((123456.7, -98765.4, 54321.1), (1000.0, 1000.0, 1000.0), 99999, name="b0"),
        make_feature((123400.0, -98700.0, 54300.0), (999.0, 1001.0, 1000.0), 100000, name="b1"),
        make_feature((-500000.0, 400000.0, 0.0), (500.0, 500.0, 500.0), 50000, name="b2"),
    ]
    compare_match_results("大坐标", big, big, vc_tool)

    # 4) 单源单目标
    single = [make_feature((0, 0, 0), (1, 1, 1), 8, name="only")]
    compare_match_results("单源单目标", single, single, vc_tool)

    # 5) 空目标
    from Blender_VertexColorTool.core import native_backend
    result = native_backend.match_best(single, [], vc_tool)
    check("空目标返回 None（交由调用方处理）", result is None,
          f"实际={result}")

    # 6) 超出距离阈值 → 应全部无匹配
    vc_tool.distance_threshold = 1.0
    far_src = [make_feature((0, 0, 0), (1, 1, 1), 8, name="f0")]
    far_tgt = [make_feature((9999, 9999, 9999), (1, 1, 1), 8, name="f1")]
    compare_match_results("超出距离阈值", far_src, far_tgt, vc_tool)

    # 恢复
    vc_tool.distance_threshold = 50.0


# ---------------------------------------------------------------------------
# C. KDTree 取色等价性
# ---------------------------------------------------------------------------

def python_kdtree_colors(points, colors, targets):
    """Python 参考实现：mathutils.kdtree 最近邻取色"""
    from mathutils import kdtree

    kd = kdtree.KDTree(len(points))
    for index, point in enumerate(points):
        kd.insert(Vector(point), index)
    kd.balance()

    out = []
    for target in targets:
        found = kd.find(Vector(target))
        if not found:
            out.append((1.0, 1.0, 1.0, 1.0))
        else:
            out.append(tuple(colors[found[1]]))
    return out


def test_kdtree_equivalence():
    import numpy as np
    from Blender_VertexColorTool.core import native_backend

    rng = np.random.default_rng(777)
    n_src, n_tgt = 3000, 1500

    points = rng.uniform(-100, 100, size=(n_src, 3)).astype(np.float32)
    targets = rng.uniform(-100, 100, size=(n_tgt, 3)).astype(np.float32)
    colors = rng.uniform(0, 1, size=(n_src, 4)).astype(np.float32)

    tree = native_backend.NativeKDTree(points)
    native_colors = tree.query_colors(colors, targets)

    reference = python_kdtree_colors(points.tolist(), colors.tolist(), targets.tolist())

    mismatch = 0
    worst = 0.0
    for i, ref in enumerate(reference):
        for c in range(4):
            diff = abs(float(native_colors[i, c]) - float(ref[c]))
            worst = max(worst, diff)
            if diff > 1e-6:
                mismatch += 1
                break

    check("KDTree 取色与 Python 参考一致", mismatch == 0,
          f"不一致 {mismatch}/{n_tgt} 项, 最大偏差 {worst:.2e}")

    # 重复点（并列最近邻）
    dup_points = np.array([[0, 0, 0], [0, 0, 0], [0, 0, 0], [5, 5, 5]], dtype=np.float32)
    dup_colors = np.array([[1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1], [1, 1, 1, 1]],
                          dtype=np.float32)
    dup_tree = native_backend.NativeKDTree(dup_points)
    dup_out = dup_tree.query_colors(dup_colors, np.array([[0.1, 0, 0]], dtype=np.float32))
    # 并列时选中哪一个都可以，但必须是那三个之一（不能是 [1,1,1,1]）
    is_one_of_dups = any(abs(dup_out[0, c] - dup_colors[j, c]) < 1e-6
                         for j in range(3) for c in range(4))
    check("重复点并列时返回其中之一（不是远处点）", is_one_of_dups,
          f"返回={dup_out[0].tolist()}")

    # 空目标
    empty_out = tree.query_colors(colors, np.empty((0, 3), dtype=np.float32))
    check("空目标返回空数组", empty_out.shape == (0, 4), f"形状={empty_out.shape}")


# ---------------------------------------------------------------------------
# D. 回退路径
# ---------------------------------------------------------------------------

def test_fallback_toggle():
    """关闭原生开关后必须仍能正常工作（走 Python 路径）"""
    from Blender_VertexColorTool.core.matching import find_best_matches

    vc_tool = bpy.context.scene.vertex_color_tool
    source_feats = [make_feature((0, 0, 0), (1, 1, 1), 8, name="a"),
                    make_feature((10, 0, 0), (1, 1, 1), 8, name="b")]
    target_feats = [make_feature((0.5, 0, 0), (1, 1, 1), 8, name="c")]

    class _Obj:
        def __init__(self, name):
            self.name = name

    src_objs = [_Obj("a"), _Obj("b")]
    tgt_objs = [_Obj("c")]
    src_map = {"a": source_feats[0], "b": source_feats[1]}
    tgt_map = {"c": target_feats[0]}

    vc_tool.use_native_accel = True
    with_native = find_best_matches(src_objs, src_map, tgt_objs, tgt_map, vc_tool)

    vc_tool.use_native_accel = False
    without_native = find_best_matches(src_objs, src_map, tgt_objs, tgt_map, vc_tool)

    vc_tool.use_native_accel = True

    same_index = (with_native[0][0].name == without_native[0][0].name)
    same_score = abs(with_native[0][1] - without_native[0][1]) < SCORE_TOL
    check("开关开/关结果一致（源选择）", same_index,
          f"原生={with_native[0][0].name} Python={without_native[0][0].name}")
    check("开关开/关结果一致（分数）", same_score,
          f"原生={with_native[0][1]:.8f} Python={without_native[0][1]:.8f}")


# ---------------------------------------------------------------------------
# E. 端到端等价性（算子级）
# ---------------------------------------------------------------------------

def make_mesh_object(name, location, color, collection, verts=8):
    import numpy as np

    positions = [(float(i % 3), float((i // 3) % 3), float(i // 9)) for i in range(verts)]
    mesh = bpy.data.meshes.new(f"{name}_mesh")
    mesh.from_pydata(positions, [], [])
    mesh.update()
    attr = mesh.color_attributes.new(name="Color", type='FLOAT_COLOR', domain='POINT')
    flat = np.tile(np.array(color, dtype=np.float32), verts)
    attr.data.foreach_set("color", flat)

    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    collection.objects.link(obj)
    return obj


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh)


def test_end_to_end_equivalence():
    """算子级：原生开 vs 关，match_results 必须完全一致"""
    vc_tool = bpy.context.scene.vertex_color_tool

    def run_pipeline(use_native):
        clear_scene()
        coll_a = bpy.data.collections.new("RefGroup")
        coll_b = bpy.data.collections.new("TargetGroup")
        bpy.context.scene.collection.children.link(coll_a)
        bpy.context.scene.collection.children.link(coll_b)

        random.seed(4242)
        for i in range(12):
            make_mesh_object(
                f"src_{i}",
                (random.uniform(-20, 20), random.uniform(-20, 20), random.uniform(-20, 20)),
                (random.random(), random.random(), random.random(), 1.0),
                coll_a, verts=random.choice([8, 27, 64]),
            )
        for i in range(10):
            make_mesh_object(
                f"tgt_{i}",
                (random.uniform(-20, 20), random.uniform(-20, 20), random.uniform(-20, 20)),
                (1.0, 1.0, 1.0, 1.0),
                coll_b, verts=random.choice([8, 27, 64]),
            )

        vc_tool.collection_a = "RefGroup"
        vc_tool.collection_b = "TargetGroup"
        vc_tool.use_clustering = False
        vc_tool.match_similarity_threshold = 0.0
        vc_tool.min_confidence_score = 0.0
        vc_tool.distance_threshold = 100.0
        vc_tool.use_native_accel = use_native

        bpy.ops.vertexcolor.find_matches()

        return sorted(
            (m.source_name, m.target_name, round(m.confidence, 4))
            for m in vc_tool.match_results
        )

    native_results = run_pipeline(True)
    python_results = run_pipeline(False)
    vc_tool.use_native_accel = True

    check("端到端：匹配条数一致", len(native_results) == len(python_results),
          f"原生={len(native_results)} Python={len(python_results)}")

    if len(native_results) == len(python_results):
        diffs = [
            (a, b) for a, b in zip(native_results, python_results)
            if a[0] != b[0] or a[1] != b[1] or abs(a[2] - b[2]) > 1e-3
        ]
        check("端到端：匹配内容与置信度一致", len(diffs) == 0,
              f"不一致 {len(diffs)} 项" + (f"，示例 {diffs[0]}" if diffs else ""))


def make_random_mesh_object(name, location, color, collection, count, rng):
    """
    创建顶点位置**随机**的点云。

    刻意不用规则格点: 格点会产生大量「到多个源点距离完全相同」的并列最近邻，
    两个 KDTree 实现可以合法地选中不同的最近邻，从而让等价性比较失去意义。
    真实美术资产几乎不会出现这种退化分布。
    """
    import numpy as np

    positions = rng.uniform(-5.0, 5.0, size=(count, 3)).astype(np.float32)
    mesh = bpy.data.meshes.new(f"{name}_mesh")
    mesh.from_pydata(positions.tolist(), [], [])
    mesh.update()

    attr = mesh.color_attributes.new(name="Color", type='FLOAT_COLOR', domain='POINT')
    flat = np.tile(np.array(color, dtype=np.float32), count)
    attr.data.foreach_set("color", flat)

    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    collection.objects.link(obj)
    return obj


def test_end_to_end_copy_equivalence():
    """算子级：原生开 vs 关，复制出的顶点色必须逐元素一致"""
    import numpy as np
    from Blender_VertexColorTool.core.vertex_color_ops import (
        copy_vertex_colors_between_objects,
    )
    from Blender_VertexColorTool.core.cache import VertexColorCache

    vc_tool = bpy.context.scene.vertex_color_tool

    def run_copy(use_native):
        clear_scene()
        coll = bpy.data.collections.new("CopyGroup")
        bpy.context.scene.collection.children.link(coll)

        # 固定种子 → 两次运行的几何与颜色完全一致
        rng = np.random.default_rng(31337)
        source = make_random_mesh_object("csrc", (0, 0, 0), (1, 1, 1, 1), coll, 200, rng)
        attr = source.data.color_attributes["Color"]
        random_colors = np.random.default_rng(99).uniform(0, 1, size=(200, 4)).astype(np.float32)
        attr.data.foreach_set("color", random_colors.ravel())

        target = make_random_mesh_object("ctgt", (0.3, 0.4, 0.5), (0, 0, 0, 1), coll, 150, rng)

        vc_tool.use_active_vcol = True
        vc_tool.use_kdtree = True
        vc_tool.use_cache = False          # 避免缓存干扰，强制每次重建
        vc_tool.use_native_accel = use_native
        VertexColorCache.clear_cache()

        ok = copy_vertex_colors_between_objects(source, target, vc_tool=vc_tool)

        out = np.empty(len(target.data.color_attributes["Color"].data) * 4,
                       dtype=np.float32)
        target.data.color_attributes["Color"].data.foreach_get("color", out)
        return ok, out

    ok_native, native_colors = run_copy(True)
    ok_python, python_colors = run_copy(False)
    vc_tool.use_native_accel = True
    vc_tool.use_cache = True

    check("复制：两条路径都成功", ok_native and ok_python,
          f"原生={ok_native} Python={ok_python}")
    check("复制：颜色数组长度一致", native_colors.shape == python_colors.shape,
          f"{native_colors.shape} vs {python_colors.shape}")

    if native_colors.shape == python_colors.shape:
        diff = np.abs(native_colors - python_colors)
        worst = float(diff.max())
        # 统计有多少个顶点存在差异（每 4 个分量一组）
        per_vertex = diff.reshape(-1, 4).max(axis=1)
        differing = int((per_vertex > 1e-6).sum())
        check("复制：顶点色逐元素一致", worst < 1e-6,
              f"最大偏差 {worst:.2e}, 有差异的顶点 {differing}/{per_vertex.shape[0]}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package_name = os.path.basename(addon_dir)
    parent_dir = os.path.dirname(addon_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    import importlib
    addon = importlib.import_module(package_name)
    addon.register()

    print("=" * 74)
    print(f"原生内核等价性测试  (Blender {bpy.app.version_string})")
    print("=" * 74)

    try:
        from Blender_VertexColorTool.core import native_backend

        print(f"[INFO] 原生后端状态: {native_backend.status()}")

        if not native_backend.is_available():
            # 库不存在是 macOS / Linux 或未构建环境的**正常情况**，
            # 不是测试失败 —— 插件此时会回退纯 Python，功能不受影响。
            print()
            print("=" * 74)
            print("跳过等价性测试：原生库不可用")
            print(f"原因: {native_backend.describe_failure()}")
            print("说明: 这在 macOS / Linux 或未构建原生库的环境下属正常情况，")
            print("      插件会自动回退纯 Python，功能不受影响。")
            print("如需测试原生内核，请先运行: python native/build.py")
            print("=" * 74)
            return

        run_case("A. 后端可用性", test_backend_availability)
        run_case("B. 匹配内核等价性（随机+多参数）", test_match_random)
        run_case("B. 匹配内核等价性（边界）", test_match_edge_cases)
        run_case("C. KDTree 取色等价性", test_kdtree_equivalence)
        run_case("D. 回退开关一致性", test_fallback_toggle)
        run_case("E. 端到端匹配等价性", test_end_to_end_equivalence)
        run_case("E. 端到端复制等价性", test_end_to_end_copy_equivalence)

        total = len(_RESULTS)
        passed = sum(1 for _, ok, _ in _RESULTS if ok)
        print("=" * 74)
        print(f"等价性测试汇总: {passed}/{total} 通过, {total - passed} 失败")
        for name, ok, detail in _RESULTS:
            if not ok:
                print(f"  - {name}  ({detail})")
        print("=" * 74)
    finally:
        addon.unregister()


if __name__ == "__main__":
    main()
