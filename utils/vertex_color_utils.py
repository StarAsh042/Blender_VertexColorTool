"""
顶点色工具函数模块

提供统一的顶点色检查与操作函数，并集中封装 Blender 3.2+ 新式颜色属性
（mesh.color_attributes）与旧式顶点色层（mesh.vertex_colors）之间的兼容判断，
避免兼容逻辑散落到各个调用点。

审计修复说明:
    - P2-12: 删除 4 个全项目零调用的死函数
      （get_active_vertex_color_layer / has_multiple_colors /
        save_color_attribute_data / restore_color_attribute_data）
    - P2-14: 新增 supports_color_attributes() 统一能力探测
    - P0-2 : backup_color_layer 改用「名称锚定」而非绝对索引恢复激活层。
      原实现用删除残留备份之前的 active_color_index 恢复，而删除层会让
      后续层索引前移，导致激活层错位为备份层自身，进而在 RGBA 恢复时
      把备份拷给自己再删除自己，原始色永久丢失。
      restore_color_layer 同时增加护栏：拒绝以备份层为恢复目标。
"""

import bmesh

from .logging_utils import log_error


# 通道预览备份层名称。
# 使用真实颜色属性层做备份，而非 JSON 字符串：
#   1) 属于网格原生数据，保存 .blend / 崩溃后依然存在，不会永久丢失原始色
#   2) 无需在 Python 侧序列化海量浮点数组，大模型下不会卡顿
PREVIEW_BACKUP_LAYER_NAME = "__vct_preview_backup__"


def supports_color_attributes(mesh):
    """
    统一的新式颜色属性能力探测。

    说明:
        Blender 3.2 起引入 mesh.color_attributes，4.x 中 mesh.vertex_colors 被弃用。
        全项目统一通过本函数判断，避免 hasattr(mesh, "color_attributes") 判断
        散落在十余个调用点（审计报告「可扩展性」项）。
    """
    return hasattr(mesh, "color_attributes")


def has_vertex_colors(obj):
    """
    检查物体是否有顶点色（统一实现，更严谨）
    """
    try:
        if not obj or obj.type != 'MESH':
            return False

        mesh = obj.data

        # 检查传统顶点色层
        if mesh.vertex_colors and len(mesh.vertex_colors) > 0 and len(mesh.vertex_colors[0].data) > 0:
            return True

        # 检查颜色属性
        if supports_color_attributes(mesh):
            for attr in mesh.color_attributes:
                if attr.domain in {'POINT', 'CORNER'} and len(attr.data) > 0:
                    return True

        return False

    except Exception as e:
        print(f"检查顶点色时出错 ({obj.name}): {e}")
        return False


def get_active_vcol_layer(obj):
    """
    获取物体上处于激活状态的顶点色层（颜色属性或传统顶点色层）。
    此函数只获取，不创建。
    """
    if not obj or obj.type != 'MESH':
        return None

    mesh = obj.data

    # 优先检查颜色属性 (Blender 3.2+)
    if supports_color_attributes(mesh) and mesh.color_attributes.active_color:
        return mesh.color_attributes.active_color

    # 其次检查传统顶点色层
    if mesh.vertex_colors and mesh.vertex_colors.active:
        return mesh.vertex_colors.active

    return None


def is_pure_color(r, g, b, a):
    """
    检查颜色是否为纯色（RGB中只有一个通道为1，其他为0）

    Args:
        r, g, b, a: RGBA颜色值

    Returns:
        bool: True 如果是纯色，否则 False
    """
    alpha_ok = abs(a) < 0.00001 or abs(a - 1.0) < 0.001
    r_on = abs(r - 1.0) < 0.00001
    g_on = abs(g - 1.0) < 0.00001
    b_on = abs(b - 1.0) < 0.00001
    r_off = abs(r) < 0.00001
    g_off = abs(g) < 0.00001
    b_off = abs(b) < 0.00001
    rgb_ok = (r_on and g_off and b_off) or (r_off and g_on and b_off) or (r_off and g_off and b_on)
    return alpha_ok and rgb_ok


def is_impure_vertex_color(obj):
    """
    检查物体顶点色是否不纯（最终修复版）。
    不纯定义为以下任一情况：
    1. 物体上存在多种颜色。
    2. 物体上存在任何非纯色（纯红、纯绿、纯蓝之外的颜色）。
    """
    if not obj or obj.type != 'MESH':
        return False

    vcol_layer = get_active_vcol_layer(obj)

    if not vcol_layer or len(vcol_layer.data) == 0:
        return False

    colors = set()
    # 遍历所有颜色数据
    for color_data in vcol_layer.data:
        r, g, b, a = color_data.color

        # 条件2: 检查颜色本身是否为“不纯”的混合色
        if not is_pure_color(r, g, b, a):
            return True  # 发现一个混合色，立即判定为不纯

        # 条件1: 检查是否有多种颜色
        # 使用 round 来避免浮点数精度问题
        rounded_color = (round(r, 4), round(g, 4), round(b, 4), round(a, 4))
        colors.add(rounded_color)
        if len(colors) > 1:
            return True  # 发现了第二种颜色，立即判定为不纯

    # 如果循环结束，说明所有颜色都是同一种纯色
    return False


def get_vertex_color_info(obj):
    """
    获取物体的顶点色层信息

    Args:
        obj: Blender物体对象

    Returns:
        tuple: (顶点色层名称, 顶点色域) 或 (None, None)
    """
    if obj.type != 'MESH':
        return None, None

    mesh = obj.data

    # 检查传统顶点色层
    if mesh.vertex_colors and len(mesh.vertex_colors) > 0:
        if mesh.vertex_colors.active:
            return mesh.vertex_colors.active.name, "CORNER"
        return mesh.vertex_colors[0].name, "CORNER"

    # 检查颜色属性
    if supports_color_attributes(mesh) and len(mesh.color_attributes) > 0:
        attr = mesh.color_attributes[0]
        return attr.name, attr.domain

    return None, None


def get_average_vertex_color_from_selection(context):
    """
    从选中的物体或顶点获取平均颜色（消除代码重复的统一实现）

    功能:
        - 支持物体模式和编辑模式
        - 多物体选择
        - 计算选中顶点的平均颜色
        - 计算选中物体的平均颜色

    Args:
        context: Blender上下文

    Returns:
        tuple: (r, g, b, a) 平均颜色值或 None

    使用场景:
        - VERTEXCOLOR_OT_ModifyVertexColor.get_vertex_color_from_selection
        - 其他需要从选择获取颜色的操作
    """
    selected_objects = context.selected_objects

    if not selected_objects:
        return None

    # 检查是否有物体处于编辑模式
    has_edit_mode = any(obj.mode == 'EDIT' for obj in selected_objects)

    if has_edit_mode:
        return _get_average_color_from_edit_mode(selected_objects)
    else:
        return _get_average_color_from_object_mode(selected_objects)


def _get_average_color_from_edit_mode(selected_objects):
    """
    从编辑模式的选中顶点获取平均颜色（内部辅助函数）

    修复说明（审计报告）:
        原实现只支持旧式顶点色层且依赖 bmesh 颜色层查找，
        这里改为统一读取激活层，并按 domain 区分 POINT / CORNER 索引方式，
        对 POINT 域（新式颜色属性）也能正确取值。
    """
    total_r = 0.0
    total_g = 0.0
    total_b = 0.0
    total_a = 0.0
    count = 0

    for obj in selected_objects:
        if obj.type != 'MESH' or obj.mode != 'EDIT':
            continue

        mesh = obj.data
        vcol_layer = get_active_vcol_layer(obj)

        if not vcol_layer or len(vcol_layer.data) == 0:
            continue

        # 使用bmesh获取选中的顶点索引
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()

        selected_verts = {v.index for v in bm.verts if v.select}
        if not selected_verts:
            continue

        data = vcol_layer.data
        domain = getattr(vcol_layer, 'domain', 'CORNER')

        if domain == 'POINT':
            # POINT 域：数据按顶点索引排列
            for vert_index in selected_verts:
                if vert_index < len(data):
                    color = data[vert_index].color
                    total_r += color[0]
                    total_g += color[1]
                    total_b += color[2]
                    total_a += color[3]
                    count += 1
        else:
            # CORNER 域：数据按 loop 索引排列，需经 mesh.loops 映射到顶点
            for loop_index, loop in enumerate(mesh.loops):
                if loop.vertex_index in selected_verts and loop_index < len(data):
                    color = data[loop_index].color
                    total_r += color[0]
                    total_g += color[1]
                    total_b += color[2]
                    total_a += color[3]
                    count += 1

    if count > 0:
        return (total_r / count, total_g / count, total_b / count, total_a / count)

    return None


def _get_average_color_from_object_mode(selected_objects):
    """从物体模式的选中物体获取平均颜色（内部辅助函数）"""
    total_r = 0.0
    total_g = 0.0
    total_b = 0.0
    total_a = 0.0
    count = 0

    for obj in selected_objects:
        if obj.type != 'MESH':
            continue

        vcol_layer = get_active_vcol_layer(obj)

        if not vcol_layer or len(vcol_layer.data) == 0:
            continue

        # 计算该物体的平均颜色
        obj_r = 0.0
        obj_g = 0.0
        obj_b = 0.0
        obj_a = 0.0
        obj_count = 0

        for color_data in vcol_layer.data:
            obj_r += color_data.color[0]
            obj_g += color_data.color[1]
            obj_b += color_data.color[2]
            obj_a += color_data.color[3]
            obj_count += 1

        if obj_count > 0:
            total_r += obj_r / obj_count
            total_g += obj_g / obj_count
            total_b += obj_b / obj_count
            total_a += obj_a / obj_count
            count += 1

    if count > 0:
        return (total_r / count, total_g / count, total_b / count, total_a / count)

    return None


def get_or_create_active_vcol_layer(obj):
    """
    获取或创建物体上处于激活状态的顶点色层。
    - 如果有激活的，直接返回。
    - 如果没有激活的，但有存在的，则激活第一个并返回。
    - 如果一个都没有，则创建一个名为'Color'的新层并返回。
    """
    if not obj or obj.type != 'MESH':
        return None

    mesh = obj.data

    # 1. 检查是否有激活的颜色属性 (Blender 3.2+)
    if supports_color_attributes(mesh) and mesh.color_attributes.active_color:
        return mesh.color_attributes.active_color

    # 2. 检查是否有激活的传统顶点色层
    if mesh.vertex_colors and mesh.vertex_colors.active:
        return mesh.vertex_colors.active

    # 3. 如果没有激活的，尝试激活一个已有的
    if supports_color_attributes(mesh) and len(mesh.color_attributes) > 0:
        mesh.color_attributes.active_index = 0
        if mesh.color_attributes.active_color:
            return mesh.color_attributes.active_color

    if len(mesh.vertex_colors) > 0:
        mesh.vertex_colors.active_index = 0
        if mesh.vertex_colors.active:
            return mesh.vertex_colors.active

    # 4. 如果一个都没有，创建一个新的颜色属性
    try:
        if supports_color_attributes(mesh):
            # 优先创建新式颜色属性
            new_layer = mesh.color_attributes.new(name="Color", type='FLOAT_COLOR', domain='POINT')
            mesh.color_attributes.active_index = len(mesh.color_attributes) - 1
            return new_layer
        else:
            # 兼容旧版
            new_layer = mesh.vertex_colors.new(name="Color")
            mesh.vertex_colors.active_index = len(mesh.vertex_colors) - 1
            return new_layer
    except RuntimeError as e:
        print(f"创建新的顶点色层时出错: {e}. 可能正处于不支持此操作的模式下。")
        return None


def get_vcol_layer_by_name(obj, name):
    """
    根据名称获取物体的顶点色层（新旧两种都支持）。
    """
    if not obj or obj.type != 'MESH' or not name:
        return None

    mesh = obj.data

    # 优先查找新式颜色属性
    if supports_color_attributes(mesh):
        if name in mesh.color_attributes:
            return mesh.color_attributes[name]

    # 其次查找旧式顶点色层
    if name in mesh.vertex_colors:
        return mesh.vertex_colors[name]

    return None


def get_vcol_layer(obj, name=None, create_if_missing=False):
    """
    顶点色层获取的终极核心函数。

    Args:
        obj (bpy.types.Object): 目标物体。
        name (str, optional): 指定的层名称。 Defaults to None.
        create_if_missing (bool, optional): 如果找不到是否创建（仅当指定name时有效）。 Defaults to False.

    Returns:
        bpy.types.VertexColorLayer or bpy.types.Attribute: 顶点色层对象或None。
    """
    if not obj or obj.type != 'MESH':
        return None

    mesh = obj.data

    # --- 1. 如果指定了名称 ---
    if name:
        # 优先按名称查找
        layer = get_vcol_layer_by_name(obj, name)
        if layer:
            return layer

        # 如果找不到，且被告知要创建
        if create_if_missing:
            try:
                if supports_color_attributes(mesh):
                    new_layer = mesh.color_attributes.new(name=name, type='FLOAT_COLOR', domain='POINT')
                    mesh.color_attributes.active_index = len(mesh.color_attributes) - 1
                    return new_layer
                else:
                    new_layer = mesh.vertex_colors.new(name=name)
                    mesh.vertex_colors.active_index = len(mesh.vertex_colors) - 1
                    return new_layer
            except RuntimeError as e:
                print(f"创建名为 '{name}' 的顶点色层时出错: {e}")
                return None  # 创建失败
        else:
            # 如果不创建，则返回None，让上层逻辑决定如何处理（比如回退到激活层）
            return None

    # --- 2. 如果未指定名称，则获取或创建激活层 ---
    return get_or_create_active_vcol_layer(obj)


# =============================================================================
# 通道预览备份层（P0-3 / P0-2 修复）
# =============================================================================

def has_preview_backup(mesh):
    """检查网格上是否存在通道预览的备份颜色层"""
    return supports_color_attributes(mesh) and PREVIEW_BACKUP_LAYER_NAME in mesh.color_attributes


def _find_color_attribute_index(attributes, name):
    """
    在颜色属性集合中按名称查找索引，找不到返回 -1。

    `bpy_prop_collection.find()` 在不同 Blender 版本上行为不一致
    （部分版本对颜色属性集合不抛异常但恒返回 -1），
    因此这里**以遍历为准**、find() 仅作为快速路径：
    只有 find() 返回非负索引时才采信，否则一律回退到遍历，
    确保两条路径都不会给出错误答案。

    Args:
        attributes: mesh.color_attributes
        name: 颜色层名称

    Returns:
        int: 索引，未找到返回 -1
    """
    if not name:
        return -1

    # 快速路径：find() 可用且明确命中时直接采信
    try:
        index = attributes.find(name)
        if index is not None and index >= 0:
            return index
    except Exception:
        pass

    # 权威路径：遍历（find() 不可用或恒返回 -1 时的兜底）
    try:
        for index, attribute in enumerate(attributes):
            if attribute.name == name:
                return index
    except Exception:
        pass

    return -1


def _activate_color_attribute_by_name(mesh, name):
    """
    按名称激活颜色层（而非按绝对索引）。

    绝对索引在「删除层」后会错位——Blender 的color_attributes 删除元素时
    后续元素索引前移。P0-2 的数据丢失根因正是用旧索引恢复激活层，
    结果激活到了备份层自身。这里统一以名称为锚点，天然免疫索引漂移。

    Args:
        mesh: 网格数据
        name: 要激活的颜色层名称

    Returns:
        bool: 是否成功激活
    """
    if not name:
        return False

    index = _find_color_attribute_index(mesh.color_attributes, name)
    if index < 0:
        return False

    try:
        mesh.color_attributes.active_color_index = index
        return True
    except Exception:
        return False


def backup_color_layer(mesh, layer):
    """
    把 layer 的颜色复制到一个备份颜色属性层中（用于通道预览的安全恢复）。

    与旧的 JSON 字符串方案相比:
        - 备份是网格原生数据，保存 .blend 或崩溃后依然存在
        - 不需要在 Python 侧序列化海量浮点数组，大模型下不卡顿

    注意:
        mesh.color_attributes.new() 会把新层设为激活层，
        这里在创建后按「名称」把激活层恢复为原来的 layer，
        否则下一次 get_or_create_active_vcol_layer 会错误地返回备份层。

    P0-2 安全修复:
        原实现记录 `active_color_index` 绝对索引，在「先删除残留备份、
        再新建备份」之后用该旧索引恢复，索引已因删除而前移，导致激活的
        变成备份层自身。RGBA 恢复分支随后会把备份拷给自己再删除自己，
        使真实颜色层只剩灰色且原始色随备份层一起被删除（不可逆）。
        现改为记录并按**名称**恢复，免疫索引漂移。

    Args:
        mesh: 网格数据
        layer: 需要备份的颜色层

    Returns:
        备份层对象，失败返回 None
    """
    if not supports_color_attributes(mesh):
        return None

    # 记录原始激活层的「名称」（P0-2：不用绝对索引，删除层会导致索引漂移）
    original_name = None
    try:
        active = mesh.color_attributes.active_color
        if active is not None:
            original_name = active.name
    except Exception:
        original_name = None

    try:
        # 先清理可能残留的旧备份（例如上次预览异常退出）
        if PREVIEW_BACKUP_LAYER_NAME in mesh.color_attributes:
            mesh.color_attributes.remove(mesh.color_attributes[PREVIEW_BACKUP_LAYER_NAME])

        backup = mesh.color_attributes.new(
            name=PREVIEW_BACKUP_LAYER_NAME,
            type=getattr(layer, 'data_type', 'FLOAT_COLOR'),
            domain=getattr(layer, 'domain', 'CORNER'),
        )
    except Exception as e:
        print(f"创建预览备份层失败: {e}")
        return None

    count = min(len(backup.data), len(layer.data))
    for i in range(count):
        backup.data[i].color = layer.data[i].color

    # 按名称恢复原本的激活层，避免备份层抢占激活状态
    # 若原激活层名恰好是备份层（异常状态），则退而激活 layer 本身，
    # 绝不能让备份层成为激活层。
    restore_name = original_name
    if not restore_name or restore_name == PREVIEW_BACKUP_LAYER_NAME:
        restore_name = getattr(layer, 'name', None)
    if restore_name and restore_name != PREVIEW_BACKUP_LAYER_NAME:
        _activate_color_attribute_by_name(mesh, restore_name)

    return backup


def restore_color_layer(mesh, layer):
    """
    从备份层恢复颜色到 layer，并删除备份层。

    P0-2 第二道防线:
        若目标 layer 本身就是备份层，则本函数会把备份拷给自己再删除自己，
        结果真实颜色层里只剩预览灰度、原始色随备份层一起消失（不可逆）。
        因此这里直接拒绝该调用并返回 False。

    Returns:
        bool: 是否成功恢复
    """
    if not has_preview_backup(mesh):
        return False

    if layer is None:
        return False

    # P0-2 安全护栏：绝不把备份层当作恢复目标
    if getattr(layer, 'name', None) == PREVIEW_BACKUP_LAYER_NAME:
        log_error(
            "拒绝从备份层恢复到备份层（会删除原始色）。"
            "请点「RGBA」恢复，或检查该物体的激活颜色层是否被手动改动。"
        )
        return False

    backup = mesh.color_attributes[PREVIEW_BACKUP_LAYER_NAME]
    count = min(len(backup.data), len(layer.data))
    for i in range(count):
        layer.data[i].color = backup.data[i].color

    mesh.color_attributes.remove(backup)

    # 删除备份层后重新激活被恢复的层，保持工具行为可预期
    _activate_color_attribute_by_name(mesh, getattr(layer, 'name', None))

    return True
