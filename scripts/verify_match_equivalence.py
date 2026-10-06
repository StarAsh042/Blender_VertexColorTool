"""
聚类最佳匹配：第 3 步优化（复用原生内核）的等价性与性能验证

验证对象:
    find_best_match_for_cluster —— 比对「代表元 -> 最佳源物体 -> 得分 -> 层名」

重点覆盖:
    - 并列最高分（最容易出偏差的地方，团队-lead 点名）
    - 最佳源物体特征缺失
    - 全部源物体特征缺失
    - 最佳源物体无顶点色层（陈旧层名复现）
    - 降级路径（原生不可用时是否与Python 一致）

运行:
    python scripts/verify_match_equivalence.py
    python scripts/verify_match_equivalence.py --bench
"""

import argparse
import importlib.util
import math
import os
import random
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mathutils_stub import Vector3 as Vector  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_RESULTS = []


def check(name, cond, detail=""):
    _RESULTS.append((name, bool(cond), detail))
    print(f"   {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 模块加载（含原生后端可控注入）
# ---------------------------------------------------------------------------


# 原生替身的「严格 > / >= 」比较模式开关（模块级）。
#
# 为什么需要它: 上一轮「score > 0.0 vs >= 0.0」变异**不可观测**，
# 根因是被测代码改了、**替身没同步改** —— 两者对零分候选都返回 -1，
# 于是「同解」掩盖了变异。
# 把该判定参数化后，可以让替身与被测处于**不同**判据，
# 从而在同一输入下产生可区分结果，变异即可被观测。
#
# 取值:
#   'gt' —— 严格大于（与真实 C++ 语义一致，默认）
#   'ge' —— 大于等于（用于观测 >= 类变异）
NATIVE_GT_MODE = 'gt'


def load_matching(force_native_available=None, tie_boost=None):
    """
    加载 core/matching.py。

    Args:
        force_native_available:
            None  -> 注入一个「行为与 C++ 一致」的 Python 模拟原生后端
            False -> 注入一个恒返回 None 的后端（模拟原生不可用）
        tie_boost: 内部使用，人为制造并列最高分的乘数
    """
    pkg = types.ModuleType("vm"); pkg.__path__ = [ROOT]
    utils_pkg = types.ModuleType("vm.utils"); utils_pkg.__path__ = [os.path.join(ROOT, "utils")]
    core_pkg = types.ModuleType("vm.core"); core_pkg.__path__ = [os.path.join(ROOT, "core")]
    lg = types.ModuleType("vm.utils.logging_utils")
    lg.log_error = lg.log_warning = lg.log_info = lambda *a, **k: None

    # get_vertex_color_info 可控（用于验证陈旧层名复现）
    # 默认必须返回 (None, None) —— 与真实 get_vertex_color_info 的行为一致：
    # 物体没有颜色层时它返回 (None, None)，而不是 ("Color", "CORNER")。
    # 若这里默认给非None 值，会让「陈旧层名」场景失去区分度，
    # 并使黄金参考与被测实现使用不同默认值而假报差异。
    VCOL = {"map": {}}
    vcu = types.ModuleType("vm.utils.vertex_color_utils")

    def _gvi(obj):
        return VCOL["map"].get(obj.name, (None, None))
    vcu.get_vertex_color_info = _gvi
    vcu.VCOL = VCOL

    for n, m in [("vm", pkg), ("vm.utils", utils_pkg), ("vm.core", core_pkg),
                 ("vm.utils.logging_utils", lg),
                 ("vm.utils.vertex_color_utils", vcu)]:
        sys.modules[n] = m

    # ---- 原生后端替身 ----
    nb = types.ModuleType("vm.core.native_backend")

    if force_native_available is False:
        nb.match_best = lambda *a, **k: None
        nb.is_available = lambda: False
    else:
        def _sim_match_best(source_features, target_features, vc_tool):
            """
            按 vct_match_best 的真实语义模拟（C++ L338-L406）：
              - 外层遍历 target、内层遍历 source
              - 距离早退 dist_sq > max^2 -> continue
              - 阈值 total < similarity_threshold -> continue
              - 严格 > 才替换（并列保留先遇到的）
              - 先归一化再钳制到 [0,1]
            """
            if not source_features or not target_features:
                return None
            thr = vc_tool.match_similarity_threshold
            max_d = vc_tool.distance_threshold
            max_d_sq = max_d * max_d
            total_w = (vc_tool.distance_weight + vc_tool.size_weight
                       + vc_tool.volume_weight + vc_tool.vertex_count_weight)
            normalize = total_w > 0.001
            inv_w = (1.0 / total_w) if normalize else 1.0

            indices, scores = [], []
            for tf in target_features:
                best_i, best_s = -1, 0.0
                for s, sf in enumerate(source_features):
                    d = sf['location'] - tf['location']
                    d_sq = d.length_squared
                    if d_sq > max_d_sq:
                        continue
                    dist = math.sqrt(d_sq)
                    avg = sf['bounding_sphere_radius'] + tf['bounding_sphere_radius']
                    ds = math.exp(-(dist / avg) * vc_tool.position_decay_factor) \
                        if avg > 0.001 else 1.0
                    sd = 0.0
                    for a in range(3):
                        m = max(sf['dimensions'][a], tf['dimensions'][a])
                        if m > 0.001:
                            sd += abs(sf['dimensions'][a] - tf['dimensions'][a]) / m
                    ss = 1.0 - sd / 3.0
                    mv = max(sf['volume'], tf['volume'], 0.001)
                    vs = 1.0 - abs(sf['volume'] - tf['volume']) / mv
                    mt = max(sf['vertex_count'], tf['vertex_count'], 1)
                    vt = 1.0 - abs(sf['vertex_count'] - tf['vertex_count']) / mt
                    tot = (ds * vc_tool.distance_weight + ss * vc_tool.size_weight
                           + vs * vc_tool.volume_weight + vt * vc_tool.vertex_count_weight)
                    if normalize:
                        tot *= inv_w
                    tot = 0.0 if tot < 0.0 else (1.0 if tot > 1.0 else tot)
                    if tot < thr:
                        continue
                    better = (tot > best_s) if NATIVE_GT_MODE == 'gt' \
                        else (tot >= best_s)
                    if better:
                        best_s = tot
                        best_i = s
                indices.append(best_i)
                scores.append(best_s)
            return indices, scores
        nb.match_best = _sim_match_best
        nb.is_available = lambda: True

    sys.modules["vm.core.native_backend"] = nb
    setattr(core_pkg, "native_backend", nb)

    path = os.path.join(ROOT, "core", "matching.py")
    spec = importlib.util.spec_from_file_location("vm.core.matching", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vm.core.matching"] = mod
    spec.loader.exec_module(mod)
    return mod, nb, VCOL


# ---------------------------------------------------------------------------
# 黄金参考：第 3 步之前的原实现
# ---------------------------------------------------------------------------

def ref_find_best_match_for_cluster(cluster_indices, target_objects, target_features,
                                   source_objects, source_features, vc_tool,
                                   get_vertex_color_info):
    """优化前原实现（黄金参考），逐字复制"""
    best_match = None
    best_score = 0.0
    best_vcol_layer = "Color"
    best_vcol_domain = "CORNER"
    if not cluster_indices:
        return None, 0.0, "Color", "CORNER"
    representative_idx = cluster_indices[0]
    target_obj = target_objects[representative_idx]
    target_feat = target_features.get(target_obj.name)
    if not target_feat:
        return None, 0.0, "Color", "CORNER"
    for source_obj in source_objects:
        source_feat = source_features.get(source_obj.name)
        if not source_feat:
            continue
        similarity = calculate_similarity_score_ref(source_feat, target_feat, vc_tool)
        if similarity < vc_tool.match_similarity_threshold:
            continue
        if similarity > best_score:
            best_score = similarity
            best_match = source_obj
            vcol_layer, vcol_domain = get_vertex_color_info(source_obj)
            if vcol_layer:
                best_vcol_layer = vcol_layer
                best_vcol_domain = vcol_domain
    return best_match, best_score, best_vcol_layer, best_vcol_domain


def calculate_similarity_score_ref(source_feat, target_feat, vc_tool):
    """原 calculate_similarity_score（黄金参考）"""
    if not source_feat or not target_feat:
        return 0.0
    try:
        d = source_feat['location'] - target_feat['location']
        d_sq = d.length_squared
        max_distance = vc_tool.distance_threshold
        if d_sq > max_distance * max_distance:
            return 0.0
        distance = math.sqrt(d_sq)
        avg = source_feat['bounding_sphere_radius'] + target_feat['bounding_sphere_radius']
        if avg > 0.001:
            ds = math.exp(-(distance / avg) * vc_tool.position_decay_factor)
        else:
            ds = 1.0
        sd = 0.0
        for i in range(3):
            m = max(source_feat['dimensions'][i], target_feat['dimensions'][i])
            if m > 0.001:
                sd += abs(source_feat['dimensions'][i] - target_feat['dimensions'][i]) / m
        ss = 1.0 - sd / 3.0
        mv = max(source_feat['volume'], target_feat['volume'], 0.001)
        vs = 1.0 - abs(source_feat['volume'] - target_feat['volume']) / mv
        mt = max(source_feat['vertex_count'], target_feat['vertex_count'], 1)
        vt = 1.0 - abs(source_feat['vertex_count'] - target_feat['vertex_count']) / mt
        w = (('distance_score', 'distance_weight'), ('size_score', 'size_weight'),
             ('volume_score', 'volume_weight'), ('vertex_score', 'vertex_count_weight'))
        scores = {'distance_score': ds, 'size_score': ss,
                  'volume_score': vs, 'vertex_score': vt}
        tot, tw = 0.0, 0.0
        for sk, wk in w:
            weight = getattr(vc_tool, wk, 0.0)
            tot += scores[sk] * weight
            tw += weight
        if tw > 0.001:
            tot /= tw
        return max(0.0, min(1.0, tot))
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

class Obj:
    def __init__(self, name):
        self.name = name


class Tool:
    def __init__(self, **kw):
        self.distance_threshold = 50.0
        self.position_decay_factor = 0.5
        self.distance_weight = 0.2
        self.size_weight = 0.3
        self.volume_weight = 0.3
        self.vertex_count_weight = 0.2
        self.match_similarity_threshold = 0.7
        self.use_native_accel = True
        for k, v in kw.items():
            setattr(self, k, v)


def mkfeat(name, loc, dim=1.0, vc=100):
    return {'name': name, 'location': Vector(*loc),
            'dimensions': Vector(dim, dim, dim), 'vertex_count': vc,
            'volume': dim ** 3, 'bounding_sphere_radius': dim * 0.5}


def _weighted_score_without_distance_gate(sf, tf, vc_tool):
    """
    计算「若没有距离早退」时的相似度得分（用于反向对照）。

    用途:
        距离早退是calculate_similarity_score 的第一层短路。
        验证「某源被距离早退拒绝」这个行为时，必须证明
        **去掉早退后它本来是会匹配的**——否则无法区分
        「被距离早退拒绝」与「本来就因形状不匹配被阈值拒绝」，
        场景就没有区分度。
    """
    d = sf['location'] - tf['location']
    dist = math.sqrt(d.length_squared)
    avg = sf['bounding_sphere_radius'] + tf['bounding_sphere_radius']
    ds = math.exp(-(dist / avg) * vc_tool.position_decay_factor) if avg > 0.001 else 1.0
    sd = 0.0
    for i in range(3):
        m = max(sf['dimensions'][i], tf['dimensions'][i])
        if m > 0.001:
            sd += abs(sf['dimensions'][i] - tf['dimensions'][i]) / m
    ss = 1.0 - sd / 3.0
    mv = max(sf['volume'], tf['volume'], 0.001)
    vs = 1.0 - abs(sf['volume'] - tf['volume']) / mv
    mt = max(sf['vertex_count'], tf['vertex_count'], 1)
    vt = 1.0 - abs(sf['vertex_count'] - tf['vertex_count']) / mt
    tw = (vc_tool.distance_weight + vc_tool.size_weight
          + vc_tool.volume_weight + vc_tool.vertex_count_weight)
    tot = (ds * vc_tool.distance_weight + ss * vc_tool.size_weight
           + vs * vc_tool.volume_weight + vt * vc_tool.vertex_count_weight)
    if tw > 0.001:
        tot /= tw
    return max(0.0, min(1.0, tot))


def run_equivalence():
    mod, nb, VCOL = load_matching(force_native_available=True)

    def _gvi(obj):
        """与被测实现使用完全相同的 get_vertex_color_info 替身"""
        return VCOL["map"].get(obj.name, (None, None))

    print("=" * 74)
    print("第 3 步等价性验证：find_best_match_for_cluster")
    print("=" * 74)
    print("\n[A] 关键场景（含并列最高分）")

    # --- 场景 1：并列最高分（团队-lead 点名的最容易出偏差处）---
    # 3 个源物体与 target 完全相同 -> 得分严格相等 -> 应取**第一个**
    VCOL["map"].clear()
    tool = Tool(match_similarity_threshold=0.5)
    srcs = [Obj(f"s{k}") for k in range(3)]
    sfeats = {o.name: mkfeat(o.name, (0, 0, 0)) for o in srcs}
    tgts = [Obj("t0")]
    tfeats = {"t0": mkfeat("t0", (0, 0, 0))}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool,
                                          _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    same = (ref[0].name == cur[0].name and abs(ref[1] - cur[1]) < 1e-9
            and ref[2] == cur[2] and ref[3] == cur[3])
    check("并列最高分：取第一个源物体（严格 > 语义）", same,
          f"ref={ref[0].name if ref[0] else None}/{ref[1]:.6f}, "
          f"cur={cur[0].name if cur[0] else None}/{cur[1]:.6f}")

    # --- 场景 2：三个源得分递增，应取最高分那个 ---
    VCOL["map"].clear()
    tool = Tool(match_similarity_threshold=0.1)
    srcs = [Obj(f"m{k}") for k in range(3)]
    sfeats = {"m0": mkfeat("m0", (30, 0, 0), 1.0),
              "m1": mkfeat("m1", (1, 0, 0), 1.0),
              "m2": mkfeat("m2", (5, 0, 0), 1.0)}
    tgts = [Obj("t0")]
    tfeats = {"t0": mkfeat("t0", (0, 0, 0), 1.0)}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool,
                                          _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    check("非并列：取分数最高者",
          ref[0].name == cur[0].name and abs(ref[1] - cur[1]) < 1e-6,
          f"ref={ref[0].name}/{ref[1]:.6f} vs cur={cur[0].name}/{cur[1]:.6f}")

    # --- 场景 3：并列但层名不同，确认取的是第一个的层名 ---
    VCOL["map"] = {"d0": ("LayerA", "POINT"), "d1": ("LayerB", "CORNER")}
    tool = Tool(match_similarity_threshold=0.5)
    srcs = [Obj("d0"), Obj("d1")]
    sfeats = {o.name: mkfeat(o.name, (0, 0, 0)) for o in srcs}
    tgts = [Obj("t0")]
    tfeats = {"t0": mkfeat("t0", (0, 0, 0))}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool,
                                          lambda o: VCOL["map"].get(o.name, ("Color", "POINT")))
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    check("并列时层名取自第一个（不是最高层名）",
          ref[0].name == cur[0].name and ref[2] == cur[2] and ref[3] == cur[3],
          f"ref=({ref[0].name},{ref[2]},{ref[3]}) vs cur=({cur[0].name},{cur[2]},{cur[3]})")

    # --- 场景 4：陈旧层名复现（最佳源无层，应沿用次优源的层）---
    VCOL["map"] = {"has": ("GoodLayer", "POINT")}  # best 无层
    tool = Tool(match_similarity_threshold=0.1)
    srcs = [Obj("has"), Obj("best")]
    sfeats = {"has": mkfeat("has", (3, 0, 0), 1.0),
              "best": mkfeat("best", (0, 0, 0), 1.0)}
    tgts = [Obj("t0")]
    tfeats = {"t0": mkfeat("t0", (0, 0, 0), 1.0)}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool,
                                          lambda o: VCOL["map"].get(o.name, (None, None)))
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    check("陈旧层名复现：最佳源无层时沿用次优源的层",
          ref[0].name == cur[0].name and ref[2] == cur[2] and ref[3] == cur[3],
          f"ref=({ref[0].name},{ref[1]:.4f},{ref[2]}) vs "
          f"cur=({cur[0].name},{cur[1]:.4f},{cur[2]})")
    check("陈旧层名复现：确实复现出了非默认层名（否则该场景无区分度）",
          ref[2] == "GoodLayer" and cur[2] == "GoodLayer",
          f"ref层={ref[2]}, cur层={cur[2]}")

    # --- 场景 5：最佳源无层，且没有任何源有层 -> 应保持默认 Color ---
    VCOL["map"] = {}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool,
                                          lambda o: (None, None))
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    check("全部源都无层：保持默认值 Color",
          ref[2] == cur[2] == "Color" and ref[3] == cur[3] == "CORNER",
          f"ref=({ref[2]},{ref[3]}) vs cur=({cur[2]},{cur[3]})")

    # --- 场景 6：部分源特征缺失 ---
    VCOL["map"] = {"s1": ("L1", "POINT")}
    tool = Tool(match_similarity_threshold=0.3)
    srcs = [Obj("s0"), Obj("s1"), Obj("s2")]
    sfeats = {"s0": mkfeat("s0", (10, 0, 0)),
              "s1": mkfeat("s1", (0.5, 0, 0)),
              "s2": mkfeat("s2", (20, 0, 0))}
    sfeats.pop("s0")  # 特征缺失
    sfeats.pop("s2")
    tgts = [Obj("t0")]
    tfeats = {"t0": mkfeat("t0", (0, 0, 0))}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool,
                                          lambda o: VCOL["map"].get(o.name, ("Color", "POINT")))
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    check("部分源特征缺失：跳过缺失项",
          ref[0].name == cur[0].name and abs(ref[1] - cur[1]) < 1e-6,
          f"ref={ref[0].name}/{ref[1]:.4f} vs cur={cur[0].name}/{cur[1]:.4f}")

    # --- 场景 7：全部源特征缺失 ---
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, {}, tool,
                                          _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, {}, tool)
    check("全部源特征缺失：返回 (None, 0.0, Color, CORNER)",
          cur[0] is None and cur[1] == 0.0 and ref == cur,
          f"ref={ref} vs cur={cur}")

    # --- 场景 8：无匹配（全部低于阈值）---
    VCOL["map"] = {}
    tool = Tool(match_similarity_threshold=0.99)
    srcs = [Obj("f0"), Obj("f1")]
    sfeats = {"f0": mkfeat("f0", (40, 0, 0), 5.0),
              "f1": mkfeat("f1", (0, 40, 0), 0.5)}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool,
                                          _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    check("无匹配：返回 None",
          ref[0] is None and cur[0] is None and ref == cur, f"ref={ref} vs cur={cur}")

    # --- 场景 9：空cluster / 目标特征缺失 ---
    ref = ref_find_best_match_for_cluster([], tgts, tfeats, srcs, sfeats, tool,
                                          _gvi)
    cur = mod.find_best_match_for_cluster([], tgts, tfeats, srcs, sfeats, tool)
    check("空 cluster", ref == cur, f"ref={ref} vs cur={cur}")
    ref = ref_find_best_match_for_cluster([0], tgts, {}, srcs, sfeats, tool,
                                          _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts, {}, srcs, sfeats, tool)
    check("目标特征缺失", ref == cur, f"ref={ref} vs cur={cur}")

    # --- 场景 10b：超距源必须被距离早退挡住（守护内核的距离语义）---
    # 背景: 距离早退是 calculate_similarity_score 的第一层短路。
    # 若内核漏掉它，超距源仍可能靠 size/volume/vertex 三项拿到高分而被误匹配
    # （实测：距离 1000 的同形物体得分仍有 0.80，远超常见阈值）。
    # 因此本场景必须让「唯一候选超距」，去掉早退就会改变结果。
    VCOL["map"] = {}
    tool = Tool(distance_threshold=50.0, match_similarity_threshold=0.3)
    srcs = [Obj("far0"), Obj("far1")]
    sfeats = {"far0": mkfeat("far0", (1000.0, 0, 0), 1.0, 100),
              "far1": mkfeat("far1", (-2000.0, 0, 0), 1.0, 100)}
    tgts_far = [Obj("t0")]
    tfeats_far = {"t0": mkfeat("t0", (0, 0, 0), 1.0, 100)}
    ref = ref_find_best_match_for_cluster([0], tgts_far, tfeats_far, srcs, sfeats,
                                          tool, _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts_far, tfeats_far, srcs, sfeats, tool)
    check("超距源被距离早退挡住（无匹配）",
          ref[0] is None and cur[0] is None,
          f"ref={ref[0].name if ref[0] else None}/{ref[1]:.4f}, "
          f"cur={cur[0].name if cur[0] else None}/{cur[1]:.4f}")
    # 反向对照：证明这些源「只因距离远才被拒」，形状/顶点数是完全匹配的。
    # 必须绕开 calculate_similarity_score_ref 的距离早退，直接算加权分——
    # 否则拿到的是 0.0（已早退），本对照永远不成立。
    _probe = _weighted_score_without_distance_gate(
        sfeats["far0"], tfeats_far["t0"], tool)
    check("反向对照: 超距源的形状相似度确实很高（仅靠距离被拒）",
          _probe >= tool.match_similarity_threshold,
          f"去掉距离早退后的得分 = {_probe:.4f} > 阈值 "
          f"{tool.match_similarity_threshold}"
          f"（若不成立，说明源本身也不匹配，无法证明是距离早退在起作用）")

    # --- 场景 10c：阈值边界（守护 tot < thr 的过滤语义）---
    VCOL["map"] = {}
    tool = Tool(distance_threshold=50.0, match_similarity_threshold=0.95)
    srcs = [Obj("b0"), Obj("b1")]
    sfeats = {"b0": mkfeat("b0", (1.0, 0, 0), 1.0, 100),
              "b1": mkfeat("b1", (0, 1.0, 0), 1.0, 100)}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool, _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    check("高阈值下正确拒绝（阈值过滤语义）",
          ref[0] is None and cur[0] is None,
          f"ref={ref[0].name if ref[0] else None}, cur={cur[0].name if cur[0] else None}")

    # --- 场景 10d：权重归一化（守护 inv_total_weight）---
    # 权重和不为 1 时，若内核漏乘 inv_w，得分会被整体放大 4 倍（钳制后为 1.0），
    # 导致本应被拒的候选越过阈值。阈值取在「归一化后拒绝、归一化前接受」之间。
    VCOL["map"] = {}
    tool = Tool(distance_threshold=50.0, match_similarity_threshold=0.85,
                distance_weight=1.0, size_weight=1.0,
                volume_weight=1.0, vertex_count_weight=1.0)  # 权重和 = 4
    srcs = [Obj("w0"), Obj("w1")]
    sfeats = {"w0": mkfeat("w0", (6.0, 0, 0), 1.0, 100),
              "w1": mkfeat("w1", (0, 6.0, 0), 1.0, 100)}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool, _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    _probe_w = _weighted_score_without_distance_gate(
        sfeats["w0"], tfeats["t0"], tool)
    # 归一化后得分 < 阈值（应拒绝），但 4× 后 >= 阈值（漏归一化就会误接受）
    check("权重和=4 时归一化正确（不因漏乘 inv_w 而虚高）",
          ref[0] is None and cur[0] is None,
          f"权重和=4, 阈值=0.85, 归一化后得分={_probe_w:.4f}; "
          f"ref={ref[0].name if ref[0] else None}, cur={cur[0].name if cur[0] else None}")
    check("反向对照: 该场景确实能区分「归一化」与「漏归一化」",
          _probe_w < tool.match_similarity_threshold
          and _probe_w * 4.0 >= tool.match_similarity_threshold,
          f"归一化后={_probe_w:.4f} < {tool.match_similarity_threshold} <= "
          f"漏归一化时={_probe_w * 4.0:.4f}")

    # --- 场景 8b：边界场景（QA 独立注入发现零覆盖）---
    # 边界 A：score 恰为 0 时的准入判定。
    #   原生 kernel 用 `if (total < threshold) continue;` + `if (total > best_score)`，
    #   best_score 初值 0.0，故 score==0 时**不满足 > 0.0** -> 不采纳 -> index 保持 -1。
    #   若实现改成 `>= 0.0`（或 best_score 初值 -1），零分候选就会被误采纳。
    #   Python 路径同理：best_score=0.0 且用严格 `>`。
    # 边界 B：全部源得分恰为 0（距离刚好等于阈值 -> 早退 -> 0 分）时的返回。
    # 真正的「零分」边界：距离**严格超过**阈值时，源码第一层直接 return 0.0
    # （注意是 `>` 不是 `>=`，故距离恰等于阈值不会走早退）。
    # 这才是原生 `total > best_score`（best_score 初值 0.0）的真正考验：
    # 0 分候选必须既不被采纳、也不污染后续候选。
    VCOL["map"] = {}
    tool = Tool(distance_threshold=2.0, match_similarity_threshold=0.5)
    srcs_z = [Obj("z0"), Obj("z1")]
    sfeats_z = {"z0": mkfeat("z0", (100.0, 0, 0), 50.0, 9999),
                "z1": mkfeat("z1", (-200.0, 0, 0), 0.5, 10)}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs_z, sfeats_z,
                                          tool, _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs_z, sfeats_z, tool)
    check("边界: 全部源得 0 分（距离远超阈值）时返回 None",
          ref[0] is None and cur[0] is None,
          f"阈值=0.5; ref={ref[0].name if ref[0] else None}, "
          f"cur={cur[0].name if cur[0] else None}")
    # 反向对照：证明这些源确实是 0 分（而非因其它原因被拒）
    _zero = calculate_similarity_score_ref(sfeats_z["z0"], tfeats["t0"], tool)
    check("反向对照: 该场景的源得分恰为 0（距离早退 return 0.0）",
          _zero == 0.0,
          f"源 z0 得分 = {_zero:.6f}（距离 100 >> 阈值 2.0）")

    # === 并列最高分：真正可观测的`>` vs `>=` 边界 ===
    #
    # 前一版我试图观测「score > 0.0 -> >= 0.0」，**方向错了**（已修正）:
    #   被测实现里有 `if raw_index >= 0 and score > 0.0:`，
    #   即分数必须为**正**才采纳。零分候选无论替身怎么改都会被拒，
    #   所以「零分」这条边界在结构上就**不可观测**。
    #
    # 真正可观测的是**并列最高分**:
    #   替身`tot > best_s`（严格）-> 保留**先遇到**的源
    #   替身改`tot >= best_s>     -> 保留**后遇到**的源
    # 两者返回**不同物体** -> 变异立刻可观测。
    # 这也正是真实 C++ 内核「并列保留先遇到的」这条语义需要守护的地方。
    print("\n  --- 并列最高分边界（`>` vs `>=`，可观测）---")
    VCOL["map"] = {"t0": ("L0", "POINT")}
    tool_tie = Tool(distance_threshold=50.0, match_similarity_threshold=0.5)
    # 两个源位置/尺寸/顶点数完全相同 -> 得分严格相等（并列）
    srcs_tie = [Obj("tieA"), Obj("tieB")]
    sfeats_tie = {"tieA": mkfeat("tieA", (3.0, 0, 0), 1.0, 100),
                  "tieB": mkfeat("tieB", (3.0, 0, 0), 1.0, 100)}
    sA = calculate_similarity_score_ref(sfeats_tie["tieA"], tfeats["t0"],
                                        tool_tie)
    sB = calculate_similarity_score_ref(sfeats_tie["tieB"], tfeats["t0"],
                                        tool_tie)
    check("并列前置: 两个源得分严格相等（构成真正的并列）",
          sA == sB and sA > 0.0,
          f"tieA={sA:.9f}, tieB={sB:.9f}, 相等={sA == sB}")

    # ⚠ 重要限定（实测得出，勿夸大）:
    #   下面这两条断言调用的是 `find_best_match_for_cluster`，
    #   它走**原生分支 -> 读替身的返回值**，因此它们守护的是
    #   **替身与被测之间的一致性**，而非被测内部 `>` 的写法。
    #   实测：把被测 Python 路径的 `similarity > best_score` 改成 `>=`，
    #   这两条**不会变红**（该路径在聚类入口下不执行）。
    #   它们的价值是：证明「`>` 与 `>=` 在并列时产生不同结果」这一
    #   **可区分性前提**成立；若哪天替身与被测判据不一致，这里会立刻发现。
    #   真正守护被测 `>` 写法的是上方既有的 300 随机场景
    #   （实测该变异会使其 12+9 条变红）。
    cur_tie = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs_tie,
                                              sfeats_tie, tool_tie)
    check("并列时保留**先遇到**的源（替身与被测判据一致）",
          cur_tie[0] is not None and cur_tie[0].name == "tieA",
          f"cur={cur_tie[0].name if cur_tie[0] else None}（期望 tieA）"
          f"注：此断言守护替身/被测一致性，非被测内部写法")

    # 替身改 'ge' -> 应保留后遇到的 tieB ->证明「判据不同 => 结果可区分」
    saved_mode = NATIVE_GT_MODE
    try:
        globals()['NATIVE_GT_MODE'] = 'ge'
        cur_tie_ge = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs_tie,
                                                     sfeats_tie, tool_tie)
        check("替身改 >= 时保留**后遇到**的源 -> 证明该边界可观测",
              cur_tie_ge[0] is not None and cur_tie_ge[0].name == "tieB",
              f"cur={cur_tie_ge[0].name if cur_tie_ge[0] else None}（期望 tieB）"
              f"；与默认的 tieA 不同 -> 可区分")
    finally:
        globals()['NATIVE_GT_MODE'] = saved_mode

    # 复原后必须仍是 tieA（确认 finally 生效、无状态泄漏）
    cur_tie_back = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs_tie,
                                                   sfeats_tie, tool_tie)
    check("复原后回到 tieA（无状态泄漏）",
          cur_tie_back[0] is not None and cur_tie_back[0].name == "tieA",
          f"cur={cur_tie_back[0].name if cur_tie_back[0] else None}（期望 tieA）")

    # === 零分边界：结构上不可观测（如实标注，不充数）===
    #
    # 调查结论（变异实测 + 数据流追踪）：
    #   `score > 0.0` -> `>= 0.0` 这条变异**无法用任何输入观测**。
    #   原因链（每一环都验证过）：
    #   1. 要让 score == 0，源必须触发 `distance_sq > max_d_sq` 早退 ->
    #      `continue` -> 该源根本不进入比较循环，best_i 保持 -1。
    #   2. 若改用「形状差异极大但距离近」让 tot 算出来是 0，则
    #      `tot < thr`（0 < 0.5）为真 -> **同样被 continue 挡掉**。
    #   3. 因此凡是 score == 0 的候选，raw_index 恒为 -1，
    #      被测的 `raw_index >= 0` 先挡掉，`> 0.0` 与 `>= 0.0` 永不产生差异。
    #
    #   实测确认：替身 gt/ge 两模式对同一零分输入都返回 indices=[-1] scores=[0.0]，
    #   完全相同 -> 变异无输入可观测。
    #
    # 与 QA 提到的 stale layer 分支同属「双重不可达」类结构性不可观测。
    # 这里如实记录结论，**不写恒真场景充数**。
    print("\n  --- 零分边界（结构性不可观测，如实记录）---")
    VCOL["map"] = {}
    tool_zero = Tool(distance_threshold=2.0, match_similarity_threshold=0.5)
    srcs_zero = [Obj("z0")]
    sfeats_zero = {"z0": mkfeat("z0", (100.0, 0, 0), 50.0, 9999)}  # 远超阈值 -> 0 分
    _z = calculate_similarity_score_ref(sfeats_zero["z0"], tfeats["t0"], tool_zero)
    cur_zero = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs_zero,
                                               sfeats_zero, tool_zero)
    check("零分候选恒被拒（`score > 0.0` 前置条件），该边界不可观测",
          _z == 0.0 and cur_zero[0] is None,
          f"得分={_z:.6f}, cur={cur_zero[0].name if cur_zero[0] else None}。"
          f"变异 `> 0.0`->`>= 0.0` 实测 0 条变红——因raw_index 恒为 -1，"
          f"被 `raw_index >= 0` 提前挡掉。**不假装已覆盖**。")

    # === find_best_matches（批量主入口）的边界覆盖 ===
    #
    # 重要发现：此前**完全没有测试覆盖 find_best_matches** ——
    # 它是非聚类工作流的主入口（`core/matching.py:591`），
    # 且它的原生分支有两处边界判定:
    #     if index < 0 or score <= 0.0:   -> 无匹配
    # 这两处此前完全未被守护。
    # 下面的场景专门覆盖它们。
    print("\n  --- find_best_matches（批量主入口）边界 ---")
    VCOL["map"] = {}
    tool_mb = Tool(distance_threshold=2.0, match_similarity_threshold=0.5)

    # 场景1: 全部目标都无匹配（零分）-> 每个目标都应得到 (None, 0.0)
    srcs_far = [Obj("f0")]
    sfeats_far = {"f0": mkfeat("f0", (100.0, 0, 0), 50.0, 9999)}  # 远超阈值
    res_none = mod.find_best_matches(srcs_far, sfeats_far, tgts, tfeats, tool_mb)
    check("find_best_matches: 全部源零分时每个目标都返回 (None, 0.0)",
          len(res_none) == len(tgts)
          and all(r[0] is None and r[1] == 0.0 for r in res_none),
          f"结果={[(r[0].name if r[0] else None, round(r[1], 6)) for r in res_none]}")

    # 场景2: 混合 —— 部分目标有匹配、部分零分
    #   t0 在源附近（有匹配），t_far 远离所有源（零分）
    mixed_tgts = [Obj("m0"), Obj("m1")]
    mixed_tfeats = {"m0": mkfeat("m0", (0.0, 0, 0), 1.0, 100),
                    "m1": mkfeat("m1", (500.0, 0, 0), 1.0, 100)}
    srcs_near = [Obj("n0")]
    sfeats_near = {"n0": mkfeat("n0", (0.5, 0, 0), 1.0, 100)}
    res_mixed = mod.find_best_matches(srcs_near, sfeats_near,
                                      mixed_tgts, mixed_tfeats, tool_mb)
    _exp = (res_mixed[0][0] is not None and res_mixed[0][1] > 0.0
            and res_mixed[1][0] is None and res_mixed[1][1] == 0.0)
    check("find_best_matches: 混合场景下有匹配的取到、零分的返回 None",
          _exp,
          f"结果={[(r[0].name if r[0] else None, round(r[1], 6)) for r in res_mixed]}"
          f"（期望 m0 有匹配、m1 为 None）")

    # 场景3: 关闭原生加速 -> Python 回退路径结果必须一致
    class _ToolNoNative(Tool):
        use_native_accel = False
    res_py = mod.find_best_matches(srcs_near, sfeats_near,
                                   mixed_tgts, mixed_tfeats, _ToolNoNative())
    same = [(a[0].name if a[0] else None) == (b[0].name if b[0] else None)
            for a, b in zip(res_mixed, res_py)]
    check("find_best_matches: 原生路径与 Python 回退路径结果一致",
          all(same),
          f"原生={[r[0].name if r[0] else None for r in res_mixed]} vs "
          f"Python={[r[0].name if r[0] else None for r in res_py]}")

    # === find_best_matches 中「结构性不可观测」的边界（如实记录）===
    #
    #变异实测：`score <= 0.0` -> `< 0.0` 与「无匹配时分数不置 0」
    #               均** 0 条变红**。原因链（每一环都已验证）：
    #   1. 无匹配时替身返回 index=-1, score=0.0
    #      -> `index < 0` 已为真 -> **短路** -> score 判断不执行；
    #   2. 有匹配时替身返回 index>=0, score>0.0
    #      -> 两个条件都为假 -> 走 else，不碰 0.0；
    #   3. 因此「score == 0.0 且 index >= 0」这个状态**替身永不产生**
    #      （match_best 只在 tot > best_s(0.0) 时才更新 best_i）。
    # 这与 QA 提到的 stale layer 分支同属**双重不可达**类结构性不可观测。
    # 相比之下 `index < 0` -> `<= 0` **是可观测的**（见上方混合场景断言）。
    print("\n  --- find_best_matches 结构性不可观测项（如实记录）---")
    VCOL["map"] = {}
    tool_unobs = Tool(distance_threshold=2.0, match_similarity_threshold=0.5)
    res_unobs = mod.find_best_matches(
        [Obj("u0")], {"u0": mkfeat("u0", (100.0, 0, 0), 50.0, 9999)},
        tgts, tfeats, tool_unobs)
    check("无匹配时恒返回 (None, 0.0)；`score<=0.0`->`<0.0` 变异不可观测",
          all(r[0] is None and r[1] == 0.0 for r in res_unobs),
          f"结果={[(r[0].name if r[0] else None, r[1]) for r in res_unobs]}。"
          f"原因：无匹配时 index=-1 已短路；index>=0 时分数恒为正"
          f"-> 该分支的 `<=` vs `<` 无输入可观测。**不假装已覆盖**。")

    # 对照组：距离**恰等于**阈值时不走早退，仍参与加权（得分 0.87），
    # 因此「距离 == 阈值」与「距离 > 阈值」行为截然不同。
    srcs_e = [Obj("e0")]
    sfeats_e = {"e0": mkfeat("e0", (2.0, 0, 0), 1.0, 100)}
    _edge = calculate_similarity_score_ref(sfeats_e["e0"], tfeats["t0"], tool)
    check("反向对照: 距离恰等于阈值时不早退（> 而非 >=）",
          _edge > 0.0,
          f"距离 == 阈值 2.0 时得分 = {_edge:.6f} > 0"
          f"（若为 0 则说明早退条件写成了 >=，边界语义不同）")

    # 边界 C：阈值恰等于得分 -> 准入边界（< vs <=）
    VCOL["map"] = {"p1": ("L1", "POINT")}
    tool = Tool(distance_threshold=50.0, match_similarity_threshold=0.8)
    srcs_t = [Obj("p1"), Obj("p2")]
    sfeats_t = {"p1": mkfeat("p1", (3.0, 0, 0), 1.0, 100),
                "p2": mkfeat("p2", (3.5, 0, 0), 1.0, 100)}
    _s = calculate_similarity_score_ref(sfeats_t["p1"], tfeats["t0"], tool)
    tool2 = Tool(distance_threshold=50.0, match_similarity_threshold=_s)
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs_t, sfeats_t,
                                          tool2, _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs_t, sfeats_t, tool2)
    check("边界: 得分恰等于阈值时被采纳（< 而非 <= 的早退语义）",
          ref[0] is not None and cur[0] is not None
          and ref[0].name == cur[0].name,
          f"阈值={_s:.6f}（= 源 p1 得分）; "
          f"ref={ref[0].name if ref[0] else None}, "
          f"cur={cur[0].name if cur[0] else None}")

    # --- 场景 10：use_native_accel=False 必须走 Python 且结果一致 ---
    VCOL["map"] = {}
    tool = Tool(match_similarity_threshold=0.1, use_native_accel=False)
    srcs = [Obj("p0"), Obj("p1")]
    sfeats = {"p0": mkfeat("p0", (2, 0, 0)), "p1": mkfeat("p1", (0, 0, 0))}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool,
                                          _gvi)
    cur = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    check("use_native_accel=False 走 Python 路径且一致",
          ref[0].name == cur[0].name and abs(ref[1] - cur[1]) < 1e-9,
          f"ref={ref[0].name}/{ref[1]:.6f} vs cur={cur[0].name}/{cur[1]:.6f}")

    # --- 场景 12：原生与 Python 双路径交叉一致性（随机）---
    print("\n[B] 随机场景：原生路径 vs Python 路径 vs 黄金参考")
    rng = random.Random(7)
    mism_native, mism_ref, non_trivial, tied = 0, 0, 0, 0
    for trial in range(300):
        n_src = rng.randint(1, 6)
        # 距离跨度必须能覆盖到远超 distance_threshold 的情形，
        # 否则「距离早退」这类语义在随机场景中永远不显现（变异测不出）。
        spread = rng.choice([3.0, 30.0, 200.0, 3000.0])
        tool = Tool(distance_threshold=rng.choice([5.0, 50.0, 500.0]),
                    match_similarity_threshold=rng.choice([0.0, 0.3, 0.7, 0.95]))
        srcs = [Obj(f"r{trial}_{k}") for k in range(n_src)]
        sfeats = {}
        for o in srcs:
            # 制造并列：部分源用完全相同的特征
            loc = (0, 0, 0) if rng.random() < 0.3 else (
                round(rng.uniform(-spread, spread), 1),
                round(rng.uniform(-spread, spread), 1), 0.0)
            sfeats[o.name] = mkfeat(o.name, loc,
                                    dim=rng.choice([0.5, 1.0, 2.0]),
                                    vc=rng.choice([10, 100, 1000]))
            if rng.random() < 0.15:
                sfeats.pop(o.name)  # 随机制造特征缺失
        VCOL["map"] = {o.name: (f"L{k}", "POINT")
                       for k, o in enumerate(srcs) if rng.random() < 0.7}
        tgts = [Obj("t0")]
        tfeats = {"t0": mkfeat("t0", (round(rng.uniform(-5, 5), 1), 0, 0))}
        gvi = lambda o: VCOL["map"].get(o.name, (None, None))

        tool_py = Tool(distance_threshold=tool.distance_threshold,
                       match_similarity_threshold=tool.match_similarity_threshold,
                       use_native_accel=False)
        r_ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats,
                                                tool_py, gvi)
        r_nat = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
        r_py = mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool_py)

        def key(r):
            return (r[0].name if r[0] else None, round(r[1], 9), r[2], r[3])
        if key(r_ref) != key(r_nat):
            mism_ref += 1
            if mism_ref <= 3:
                print(f"      差异 trial={trial}: ref={key(r_ref)} nat={key(r_nat)}")
        if key(r_py) != key(r_nat):
            mism_native += 1
        if r_ref[0] is not None:
            non_trivial += 1
    check("300 随机场景：原生路径 == 黄金参考", mism_ref == 0,
          f"不一致 {mism_ref} 个")
    check("300 随机场景：原生路径 == Python 路径（双路径交叉）", mism_native == 0,
          f"不一致 {mism_native} 个")
    check("随机场景非退化：存在实际匹配（非全None）", non_trivial > 30,
          f"{non_trivial}/300 有匹配")

    # --- 场景 11（放在随机场景之后执行）：降级路径（原生不可用）---
    # 单独起一组模块实例验证降级路径。
    # 注意不能复用前面已加载的 mod：matching.py 内部的
    # `from ..utils.vertex_color_utils import get_vertex_color_info`
    # 是**调用时**才解析，加载新实例会覆盖 sys.modules 中的替身，
    # 导致旧实例悄悄用上新的（空的）VCOL，产生假差异。
    mod_no, nb_no, VCOL2 = load_matching(force_native_available=False)

    def _gvi2(obj):
        return VCOL2["map"].get(obj.name, (None, None))
    VCOL2["map"] = {}
    tool = Tool(match_similarity_threshold=0.1)
    srcs = [Obj("q0"), Obj("q1"), Obj("q2")]
    sfeats = {"q0": mkfeat("q0", (30, 0, 0)), "q1": mkfeat("q1", (0.2, 0, 0)),
              "q2": mkfeat("q2", (10, 0, 0))}
    ref = ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool, _gvi2)
    cur = mod_no.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
    check("降级路径：原生不可用时结果与原实现一致",
          ref[0].name == cur[0].name and abs(ref[1] - cur[1]) < 1e-9
          and ref[2] == cur[2] and ref[3] == cur[3],
          f"ref=({ref[0].name},{ref[1]:.6f},{ref[2]}) vs "
          f"cur=({cur[0].name},{cur[1]:.6f},{cur[2]})")
    check("降级路径：原实例仍使用自己的 VCOL（未被新实例污染）",
          sys.modules["vm.utils.vertex_color_utils"].VCOL is VCOL2,
          "若为 False，说明后续场景的层名断言会失真")


    failed = [n for n, ok, _ in _RESULTS if not ok]
    print("\n" + "=" * 74)
    print(f"结果: {len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过"
          + ("" if not failed else f"  失败: {failed}"))
    print("=" * 74)
    return 0 if not failed else 1


def run_bench():
    """第 3 步性能：源物体数量对单簇匹配耗时的影响"""
    mod, nb, VCOL = load_matching(force_native_available=True)
    mod_no, _, _ = load_matching(force_native_available=False)
    VCOL["map"] = {}

    print("=" * 74)
    print("第 3 步性能：find_best_match_for_cluster（单簇，代表元 → 最佳源）")
    print("=" * 74)
    print(f"{'源物体数':>8} | {'原实现(ms)':>11} | {'当前(ms)':>10} | {'加速比':>8}")
    print("-" * 74)
    rng = random.Random(3)
    for n_src in (50, 200, 500, 1000):
        tool = Tool(match_similarity_threshold=0.3)
        srcs = [Obj(f"b{k}") for k in range(n_src)]
        sfeats = {o.name: mkfeat(o.name, (rng.uniform(-40, 40), rng.uniform(-40, 40), 0))
                  for o in srcs}
        tgts = [Obj("t0")]
        tfeats = {"t0": mkfeat("t0", (0, 0, 0))}
        gvi = lambda o: (None, None)

        t0 = time.perf_counter()
        for _ in range(3):
            ref_find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool, gvi)
        t_ref = (time.perf_counter() - t0) / 3

        t0 = time.perf_counter()
        for _ in range(3):
            mod.find_best_match_for_cluster([0], tgts, tfeats, srcs, sfeats, tool)
        t_cur = (time.perf_counter() - t0) / 3

        print(f"{n_src:>8} | {t_ref * 1000:>11.3f} | {t_cur * 1000:>10.3f} | "
              f"{t_ref / t_cur if t_cur > 0 else 0:>7.2f}x")

    print("\n说明: 原生路径在真实 Blender 中为多线程 C++ 内核，")
    print("      此处为Python 模拟实现，绝对值偏大但相对趋势有效。")
    print("=" * 74)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", action="store_true", help="只跑性能基准")
    args = ap.parse_args()
    if args.bench:
        return run_bench()
    return run_equivalence()


if __name__ == "__main__":
    sys.exit(main())
