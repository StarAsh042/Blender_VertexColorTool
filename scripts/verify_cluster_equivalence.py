"""
聚类等价性压力验证（第 1、2 步优化）

用大量随机场景 + 边界场景，逐档比对「优化后实现」与「优化前黄金参考」
的分组结果，要求**完全一致**（含每簇成员与簇内顺序）。

为什么需要单独脚本:
    bench_cluster.py 的四档对比只用了单一 seed，
    「跑通一次」不等于「语义未变」。本脚本用
    多 seed × 多阈值 × 多分布 × 边界用例做压力验证。

运行:
    python scripts/verify_cluster_equivalence.py
"""

import importlib.util
import math
import os
import random
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mathutils_stub import Vector3  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 黄金参考：优化前原实现的逐字副本
# ---------------------------------------------------------------------------

def _ref_sim(feat1, feat2, vc_tool):
    try:
        distance = (feat1['location'] - feat2['location']).length
        avg_size = (feat1['bounding_sphere_radius'] + feat2['bounding_sphere_radius'])
        if distance > vc_tool.distance_threshold * 0.5:
            return 0.0
        if avg_size > 0.001:
            distance_score = math.exp(-(distance / avg_size))
        else:
            distance_score = 1.0
        size_diff = 0
        for k in range(3):
            max_dim = max(feat1['dimensions'][k], feat2['dimensions'][k])
            if max_dim > 0.001:
                size_diff += abs(feat1['dimensions'][k] - feat2['dimensions'][k]) / max_dim
        size_score = 1.0 - (size_diff / 3.0)
        max_vol = max(feat1['volume'], feat2['volume'], 0.001)
        volume_score = 1.0 - abs(feat1['volume'] - feat2['volume']) / max_vol
        max_vert = max(feat1['vertex_count'], feat2['vertex_count'], 1)
        vertex_score = 1.0 - abs(feat1['vertex_count'] - feat2['vertex_count']) / max_vert
        return min(1.0, (distance_score + size_score + volume_score + vertex_score) / 4.0)
    except Exception:
        return 0.0


def ref_cluster(target_objects, target_features, vc_tool):
    """优化前原实现（黄金参考）"""
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
            if _ref_sim(target_feat_i, target_feat_j, vc_tool) >= vc_tool.clustering_threshold:
                cluster.append(j)
                assigned.add(j)
        clusters.append(cluster)
    return clusters


# ---------------------------------------------------------------------------
# 当前实现加载
# ---------------------------------------------------------------------------

def load_current():
    pkg = types.ModuleType("vceq"); pkg.__path__ = [ROOT]
    utils_pkg = types.ModuleType("vceq.utils"); utils_pkg.__path__ = [os.path.join(ROOT, "utils")]
    core_pkg = types.ModuleType("vceq.core"); core_pkg.__path__ = [os.path.join(ROOT, "core")]
    lg = types.ModuleType("vceq.utils.logging_utils")
    lg.log_error = lg.log_warning = lg.log_info = lambda *a, **k: None
    vcu = types.ModuleType("vceq.utils.vertex_color_utils")
    vcu.get_vertex_color_info = lambda obj: ("Color", "POINT")
    for n, m in [("vceq", pkg), ("vceq.utils", utils_pkg), ("vceq.core", core_pkg),
                 ("vceq.utils.logging_utils", lg),
                 ("vceq.utils.vertex_color_utils", vcu)]:
        sys.modules[n] = m
    spec = importlib.util.spec_from_file_location(
        "vceq.core.matching", os.path.join(ROOT, "core", "matching.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vceq.core.matching"] = mod
    spec.loader.exec_module(mod)
    return mod.cluster_target_objects


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

class FakeObj:
    def __init__(self, name):
        self.name = name


class FakeTool:
    def __init__(self, distance_threshold=50.0, clustering_threshold=0.8):
        self.distance_threshold = distance_threshold
        self.clustering_threshold = clustering_threshold


def build(n, seed, spread, dup_ratio, tool, dist_scale=1.0, dim_scale=1.0):
    rng = random.Random(seed)
    objs, feats = [], {}
    for i in range(n):
        name = f"o{i:05d}"
        objs.append(FakeObj(name))
        feats[name] = {'name': name, 'location': Vector3(0, 0, 0),
                       'dimensions': Vector3(1, 1, 1), 'vertex_count': 100,
                       'volume': 1.0, 'bounding_sphere_radius': 0.5}
    idx, group = 0, 0
    n_dup = int(n * dup_ratio)
    while idx < n and group < n_dup:
        size = min(rng.randint(2, 5), n - idx)
        base = Vector3(rng.uniform(-spread, spread), rng.uniform(-spread, spread),
                       rng.uniform(-spread, spread))
        dim = Vector3(*(rng.uniform(0.5, 4.0) * dim_scale for _ in range(3)))
        vc = rng.randint(20, 5000)
        for k in range(size):
            f = feats[objs[idx + k].name]
            j = 0.01 if k else 0.0
            f['location'] = Vector3(base.x + rng.uniform(-j, j) * dist_scale,
                                    base.y + rng.uniform(-j, j) * dist_scale,
                                    base.z + rng.uniform(-j, j) * dist_scale)
            f['dimensions'] = dim
            f['vertex_count'] = max(1, vc + rng.randint(-2, 2))
            f['volume'] = dim.x * dim.y * dim.z
            f['bounding_sphere_radius'] = max(dim.x, dim.y, dim.z) * 0.5
        idx += size
        group += 1
    for i in range(idx, n):
        f = feats[objs[i].name]
        dim = Vector3(*(rng.uniform(0.5, 4.0) * dim_scale for _ in range(3)))
        f['location'] = Vector3(rng.uniform(-spread, spread) * dist_scale,
                                rng.uniform(-spread, spread) * dist_scale,
                                rng.uniform(-spread, spread) * dist_scale)
        f['dimensions'] = dim
        f['vertex_count'] = rng.randint(20, 5000)
        f['volume'] = dim.x * dim.y * dim.z
        f['bounding_sphere_radius'] = max(dim.x, dim.y, dim.z) * 0.5
    return objs, feats


def edge_cases():
    """边界与退化场景"""
    cases = []
    tool = FakeTool()

    # 1. 空列表
    cases.append(("空列表", [], {}, tool))
    # 2. 单元素
    o, f = build(1, 1, 10, 0.0, tool)
    cases.append(("单元素", o, f, tool))
    # 3. 全部特征缺失
    objs = [FakeObj(f"m{i}") for i in range(5)]
    cases.append(("全部特征缺失", objs, {}, tool))
    # 4. 部分特征缺失（关键：验证 valid_idx 过滤逻辑）
    objs, f = build(10, 2, 10, 0.5, tool)
    for k in (2, 5, 7):
        f.pop(objs[k].name, None)
    cases.append(("部分特征缺失", objs, f, tool))
    # 5. 全部重合于一点（距离 0，最极端的 tie）
    objs, f = build(30, 3, 0.0, 1.0, tool)
    cases.append(("全部重合于一点", objs, f, tool))
    # 6. 距离阈值 0（退化：空间索引不建）
    t0 = FakeTool(distance_threshold=0.0)
    objs, f = build(20, 4, 10, 0.4, t0)
    cases.append(("距离阈值为 0", objs, f, t0))
    # 7. 负坐标（floor 分桶的符号边界）
    objs, f = build(40, 5, 15, 0.3, tool)
    for name in f:
        f[name]['location'] = Vector3(f[name]['location'].x - 7.5,
                                      f[name]['location'].y - 7.5,
                                      f[name]['location'].z - 7.5)
    cases.append(("负坐标", objs, f, tool))
    # 8. 恰好在半径边界上（浮点相等，验证 <= / > 的边界行为）
    t = FakeTool(distance_threshold=2.0)  # cluster_radius = 1.0
    objs, f = build(20, 6, 0.0, 1.0, t)
    for k, name in enumerate(f):
        f[name]['location'] = Vector3(1.0 if k % 2 else 0.0, 0.0, 0.0)
        f[name]['dimensions'] = Vector3(1, 1, 1)
        f[name]['volume'] = 1.0
        f[name]['bounding_sphere_radius'] = 0.5
        f[name]['vertex_count'] = 100
    cases.append(("恰好在半径边界", objs, f, t))
    # 9. 阈值极端：0（全合并）与 1.0（几乎不合并）
    for thr in (0.0, 1.0):
        tt = FakeTool(distance_threshold=50.0, clustering_threshold=thr)
        objs, f = build(50, 7, 20, 0.5, tt)
        cases.append((f"相似度阈值={thr}", objs, f, tt))
    # 10. 网格边界：坐标恰好是 cell_size 的整数倍
    t = FakeTool(distance_threshold=4.0)  # radius=2.0
    objs, f = build(30, 8, 0.0, 1.0, t)
    for k, name in enumerate(f):
        f[name]['location'] = Vector3(2.0 * (k % 3), 2.0 * ((k // 3) % 3), 0.0)
        f[name]['dimensions'] = Vector3(1, 1, 1)
        f[name]['volume'] = 1.0
        f[name]['bounding_sphere_radius'] = 0.5
        f[name]['vertex_count'] = 100
    cases.append(("坐标恰在网格边界", objs, f, t))
    return cases


def main():
    current = load_current()
    total = 0
    failed = 0

    print("=" * 74)
    print("聚类等价性压力验证（优化后 vs 优化前黄金参考）")
    print("=" * 74)

    # --- 边界场景 ---
    print("\n[A] 边界与退化场景")
    for label, objs, feats, tool in edge_cases():
        total += 1
        try:
            ref = ref_cluster(objs, feats, tool)
        except Exception as e:
            ref = f"参考实现异常: {e}"
        try:
            cur = current(objs, feats, tool)
        except Exception as e:
            cur = f"当前实现异常: {e}"
        ok = ref == cur
        if not ok:
            failed += 1
        n_desc = len(ref) if isinstance(ref, list) else "?"
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<22} (簇数={n_desc})")
        if not ok:
            print(f"        参考={str(ref)[:120]}")
            print(f"        当前={str(cur)[:120]}")

    # --- 随机压力场景 ---
    print("\n[B] 随机压力场景（多 seed × 阈值 × 分布）")
    configs = [
        # (spread, dup_ratio, dist_scale, dim_scale)
        (60.0, 0.35, 1.0, 1.0),   # 标准
        (5.0, 0.5, 1.0, 1.0),     # 密集（几乎全在半径内）
        (500.0, 0.05, 1.0, 1.0),   # 稀疏（几乎全被粗筛掉）
        (60.0, 0.35, 20.0, 1.0),   # 位置被放大（考验空间分桶跨度）
        (60.0, 0.35, 0.02, 1.0),   # 位置被压扁（大量近重合）
        (60.0, 0.35, 1.0, 50.0),   # 尺寸被放大
        (30.0, 0.8, 1.0, 1.0),     # 高度重复
    ]
    sizes = (1, 2, 5, 17, 60, 200, 500)
    thresholds = (0.5, 0.8, 0.95)

    n_run = 0
    for size in sizes:
        for seed in range(6):
            for spread, dup, ds, dimS in configs:
                for thr in thresholds:
                    tool = FakeTool(distance_threshold=50.0,
                                    clustering_threshold=thr)
                    objs, feats = build(size, seed, spread, dup, tool, ds, dimS)
                    n_run += 1
                    total += 1
                    ref = ref_cluster(objs, feats, tool)
                    cur = current(objs, feats, tool)
                    if ref != cur:
                        failed += 1
                        print(f"  FAIL n={size} seed={seed} spread={spread} "
                              f"dup={dup} ds={ds} dimS={dimS} thr={thr}")
                        print(f"       ref簇数={len(ref)} cur簇数={len(cur)}")
                        for a, b in zip(ref, cur):
                            if a != b:
                                print(f"       首个差异 ref={a} cur={b}")
                                break
    print(f"  共跑 {n_run} 个随机场景")

    # --- 边界场景：距离恰等于 cluster_radius（半径非零）---
    #
    # 上一轮的场景（cluster_radius=0）**不可观测**，两个原因（QA 核实）：
    #   1) build() 的重复分支含抖动 j = 0.01 if k else 0.0，
    #      spread=0.0 只让 base 为 0，jitter 仍在 -> 距离严格 > 0，
    #      永远碰不到 radius=0 的边界；
    #   2) cluster_radius=0 时 `spatial = ... if cluster_radius > 0 else None`
    #      走退化路径，参考与被测在该输入上恰好同解。
    #
    # 正确做法：用**非零** radius + 距离**恰等于** radius。
    #   distance_threshold=2.0 -> cluster_radius=1.0（非零，走空间索引路径）
    #   两物体距离恰为 1.0 -> `distance > 1.0` 为假（不跳过 -> 同簇）
    #                          `distance >= 1.0` 为真（跳过 -> 各成一簇）
    # 关键：参考实现是**脚本内独立的 ref_cluster**，被测实现来自
    # core/matching.py；变异只改后者，故两者必然不同解 -> 变异可观测。
    print("\n[C] 边界场景：距离恰等于 cluster_radius（半径非零）")

    def make_pair(distance):
        """构造两个特征完全相同、距离恰为 distance 的物体（位置精确可控）"""
        objs, feats = [], {}
        for name, loc in (("p0", Vector3(0.0, 0.0, 0.0)),
                          ("p1", Vector3(distance, 0.0, 0.0))):
            objs.append(FakeObj(name))
            # 维度/顶点数刻意相同 -> 相似度足够高，使分组只由距离决定
            feats[name] = {'name': name, 'location': loc,
                           'dimensions': Vector3(1.0, 1.0, 1.0),
                           'vertex_count': 100, 'volume': 1.0,
                           'bounding_sphere_radius': 0.5}
        return objs, feats

    RADIUS = 1.0
    tool_b = FakeTool(distance_threshold=RADIUS * 2, clustering_threshold=0.5)
    boundary_cases = [
        (RADIUS - 0.001, "距离略小于半径（应同簇）", True),
        (RADIUS, "距离恰等于半径 ★关键边界", True),
        (RADIUS + 0.001, "距离略大于半径（应分簇）", False),
    ]
    for dist, label, should_merge in boundary_cases:
        total += 1
        objs_b, feats_b = make_pair(dist)
        ref = ref_cluster(objs_b, feats_b, tool_b)
        cur = current(objs_b, feats_b, tool_b)
        ok = ref == cur
        if not ok:
            failed += 1
        merged = any(len(c) > 1 for c in cur)
        # 反向对照：确认该场景真的处在预期的合并/分离状态，
        # 否则「ref == cur」可能只是因为两者都没配到对（空洞的一致）。
        state_ok = (merged == should_merge)
        if not state_ok:
            failed += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: ref={ref} cur={cur}")
        print(f"        {'PASS' if state_ok else 'FAIL'}  反向对照: "
              f"期望{'合并' if should_merge else '分离'}"
              f"  实际{'合并' if merged else '分离'}")


    # --- 边界场景：相似度恰等于 clustering_threshold ---
    #
    # QA-4 独立发现的缺口（我上一轮漏掉）：`core/matching.py:408`
    # 的 `if similarity >= threshold:` 此前**零覆盖**——
    # 双向验证都逃逸：改实现侧 `>=`->`>` 全绿，改参考侧也全绿，
    # 说明现有场景里没有任何一对的相似度恰好等于阈值。
    # 这与已修的 `j=0.01` 抖动属同一类问题的残留。
    #
    # 构造思路（**不用抖动**，反解出精确距离）：
    #   sim = (exp(-d/avg) + size + volume + vertex) / 4
    # 令三项常数项皆为 1（两物体尺寸/体积/顶点数完全相同）-> 和为 3
    #   要sim == thr  =>  exp(-d/avg) = 4*thr - 3
    #   =>  d = -avg * ln(4*thr - 3)
    # 取 thr=0.9、avg=1.0 -> d = -ln(0.6) = 0.5108256237659906
    # 该构造实测**浮点余量为 0**（回代相似度恰为 0.900000000000），
    # 真正落在 `>=` 与 `>` 的分界线上。
    # 合法区间提示: 需4*thr-3 ∈ (0,1] => thr ∈ (0.75, 1.0]
    print("\n[D] 边界场景：相似度恰等于 clustering_threshold")
    import math as _math
    SIM_THR = 0.9
    SIM_AVG = 1.0# 两物体 bounding_sphere_radius 各 0.5 -> avg = 1.0
    # 反解：exp(-d/avg) = 4*thr - 3
    SIM_D = -SIM_AVG * _math.log(4 * SIM_THR - 3)
    sim_radius = 1.0# cluster_radius，须大于 SIM_D 以免被距离粗筛挡掉
    tool_s = FakeTool(distance_threshold=sim_radius * 2,
                      clustering_threshold=SIM_THR)

    def make_pair_sim(distance):
        """构造相似度由距离精确决定的一对物体（三项常数项固定为 1）"""
        objs, feats = [], {}
        for name, loc in (("s0", Vector3(0.0, 0.0, 0.0)),
                          ("s1", Vector3(distance, 0.0, 0.0))):
            objs.append(FakeObj(name))
            feats[name] = {'name': name, 'location': loc,
                           'dimensions': Vector3(1.0, 1.0, 1.0),
                           'vertex_count': 100, 'volume': 1.0,
                           'bounding_sphere_radius': 0.5}
        return objs, feats

    sim_cases = [
        (SIM_D - 0.001, "相似度略高于阈值（应同簇）", True),
        (SIM_D, "相似度恰等于阈值 ★关键边界", True),
        (SIM_D + 0.001, "相似度略低于阈值（应分簇）", False),
    ]
    for dist, label, should_merge in sim_cases:
        total += 1
        objs_s, feats_s = make_pair_sim(dist)
        ref = ref_cluster(objs_s, feats_s, tool_s)
        cur = current(objs_s, feats_s, tool_s)
        ok = ref == cur
        if not ok:
            failed += 1
        merged = any(len(c) > 1 for c in cur)
        state_ok = (merged == should_merge)
        if not state_ok:
            failed += 1
        # 打印该场景的实际相似度，便于确认是否精确落在边界上
        sim_val = (_math.exp(-dist / SIM_AVG) + 3) / 4
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: "
              f"相似度={sim_val:.12f} ref={ref} cur={cur}")
        print(f"        {'PASS' if state_ok else 'FAIL'}  反向对照: "
              f"期望{'合并' if should_merge else '分离'}"
              f"  实际={'合并' if merged else '分离'}")

    # 前置断言：确认构造出的相似度**精确等于**阈值（浮点余量为 0）。
    # 若不精确，这条边界就等价于「略高于阈值」，守护不到 >= 与 > 的分界。
    _sim_exact = (_math.exp(-SIM_D / SIM_AVG) + 3) / 4
    total += 1
    exact_ok = (_sim_exact == SIM_THR)
    if not exact_ok:
        failed += 1
    print(f"  {'PASS' if exact_ok else 'FAIL'}  前置: 构造的相似度**精确等于**阈值"
          f"（余量={_sim_exact - SIM_THR:+.3e}，必须为 0 才是真边界）")

    print("\n" + "=" * 74)
    print(f"结果: {total - failed}/{total} 通过" + ("" if failed else "  全部一致 ✔"))
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
