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
"""

import bpy
from mathutils import kdtree

from .cache import VertexColorCache, BRUTEFORCE_VERTEX_LIMIT  # noqa: F401  (常量对外暴露)
from ..utils.vertex_color_utils import get_vcol_layer, get_active_vcol_layer
from ..utils.logging_utils import log_warning, log_error


# 暴力搜索的运算量上限（源顶点数 × 目标顶点数）。
# 超过后自动启用 KDTree，防止 Blender 长时间无响应。
BRUTEFORCE_OP_LIMIT = 20000000


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


def _copy_colors_to_attribute_kdtree(vcol_layer, target_vertices, target_mesh, vertex_to_loops, source_vertex_colors, kd):
    """使用KDTree将颜色复制到颜色属性"""
    for vert_idx, target_pos in enumerate(target_vertices):
        if vert_idx >= len(target_mesh.vertices):
            continue

        nearest = kd.find(target_pos)
        if not nearest:
            continue

        nearest_vert_idx = nearest[1]
        color = source_vertex_colors.get(nearest_vert_idx, (1.0, 1.0, 1.0, 1.0))

        if vcol_layer.domain == 'POINT' and vert_idx < len(vcol_layer.data):
            vcol_layer.data[vert_idx].color = (color[0], color[1], color[2], color[3] if len(color) > 3 else 1.0)
        elif vcol_layer.domain == 'CORNER' and vert_idx in vertex_to_loops:
            for loop_idx in vertex_to_loops[vert_idx]:
                if loop_idx < len(vcol_layer.data):
                    vcol_layer.data[loop_idx].color = (color[0], color[1], color[2], color[3] if len(color) > 3 else 1.0)


def _copy_colors_to_attribute_bruteforce(vcol_layer, target_vertices, target_mesh, vertex_to_loops, source_vertices, source_vertex_colors):
    """使用暴力搜索将颜色复制到颜色属性"""
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

        if vcol_layer.domain == 'POINT' and vert_idx < len(vcol_layer.data):
            vcol_layer.data[vert_idx].color = (nearest_color[0], nearest_color[1], nearest_color[2], nearest_color[3] if len(nearest_color) > 3 else 1.0)
        elif vcol_layer.domain == 'CORNER' and vert_idx in vertex_to_loops:
            for loop_idx in vertex_to_loops[vert_idx]:
                if loop_idx < len(vcol_layer.data):
                    vcol_layer.data[loop_idx].color = (nearest_color[0], nearest_color[1], nearest_color[2], nearest_color[3] if len(nearest_color) > 3 else 1.0)


def _copy_colors_to_vcol_kdtree(vcol_layer, target_vertices, target_mesh, vertex_to_loops, source_vertex_colors, kd):
    """使用KDTree将颜色复制到传统顶点色层"""
    for vert_idx, target_pos in enumerate(target_vertices):
        if vert_idx >= len(target_mesh.vertices):
            continue

        nearest = kd.find(target_pos)
        if not nearest:
            continue

        nearest_vert_idx = nearest[1]
        color = source_vertex_colors.get(nearest_vert_idx, (1.0, 1.0, 1.0, 1.0))

        if vert_idx in vertex_to_loops:
            for loop_idx in vertex_to_loops[vert_idx]:
                if loop_idx < len(vcol_layer.data):
                    vcol_layer.data[loop_idx].color = (color[0], color[1], color[2], color[3] if len(color) > 3 else 1.0)


def _copy_colors_to_vcol_bruteforce(vcol_layer, target_vertices, target_mesh, source_vertices, source_vertex_colors):
    """使用暴力搜索将颜色复制到传统顶点色层"""
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

            if loop_idx < len(vcol_layer.data):
                vcol_layer.data[loop_idx].color = (nearest_color[0], nearest_color[1], nearest_color[2], nearest_color[3] if len(nearest_color) > 3 else 1.0)


def copy_vertex_colors_between_objects(source_obj: bpy.types.Object, target_obj: bpy.types.Object, vc_tool=None) -> bool:
    """
    复制顶点色的核心函数（V3版，支持指定层名）
    """
    try:
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
                log_warning(f"源物体 {source_obj.name} 既没有名为 '{specified_name}' 的层，也没有激活层。")
                return False

        source_data = VertexColorCache.get_source_data(source_obj, source_layer.name, vc_tool)
        if not source_data:
            log_warning(f"无法从源物体 {source_obj.name} 的层 '{source_layer.name}' 中获取顶点色数据。")
            return False

        # --- 处理目标物体 ---
        target_layer = get_vcol_layer(target_obj, name=specified_name, create_if_missing=True)
        if not target_layer:
            log_warning(f"无法为目标物体 '{target_obj.name}' 获取或创建名为 '{specified_name or 'Color'}' 的顶点色层。")
            return False

        depsgraph = bpy.context.evaluated_depsgraph_get()
        target_obj_eval = target_obj.evaluated_get(depsgraph)
        target_mesh_eval = target_obj_eval.to_mesh()
        if not target_mesh_eval:
            target_obj_eval.to_mesh_clear()
            return False

        matrix_world = target_obj.matrix_world
        target_vertices = [matrix_world @ v.co for v in target_mesh_eval.vertices]

        target_mesh = target_obj.data
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

        if is_attribute:
            if use_kdtree:
                _copy_colors_to_attribute_kdtree(target_layer, target_vertices, target_mesh, vertex_to_loops, source_data['vertex_colors'], kd)
            else:
                _copy_colors_to_attribute_bruteforce(target_layer, target_vertices, target_mesh, vertex_to_loops, source_data['vertices'], source_data['vertex_colors'])
        else:
            if use_kdtree:
                _copy_colors_to_vcol_kdtree(target_layer, target_vertices, target_mesh, vertex_to_loops, source_data['vertex_colors'], kd)
            else:
                _copy_colors_to_vcol_bruteforce(target_layer, target_vertices, target_mesh, source_data['vertices'], source_data['vertex_colors'])

        target_obj_eval.to_mesh_clear()
        target_mesh.update()
        return True

    except Exception as e:
        log_error(f"复制顶点色时出错 ({target_obj.name})", exc=e)
        return False


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
