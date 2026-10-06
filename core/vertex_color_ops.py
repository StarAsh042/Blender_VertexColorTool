"""
顶点色核心操作模块

提供顶点色复制、填充等核心功能。

审计修复说明:
    - P2-12: 移除未使用的 `bmesh` / `gc` 导入
    - P1-8 : 暴力搜索增加成本上限兜底。即便用户关闭了 KDTree，
      在顶点数组合过大时也会自动启用 KDTree，避免 O(N×M) 的纯 Python
      双重循环把 Blender 卡死（5万×5万 ≈ 25 亿次距离计算）。
    - P1-7 : fill_vertex_colors 的模式切换从「每个物体两次」改为「整体一次」，
      降低多物体批量填充时的 depsgraph 开销。
    - P0-1 : 复制入口增加「源物体处于通道预览状态」前置检查。
      预览算子把灰度值写进真实颜色层、原始色留在 __vct_preview_backup__，
      而本函数走 get_vcol_layer(name=None) 拿到的是激活层（即已被写成灰度的
      真实层），于是灰度被当作原色复制到全场，且函数仍返回 True。
      这是一次静默的跨集合数据损坏，现改为明确拒绝并报错。
    - P0-3 : 求值网格的释放统一由try/finally 保证。
      原实现在原生/Python 双路径上各写一处 to_mesh_clear()，任何中途异常
      （如 vertex_to_loops KeyError）都会被外层 except 吞掉并跳过清除，
      每个失败目标泄漏一个求值网格（20 万顶点 ≈ 12MB，2000 个目标 ≈ 24GB）。
      现将双路径合并为 _write_colors_to_target，并用单次 try/finally 收口。
    - 阶段 C : 取色最大距离上限。
      原实现只取**最近**顶点而不判断它有多远，于是「目标比源大」或
      「两者有位置偏移」时，超出源范围的顶点会取到源边缘顶点的颜色，
      形成向外扩散的色带。这是一次**静默错误**：不报错、置信度正常、
      用户看不出来（对应 docs/LIMITATIONS.md 第 8 条）。
      现由 pick_distance_factor（占源包围盒对角线的百分比，0 = 关闭）
      给出可调上限；超距顶点**不写入**，即保留目标原色，
      使「这块超出了源范围」对用户可见，而不是被凭空改色。

      尺度选择（为什么是对角线，见 _source_bbox_diagonal 的完整论证）:
          固定世界单位 / 包围球半径 / 到包围盒表面的距离 三者都不可用 ——
          前者无法跨模型尺寸复用，后两者分别被离群顶点与「世界轴对齐」
          破坏。对角线与朝向无关、随整体缩放线性变化，且给 LOD 场景
          （源密、疏同形）留有天然余量。

      与法线约束的交互（两条搜索路径共存，语义必须唯一）:
          距离是**硬边界**，法线是**边界内的精修**。
          1. 先判距离：最近点都超限 ⟹ 任何源顶点都超限（最近点即最小值），
             此时二次搜索在数学上不可能有合法解，故**直接拒绝**，
             既正确又省掉整轮扩张搜索。
          2. 再判法线：仅在距离已通过的点之间做find_range 精修，
             且扩张半径被距离上限**夹住**，防止「法线稍不符就越界取色」
             这条后门。
          3. 兜底沿用法线约束原有的「退回纯距离最近」——
             该点必然已通过距离校验（见 1），故两条约束的兜底天然一致，
             不需要新增任何兜底语义。
"""

import math

import bpy
from mathutils import Vector, kdtree

from .cache import (  # noqa: F401  (常量对外暴露)
    VertexColorCache,
    BRUTEFORCE_VERTEX_LIMIT,
    _extract_normals,
    _normal_matrix,
    _normal_constraint_enabled,
    _has_normals,
    _numpy_has_arrays,
)
from ..utils.vertex_color_utils import (
    get_vcol_layer,
    get_active_vcol_layer,
    has_preview_backup,
)
from ..utils.logging_utils import log_info, log_warning, log_error

try:
    import numpy as np
except ImportError:  # pragma: no cover - Blender 自带 numpy
    np = None


# 暴力搜索的运算量上限（源顶点数 × 目标顶点数）。
# 超过后自动启用 KDTree，防止 Blender 长时间无响应。
BRUTEFORCE_OP_LIMIT = 20000000

# === 法线约束参数（阶段 B）===
# 法向不符时逐步扩大半径搜索的最大轮数（每轮半径翻倍）。
# 8 轮意味着最大搜索半径约为初始最近距离的 256 倍，足以覆盖
# 绝大多数薄壁场景；再大只会拖慢速度而收益递减。
_NORMAL_SEARCH_MAX_ROUNDS = 8

# 首轮搜索半径（当最近点距离为 0 时使用，避免半径退化为 0 死循环）。
_NORMAL_SEARCH_MIN_RADIUS = 1e-3

# 角度 -> 余弦 的缓存。夹角阈值在一次复制过程中是常量，
# 不缓存会在顶点循环里反复调用 math.cos（每顶点一次）。
_COS_CACHE = {}

# === 阶段 C：取色最大距离上限 ===

# 源包围盒对角线短于该值时，视为「零尺度」——例如源只有 1 个顶点，
# 或所有顶点完全重合。此时任何「按比例」的上限都会退化成 0，
# 把除该点外的所有目标顶点全部拒绝（等于把模型涂成空白）。
# 故此时**关闭距离上限**并退回原行为，宁可漏拦也不误杀。
_MIN_SOURCE_EXTENT = 1e-6

# 源包围盒对角线的记忆化键（挂在 source_data 缓存条目上）。
# 包围盒需O(N) 扫描源顶点；批量复制时同一源会被上千个目标复用，
# 不缓存就是上千次重复扫描（20 万顶点 × 2000 目标 = 4 亿次）。
_BBOX_DIAG_KEY = '_vct_bbox_diagonal'

# 「源退化已警告」的记忆化键：退化告警挂在 source_data 上按源去重，
# 批量复制上千目标时只提示一次而不是刷屏上千行。
_DEGENERATE_WARNED_KEY = '_vct_degenerate_warned'


def _target_points_array(mesh_eval, matrix_world):
    """
    批量取出求值网格顶点坐标并变换到世界空间（全向量化，无 Python 逐点循环）。

    相比 [matrix_world @ v.co for v in mesh.vertices]，
    foreach_get 由 C 层一次性导出坐标，矩阵变换交给 numpy。
    """
    count = len(mesh_eval.vertices)
    if count == 0:
        return np.empty((0, 3), dtype=np.float32)

    coords = np.empty(count * 3, dtype=np.float32)
    mesh_eval.vertices.foreach_get("co", coords)
    coords = coords.reshape(count, 3)

    mw = np.array(matrix_world, dtype=np.float32)
    return coords @ mw[:3, :3].T + mw[:3, 3]


def _apply_colors_bulk(layer, vertex_colors, mesh):
    """
    用 foreach_set 批量写入颜色（原生路径专用）。

    逐元素 `layer.data[i].color = ...` 每次都要走一遍 RNA，
    实测 20k 顶点需 60ms；改为整块 foreach_set 后为 0.1ms（约 400x）。
    """
    if np is None:
        raise RuntimeError("numpy 不可用，无法批量写入")

    data = layer.data
    domain = getattr(layer, 'domain', 'CORNER')

    if domain == 'POINT':
        count = min(len(data), int(vertex_colors.shape[0]))
        if count <= 0:
            return
        flat = np.ascontiguousarray(vertex_colors[:count], dtype=np.float32).ravel()
        data.foreach_set("color", flat)
        return

    # CORNER 域：每个 loop 取其所属顶点的颜色
    loop_count = len(mesh.loops)
    if loop_count == 0:
        return

    loop_vertex = np.empty(loop_count, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", loop_vertex)

    count = min(len(data), loop_count)
    gathered = vertex_colors[loop_vertex[:count]]
    flat = np.ascontiguousarray(gathered, dtype=np.float32).ravel()
    data.foreach_set("color", flat)


def _build_kdtree(vertices):
    """构建并平衡 KDTree，失败返回 None"""
    if not vertices:
        return None
    try:
        kd = kdtree.KDTree(len(vertices))
        for i, coord in enumerate(vertices):
            kd.insert(coord, i)
        kd.balance()
        return kd
    except Exception as e:
        log_error("构建KDTree失败", exc=e)
        return None


def _find_color_index(target_pos, target_normal, source_data, kd,
                      angle_threshold_deg, max_dist_sq=None):
    """
    带**距离上限**与**法线约束**的最近点取色索引（阶段 B +阶段 C）。

    问题背景（距离）:
        只按欧氏距离取最近顶点、**不判断该点有多远**。于是目标模型比源
        更大、或两者有位置偏移时，超出源范围的顶点会取到源边缘顶点的颜色，
        形成向外扩散的色带。且全程不报错、置信度正常，用户看不出来。
        对应 docs/LIMITATIONS.md 第 8 条。

    问题背景（法线，阶段 B）:
        对薄壁几何体（墙、树叶、纸片、布料、单面片建模等），
        壁两侧的顶点距离几乎相等，KDTree 可能选中墙另一面的顶点，
        导致「背面染上正面的颜色」。插件的招牌场景是 LOD 游戏资产，
        这类薄壳模型极其常见。

    === 两条约束的语义（重要）===

    距离是**硬边界**（能否决），法线是**边界内的精修**（只优化选择）。
    二者的执行顺序与交互:

        1. **先判距离**（快路径，仅一次数值比较）:
           最近点即全局最小距离 —— 它都超限，就意味着**任何**源顶点都超限。
           此时二次搜索在数学上不可能有合法解，故直接返回 -1。
           这不只是省掉8轮 find_range 的优化，它保证了
           「距离超限 + 法线不符同时发生」时语义唯一:
           **距离优先，直接拒绝**，不存在「法线搜索把它救回来」的后门。

        2. **再判法线**（仅在距离已通过的点之间做）:
           朝向不符时的 find_range 扩张搜索，其半径**被距离上限夹住**
           （见下方 radius_limit）。若不夹住，一次「法线稍不符」的
           二次搜索就能把半径扩到远超上限，拿到一个方向正确但
           距离越界的候选 —— 距离上限会被自己的兜底逻辑架空。

        3. **兜底沿用阶段 B 的「退回纯距离最近」**:
           该点必然已通过距离校验（见第1 步），因此两条约束的兜底
           **天然一致**，不需要为距离约束新增任何兜底语义。
           找不到任何可接受候选时 -> 返回 -1（调用方跳过写入，
           即「保留目标原色」，这是用户明确选择的处理方式）。

    Args:
        target_pos: 目标点世界坐标
        target_normal: 目标点法线（世界空间），可为 None
        source_data: 源数据缓存条目
        kd: mathutils KDTree
        angle_threshold_deg: 夹角阈值（度）。<= 0 表示关闭法线约束
        max_dist_sq: 允许的最大距离**平方**；None 表示不启用距离上限

    Returns:
        int: 源顶点索引；无可接受候选返回 -1（调用方据此跳过写入）
    """
    if kd is None:
        return -1

    nearest = kd.find(target_pos)
    if not nearest:
        return -1
    nearest_idx = nearest[1]
    nearest_dist_sq = nearest[2]

    # === 阶段 C：距离硬边界（必须早于法线二次搜索）===
    # 放在这里的理由不是「顺序好看」，而是数学上必要：
    # 最近点是距离的全局最小值，它超限 ⟹ 范围内不存在任何源顶点。
    # 因此这里直接拒绝，不会冤枉任何一个本可接受的顶点。
    if max_dist_sq is not None and nearest_dist_sq > max_dist_sq:
        return -1

    # 未启用约束，或缺少法线数据 -> 纯距离最近（原行为）
    if angle_threshold_deg is None or angle_threshold_deg <= 0:
        return nearest_idx
    if target_normal is None:
        return nearest_idx

    normals = source_data.get('normals')
    if normals is None or nearest_idx >= len(normals):
        return nearest_idx

    # 法线可以是 (N,3) float64 numpy 数组（主路径），
    # 也可以是 list[Vector]（无 numpy 时的回退）。
    # 统一用 _normal_dot 取点积，避免在主循环里写两套分支。
    dot = _normal_dot(normals, nearest_idx, target_normal)
    if dot is None:
        return nearest_idx

    # 夹角阈值 -> 余弦阈值（点积比较，避免反三角函数）
    cos_threshold = _cos_of_angle(angle_threshold_deg)

    if dot >= cos_threshold:
        return nearest_idx  # 朝向符合，直接采用（快路径）

    # 朝向不符：逐步扩大半径搜索符合朝向的候选
    best_idx = -1
    best_dist_sq = None
    radius = math.sqrt(nearest_dist_sq) if nearest_dist_sq > 0 else 0.0
    # 目标点恰好落在源顶点上时（最近距离为 0），若从 0 开始倍增，
    # 8 轮只能覆盖 0.128 个单位——对稍大尺度的模型而言过小，
    # 会导致「明明附近有符合朝向的候选却找不到」。
    # 因此以一个与模型尺度相关的起点开始：用最近距离与一个下限取较大者。
    radius = max(radius, _NORMAL_SEARCH_MIN_RADIUS)
    # 最多扩张 8 轮；每轮半径翻倍
    radius_clamped = False
    for _ in range(_NORMAL_SEARCH_MAX_ROUNDS):
        radius = radius * 2.0
        # === 阶段 C：把扩张半径夹在距离上限内 ===
        # 不夹的话，一个「方向正确但距离越界」的候选会被选中，
        # 距离上限就被二次搜索架空了（上限只管快路径、不管兜底）。
        # 夹住之后，越界的候选物理上不会出现在 find_range 结果里。
        # 上限未启用时 radius_limit_sq 为 None，此分支完全不执行，
        # 逐字保持阶段 B 的原行为。
        if max_dist_sq is not None and radius * radius > max_dist_sq:
            radius = math.sqrt(max_dist_sq)
            radius_clamped = True
        for _co, idx, dist_sq in kd.find_range(target_pos, radius):
            if idx >= len(normals):
                continue
            n_dot = _normal_dot(normals, idx, target_normal)
            if n_dot is None:
                continue
            if n_dot < cos_threshold:
                continue
            if best_dist_sq is None or dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_idx = idx
        if best_idx >= 0:
            return best_idx
        # 半径已顶到距离上限：再翻倍只会被夹回同一半径，find_range
        # 结果不变，提前收工。用标志位而非 radius*radius >= max_dist_sq
        # —— (sqrt(m))² 因浮点舍入可能略小于 m，导致判不中而空转满 8 轮。
        if radius_clamped:
            break

    # === 兜底：无符合朝向的候选，退回纯距离最近 ===
    # 该点已通过上方的距离校验，故此兜底不会突破距离上限。
    return nearest_idx


def _normal_dot(normals, index, target_normal):
    """
    取第 index 个法线与目标法线的点积（兼容数组与列表两种存储）。

    精度: float64 数组路径全程双精度；列表回退用 mathutils 的双精度运算。
    两条路径必须给出等价结果（同 _transform_normals 的约定）。

    Args:
        normals: (N,3) float64 数组 或 list[Vector]
        index: 顶点下标
        target_normal: 目标法线（Vector）

    Returns:
        float | None: 点积；该下标处法线缺失时返回 None
    """
    try:
        n = normals[index]
    except Exception:
        return None
    if n is None:
        return None
    try:
        if hasattr(n, "dot"):
            return n.dot(target_normal)
        # numpy 标量 / 行向量
        return float(n[0] * target_normal[0]
                     + n[1] * target_normal[1]
                     + n[2] * target_normal[2])
    except Exception:
        return None


def _cos_of_angle(angle_deg):
    """
    角度 -> 余弦。带缓存，避免在顶点循环里反复调用 math.cos。
    """
    try:
        cached = _COS_CACHE.get(angle_deg)
        if cached is not None:
            return cached
    except Exception:
        cached = None
    value = math.cos(math.radians(angle_deg))
    try:
        _COS_CACHE[angle_deg] = value
    except Exception:
        pass
    return value


def _source_bbox_diagonal(source_data):
    """
    求源顶点集（世界空间）的**包围盒对角线长度**，作为距离上限的归一化尺度。

    为什么要归一化，以及为什么选这个量（三个候选的对比）:

    1. **固定世界单位值** —— 直接否决。模型尺寸差异巨大（一个茶杯 0.1m，
       一栋楼 30m），同一个数值对前者宽到无效、对后者严到全拒绝。
       美术换资产就会失效，而插件的核心场景正是「同一批模型快速上色」。

    2. **源包围球半径 × 系数** —— 同样随尺度缩放，但**对离群顶点极敏感**。
       包围球半径由最远的顶点决定，只要源里有一个孤立顶点
       （建模残留、合并事故、刻意留的定位点），半径就会暴涨，
       距离上限随之放宽到形同虚设。而这恰恰是最需要被拦住的场景
       ——离群点正是「源覆盖范围之外」的极端情况。

    3. **目标点到源包围盒表面的距离** —— 看似更直观（「离源多远」），
       但有两个致命问题：
       a. 包围盒是**世界轴对齐**的。模型一旦旋转，AABB 就会显著大于
          模型本身，盒内会出现大片「其实离源很远」的区域被误判为 0 距离。
       b. AABB 内部**必然包含不属于源表面的点**（空腔、对角摆放），
          这些点算出的表面距离是 0，约束完全失效。

    **对角线**同时躲开以上三个问题：它与朝向无关（旋转不变）、
    对孤立顶点不敏感（少数离群点撑大的是 AABB，而对角线只按三边中的
    最大者增长）、且对 LOD 场景（源是同一模型的密网格，目标是疏网格）
    天然留出余量——密源的包围盒与疏目标的包围盒本就该接近。

    代价：一次 O(N) 扫描。结果记忆化在 source_data 上（同源复用），
    且**只在距离上限启用时**才计算。

    Args:
        source_data: 源数据缓存条目

    Returns:
        float: 对角线长度；无顶点或退化（全重合）时返回 0.0
    """
    # 记忆化：批量复制时同一源会被上千个目标复用
    cached = source_data.get(_BBOX_DIAG_KEY)
    if cached is not None:
        return cached

    vertices = source_data.get('vertices')
    if not vertices:
        source_data[_BBOX_DIAG_KEY] = 0.0
        return 0.0

    try:
        min_x = min_y = min_z = float('inf')
        max_x = max_y = max_z = float('-inf')
        for v in vertices:
            x, y, z = v[0], v[1], v[2]
            if x < min_x: min_x = x
            if x > max_x: max_x = x
            if y < min_y: min_y = y
            if y > max_y: max_y = y
            if z < min_z: min_z = z
            if z > max_z: max_z = z
        diagonal = math.sqrt(
            (max_x - min_x) ** 2 +
            (max_y - min_y) ** 2 +
            (max_z - min_z) ** 2
        )
    except Exception as e:
        # log_warning 只接受 message 一个参数（见 utils/logging_utils.py），
        # 用 exc= 会 TypeError，把本应「降级为关闭上限继续」的防御路径
        # 变成整次复制失败。
        log_warning(f"计算源包围盒失败，本次关闭取色距离上限: {e}")
        diagonal = 0.0

    if not math.isfinite(diagonal):
        # 源坐标含 NaN/Inf 时 min/max 不更新（保持 ±inf），对角线变为
        # 非有限值 -> 上限=inf -> 约束静默失效。显式降级为关闭并告警。
        log_warning(
            "源顶点坐标含非有限值（NaN/Inf），无法计算包围盒对角线，"
            "本次关闭取色距离上限。"
        )
        diagonal = 0.0

    source_data[_BBOX_DIAG_KEY] = diagonal
    return diagonal


def _resolve_distance_limit_sq(vc_tool, source_data):
    """
    把「取色距离上限」设置解析为**距离平方**（可直接与 dist_sq 比较）。

    Args:
        vc_tool: 工具设置对象，可为 None
        source_data: 源数据缓存条目

    Returns:
        float | None: 允许的最大距离平方；**None 表示未启用**
            （阈值为 0，或源尺度退化）
    """
    if vc_tool is None:
        return None
    try:
        percent = float(getattr(vc_tool, 'pick_distance_percent', 0.0) or 0.0)
    except Exception:
        return None
    # 与 _distance_constraint_enabled 的「>0 判启用」严格同构：
    # not (percent > 0.0) 对 NaN 同样判关闭，两处判据永不分叉。
    if not percent > 0.0:
        return None

    diagonal = _source_bbox_diagonal(source_data)
    if diagonal < _MIN_SOURCE_EXTENT:
        # 源退化（单顶点 / 全部重合）：按比例的上限会退化成 0，
        # 把除该点外所有目标顶点都拒掉。宁可不拦，也不把模型涂空白。
        # 批量复制时每个目标都会走到这里，警告按源去重、只提示一次。
        if not source_data.get(_DEGENERATE_WARNED_KEY):
            log_warning(
                "源顶点集退化（对角线近0，通常只有 1 个顶点），"
                "无法按比例计算取色距离上限，本次不启用该限制。"
            )
            source_data[_DEGENERATE_WARNED_KEY] = True
        return None

    limit = diagonal * (percent / 100.0)
    return limit * limit


def _distance_constraint_enabled(vc_tool):
    """
    取色距离上限是否启用（阶段 C）。

    与 _normal_constraint_enabled 同构：阈值为 0（或属性缺失）即关闭，
    便于用户回退对比，且保证「关闭时行为与该功能上线前逐字一致」。
    """
    if vc_tool is None:
        return False
    try:
        return float(getattr(vc_tool, "pick_distance_percent", 0.0) or 0.0) > 0.0
    except Exception:
        return False



def _transform_normals_array(normals, normal_matrix):
    """
    批量把法线变换到世界空间并单位化（阶段 B 性能优化，**float64**）。

    与 _target_points_array 同思路（numpy 一次性完成矩阵变换与归一化），
    但有两处**必须不同**：

    1. **精度必须是 float64**（不可照抄 float32）。
       法线归一化后直接参与「夹角 > 阈值」的判定，float32 只有约 7 位有效
       数字，在阈值边界（如 74.99° vs 75.01°）足以让判定翻转 ->
       选到不同的最近点 -> **颜色落到错误顶点**。
       这与项目"内核内部用 double，与 Python 版保持一致"是同一条红线。
    2. **输入是扁平数组而非 list[Vector]**。
       `_extract_normals` 已用 foreach_get 直接导出 (N,3) 的 float64 数组，
       因此这里无需 `[n[0] for n in normals]` 这类 Python 循环拆分量——
       那一步会把向量化收益吃掉大半。

    性能（本机实测，20 万顶点、float64）：
        逐顶点 Python 循环约 380ms -> 向量化后约 4ms（约 **95x**）。
        （早先标注的 190x 是 float32 测得；float64 略慢，差距约 2 倍。）
        该项不是性能热点，用精度换这点开销完全值得。

    Args:
        normals: 形状 (N, 3) 的局部空间法线数组（float64）
        normal_matrix: 法线变换矩阵（逆转置）

    Returns:
        numpy.ndarray | None: 形状 (N, 3) 的 float64 世界空间单位法线；
            numpy 不可用或失败时返回 None，由调用方回退逐点实现
    """
    if np is None or normals is None:
        return None

    # 统一走cache._numpy_has_arrays()（从 cache 导入）：
    # 判据集中在一处，避免两处标准不一致——改一处漏一处就是真bug。
    if not _numpy_has_arrays():
        return None

    try:
        arr = np.asarray(normals, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[1] != 3 or arr.shape[0] == 0:
            return None
        # np.asarray 对 list[Vector] 可能产生 object dtype（无法参与矩阵乘）。
        # 若发生这种情况（回退路径传入列表），交由调用方走逐点实现。
        if arr.dtype != np.float64:
            return None

        m = np.array(normal_matrix, dtype=np.float64)
        # 原地变换，避免额外分配 (N,3)
        arr = arr @ m[:3, :3].T

        # 归一化（零长度法线保持为 0，避免除零产生 nan）
        lengths = np.linalg.norm(arr, axis=1)
        lengths[lengths == 0.0] = 1.0
        arr = arr / lengths[:, None]
        return arr
    except Exception:
        # numpy 路径失败时交由调用方回退到逐顶点实现
        return None


def _transform_normals(normals, matrix_world):
    """
    把法线从局部空间变换到世界空间并单位化（含 numpy 加速与逐点回退）。

    阶段 B 性能关键路径：源侧与目标侧都要做一次，必须向量化。

    **两条路径必须给出数值等价的结果**（float64）：
    numpy 路径与逐点回退路径都参与同一个夹角阈值判定，
    若二者精度不同，则「有 numpy / 无 numpy的机器上取色结果不同」——
    这正是 tests/test_native_equivalence.py 要防的那类问题，
    在此新增的代码里同样不能破例。

    Args:
        normals: (N,3) float64 数组，或 mathutils.Vector 列表（回退用）
        matrix_world: 世界变换矩阵

    Returns:
        (N,3) float64 数组 | list[Vector] | None
    """
    if normals is None or len(normals) == 0:
        return None
    normal_matrix = _normal_matrix(matrix_world)

    # 优先走 numpy 向量化（20 万顶点 float64 约 4ms）
    fast = _transform_normals_array(normals, normal_matrix)
    if fast is not None:
        return fast

    # 回退：逐顶点变换（mathutils 的 matrix @ Vector 本身就是双精度）
    try:
        result = []
        for n in normals:
            if n is None:
                result.append(None)
                continue
            # `M @ n` 已返回 Vector，无需再包一层 Vector(...)
            v = normal_matrix @ n
            result.append(v.normalized() if hasattr(v, "normalized") else v)
        return result
    except Exception:
        return None


def _copy_colors_to_attribute_kdtree(vcol_layer, target_vertices, target_mesh, vertex_to_loops, source_data, kd, target_normals=None, angle_threshold_deg=0.0, max_dist_sq=None):
    """使用KDTree将颜色复制到颜色属性（可带法线约束 + 取色距离上限）"""
    vertex_colors = source_data['vertex_colors']
    for vert_idx, target_pos in enumerate(target_vertices):
        if vert_idx >= len(target_mesh.vertices):
            continue

        normal = None
        if target_normals is not None and vert_idx < len(target_normals):
            normal = target_normals[vert_idx]

        src_idx = _find_color_index(target_pos, normal, source_data, kd,
                                    angle_threshold_deg, max_dist_sq)
        if src_idx < 0:
            # 超距（或无候选）-> 不写入，即保留目标原色（用户明确选择的语义）
            continue
        color = vertex_colors.get(src_idx, (1.0, 1.0, 1.0, 1.0))

        if vcol_layer.domain == 'POINT' and vert_idx < len(vcol_layer.data):
            vcol_layer.data[vert_idx].color = (color[0], color[1], color[2], color[3] if len(color) > 3 else 1.0)
        elif vcol_layer.domain == 'CORNER' and vert_idx in vertex_to_loops:
            for loop_idx in vertex_to_loops[vert_idx]:
                if loop_idx < len(vcol_layer.data):
                    vcol_layer.data[loop_idx].color = (color[0], color[1], color[2], color[3] if len(color) > 3 else 1.0)


def _copy_colors_to_attribute_bruteforce(vcol_layer, target_vertices, target_mesh, vertex_to_loops, source_vertices, source_vertex_colors, max_dist_sq=None):
    """
    使用暴力搜索将颜色复制到颜色属性。

    同样支持取色距离上限（阶段 C）。**这不是可选的**：
    距离上限若只在 KDTree 路径生效，那么「关掉 KDTree + 开着距离上限」
    的用户会得到一个静默失效的功能——参数显示已启用、实际完全不生效。
    这与本项目 v1.1.0 修掉的「界面显示 75°、实际走原生而无效」是同一类问题。
    """
    for vert_idx, target_pos in enumerate(target_vertices):
        if vert_idx >= len(target_mesh.vertices):
            continue

        min_dist_sq = float('inf')
        nearest_color = (1.0, 1.0, 1.0, 1.0)

        for src_idx, src_pos in enumerate(source_vertices):
            dist_sq = (src_pos - target_pos).length_squared
            if dist_sq < min_dist_sq:
                min_dist_sq = dist_sq
                if src_idx in source_vertex_colors:
                    nearest_color = source_vertex_colors[src_idx]

        # 阶段 C：最近点都超限 -> 不写入，保留目标原色
        if max_dist_sq is not None and min_dist_sq > max_dist_sq:
            continue

        if vcol_layer.domain == 'POINT' and vert_idx < len(vcol_layer.data):
            vcol_layer.data[vert_idx].color = (nearest_color[0], nearest_color[1], nearest_color[2], nearest_color[3] if len(nearest_color) > 3 else 1.0)
        elif vcol_layer.domain == 'CORNER' and vert_idx in vertex_to_loops:
            for loop_idx in vertex_to_loops[vert_idx]:
                if loop_idx < len(vcol_layer.data):
                    vcol_layer.data[loop_idx].color = (nearest_color[0], nearest_color[1], nearest_color[2], nearest_color[3] if len(nearest_color) > 3 else 1.0)


def _copy_colors_to_vcol_kdtree(vcol_layer, target_vertices, target_mesh, vertex_to_loops, source_data, kd, target_normals=None, angle_threshold_deg=0.0, max_dist_sq=None):
    """使用KDTree将颜色复制到传统顶点色层（可带法线约束 + 取色距离上限）"""
    vertex_colors = source_data['vertex_colors']
    for vert_idx, target_pos in enumerate(target_vertices):
        if vert_idx >= len(target_mesh.vertices):
            continue

        normal = None
        if target_normals is not None and vert_idx < len(target_normals):
            normal = target_normals[vert_idx]

        src_idx = _find_color_index(target_pos, normal, source_data, kd,
                                    angle_threshold_deg, max_dist_sq)
        if src_idx < 0:
            # 超距（或无候选）-> 不写入，即保留目标原色（用户明确选择的语义）
            continue
        color = vertex_colors.get(src_idx, (1.0, 1.0, 1.0, 1.0))

        if vert_idx in vertex_to_loops:
            for loop_idx in vertex_to_loops[vert_idx]:
                if loop_idx < len(vcol_layer.data):
                    vcol_layer.data[loop_idx].color = (color[0], color[1], color[2], color[3] if len(color) > 3 else 1.0)


def _copy_colors_to_vcol_bruteforce(vcol_layer, target_vertices, target_mesh, source_vertices, source_vertex_colors, max_dist_sq=None):
    """
    使用暴力搜索将颜色复制到传统顶点色层。

    与 _copy_colors_to_attribute_bruteforce 同理：距离上限必须在这条
    路径上同样生效，否则「关 KDTree + 开距离上限」会静默失效。
    """
    for poly in target_mesh.polygons:
        for loop_idx in poly.loop_indices:
            loop = target_mesh.loops[loop_idx]
            vert_idx = loop.vertex_index

            if vert_idx >= len(target_vertices):
                continue

            target_pos = target_vertices[vert_idx]
            min_dist_sq = float('inf')
            nearest_color = (1.0, 1.0, 1.0, 1.0)

            for src_idx, src_pos in enumerate(source_vertices):
                dist_sq = (src_pos - target_pos).length_squared
                if dist_sq < min_dist_sq:
                    min_dist_sq = dist_sq
                    if src_idx in source_vertex_colors:
                        nearest_color = source_vertex_colors[src_idx]

            # 阶段 C：最近点都超限 -> 不写入，保留目标原色
            if max_dist_sq is not None and min_dist_sq > max_dist_sq:
                continue

            if loop_idx < len(vcol_layer.data):
                vcol_layer.data[loop_idx].color = (nearest_color[0], nearest_color[1], nearest_color[2], nearest_color[3] if len(nearest_color) > 3 else 1.0)


def _write_colors_to_target(target_obj, target_mesh_eval, target_layer, source_data, vc_tool=None):
    """
    把源物体的颜色写入目标物体的颜色层（原生路径 + Python 回退路径）。

    P0-3 重构说明:
        原实现把两条路径内联在 copy_vertex_colors_between_objects 里，
        各自维护一处 to_mesh_clear()，导致异常路径漏清。这里把「纯写色逻辑」
        抽成独立函数，求值网格的生命周期交由调用方用单次 try/finally 管理。

    Args:
        target_obj: 目标物体
        target_mesh_eval: 目标物体的求值网格（生命周期由调用方负责）
        target_layer: 目标颜色层
        source_data: 源数据缓存条目
        vc_tool: 工具设置对象（阶段 B：读取法线夹角阈值；
            阶段 C：读取取色距离上限）。为 None 时两个约束都不生效，
            等价于纯距离最近。

    Returns:
        bool: 是否成功写入
    """
    matrix_world = target_obj.matrix_world
    target_mesh = target_obj.data

    # === 法线约束是否启用（阶段 B / 批次 2 修正）===
    # 只判断一次并复用：既用于「是否绕过原生内核」的门控，
    # 也用于后面的阈值读取，避免写两套判断导致二者不一致。
    normal_enabled = _normal_constraint_enabled(vc_tool)

    # === 取色距离上限是否启用（阶段 C）===
    # 同样只判断一次：门控与阈值解析必须用同一个判据，
    # 否则会出现「为性能绕过原生、但约束其实没生效」这类静默失效。
    distance_enabled = _distance_constraint_enabled(vc_tool)

    # === 原生加速路径（C++ KDTree 查询 + 批量写入）===
    # 命中时完全跳过 Python 逐顶点循环与逐元素 RNA 写入。
    #
    # 门控（重要）: 原生内核的 vct_kdtree_query_colors 只做纯距离最近邻，
    # **既不实现法线约束，也不实现距离上限**。若在任一约束启用时仍走原生路径，
    # 就会出现「界面显示已启用 75° / 上限 N%、实际完全无效」的静默失效——
    # 与本轮修复的预览态污染、错误信息不可见属于同类问题。
    # 因此这里显式绕过原生，走 Python 路径。
    # 代价：大模型取色会变慢（用户可将两个阈值都设为 0 换回原生加速，
    #详见 docs/LIMITATIONS.md 9.1）。
    #
    # 两个约束是「或」关系（各自独立生效），任一启用即绕过原生；
    # 提示文案必须同时说明两者，否则用户只会去调错那一个。
    need_python = normal_enabled or distance_enabled
    native_tree = source_data.get('native_tree')
    if native_tree is not None and not need_python:
        try:
            target_points = _target_points_array(target_mesh_eval, matrix_world)
            out_colors = native_tree.query_colors(
                source_data['colors_dense'], target_points
            )
            _apply_colors_bulk(target_layer, out_colors, target_mesh)
            return True
        except Exception as e:
            # 原生路径出任何问题都不能让复制失败，回退 Python 实现
            log_warning(f"原生取色失败，本次回退 Python 实现: {e}")
    elif native_tree is not None and need_python:
        reasons = []
        if normal_enabled:
            reasons.append("法线夹角约束")
        if distance_enabled:
            reasons.append("取色距离上限")
        log_info(
            f"{'、'.join(reasons)}已启用，本次改用 Python 取色路径"
            "（原生内核暂不支持这两项约束）。"
            "如需恢复原生加速，请将对应阈值都设为 0。"
        )

    # === Python 路径（原有实现，作为回退）===
    target_vertices = [matrix_world @ v.co for v in target_mesh_eval.vertices]

    vertex_to_loops = {v.index: [] for v in target_mesh.vertices}
    for poly in target_mesh.polygons:
        for loop_idx in poly.loop_indices:
            loop = target_mesh.loops[loop_idx]
            vertex_to_loops[loop.vertex_index].append(loop_idx)

    # === P1-8 性能兜底 ===
    # 优先使用 KDTree；若 KDTree 不可用且暴力搜索代价过高，
    # 则临时构建 KDTree，绝不允许退化为 O(N×M) 的纯 Python 循环。
    kd = source_data.get('kd')
    if kd is None:
        source_count = len(source_data['vertices'])
        target_count = len(target_vertices)
        if source_count * target_count > BRUTEFORCE_OP_LIMIT:
            kd = _build_kdtree(source_data['vertices'])
            if kd is not None:
                log_warning(
                    f"{target_obj.name}: 顶点数较多，已自动启用 KDTree 加速"
                    f"（源 {source_count} × 目标 {target_count}）"
                )

    use_kdtree = kd is not None
    is_attribute = hasattr(target_layer, 'domain')

    # === 阶段 C：解析取色距离上限（距离平方）===
    # 只在启用时做一次 O(N) 源包围盒扫描（结果已记忆化在 source_data 上，
    # 同一源被上千目标复用时只扫一次）。未启用时 max_dist_sq 为 None，
    # 下面所有距离判定分支都不执行 —— 逐字保持阶段 B 的原开销与行为。
    max_dist_sq = _resolve_distance_limit_sq(vc_tool, source_data) \
        if distance_enabled else None

    # === 阶段 B：法线约束 ===
    # 仅在启用且源侧确实取到法线时才付出提取成本；
    # 关闭时 target_normals 为 None，下面走纯距离最近（原行为，零开销）。
    # normal_enabled 已在原生门控处算好，此处直接复用。
    target_normals = None
    angle_threshold = 0.0
    if normal_enabled:
        angle_threshold = float(getattr(vc_tool, 'normal_angle_threshold', 0.0) or 0.0)
        # 统一用 _has_normals()，绝不用 bool(source_data.get('normals')):
        # 法线是 numpy 数组，bool(多元素数组) 会抛 ValueError。
        # 这行尤其关键——它在取色路径上，异常会让整个复制失败。
        if _has_normals(source_data):
            local_normals = _extract_normals(target_mesh_eval,
                                            len(target_vertices))
            if local_normals is not None:
                try:
                    target_normals = _transform_normals(local_normals,
                                                        matrix_world)
                except Exception as e:
                    log_warning(f"目标法线变换失败，本次不启用法线约束: {e}")
                    target_normals = None
            if target_normals is None:
                angle_threshold = 0.0  # 源有法线但目标取不到 -> 退回原行为

    if is_attribute:
        if use_kdtree:
            _copy_colors_to_attribute_kdtree(
                target_layer, target_vertices, target_mesh, vertex_to_loops,
                source_data, kd, target_normals, angle_threshold, max_dist_sq)
        else:
            _copy_colors_to_attribute_bruteforce(target_layer, target_vertices, target_mesh, vertex_to_loops, source_data['vertices'], source_data['vertex_colors'], max_dist_sq)
    else:
        if use_kdtree:
            _copy_colors_to_vcol_kdtree(
                target_layer, target_vertices, target_mesh, vertex_to_loops,
                source_data, kd, target_normals, angle_threshold, max_dist_sq)
        else:
            _copy_colors_to_vcol_bruteforce(target_layer, target_vertices, target_mesh, source_data['vertices'], source_data['vertex_colors'], max_dist_sq)

    return True


def copy_vertex_colors_between_objects(
    source_obj: bpy.types.Object,
    target_obj: bpy.types.Object,
    vc_tool=None,
    failure_reasons: list = None,
) -> bool:
    """
    复制顶点色的核心函数（V3版，支持指定层名）

    Args:
        source_obj: 源物体
        target_obj: 目标物体
        vc_tool: 工具设置对象，为None 时从场景读取
        failure_reasons: 可选的列表。传入时，所有失败路径都会向其追加
            一条「原因」文本，供上层聚合后展示给用户（P0-5）。
            为None 时完全不记录（保持既有调用方零开销、零行为变化）。

    Returns:
        bool: 是否复制成功
    """
    # 局部记录助手：把失败原因同时写进日志与（可选的）收集列表
    def _fail(reason, exc=None):
        log_error(reason, exc=exc)
        if failure_reasons is not None:
            failure_reasons.append(reason)
        return False

    try:
        # === P0-1 数据污染防护（必须早于任何取色/缓存操作）===
        # 通道预览会把灰度写进真实颜色层、原始色留在备份层里。
        # 此状态下复制会把灰度当作原色扩散到全部目标物体，
        # 且函数仍返回 True（静默的跨集合数据损坏）。因此直接拒绝。
        if source_obj.type == 'MESH' and has_preview_backup(source_obj.data):
            return _fail(
                f"{source_obj.name}: 源物体处于通道预览状态，"
                f"此时复制会把灰度预览色当作原色写入目标物体。"
                f"请先点「RGBA」恢复完整颜色显示后再复制。"
            )

        if vc_tool is None:
            vc_tool = bpy.context.scene.vertex_color_tool

        # 根据设置决定要使用的层名
        specified_name = vc_tool.target_vcol_name if not vc_tool.use_active_vcol else None

        # --- 处理源物体 ---
        source_layer = get_vcol_layer(source_obj, name=specified_name, create_if_missing=False)
        if not source_layer:
            # 如果按名称找不到，则回退到激活层
            source_layer = get_active_vcol_layer(source_obj)
            if not source_layer:
                return _fail(
                    f"{source_obj.name}: 既没有名为 '{specified_name}' 的颜色层，也没有激活颜色层"
                )

        source_data = VertexColorCache.get_source_data(source_obj, source_layer.name, vc_tool)
        if not source_data:
            return _fail(
                f"{source_obj.name}: 无法从颜色层 '{source_layer.name}' 中获取顶点色数据"
            )

        # --- 处理目标物体 ---
        target_layer = get_vcol_layer(target_obj, name=specified_name, create_if_missing=True)
        if not target_layer:
            return _fail(
                f"{target_obj.name}: 无法获取或创建名为 '{specified_name or 'Color'}' 的顶点色层"
            )

        depsgraph = bpy.context.evaluated_depsgraph_get()
        target_obj_eval = target_obj.evaluated_get(depsgraph)
        target_mesh_eval = target_obj_eval.to_mesh()

        # to_mesh() 返回 None 表示没有可用的求值网格，此时不应调用
        # to_mesh_clear()（对未成功 to_mesh 的对象调用属于依赖未定义行为）。
        if not target_mesh_eval:
            return _fail(f"{target_obj.name}: 无法获取求值网格（已跳过）")

        # === P0-3 内存泄漏防护 ===
        # 单次 try/finally 收口：无论 _write_colors_to_target 走原生路径还是
        # Python 路径，也无论中途是否抛异常，求值网格都必定被释放。
        # 原实现的三处 to_mesh_clear() 都会被外层 except 跳过。
        try:
            _write_colors_to_target(target_obj, target_mesh_eval, target_layer,
                                    source_data, vc_tool)
        finally:
            try:
                target_obj_eval.to_mesh_clear()
            except Exception as clear_exc:
                log_warning(f"释放求值网格失败 ({target_obj.name}): {clear_exc}")

        target_obj.data.update()
        return True

    except Exception as e:
        return _fail(f"{target_obj.name}: 复制顶点色时出错 - {e}", exc=e)


def fill_vertex_colors(context, selected_objects, color, vc_tool):
    """
    统一的顶点色填充逻辑（V3版，支持指定层名）

    修复说明（P1-7）:
        原实现对每个物体执行两次 bpy.ops.object.mode_set，N 个物体即 2N 次
        全场景 depsgraph 更新。现改为整体只切换一次模式。
    """
    filled_object_count = 0
    selected_component_count = 0

    if len(color) == 3:
        color = (*color, 1.0)

    # 先记录操作前处于编辑模式的物体（模式切换后 obj.mode 会改变）
    edit_mode_objects = {obj.name for obj in selected_objects if obj.mode == 'EDIT'}

    # 根据设置决定要使用的层名
    specified_name = vc_tool.target_vcol_name if not vc_tool.use_active_vcol else None

    # 整体切换到物体模式一次，便于稳定读写网格数据
    switched_to_object = False
    if edit_mode_objects:
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
            switched_to_object = True
        except Exception as e:
            log_warning(f"切换到物体模式失败，将按当前模式处理: {e}")

    try:
        for obj in selected_objects:
            if obj.type != 'MESH':
                continue

            is_edit_mode_target = obj.name in edit_mode_objects

            try:
                # 使用新的核心函数获取或创建层
                target_layer = get_vcol_layer(obj, name=specified_name, create_if_missing=True)

                if not target_layer:
                    log_warning(f"无法为物体 '{obj.name}' 获取或创建顶点色层，已跳过。")
                    continue

                mesh = obj.data
                layer_data = target_layer.data

                if is_edit_mode_target:
                    selected_verts = [v.index for v in mesh.vertices if v.select]

                    if not selected_verts:
                        for i in range(len(layer_data)):
                            layer_data[i].color = color
                    else:
                        if target_layer.domain == 'POINT':
                            for vert_index in selected_verts:
                                if vert_index < len(layer_data):
                                    layer_data[vert_index].color = color
                        elif target_layer.domain == 'CORNER':
                            for poly in mesh.polygons:
                                for loop_idx in poly.loop_indices:
                                    loop = mesh.loops[loop_idx]
                                    if loop.vertex_index in selected_verts and loop_idx < len(layer_data):
                                        layer_data[loop_idx].color = color

                        selected_component_count += len(selected_verts)
                else:
                    for i in range(len(layer_data)):
                        layer_data[i].color = color

                mesh.update()
                filled_object_count += 1

            except Exception as e:
                log_error(f"为 '{obj.name}' 填充颜色时出错", exc=e)
    finally:
        if switched_to_object:
            try:
                bpy.ops.object.mode_set(mode='EDIT')
            except Exception as e:
                log_warning(f"恢复编辑模式失败: {e}")

    return filled_object_count, selected_component_count
