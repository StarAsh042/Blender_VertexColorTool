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

from math import exp, sqrt

from ..utils.logging_utils import log_error


# 相似度维度注册表: (得分键, vc_tool 上的权重属性名)
# 新增维度时只需在此追加一行，并补充该维度的得分计算。
SIMILARITY_METRICS = (
    ('distance_score', 'distance_weight'),
    ('size_score', 'size_weight'),
    ('volume_score', 'volume_weight'),
    ('vertex_score', 'vertex_count_weight'),
)


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
        avg_size = (feat1['bounding_sphere_radius'] + feat2['bounding_sphere_radius'])

        if distance > vc_tool.distance_threshold * 0.5:  # 聚类使用更宽松的距离
            return 0.0

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
        - O(n^2)时间复杂度，但用距离粗筛降低常数项
    """
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

        for j, other_obj in enumerate(target_objects[i+1:], i+1):
            if j in assigned:
                continue

            target_feat_j = target_features.get(other_obj.name)
            if not target_feat_j:
                continue

            # 廉价距离粗筛：明显超出聚类半径的物体直接跳过，
            # 避免为它们执行完整的相似度计算（calculate_target_similarity
            # 内部也会做同样的判定，这里只是省掉函数调用开销）。
            if (loc_i - target_feat_j['location']).length > cluster_radius:
                continue

            similarity = calculate_target_similarity(target_feat_i, target_feat_j, vc_tool)

            if similarity >= vc_tool.clustering_threshold:
                cluster.append(j)
                assigned.add(j)

        clusters.append(cluster)

    return clusters


def find_best_match_for_cluster(cluster_indices, target_objects, target_features,
                               source_objects, source_features, vc_tool):
    """
    为聚类寻找最佳匹配（优化版）

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
