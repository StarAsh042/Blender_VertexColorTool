"""
顶点色数据缓存模块

审计修复说明:
    - P0-2: 顶点位置与顶点颜色改为取自「同一个求值网格」。
      原实现顶点来自 to_mesh()（含修改器结果），颜色却来自原始网格的
      loop.vertex_index，两者在带修改器（细分/布尔/镜像等）时会错位，
      导致颜色被静默地复制错，且不产生任何报错。
    - P1-4: KDTree 在顶点数超过暴力搜索阈值时强制构建，
      避免静默退化为 O(N×M) 的纯 Python 双重循环。
    - P1-5: 缓存键从「物体名称」改为「内存地址 + 顶点数」，
      并且**在缓存条目中保存对象引用，命中时用 `is` 做身份校验**。
      名称无法可信解析对象（重命名/重建同名/Linked Duplicate 会误命中）；
      而单靠内存地址同样不安全（对象删除后地址会被新对象复用）。
      实测 Blender 3.4 既无 ID.session_uid，也不支持对 bpy_struct 建立 weakref，
      因此采用「地址分桶 + 身份校验」方案：校验失败只会造成缓存未命中（重新计算），
      绝不会返回错误数据。
    - P0-3: 求值网格的释放统一由 try/finally 保证。原实现的多处
      to_mesh_clear() 在异常路径上会被外层 except 跳过，每个失败目标
      泄漏一个求值网格（20 万顶点 ≈ 12MB），批量复制时可达数十 GB。
    - P0-4: 缓存上限从「顶点数」改为「估算字节数」（详见下方常量区推导）。
      原 _max_cached_vertices = 2000000 实际对应约 532MB，
      在用户自己的 Blender 进程内足以被 OS 直接杀掉。
"""

import bpy
from mathutils import kdtree
import gc
from collections import OrderedDict

from ..utils.logging_utils import log_error, log_warning
from . import native_backend

try:
    import numpy as np
except ImportError:  # pragma: no cover - Blender 自带 numpy，此处仅作兜底
    np = None


# 超过该顶点数后，即便用户关闭了 KDTree 也会强制构建，
# 防止退化为 O(N×M) 的纯 Python 暴力搜索（5万×5万 ≈ 25 亿次距离计算）。
BRUTEFORCE_VERTEX_LIMIT = 20000


# =============================================================================
# P0-4 缓存内存预算
# =============================================================================
# 原实现用「顶点数」（_max_cached_vertices = 2000000）约束内存，
# 但顶点数并不与内存占用成正比，注释里声称的「约束内存占用」并未真正成立。
#
# 每顶点占用实测（本机 CPython 3.13 64 位，sys.getsizeof 逐项累加，20 万条目）：
#     list 槽位8 B
#     mathutils.Vector 本体                      56 B   （以 3 槽位对象实测为代理）
#     dict 表摊销                               52 B
#     int 键对象                28 B   （>256 的索引不被 CPython 缓存）
#     4 元组 (r,g,b,a) 容器                     72 B
#     4 个 float 载荷                          96 B   （元组是「引用」4 个独立
#                                                     float 对象，24B × 4；
#                                                     这一项最容易被漏算）
#     ---------------------------------------------------------
#     Python 侧合计                            312 B
#     numpy 稠密数组（points 3×f32 + colors 4×f32） 28 B
#     原生 KDTree 节点                            约 30 B
#     ---------------------------------------------------------
#     实测合计                                  约 370 B/顶点
#
# 2,000,000 顶点 × 370 B ≈ 740 MB。插件跑在用户自己的 Blender 进程内，
# 在 8GB 机器上叠加 Blender 自身占用后足以被 OS 直接杀掉（无任何报错）。
#
# 因此改为**按估算字节数限流**：直接以「内存」为约束对象，
# 而顶点数只是估算 bytes 的中间量。取 256 MB 作为默认预算：
#   - 相比原来的 ~740 MB 峰值，内存占用下降约三分之二；
#   - 仍可容纳约 72 万顶点的缓存，覆盖绝大多数美术工作流；
#   - 预留足够余量给 Blender 自身与其余插件。
#
# 说明：本估算基于 CPython 对象大小的实测值，各版本间相当稳定；
# mathutils.Vector 与原生 KDTree 节点取保守值。
# 即便估算偏低，也还有 _max_cache_size 的条目数上限作为第二道防线。
# 注意：此值是**估算**而非精确值，测试不应把它固化为断言基线
# （固化会让后续任何重新标定都变成「测试失败」而非「更新基线」）。
#
# 阶段 B 追加项——法线数组的内存（法线约束启用时才存在）:
#     法线以 **float64 的 (N,3) numpy 数组** 随缓存条目常驻，
#     占用 = 3 × 8 × N = 24 B/顶点。
#     （早先误按 list[Vector] 估成 64 B/顶点 —— 既高估了内存，
#       又掩盖了「中间多建了一层 list[Vector]」的浪费，现已改为
#       foreach_get 直接导出扁平 float64 数组，不再有 Python 对象层。）
BYTES_PER_NORMAL_ESTIMATE = 24
BYTES_PER_VERTEX_ESTIMATE = 370
CACHE_MEMORY_BUDGET_BYTES = 256 * 1024 * 1024  # 256 MB


def estimate_cache_entry_bytes(vertex_count, has_normals=False):
    """
    估算一个缓存条目占用的字节数（P0-4）。

    Args:
        vertex_count: 条目内的顶点数量
        has_normals: 是否包含法线数组（阶段 B）

    Returns:
        int: 估算字节数
    """
    if vertex_count <= 0:
        return 0
    total = int(vertex_count) * BYTES_PER_VERTEX_ESTIMATE
    if has_normals:
        total += int(vertex_count) * BYTES_PER_NORMAL_ESTIMATE
    return total


def _native_usable(vc_tool):
    """原生加速是否应当启用"""
    if np is None:
        return False
    if not getattr(vc_tool, "use_native_accel", True):
        return False
    return native_backend.is_available()


def _build_native_artifacts(vertices, color_map):
    """
    把顶点列表与稀疏颜色字典转成原生内核需要的稠密数组，并构建 KDTree。

    Args:
        vertices: mathutils.Vector 列表（世界坐标）
        color_map: {顶点下标: (r,g,b,a)}，可能稀疏

    Returns:
        (native_tree, points_array, colors_dense) 或 (None, None, None)
    """
    if np is None or not vertices:
        return None, None, None

    count = len(vertices)
    points = np.empty((count, 3), dtype=np.float32)
    for i, co in enumerate(vertices):
        points[i, 0] = co[0]
        points[i, 1] = co[1]
        points[i, 2] = co[2]

    # 缺失颜色的顶点填白色，与 Python 路径的 .get(idx, 白色) 语义一致
    colors = np.ones((count, 4), dtype=np.float32)
    for index, color in color_map.items():
        if 0 <= index < count:
            colors[index, 0] = color[0]
            colors[index, 1] = color[1]
            colors[index, 2] = color[2]
            colors[index, 3] = color[3] if len(color) > 3 else 1.0

    tree = native_backend.NativeKDTree(points)
    return tree, points, colors


def _active_color_path(vc_tool):
    """
    返回当前实际生效的取色路径标识（阶段 B / 阶段 C）。

    取值:
        'python-normal'       仅法线约束启用 -> 强制 Python 路径
        'python-distance'     仅取色距离上限启用 -> 强制 Python 路径
        'python-constraints'  两条约束同时启用 -> 强制 Python 路径
        'native'              约束全关且原生可用 -> 原生 C++ 内核
        'python'              其余情况（dll 缺失 / 用户关闭加速 / numpy 缺失）

    存在的意义: 两条约束均未在原生内核实现，任一启用都会绕过原生。
    若不把真实路径暴露出来，UI 与性能报告会显示与实际不符的信息，
    造成「参数已设置但不生效」的静默失效观感。
    """
    normal_on = _normal_constraint_enabled(vc_tool)
    distance_on = _distance_constraint_enabled(vc_tool)
    if normal_on and distance_on:
        return 'python-constraints'
    if normal_on:
        return 'python-normal'
    if distance_on:
        return 'python-distance'
    try:
        # numpy 缺失时原生内核根本建不起来（_native_usable 为 False），
        # 这里必须同步判定，否则「dll 在而 numpy 缺」的极端环境会
        # 显示「原生内核」而实际走 Python。
        if (vc_tool is not None and np is not None
                and getattr(vc_tool, 'use_native_accel', True)):
            from . import native_backend
            if native_backend.is_available():
                return 'native'
    except Exception:
        pass
    return 'python'


def _normal_constraint_enabled(vc_tool):
    """
    法线约束是否启用（阶段 B）。

    判定: vc_tool 上normal_angle_threshold > 0 即启用。
    阈值为 0（或属性缺失）表示完全关闭，走纯距离最近的原行为，
    便于用户回退对比。
    """
    if vc_tool is None:
        return False
    try:
        return float(getattr(vc_tool, "normal_angle_threshold", 0.0) or 0.0) > 0.0
    except Exception:
        return False


def _distance_constraint_enabled(vc_tool):
    """
    取色距离上限是否启用（阶段 C）。

    与 core.vertex_color_ops 同名判据严格同构：>0 即启用，
    0（或属性缺失）表示完全关闭。not (x > 0) 的写法对 NaN 同样判关闭，
    两处判据永不分叉。
    """
    if vc_tool is None:
        return False
    try:
        return float(getattr(vc_tool, "pick_distance_percent", 0.0) or 0.0) > 0.0
    except Exception:
        return False


def _normal_matrix(matrix_world):
    """
    由世界变换矩阵求法线变换矩阵（逆转置）。

    法线不能像位置那样直接用matrix_world 变换，否则非等比缩放
    会把法线方向改错。本插件的典型用法是旋转 + 等比缩放，
    但为正确性仍实现逆转置；失败时退回直接变换。
    """
    try:
        return matrix_world.to_3x3().inverted_safe().transposed()
    except Exception:
        try:
            return matrix_world.to_3x3()
        except Exception:
            return matrix_world


def _extract_normals(mesh, vertex_count):
    """
    批量提取网格顶点法线（阶段 B：法线约束用）。

    使用 foreach_get 一次性导出为**扁平 float64 数组**，与 _target_points_array
    同思路，但有两处**必须**不同：

    1. 缓冲区长度：'normal' 是 3 分量属性，缓冲区必须是 3 × count 个元素。
       （早先误用 `[None] * vertex_count` 只有 count 个，在真实 Blender 中
       会抛异常并走进 except，导致法线约束**永远不生效**。）
    2. 精度：必须是 **float64**。法线归一化后要参与夹角阈值判定，
       float32 约 7 位有效数字，在阈值边界（如 74.99° vs 75.01°）
       足以让判定翻转——这与项目"内核内部用 double"的红线是同一类问题。

    不再构造 list[Vector] 中间层：它既逐顶点占用 56B 内存，
    又迫使后续必须用 `[n[0] for n in normals]` 这样的 Python 循环拆分量，
    抵消了向量化的收益。

    Args:
        mesh: 求值网格
        vertex_count: 需要读取的顶点数量

    Returns:
        numpy.ndarray | None: 形状 (vertex_count, 3) 的 float64 数组；
            失败返回 None（调用方需能降级到"纯距离最近"）
    """
    if mesh is None or vertex_count <= 0:
        return None

    # numpy 可用：直接导出扁平 float64 数组（主路径，无 Python 对象层）
    #
    # 判据用「是否真有 empty/reshape 能力」而非 `np is not None`：
    # 精简环境（或测试替身）下可能存在一个名字叫 numpy 的模块却并无数组能力，
    # 只判 None 会让主路径进入后立刻失败并 return None，
    # 使法线约束**静默失效**而不是走回退路径。
    if _numpy_has_arrays():
        try:
            flat = np.empty(vertex_count * 3, dtype=np.float64)
            mesh.vertices.foreach_get("normal", flat)
            return flat.reshape(vertex_count, 3)
        except Exception:
            return None

    # === 无 numpy 时的回退 ===
    # 用 list[Vector] 而非扁平列表：mathutils 的 Vector 才能承载 3 分量，
    # 而普通 list 需要 3*count 个元素、还得手工拆装。
    # 精度仍为双精度（mathutils.Vector 内部就是 double）。
    try:
        from mathutils import Vector
        normals = [Vector((0.0, 0.0, 0.0)) for _ in range(vertex_count)]
        mesh.vertices.foreach_get("normal", normals)
        return normals
    except Exception:
        # 某些上下文（如非 MESH 类型网格）可能不支持 normal 访问，
        # 此时返回 None 让上层退回「纯距离最近」的原行为。
        return None


def _has_normals(cache_data):
    """
    判断缓存条目是否带有法线数据。

    **绝不能写 `bool(cache_data.get('normals'))`**：
    法线是形状 (N,3) 的 numpy 数组，而 numpy 对多于 1 元素的数组求真值
    会**抛 ValueError**（真值歧义），不是返回 True/False：
        >>> bool(np.zeros((3, 3)))
        ValueError: The truth value of an array with more than one element
                    is ambiguous. Use a.any() or a.all()

    这个坑的危险性在于它**跨批次叠加才显现**：批次 2 引入 numpy 法线数组，
    批次 1 的字节记账里出现 bool(...)，各自单测都过（测试替身的法线
    是 list，list 的真值永远合法），合起来在默认设置下 100% 失败。

    统一在此实现，所有调用点共用同一份逻辑，避免改一处漏一处。

    Args:
        cache_data: 缓存条目字典；可为 None

    Returns:
        bool: 是否有非空法线数据
    """
    if not cache_data:
        return False
    normals = cache_data.get('normals')
    # 显式判None + 长度，绝不依赖真值求值
    return normals is not None and len(normals) > 0


def _numpy_has_arrays():
    """
    判断当前 numpy 是否具备真实数组能力（而非仅同名占位模块）。

    Blender 自带 numpy，正常情况恒为 True。
    这里额外防御精简环境与测试替身：若 numpy 存在但没有数组能力，
    调用方应走 list[Vector] 回退，而不是让法线约束静默失效。

    判据集中在此，core/vertex_color_ops.py 也从这里取，
    避免两处判断标准不一致（改一处漏一处就是真 bug）。

    必需能力: empty（分配缓冲）、asarray（类型转换）、
             linalg（求范数）、float64（精度）。
    """
    if np is None:
        return False
    return (hasattr(np, "empty") and hasattr(np, "asarray")
            and hasattr(np, "linalg") and hasattr(np, "float64"))


def _release_native(entry):
    """
    释放缓存条目持有的原生 KDTree，避免原生内存泄漏。

    调用方约定（重要）:
        使用 source_data 的代码必须在「取得后立即用完」，
        不可跨另一次缓存操作（get_source_data / clear_cache）持有该字典。
        否则该条目可能被 LRU 淘汰并释放句柄，导致使用已释放的原生内存。
        当前唯一的生产调用点 copy_vertex_colors_between_objects 满足此约定
        （取到 source_data 后到用完之间不再触碰缓存）。
    """
    if not entry:
        return
    tree = entry.get('native_tree')
    if tree is not None:
        try:
            tree.close()
        except Exception:  # noqa: BLE001
            pass
        entry['native_tree'] = None



def _extract_vertex_color_map(mesh, layer_name):
    """
    从给定网格提取 {顶点索引: (r, g, b, a)}。

    支持 POINT / CORNER 两种域，以及新式颜色属性与传统顶点色层两套 API。
    调用方必须保证「该网格」与构建 KDTree 所用的顶点列表是同一个网格，
    否则索引会错位（P0-2 的根因）。
    """
    result = {}
    if mesh is None:
        return result

    # 优先新式颜色属性 (Blender 3.2+)
    if hasattr(mesh, "color_attributes") and len(mesh.color_attributes) > 0:
        attr = None
        if layer_name and layer_name in mesh.color_attributes:
            attr = mesh.color_attributes[layer_name]
        else:
            for candidate in mesh.color_attributes:
                if candidate.name in ("Color", "Col"):
                    attr = candidate
                    break
        if attr is None:
            attr = mesh.color_attributes[0]

        data = attr.data
        if attr.domain == 'POINT':
            for i in range(min(len(data), len(mesh.vertices))):
                color = data[i].color
                result[i] = (color[0], color[1], color[2], color[3])
        else:
            for loop_index, loop in enumerate(mesh.loops):
                if loop_index < len(data):
                    color = data[loop_index].color
                    result[loop.vertex_index] = (color[0], color[1], color[2], color[3])
        return result

    # 回退到传统顶点色层
    if hasattr(mesh, "vertex_colors") and len(mesh.vertex_colors) > 0:
        vcol = None
        if layer_name and layer_name in mesh.vertex_colors:
            vcol = mesh.vertex_colors[layer_name]
        else:
            vcol = mesh.vertex_colors[0]

        if vcol:
            for loop_index, loop in enumerate(mesh.loops):
                if loop_index < len(vcol.data):
                    color = vcol.data[loop_index].color
                    result[loop.vertex_index] = (color[0], color[1], color[2], color[3])

    return result


class VertexColorCache:
    """
    顶点色数据缓存类（LRU优化版）

    功能:
        - 缓存源物体的顶点色数据，避免重复计算
        - 支持KDTree构建加速最近点搜索
        - 内存优化和垃圾回收
        - LRU缓存策略管理缓存项

    性能优化:
        - 使用OrderedDict实现真正的LRU缓存策略
        - 缓存KDTree避免重复构建
        - 限制缓存大小防止内存溢出
        - 记录缓存命中率统计
    """

    _cache = OrderedDict()
    _max_cache_size = 50  # 最大缓存条目数（第二道防线）
    # P0-4: 缓存内存预算（字节）。原先用 _max_cached_vertices（顶点数）限制，
    # 无法真正约束内存（2,000,000 顶点 ≈ 532MB），现改为按估算字节数限流。
    # 该值可由用户在插件设置中覆盖（0 = 使用默认值）。
    _max_cache_bytes = CACHE_MEMORY_BUDGET_BYTES
    _cached_bytes = 0  # 当前缓存内估算字节数
    _cached_vertices = 0  # 当前缓存内顶点总数（仅用于统计展示）
    _hits = 0  # 缓存命中次数
    _misses = 0  # 缓存未命中次数

    @classmethod
    def _resolve_budget_bytes(cls, vc_tool=None):
        """
        解析实际生效的内存预算，优先使用用户设置。

        Args:
            vc_tool: 工具设置对象，可为 None

        Returns:
            int: 字节数
        """
        if vc_tool is not None:
            try:
                override = int(getattr(vc_tool, 'cache_memory_budget_mb', 0) or 0)
            except Exception:
                override = 0
            if override > 0:
                return override * 1024 * 1024
        return cls._max_cache_bytes

    @classmethod
    def _make_cache_key(cls, source_obj, layer_name):
        """
        构造缓存「分桶」键。

        修复说明（P1-5）:
            原键为 (obj.name, layer)。仅靠名称无法可信解析对象：物体重命名、
            删除后重建同名物体、Linked Duplicate、跨 .blend 会话都会命中错误
            缓存，把 A 模型的颜色复制到 B 模型，且不产生任何报错。

            本键仅用于分桶查找，**真正的正确性由缓存条目内的对象引用 + `is`
            身份校验保证**（见 get_source_data）。

            为什么不直接用唯一 ID:
                Blender 3.4 的 Object 没有 session_uid，
                且 bpy_struct 不支持 weakref（均已实测确认），
                因此无法取得「删除后不复用」的稳定标识。
        """
        try:
            pointer = source_obj.as_pointer()
        except Exception:
            pointer = id(source_obj)

        try:
            vertex_count = len(source_obj.data.vertices) if source_obj.data else 0
        except Exception:
            vertex_count = 0

        return (pointer, layer_name, vertex_count)

    @classmethod
    def get_cache_stats(cls, vc_tool=None):
        """
        获取缓存统计信息

        Args:
            vc_tool: 工具设置对象。为 None 或未设置预算时回退到类默认值。
                **必须传入**才能反映用户在面板上设置的实际内存上限
                （否则用户调高预算后面板仍显示类默认的分母，P0-4 QA 复核项 B）。

        Returns:
            dict: 包含命中率、命中次数、未命中次数等统计信息
        """
        total = cls._hits + cls._misses
        hit_rate = (cls._hits / total * 100) if total > 0 else 0
        effective_budget = cls._resolve_budget_bytes(vc_tool)
        return {
            'hits': cls._hits,
            'misses': cls._misses,
            'hit_rate': hit_rate,
            'cache_size': len(cls._cache),
            'max_size': cls._max_cache_size,
            'cached_vertices': cls._cached_vertices,
            # P0-4: 顶点数不再是内存约束，改为暴露字节口径。
            # 分母用「实际生效预算」而非类默认值，确保用户改过设置后显示正确。
            'cached_bytes': cls._cached_bytes,
            'max_cache_bytes': effective_budget,
            'cached_mb': round(cls._cached_bytes / (1024 * 1024), 2),
            'max_cache_mb': round(effective_budget / (1024 * 1024), 2),
            # 阶段 B：当前实际生效的取色路径。
            # 法线约束启用时必须走 Python（原生内核未实现该约束），
            # 这里让 UI / 性能报告能显示真实路径，避免用户
            # 「界面显示已启用 75°、实际走原生」这种静默失效。
            'color_path': _active_color_path(vc_tool),
        }

    @classmethod
    def get_source_data(cls, source_obj, source_vcol_layer, vc_tool):
        """
        获取源物体的顶点色数据（带缓存）

        Args:
            source_obj: 源物体对象
            source_vcol_layer: 顶点色层名称
            vc_tool: 工具设置对象

        Returns:
            dict: 包含顶点位置、颜色和KDTree的缓存数据，或None

        性能优化:
            - 缓存命中时直接返回，避免重新计算
            - 限制缓存大小，自动清理旧数据
            - 顶点与颜色取自同一网格，保证索引一致（P0-2）
        """
        cache_key = cls._make_cache_key(source_obj, source_vcol_layer)

        # 检查缓存。
        # 关键：不仅要比对键，还要用 `is` 校验缓存条目记录的就是同一个对象。
        # 这可以挡住「对象被删除后内存地址被新对象复用」导致的误命中。
        if vc_tool.use_cache:
            entry = cls._cache.get(cache_key)
            if entry is not None and entry.get('obj') is source_obj:
                # 缓存命中：移到末尾表示最近使用
                cls._cache.move_to_end(cache_key)
                cls._hits += 1
                return entry

        try:
            # 获取源物体的求值后网格（含修改器结果）
            depsgraph = bpy.context.evaluated_depsgraph_get()
            source_obj_eval = source_obj.evaluated_get(depsgraph)
            source_mesh_eval = source_obj_eval.to_mesh()

            # to_mesh() 返回 None 时不应调用 to_mesh_clear()
            # （对未成功 to_mesh 的对象调用属于依赖未定义行为）。
            if not source_mesh_eval:
                return None

            # === P0-3 内存泄漏防护 ===
            # 原实现在此处的 to_mesh_clear() 会被外层 except 跳过：
            # _extract_vertex_color_map 内部、或 matrix_world @ vert.co 抛异常时
            # 直接 return None，导致泄漏一个求值网格（20 万顶点 ≈ 12MB）。
            # 批量复制 2000 个目标时任意一个失败即泄漏 12MB，可达 24GB。
            # 这里用 try/finally 保证求值网格必定释放。
            # 注意：fallback 分支读的是 source_obj.data（原始网格），
            # 与求值网格无关，放在 finally 之前执行不受影响。
            try:
                # 获取源顶点位置（世界坐标）
                matrix_world = source_obj.matrix_world
                source_vertices = []
                for vert in source_mesh_eval.vertices:
                    world_co = matrix_world @ vert.co
                    source_vertices.append(world_co)

                # === 阶段 B：法线约束（仅在启用且阈值 > 0 时才提取）===
                # 关闭时不提取，避免为绝大多数用户白付内存与时间。
                # 提取失败返回 None，上层自动退回纯距离最近（原行为）。
                source_normals = None
                if _normal_constraint_enabled(vc_tool):
                    local_normals = _extract_normals(
                        source_mesh_eval, len(source_vertices))
                    if local_normals is not None:
                        # 法线需变换到世界空间（逆转置矩阵）并单位化。
                        # 20 万顶点逐点循环约 380ms，故走 numpy 向量化。
                        # 与目标侧共用 _transform_normals，避免两处逻辑分叉。
                        from .vertex_color_ops import _transform_normals
                        source_normals = _transform_normals(
                            local_normals, matrix_world)

                # === P0-2 关键修复 ===
                # 颜色必须从「与顶点相同的求值网格」中读取，
                # 保证 source_vertex_colors 的键与 source_vertices 的下标同源。
                source_vertex_colors = _extract_vertex_color_map(
                    source_mesh_eval, source_vcol_layer
                )

                # 求值网格上没有颜色数据时，仅在拓扑一致的前提下回退到原始网格
                if not source_vertex_colors:
                    try:
                        original_vertex_count = len(source_obj.data.vertices)
                    except Exception:
                        original_vertex_count = -1
                    if original_vertex_count == len(source_vertices):
                        source_vertex_colors = _extract_vertex_color_map(
                            source_obj.data, source_vcol_layer
                        )
            finally:
                try:
                    source_obj_eval.to_mesh_clear()
                except Exception as clear_exc:
                    log_warning(f"释放源求值网格失败 ({source_obj.name}): {clear_exc}")

            if not source_vertex_colors:
                if vc_tool.use_cache:
                    cls._misses += 1
                return None

            # 构建KDTree - 大网格时强制构建，避免退化为 O(N×M)
            kd = None
            vertex_count = len(source_vertices)
            should_build_kd = vc_tool.use_kdtree or vertex_count > BRUTEFORCE_VERTEX_LIMIT
            if should_build_kd and vertex_count > 0:
                try:
                    kd = kdtree.KDTree(vertex_count)
                    for i, coord in enumerate(source_vertices):
                        kd.insert(coord, i)
                    kd.balance()
                except Exception as e:
                    log_error("构建KDTree时出错", exc=e)
                    kd = None

            # 原生加速产物（仅在启用且可用时构建，避免无谓开销）
            native_tree = None
            points_array = None
            colors_dense = None
            if _native_usable(vc_tool):
                try:
                    native_tree, points_array, colors_dense = _build_native_artifacts(
                        source_vertices, source_vertex_colors
                    )
                except Exception as e:
                    log_warning(f"原生 KDTree 构建失败，本次回退 Python 实现: {e}")
                    native_tree, points_array, colors_dense = None, None, None

            cache_data = {
                # 保存对象引用，供命中时做身份校验（P1-5）。
                # 注意：只用于 `is` 比较，绝不访问其属性，因此对象被删除也安全。
                'obj': source_obj,
                'vertices': source_vertices,
                'vertex_colors': source_vertex_colors,
                'kd': kd,
                # 阶段 B：顶点法线（世界空间，单位化）。可能为 None，
                # 表示未启用或提取失败 —— 消费方必须能处理 None。
                'normals': source_normals,
                # 原生加速相关（可能为 None，表示走 Python 路径）
                'native_tree': native_tree,
                'points_array': points_array,
                'colors_dense': colors_dense,
            }

            # 添加到缓存 - 限制大小
            if vc_tool.use_cache:
                cls._misses += 1
                cls._store(cache_key, cache_data, len(source_vertices), vc_tool)

            return cache_data

        except Exception as e:
            log_error(f"获取源物体数据时出错 ({source_obj.name})", exc=e)
            return None

    @classmethod
    def _store(cls, cache_key, cache_data, vertex_count, vc_tool=None):
        """
        写入缓存并维持容量上限（LRU）。

        P0-4 约束两个维度:
            - 条目数不超过 _max_cache_size
            - **估算字节数**不超过内存预算（原先是顶点数，
              无法真正约束内存：200 万顶点实测≈ 532MB，
              在用户自己的 Blender 进程内足以被 OS 杀掉）

        阶段 B: 字节数按条目**实际内容**估算（含法线数组时计入法线开销），
        而非一律按顶点估——否则法线缓存会让实际占用超出预算而限流失真。

        单个体量超过总预算的物体不缓存，避免一个巨型网格挤掉整个缓存。
        """
        budget_bytes = cls._resolve_budget_bytes(vc_tool)
        # 统一用_has_normals()，绝不用 bool(...):
        # 法线是 numpy 数组，bool(多元素数组) 会抛 ValueError。
        has_normals = _has_normals(cache_data)
        entry_bytes = estimate_cache_entry_bytes(vertex_count,
                                                has_normals=has_normals)

        if entry_bytes > budget_bytes:
            # 体量过大，本次不缓存。
            # 关键: 必须释放已构建的原生 KDTree，否则原生内存无人回收（泄漏）。
            # 调用方仍持有 cache_data 并可正常使用；其析构时会再触发一次 close()，
            # 而 close() 是幂等的（句柄置空后不再重复释放）。
            _release_native(cache_data)
            return

        # 覆盖同键旧条目时先释放其原生资源并扣减占用
        existing = cls._cache.pop(cache_key, None)
        if existing is not None:
            _release_native(existing)
            old_vertices = existing.get('vertices', [])
            cls._cached_bytes -= estimate_cache_entry_bytes(
                len(old_vertices),
                has_normals=_has_normals(existing),
            )
            cls._cached_vertices -= len(old_vertices)

        # 按 LRU 顺序淘汰，直到同时满足条目数与内存预算两个上限
        while cls._cache and (
            len(cls._cache) >= cls._max_cache_size
            or cls._cached_bytes + entry_bytes > budget_bytes
        ):
            lru_key = next(iter(cls._cache))
            evicted = cls._cache.pop(lru_key)
            _release_native(evicted)
            evicted_vertices = len(evicted.get('vertices', []))
            cls._cached_bytes -= estimate_cache_entry_bytes(
                evicted_vertices,
                has_normals=_has_normals(evicted),
            )
            cls._cached_vertices -= evicted_vertices

        cls._cache[cache_key] = cache_data
        cls._cached_bytes += entry_bytes
        cls._cached_vertices += vertex_count

    @classmethod
    def clear_cache(cls):
        """
        清空缓存并执行垃圾回收

        优化:
            - 释放内存（含原生 KDTree）
            - 强制垃圾回收
            - 重置统计信息
        """
        for entry in cls._cache.values():
            _release_native(entry)
        cls._cache.clear()
        cls._cached_bytes = 0
        cls._cached_vertices = 0
        cls._hits = 0
        cls._misses = 0
        gc.collect()
