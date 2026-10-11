"""
聚类优化基准与等价性验证（阶段 A 第 1、2 步）

用途:
    1. 用 git stash 取到的「原实现」作为黄金参考，逐档验证分组结果完全一致
    2. 输出 200/500/1000/2000 四档耗时对比表

运行:
    python scripts/bench_cluster.py            # 只跑当前实现
    python scripts/bench_cluster.py --golden   # 与内置黄金参考对比

说明:
    聚类只依赖特征的 location/dimensions/volume/vertex_count/bounding_sphere_radius，
    不需要真实 bpy 物体，因此可以用轻量 FakeObj 在无 Blender 环境跑。
"""

import argparse
import math
import random
import sys
import time
from mathutils_stub import Vector3


# ---------------------------------------------------------------------------
# 黄金参考：优化前的原始实现（逐字复制自优化前的 core/matching.py）
# 只用于对照，绝不参与实际运行。
# ---------------------------------------------------------------------------

def _ref_calculate_target_similarity(feat1, feat2, vc_tool):
    try:
        distance = (feat1['location'] - feat2['location']).length
        avg_size = (feat1['bounding_sphere_radius'] + feat2['bounding_sphere_radius'])
        if distance > vc_tool.distance_threshold * 0.5:
            return 0.0
        if avg_size > 0.001:
            normalized_distance = distance / avg_size
            distance_score = math.exp(-normalized_distance)
        else:
            distance_score = 1.0
        size_diff = 0
        for k in range(3):
            max_dim = max(feat1['dimensions'][k], feat2['dimensions'][k])
            if max_dim > 0.001:
                size_diff += abs(feat1['dimensions'][k] - feat2['dimensions'][k]) / max_dim
        size_score = 1.0 - (size_diff / 3.0)
        vol1 = feat1['volume']
        vol2 = feat2['volume']
        max_vol = max(vol1, vol2, 0.001)
        volume_score = 1.0 - abs(vol1 - vol2) / max_vol
        vert1 = feat1['vertex_count']
        vert2 = feat2['vertex_count']
        max_vert = max(vert1, vert2, 1)
        vertex_score = 1.0 - abs(vert1 - vert2) / max_vert
        total_similarity = (distance_score + size_score + volume_score + vertex_score) / 4.0
        return min(1.0, total_similarity)
    except Exception:
        return 0.0


def ref_cluster_target_objects(target_objects, target_features, vc_tool):
    """优化前的原始实现（黄金参考）"""
    clusters = []
    assigned = set()
    cluster_radius = vc_tool.distance_threshold * 0.5

    for i, target_obj in enumerate(target_objects):
        if i in assigned:
            continue
        cluster = [i]
        assigned.add(i)
        target_feat_i = target_features.get(target_obj.name)
        if not target_feat_i:
            continue
        loc_i = target_feat_i['location']
        for j, other_obj in enumerate(target_objects[i + 1:], i + 1):
            if j in assigned:
                continue
            target_feat_j = target_features.get(other_obj.name)
            if not target_feat_j:
                continue
            if (loc_i - target_feat_j['location']).length > cluster_radius:
                continue
            similarity = _ref_calculate_target_similarity(target_feat_i, target_feat_j, vc_tool)
            if similarity >= vc_tool.clustering_threshold:
                cluster.append(j)
                assigned.add(j)
        clusters.append(cluster)
    return clusters


# ---------------------------------------------------------------------------
# 场景构造
# ---------------------------------------------------------------------------

class FakeObj:
    def __init__(self, name):
        self.name = name


class FakeTool:
    """模拟 vc_tool 的相关属性"""
    def __init__(self, distance_threshold=50.0, clustering_threshold=0.8):
        self.distance_threshold = distance_threshold
        self.clustering_threshold = clustering_threshold
        self.position_decay_factor = 0.5
        self.match_similarity_threshold = 0.7
        self.distance_weight = 0.2
        self.size_weight = 0.3
        self.volume_weight = 0.3
        self.vertex_count_weight = 0.2
        self.use_cache = True
        self.use_kdtree = True
        self.use_native_accel = False


def make_scenario(n, seed=42, spread=60.0, dup_ratio=0.35):
    """
    构造 n 个目标物体的聚类场景。

    为了让聚类结果非退化（不至于全 1 簇或全 n 簇），
    按 dup_ratio 比例生成「近重复」物体对，其余随机散布。
    """
    rng = random.Random(seed)
    objects = []
    features = {}

    n_dup = int(n * dup_ratio)
    for i in range(n):
        name = f"obj_{i:05d}"
        obj = FakeObj(name)
        objects.append(obj)
        features[name] = {
            'name': name,
            'location': Vector3(0.0, 0.0, 0.0),
            'dimensions': Vector3(1.0, 1.0, 1.0),
            'vertex_count': 100,
            'polygon_count': 50,
            'volume': 1.0,
            'bounding_sphere_radius': 0.5,
        }

    # 近重复簇：每组 2~4 个几乎相同的物体
    idx = 0
    group = 0
    while idx < n and group < n_dup:
        size = min(rng.randint(2, 4), n - idx)
        base = Vector3(
            rng.uniform(-spread, spread),
            rng.uniform(-spread, spread),
            rng.uniform(-spread, spread),
        )
        dim = Vector3(rng.uniform(0.5, 4.0), rng.uniform(0.5, 4.0), rng.uniform(0.5, 4.0))
        vc = rng.randint(20, 5000)
        for k in range(size):
            o = objects[idx + k]
            f = features[o.name]
            jitter = 0.01 if k else 0.0
            f['location'] = Vector3(
                base.x + rng.uniform(-jitter, jitter),
                base.y + rng.uniform(-jitter, jitter),
                base.z + rng.uniform(-jitter, jitter),
            )
            f['dimensions'] = Vector3(dim.x, dim.y, dim.z)
            f['vertex_count'] = max(1, vc + rng.randint(-2, 2))
            f['volume'] = dim.x * dim.y * dim.z
            f['bounding_sphere_radius'] = max(dim.x, dim.y, dim.z) * 0.5
        idx += size
        group += 1

    # 其余散布
    for i in range(idx, n):
        o = objects[i]
        f = features[o.name]
        dim = Vector3(rng.uniform(0.5, 4.0), rng.uniform(0.5, 4.0), rng.uniform(0.5, 4.0))
        f['location'] = Vector3(
            rng.uniform(-spread, spread),
            rng.uniform(-spread, spread),
            rng.uniform(-spread, spread),
        )
        f['dimensions'] = dim
        f['vertex_count'] = rng.randint(20, 5000)
        f['volume'] = dim.x * dim.y * dim.z
        f['bounding_sphere_radius'] = max(dim.x, dim.y, dim.z) * 0.5

    return objects, features


# ---------------------------------------------------------------------------
# 基准
# ---------------------------------------------------------------------------

SIZES = (200, 500, 1000, 2000)


def get_current_impl():
    """
    直接按文件路径加载 core/matching.py，绕过包的 __init__.py。

    不能用普通 import：包__init__.py 会 import bpy，在无 Blender 环境必然失败。
    这里用 importlib.util 从源码文件直接建模块，并把它的相对导入
    （from ..utils.logging_utils import ...）替换为占位实现。
    """
    import importlib.util
    import os
    import types

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 造一个假的包层级，让 matching.py 里的相对导入能解析
    pkg = types.ModuleType("vct_bench")
    pkg.__path__ = [root]
    utils_pkg = types.ModuleType("vct_bench.utils")
    utils_pkg.__path__ = [os.path.join(root, "utils")]
    core_pkg = types.ModuleType("vct_bench.core")
    core_pkg.__path__ = [os.path.join(root, "core")]

    logging_stub = types.ModuleType("vct_bench.utils.logging_utils")
    logging_stub.log_error = lambda *a, **k: None
    logging_stub.log_warning = lambda *a, **k: None
    logging_stub.log_info = lambda *a, **k: None

    vcu_stub = types.ModuleType("vct_bench.utils.vertex_color_utils")
    vcu_stub.get_vertex_color_info = lambda obj: ("Color", "POINT")

    sys.modules["vct_bench"] = pkg
    sys.modules["vct_bench.utils"] = utils_pkg
    sys.modules["vct_bench.utils.logging_utils"] = logging_stub
    sys.modules["vct_bench.utils.vertex_color_utils"] = vcu_stub
    sys.modules["vct_bench.core"] = core_pkg

    path = os.path.join(root, "core", "matching.py")
    spec = importlib.util.spec_from_file_location("vct_bench.core.matching", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vct_bench.core.matching"] = mod
    spec.loader.exec_module(mod)
    return mod.cluster_target_objects


def bench(fn, objects, features, tool, repeat=1):
    """返回 (结果, 最短耗时)"""
    best = float('inf')
    result = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn(objects, features, tool)
        best = min(best, time.perf_counter() - t0)
    return result, best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", action="store_true",
                    help="与黄金参考（原实现）逐档对比分组结果")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    current = get_current_impl()
    tool = FakeTool()

    print("=" * 78)
    print("聚类优化基准  (相似度阈值 0.8, 距离阈值 50)")
    print("=" * 78)
    header = f"{'规模':>8} | {'原实现(s)':>12} | {'当前实现(s)':>12} | {'加速比':>8} | 簇数"
    if args.golden:
        print(header)
    else:
        print(f"{'规模':>8} | {'当前实现(s)':>12} | {'簇数':>6} | {'最大簇':>7}")
    print("-" * 78)

    all_equal = True
    rows = []
    for n in SIZES:
        objects, features = make_scenario(n)
        ref_res, ref_t = bench(ref_cluster_target_objects, objects, features, tool,
                               args.repeat)
        cur_res, cur_t = bench(current, objects, features, tool, args.repeat)

        equal = ref_res == cur_res
        if args.golden and not equal:
            all_equal = False
        n_clusters = len(cur_res)
        biggest = max((len(c) for c in cur_res), default=0)
        speedup = ref_t / cur_t if cur_t > 0 else 0.0
        rows.append((n, ref_t, cur_t, speedup, n_clusters, biggest, equal))

        if args.golden:
            print(f"{n:>8} | {ref_t:>12.4f} | {cur_t:>12.4f} | {speedup:>7.2f}x | "
                  f"{n_clusters:>5} {'✓一致' if equal else '✗不一致'}")
        else:
            print(f"{n:>8} | {cur_t:>12.4f} | {n_clusters:>6} | {biggest:>7}")

    print("-" * 78)
    if args.golden:
        print("等价性:", "全部一致 ✓" if all_equal else "存在不一致 ✗")
        if not all_equal:
            for n, _, _, _, _, _, eq in rows:
                if not eq:
                    print(f"  规模 {n}: 分组结果与原实现不同")
    else:
        print("提示: 加 --golden 可验证与优化前实现的分组结果是否完全一致")

    # 供程序化调用
    return 0 if (all_equal or not args.golden) else 1


if __name__ == "__main__":
    sys.exit(main())
