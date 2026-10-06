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
"""

import bpy
from mathutils import kdtree
import gc
from collections import OrderedDict

from ..utils.logging_utils import log_error


# 超过该顶点数后，即便用户关闭了 KDTree 也会强制构建，
# 防止退化为 O(N×M) 的纯 Python 暴力搜索（5万×5万 ≈ 25 亿次距离计算）。
BRUTEFORCE_VERTEX_LIMIT = 20000


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
    _max_cache_size = 50  # 最大缓存条目数
    _max_cached_vertices = 2000000  # 缓存内顶点总数上限（约束内存占用，QA 复核建议）
    _cached_vertices = 0  # 当前缓存内顶点总数
    _hits = 0  # 缓存命中次数
    _misses = 0  # 缓存未命中次数

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
    def get_cache_stats(cls):
        """
        获取缓存统计信息

        Returns:
            dict: 包含命中率、命中次数、未命中次数等统计信息
        """
        total = cls._hits + cls._misses
        hit_rate = (cls._hits / total * 100) if total > 0 else 0
        return {
            'hits': cls._hits,
            'misses': cls._misses,
            'hit_rate': hit_rate,
            'cache_size': len(cls._cache),
            'max_size': cls._max_cache_size,
            'cached_vertices': cls._cached_vertices,
            'max_cached_vertices': cls._max_cached_vertices,
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

            if not source_mesh_eval:
                source_obj_eval.to_mesh_clear()
                return None

            # 获取源顶点位置（世界坐标）
            matrix_world = source_obj.matrix_world
            source_vertices = []
            for vert in source_mesh_eval.vertices:
                world_co = matrix_world @ vert.co
                source_vertices.append(world_co)

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

            source_obj_eval.to_mesh_clear()

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

            cache_data = {
                # 保存对象引用，供命中时做身份校验（P1-5）。
                # 注意：只用于 `is` 比较，绝不访问其属性，因此对象被删除也安全。
                'obj': source_obj,
                'vertices': source_vertices,
                'vertex_colors': source_vertex_colors,
                'kd': kd
            }

            # 添加到缓存 - 限制大小
            if vc_tool.use_cache:
                cls._misses += 1
                cls._store(cache_key, cache_data, len(source_vertices))

            return cache_data

        except Exception as e:
            log_error(f"获取源物体数据时出错 ({source_obj.name})", exc=e)
            return None

    @classmethod
    def _store(cls, cache_key, cache_data, vertex_count):
        """
        写入缓存并维持容量上限（LRU）。

        约束两个维度（QA 复核建议）:
            - 条目数不超过 _max_cache_size
            - 缓存内顶点总数不超过 _max_cached_vertices（约束内存占用，
              因为缓存条目持有对象引用与顶点列表，仅按条目数限制不够）

        单个体量超过总上限的物体不缓存，避免一个巨型网格挤掉整个缓存。
        """
        if vertex_count > cls._max_cached_vertices:
            return

        # 覆盖同键旧条目时先扣减其占用
        existing = cls._cache.pop(cache_key, None)
        if existing is not None:
            cls._cached_vertices -= len(existing.get('vertices', []))

        # 按 LRU 顺序淘汰，直到满足两个上限
        while cls._cache and (
            len(cls._cache) >= cls._max_cache_size
            or cls._cached_vertices + vertex_count > cls._max_cached_vertices
        ):
            lru_key = next(iter(cls._cache))
            evicted = cls._cache.pop(lru_key)
            cls._cached_vertices -= len(evicted.get('vertices', []))

        cls._cache[cache_key] = cache_data
        cls._cached_vertices += vertex_count

    @classmethod
    def clear_cache(cls):
        """
        清空缓存并执行垃圾回收

        优化:
            - 释放内存
            - 强制垃圾回收
            - 重置统计信息
        """
        cls._cache.clear()
        cls._cached_vertices = 0
        cls._hits = 0
        cls._misses = 0
        gc.collect()
