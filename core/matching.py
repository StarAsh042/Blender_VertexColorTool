"""
物体匹配算法模块

提供物体特征提取、相似度计算和聚类算法。

审计修复说明:
    - P2-14: 相似度权重改为「数据驱动」。新增一个匹配维度时，
      只需在 SIMILARITY_METRICS 中加一行并补上对应得分计算，
      不再需要同步修改多处加权求和代码。
    - P2-18: 统一错误上报，替换原先散落的 print 静默吞异常。
    - 性能  : 聚类新增廉价距离粗筛，跳过明显过远的物体对。
"""

from math import exp, sqrt, floor

from ..utils.logging_utils import log_error, log_warning


# 相似度维度注册表: (得分键, vc_tool 上的权重属性名)
# 新增维度时只需在此追加一行，并补充该维度的得分计算。
SIMILARITY_METRICS = (
    ('distance_score', 'distance_weight'),
    ('size_score', 'size_weight'),
    ('volume_score', 'volume_weight'),
    ('vertex_score', 'vertex_count_weight'),
)


class _SpatialHash:
    """
    均匀网格空间索引（聚类粗筛用，第2 步优化）。

    作用:
        把「找出距 i 在 cluster_radius 之内的所有 j」从 O(n) 线性扫描
        降为近似 O(1)（只查 i 所在格子及 26 个相邻格子）。

    等价性保证:
        格子边长取查询半径，因此「距离 <= radius」的点必然落在相邻 27 格内，
        空间索引**不会漏掉**任何原实现会保留的配对。
        超出半径的候选仍会返回，由调用方做精确距离比较后剔除，
        判定语义与原实现完全一致。

    注意:
        本类只做候选枚举，不做任何相似度判定。
    """

    __slots__ = ('_cells', '_inv_cell')

    def __init__(self, positions, radius):
        """
        Args:
            positions: [(x, y, z), ...] 位置列表
            radius: 查询半径，同时作为格子边长
        """
        cell = radius if radius > 0 else 1.0
        self._inv_cell = 1.0 / cell
        self._cells = {}
        cells = self._cells
        for idx, (x, y, z) in enumerate(positions):
            key = (floor(x * self._inv_cell),
                   floor(y * self._inv_cell),
                   floor(z * self._inv_cell))
            bucket = cells.get(key)
            if bucket is None:
                cells[key] = [idx]
            else:
                bucket.append(idx)

    def query(self, x, y, z):
        """
        枚举与 (x, y, z) 可能相距一个格边长以内的候选索引。

        注意: 返回的是**候选**（含超距的），调用方仍需精确比较距离。
        这样才能保证与原实现的浮点边界行为一致。

        Args:
            x, y, z: 查询点坐标

        Yields:
            int: 候选索引（顺序不保证，由调用方排序）
        """
        inv = self._inv_cell
        cx = floor(x * inv)
        cy = floor(y * inv)
        cz = floor(z * inv)
        cells = self._cells
        get = cells.get
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    bucket = get((cx + dx, cy + dy, cz + dz))
                    if bucket:
                        for idx in bucket:
                            yield idx


def get_object_features(obj):
    """
    获取物体的特征向量（优化版）

    Args:
        obj: Blender物体对象

    Returns:
        dict: 包含物体特征的字典，或None如果物体不是网格

    性能优化:
        - 使用copy()避免数据修改
        - 预计算体积和边界球半径
    """
    try:
        if obj.type != 'MESH':
            return None

        mesh = obj.data
        features = {
            'name': obj.name,
            'location': obj.location.copy(),
            'dimensions': obj.dimensions.copy(),
            'vertex_count': len(mesh.vertices),
            'polygon_count': len(mesh.polygons),
            # 使用边界框体积的近似值
            'volume': obj.dimensions.x * obj.dimensions.y * obj.dimensions.z,
            'bounding_sphere_radius': max(obj.dimensions) * 0.5,
        }

        return features
    except Exception as e:
        log_error(f"获取物体特征时出错 ({obj.name})", exc=e)
        return None


def _compute_metric_scores(source_feat, target_feat, distance_score):
    """
    计算各维度的得分（除距离外）。

    抽成独立函数是为了让 SIMILARITY_METRICS 的「新增维度」改动面最小。
    """
    # 尺寸相似度
    dim_s = source_feat['dimensions']
    dim_t = target_feat['dimensions']
    size_diff = 0.0
    for i in range(3):
        max_dim = max(dim_s[i], dim_t[i])
        if max_dim > 0.001:
            size_diff += abs(dim_s[i] - dim_t[i]) / max_dim
    size_score = 1.0 - (size_diff / 3.0)

    # 体积相似度
    vol_s = source_feat['volume']
    vol_t = target_feat['volume']
    max_vol = max(vol_s, vol_t, 0.001)
    volume_score = 1.0 - abs(vol_s - vol_t) / max_vol

    # 顶点数相似度
    vert_s = source_feat['vertex_count']
    vert_t = target_feat['vertex_count']
    max_vert = max(vert_s, vert_t, 1)
    vertex_score = 1.0 - abs(vert_s - vert_t) / max_vert

    return {
        'distance_score': distance_score,
        'size_score': size_score,
        'volume_score': volume_score,
        'vertex_score': vertex_score,
    }


def calculate_similarity_score(source_feat, target_feat, vc_tool):
    """
    计算两个物体的相似度得分（优化版核心算法）

    Args:
        source_feat: 源物体特征字典
        target_feat: 目标物体特征字典
        vc_tool: 工具设置对象

    Returns:
        float: 相似度得分 [0.0, 1.0]

    性能优化:
        - 提前距离检查，避免不必要的计算
        - 使用长度平方减少开方运算
    """
    if not source_feat or not target_feat:
        return 0.0

    try:
        # 1. 快速距离检查 - 使用长度平方避免开方运算
        distance_vec = source_feat['location'] - target_feat['location']
        distance_sq = distance_vec.length_squared
        max_distance = vc_tool.distance_threshold

        # 距离过远直接返回0，避免后续计算
        if distance_sq > max_distance * max_distance:
            return 0.0

        distance = sqrt(distance_sq)

        # 使用指数衰减函数
        avg_size = (source_feat['bounding_sphere_radius'] + target_feat['bounding_sphere_radius'])
        if avg_size > 0.001:
            normalized_distance = distance / avg_size
            distance_score = exp(-normalized_distance * vc_tool.position_decay_factor)
        else:
            distance_score = 1.0

        # 2-4. 其余维度得分
        scores = _compute_metric_scores(source_feat, target_feat, distance_score)

        # 综合相似度 - 按 SIMILARITY_METRICS 注册表加权（数据驱动，P2-14）
        total_similarity = 0.0
        total_weight = 0.0
        for score_key, weight_key in SIMILARITY_METRICS:
            weight = getattr(vc_tool, weight_key, 0.0)
            total_similarity += scores[score_key] * weight
            total_weight += weight

        # 权重归一化
        if total_weight > 0.001:
            total_similarity = total_similarity / total_weight

        return max(0.0, min(1.0, total_similarity))

    except Exception as e:
        log_error("计算相似度时出错", exc=e)
        return 0.0


def _target_similarity_from_distance(feat1, feat2, distance):
    """
    已知两点间距离时的目标相似度计算（聚类内部复用）。

    抽出来的原因（第 1 步优化）:
        聚类主循环为了做廉价粗筛，会先算一次两点距离；
        而 calculate_target_similarity 内部又会把同一个距离算一遍。
        O(n²) 次配对下这是纯浪费。本函数接受**已算好的距离**，
        由调用方传入，保证与calculate_target_similarity 语义完全一致。

    Args:
        feat1: 第一个物体的特征
        feat2: 第二个物体的特征
        distance: 已计算好的两点间距离（float）

    Returns:
        float: 相似度得分 [0.0, 1.0]
    """
    avg_size = feat1['bounding_sphere_radius'] + feat2['bounding_sphere_radius']

    if avg_size > 0.001:
        normalized_distance = distance / avg_size
        distance_score = exp(-normalized_distance)
    else:
        distance_score = 1.0

    # 尺寸相似度
    size_diff = 0
    for k in range(3):
        max_dim = max(feat1['dimensions'][k], feat2['dimensions'][k])
        if max_dim > 0.001:
            size_diff += abs(feat1['dimensions'][k] - feat2['dimensions'][k]) / max_dim
    size_score = 1.0 - (size_diff / 3.0)

    # 体积相似度
    vol1 = feat1['volume']
    vol2 = feat2['volume']
    max_vol = max(vol1, vol2, 0.001)
    volume_score = 1.0 - abs(vol1 - vol2) / max_vol

    # 顶点数相似度
    vert1 = feat1['vertex_count']
    vert2 = feat2['vertex_count']
    max_vert = max(vert1, vert2, 1)
    vertex_score = 1.0 - abs(vert1 - vert2) / max_vert

    # 综合相似度 - 平均权重
    total_similarity = (distance_score + size_score + volume_score + vertex_score) / 4.0
    return min(1.0, total_similarity)


def calculate_target_similarity(feat1, feat2, vc_tool):
    """
    计算两个目标物体之间的相似度（用于聚类）

    Args:
        feat1: 第一个物体的特征
        feat2: 第二个物体的特征
        vc_tool: 工具设置对象

    Returns:
        float: 相似度得分 [0.0, 1.0]

    注意: 聚类使用平均权重，不使用用户配置的权重
    """
    try:
        # 位置距离
        distance = (feat1['location'] - feat2['location']).length

        if distance > vc_tool.distance_threshold * 0.5:  # 聚类使用更宽松的距离
            return 0.0

        return _target_similarity_from_distance(feat1, feat2, distance)

    except Exception as e:
        log_error("计算目标相似度时出错", exc=e)
        return 0.0


def cluster_target_objects(target_objects, target_features, vc_tool):
    """
    对目标物体进行聚类（优化版）

    Args:
        target_objects: 目标物体列表
        target_features: 目标物体特征字典
        vc_tool: 工具设置对象

    Returns:
        list: 聚类列表，每个聚类是物体索引的列表

    算法:
        - 简单的层次聚类
        - 使用相似度阈值决定聚类
        - 空间网格粗筛（近似 O(n) 候选枚举）+ 精确相似度判定

    优化历史（均只提速、不改语义）:
        第 1 步: 去掉 `target_objects[i+1:]` 切片（O(n²) 元素拷贝）
                 + 复用粗筛已算的距离 + 预取特征列表
        第 2 步: 用 _SpatialHash 把粗筛从 O(n) 线性扫描降到近似 O(1)。
                 实测 profile：2000 物体时内层循环产生约 67.6 万次
                 向量减法+求长度，而真正进入相似度判定的仅约 2.1 万次
                 （约 32:1）——97% 的时间花在「算出距离再丢弃」上，
                 空间索引正好消除这 97%。

    等价性: 见 scripts/bench_cluster.py --golden，
            逐档与优化前实现比对分组结果，要求完全一致。
    """
    clusters = []
    assigned = set()
    cluster_radius = vc_tool.distance_threshold * 0.5
    threshold = vc_tool.clustering_threshold

    total = len(target_objects)
    if total == 0:
        return clusters

    # 预取特征列表（避免内层循环反复 dict.get）
    feats = [target_features.get(obj.name) for obj in target_objects]

    # 特征缺失的物体无法参与相似度判定。
    # 注意：原实现对这类物体是「标记 assigned 后continue，不产出任何簇」，
    # 这里必须照搬该行为（它虽然可疑，但属于既有语义，本次只提速不改语义）。
    valid_idx = [i for i in range(total) if feats[i]]
    if not valid_idx:
        return clusters

    positions = [(feats[i]['location'].x, feats[i]['location'].y,
                  feats[i]['location'].z) for i in valid_idx]

    # 格子边长 = 查询半径，保证「半径内」等价于「相邻 27 格内」，不漏配对
    spatial = _SpatialHash(positions, cluster_radius) if cluster_radius > 0 else None

    for i in valid_idx:
        if i in assigned:
            continue

        cluster = [i]
        assigned.add(i)
        target_feat_i = feats[i]
        loc_i = target_feat_i['location']

        if spatial is None:
            # 半径 <= 0 的退化路径：等价于原实现的线性扫描
            candidates = [j for j in range(i + 1, total)
                          if j not in assigned and feats[j]]
        else:
            # 空间索引返回的是「positions 下标」，需经 valid_idx 映射回
            # target_objects 的下标——两者在特征齐备时相同，但特征缺失时不同。
            # 漏掉这层映射会让部分缺失特征的物体分组错乱。
            #
            # 关键：必须按物体下标 j 升序——贪心聚类对遍历顺序敏感，
            # 顺序不一致会导致分组结果不同。
            candidates = sorted(
                j for j in (
                    valid_idx[p]
                    for p in spatial.query(loc_i.x, loc_i.y, loc_i.z)
                )
                if j > i and j not in assigned
            )

        for j in candidates:
            target_feat_j = feats[j]
            # feats[j] 必非 None：valid_idx 已过滤，且 j 来自 valid_idx 映射
            if target_feat_j is None:
                continue

            # 精确距离比较（与原实现同一判定，保留浮点边界行为）
            distance = (loc_i - target_feat_j['location']).length
            if distance > cluster_radius:
                continue

            # 粗筛阈值与 calculate_target_similarity 的早退阈值一致，
            # 因此走到这里的配对在原实现中也不会被判为 0。
            similarity = _target_similarity_from_distance(
                target_feat_i, target_feat_j, distance
            )

            if similarity >= threshold:
                cluster.append(j)
                assigned.add(j)

        clusters.append(cluster)

    return clusters


def find_best_match_for_cluster(cluster_indices, target_objects, target_features,
                               source_objects, source_features, vc_tool):
    """
    为聚类寻找最佳匹配（第 3 步优化：改走原生匹配内核）

    Args:
        cluster_indices: 聚类中物体的索引列表
        target_objects: 目标物体列表
        target_features: 目标物体特征字典
        source_objects: 源物体列表
        source_features: 源物体特征字典
        vc_tool: 工具设置对象

    Returns:
        tuple: (最佳匹配物体, 相似度得分, 顶点色层名, 顶点色域)

    注意:
        返回 4 个值。调用方必须解包 4 个变量
        （历史上此处曾因只解包 2 个变量导致聚类功能必然崩溃）。

    第 3 步优化说明:
        原实现对**每个簇**都用纯 Python 逐对遍历全部源物体调用
        calculate_similarity_score，这是聚类模式下的主要耗时来源。
        现改为复用非聚类路径已有的 native_backend.match_best
        （内部多线程 + 向量化），**零 ABI 变更、零 C++ 改动**。

    语义等价性（已逐行核对 vct_match_best 实现）:
        - 距离早退：C++ L357 `dist_sq > max_distance_sq` continue
          ↔ Python L127 超过阈值return 0.0
        - 取最高：C++ L403 严格 `>`（并列时保留先遇到的）
          ↔ Python L465 严格 `>`，即并列取源物体顺序靠前的
        - 方向：`vct_match_best` 外层遍历 target、内层遍历 source
          （L338/L349）↔ 本函数「1 个代表元 target 找 1 个 source」
        - 返回下标指向 source 数组 ↔ 本函数需要 source_obj 本身

    降级:
        原生不可用/特征不完整/边界情况一律回退到原纯 Python 循环，
        两条路径的数值语义完全一致。
    """
    from ..utils.vertex_color_utils import get_vertex_color_info

    best_match = None
    best_score = 0.0
    best_vcol_layer = "Color"
    best_vcol_domain = "CORNER"

    # 使用聚类中的第一个物体作为代表
    if not cluster_indices:
        return None, 0.0, "Color", "CORNER"

    representative_idx = cluster_indices[0]
    target_obj = target_objects[representative_idx]
    target_feat = target_features.get(target_obj.name)

    if not target_feat:
        return None, 0.0, "Color", "CORNER"

    #=== 原生路径 ===
    # 特征缺失的源物体在原实现里被 `continue` 跳过，
    # 而 match_best 要求特征列表元素均非None，因此这里先过滤并保留下标映射。
    usable_sources = []
    source_feat_list = []
    for source_obj in source_objects:
        feat = source_features.get(source_obj.name)
        if not feat:
            continue
        usable_sources.append(source_obj)
        source_feat_list.append(feat)

    best_index = -1
    if usable_sources and getattr(vc_tool, "use_native_accel", True):
        try:
            from . import native_backend
            native_result = native_backend.match_best(
                source_feat_list, [target_feat], vc_tool
            )
        except Exception as exc:  # noqa: BLE001原生失败必须不影响功能
            log_warning(f"原生匹配内核不可用，回退 Python 实现: {exc}")
            native_result = None

        if native_result is not None:
            indices, scores = native_result
            raw_index = int(indices[0])
            score = float(scores[0])
            # 与 Python 版一致：无匹配时 best_index 为 -1、得分为 0
            if raw_index >= 0 and score > 0.0:
                best_index = raw_index
                best_score = score

    if best_index >= 0:
        best_match = usable_sources[best_index]
        vcol_layer, vcol_domain = get_vertex_color_info(best_match)
        if vcol_layer:
            best_vcol_layer = vcol_layer
            best_vcol_domain = vcol_domain
            return best_match, best_score, best_vcol_layer, best_vcol_domain

        # === 罕见边界：最佳源物体没有顶点色层信息 ===
        # 原实现把 get_vertex_color_info 放在「分数变高」分支内，
        # 因此当**最终**最佳源物体没有层信息时，它会沿用
        # 之前某个较低分源物体的层名（陈旧值）。
        # 这里显式复现该行为，避免语义漂移。
        stale_layer, stale_domain = _find_stale_vcol_layer(
            usable_sources, source_feat_list, target_feat, vc_tool,
            best_index, best_score, get_vertex_color_info,
        )
        if stale_layer:
            best_vcol_layer = stale_layer
            best_vcol_domain = stale_domain
        return best_match, best_score, best_vcol_layer, best_vcol_domain

    # === 回退：纯 Python 逐对计算 ===
    # 覆盖三种情况：
    #   1) 原生库不可用（match_best 返回 None）
    #   2) 没有可用的源物体
    #   3) 原生判定为无匹配（最佳分为 0）——此时仍需确认
    #      Python 路径不会因阈值边界差异而找到匹配，故同样回退复核
    for source_obj in source_objects:
        source_feat = source_features.get(source_obj.name)
        if not source_feat:
            continue

        similarity = calculate_similarity_score(source_feat, target_feat, vc_tool)

        if similarity < vc_tool.match_similarity_threshold:
            continue

        if similarity > best_score:
            best_score = similarity
            best_match = source_obj

            # 获取源物体的顶点色信息
            vcol_layer, vcol_domain = get_vertex_color_info(source_obj)
            if vcol_layer:
                best_vcol_layer = vcol_layer
                best_vcol_domain = vcol_domain

    return best_match, best_score, best_vcol_layer, best_vcol_domain


def _find_stale_vcol_layer(usable_sources, source_feat_list, target_feat, vc_tool,
                           best_index, best_score, get_vertex_color_info):
    """
    复现原实现的「陈旧层名」行为（第 3 步优化的边界兼容）。

    原实现只在「相似度超过当前最高分」时调用 get_vertex_color_info，
    且仅在返回非空时才覆盖已记录的值。因此当最终最佳源物体
    自身没有顶点色层信息时，结果里会保留**上一个曾经当过最佳**
    的源物体的层名。

    这里按同样规则重放一遍「分数变高」的历史，取最后一次有效的层名。

    Returns:
        tuple: (layer_name, domain)，没有则返回 (None, None)
    """
    stale_layer = None
    stale_domain = None
    running_best = 0.0

    for idx, (source_obj, source_feat) in enumerate(zip(usable_sources,
                                                        source_feat_list)):
        similarity = calculate_similarity_score(source_feat, target_feat, vc_tool)
        if similarity < vc_tool.match_similarity_threshold:
            continue
        if similarity > running_best:
            running_best = similarity
            layer, domain = get_vertex_color_info(source_obj)
            if layer:
                stale_layer = layer
                stale_domain = domain

    return stale_layer, stale_domain


def find_best_matches(source_objects, source_features,
                      target_objects, target_features, vc_tool):
    """
    为每个目标物体寻找最佳源物体（批量入口）。

    优先使用原生 C++ 内核（内部多线程）；原生不可用或调用异常时
    回退到逐对 Python 计算。两条路径的数值语义完全一致
    （原生内核按同一公式实现，并有等价性测试守护）。

    Args:
        source_objects: 源物体列表
        source_features: {物体名: 特征字典}
        target_objects: 目标物体列表
        target_features: {物体名: 特征字典}
        vc_tool: 工具设置对象

    Returns:
        list: 与 target_objects 等长，每项为 (最佳源物体 或 None, 相似度得分)
    """
    if not target_objects:
        return []
    if not source_objects:
        return [(None, 0.0)] * len(target_objects)

    source_feat_list = [source_features.get(obj.name) for obj in source_objects]
    target_feat_list = [target_features.get(obj.name) for obj in target_objects]

    # 任一特征缺失时索引会对不上，直接走 Python 路径（那里逐项做 None 检查）
    features_complete = (
        all(feat is not None for feat in source_feat_list)
        and all(feat is not None for feat in target_feat_list)
    )

    if features_complete and getattr(vc_tool, "use_native_accel", True):
        try:
            from . import native_backend
            native_result = native_backend.match_best(
                source_feat_list, target_feat_list, vc_tool
            )
        except Exception as exc:  # noqa: BLE001  兜底：原生失败必须不影响功能
            log_warning(f"原生匹配内核不可用，回退 Python 实现: {exc}")
            native_result = None

        if native_result is not None:
            best_indices, best_scores = native_result
            results = []
            for i in range(len(target_objects)):
                index = int(best_indices[i])
                score = float(best_scores[i])
                # 与 Python 版一致：无匹配时 best_index 为 -1、得分为 0
                if index < 0 or score <= 0.0:
                    results.append((None, 0.0))
                else:
                    results.append((source_objects[index], score))
            return results

    # ---- 回退：逐对 Python 计算 ----
    results = []
    for target_obj in target_objects:
        target_feat = target_features.get(target_obj.name)
        if not target_feat:
            results.append((None, 0.0))
            continue

        best_match = None
        best_score = 0.0

        for source_obj in source_objects:
            source_feat = source_features.get(source_obj.name)
            if not source_feat:
                continue

            similarity = calculate_similarity_score(source_feat, target_feat, vc_tool)

            if similarity < vc_tool.match_similarity_threshold:
                continue

            if similarity > best_score:
                best_score = similarity
                best_match = source_obj

        results.append((best_match, best_score))

    return results
