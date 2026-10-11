"""
编辑操作模块

包含顶点色清除、填充、修改和通道预览功能。

审计修复说明:
    - P0-3 : 通道预览的原始色不再序列化为 JSON 字符串存进 Scene 属性，
      改为在网格上创建真实备份颜色层。原方案在 json 解析失败时静默清空，
      且预览状态下保存 .blend / 崩溃会导致原始顶点色永久丢失。
    - P1-9 : 「清除顶点色」新增二次确认对话框，避免误点导致全场景颜色被删。
    - P2-12: 移除未使用的 `bmesh` / `json` 导入。
    - P2-13: 三个高度雷同的颜色算子（填充 / 应用 / 修改）抽出共用执行逻辑。
    - P2-18: 统一错误上报。
"""

import bpy

from ..utils.vertex_color_utils import (
    get_or_create_active_vcol_layer,
    has_preview_backup,
    backup_color_layer,
    restore_color_layer,
    PREVIEW_BACKUP_LAYER_NAME,
)
from ..utils.logging_utils import log_warning, log_error, report_error
from ..core.vertex_color_ops import fill_vertex_colors


def _apply_color_and_report(operator, context, verb):
    """
    三个颜色算子共用的执行逻辑（P2-13 去重）。

    Args:
        operator: 算子实例（用于 report）
        context: Blender context
        verb: 文案动词，如 "填充" / "修改"

    Returns:
        set: Blender 操作结果
    """
    selected_objects = context.selected_objects

    if not selected_objects:
        operator.report({'ERROR'}, "请先选择要修改顶点色的物体")
        return {'CANCELLED'}

    vc_tool = context.scene.vertex_color_tool

    # 获取颜色值
    color = vc_tool.picked_color

    # 使用统一的填充函数
    filled_count, selected_vertex_count = fill_vertex_colors(
        context, selected_objects, color, vc_tool
    )

    color_desc = f"R={color[0]:.2f}, G={color[1]:.2f}, B={color[2]:.2f}, A={color[3]:.2f}"
    has_edit_mode = any(obj.mode == 'EDIT' for obj in selected_objects)

    if has_edit_mode and selected_vertex_count > 0:
        vc_tool.last_operation = f"已{verb} {selected_vertex_count} 个选中顶点的顶点色 ({color_desc})"
        operator.report({'INFO'}, f"已{verb} {selected_vertex_count} 个选中顶点的顶点色")
    elif has_edit_mode:
        vc_tool.last_operation = f"已{verb} {filled_count} 个物体的顶点色 ({color_desc})"
        operator.report({'INFO'}, f"已{verb} {filled_count} 个物体的顶点色（编辑模式下未检测到选中）")
    else:
        vc_tool.last_operation = f"已{verb} {filled_count} 个物体的顶点色 ({color_desc})"
        operator.report({'INFO'}, f"已{verb} {filled_count} 个物体的顶点色")

    return {'FINISHED'}


class VERTEXCOLOR_OT_ClearVertexColors(bpy.types.Operator):
    """
    清除选中物体的顶点色

    功能:
        - 删除所有顶点色层
        - 删除所有颜色属性
        - 重置物体的顶点色状态
        - 强制刷新视口显示

    安全性（P1-9）:
        本操作会删除物体上的「全部」颜色层，且作用于所有选中物体。
        已新增二次确认对话框，避免误点后全场景颜色被清除。
    """
    bl_idname = "vertexcolor.clear_vertex_colors"
    bl_label = "清除顶点色"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        """危险操作二次确认（P1-9）"""
        selected_count = len([o for o in context.selected_objects if o.type == 'MESH'])
        if selected_count == 0:
            self.report({'ERROR'}, "请先选择要清除顶点色的物体")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        """
        清除选中物体的顶点色

        Returns:
            set: Blender操作结果

        错误处理:
            - 验证选择
            - 安全删除顶点色层
            - 强制更新网格数据
        """
        try:
            selected_objects = context.selected_objects

            if not selected_objects:
                self.report({'ERROR'}, "请先选择要清除顶点色的物体")
                return {'CANCELLED'}

            cleared_count = 0

            for obj in selected_objects:
                try:
                    if obj.type != 'MESH':
                        continue

                    mesh = obj.data

                    # 清除传统顶点色层
                    if mesh.vertex_colors and len(mesh.vertex_colors) > 0:
                        # 删除所有顶点色层
                        while len(mesh.vertex_colors) > 0:
                            try:
                                mesh.vertex_colors.remove(mesh.vertex_colors[0])
                            except Exception as e:
                                log_error("删除顶点色层时出错", exc=e)
                                break
                        cleared_count += 1

                    # 清除颜色属性
                    if hasattr(mesh, "color_attributes"):
                        # 删除所有颜色属性
                        while len(mesh.color_attributes) > 0:
                            try:
                                mesh.color_attributes.remove(mesh.color_attributes[0])
                            except Exception as e:
                                log_error("删除颜色属性时出错", exc=e)
                                break

                    # 强制更新网格数据
                    mesh.update()

                except Exception as e:
                    log_error(f"清除物体 {obj.name} 顶点色时出错", exc=e)
                    continue

            context.scene.vertex_color_tool.last_operation = f"已清除 {cleared_count} 个物体的顶点色"
            self.report({'INFO'}, f"已清除 {cleared_count} 个物体的顶点色")

            return {'FINISHED'}

        except Exception as e:
            report_error(self, context, f"清除顶点色时出错: {str(e)}", exc=e)
            return {'CANCELLED'}


class VERTEXCOLOR_OT_FillVertexColors(bpy.types.Operator):
    """
    填充选中物体的顶点色

    功能:
        - 为选中的物体创建或使用现有的顶点色层
        - 使用颜色选择器填充任意RGBA颜色
        - 支持透明度通道调节
        - 编辑模式下支持点/线/面选择填充
    """
    bl_idname = "vertexcolor.fill_vertex_colors"
    bl_label = "填充顶点色"
    bl_options = {'REGISTER', 'UNDO'}

    def draw(self, context):
        """绘制操作符的面板UI"""
        layout = self.layout
        col = layout.column()
        vc_tool = context.scene.vertex_color_tool

        col.label(text="选择颜色:", icon='COLOR')
        col.prop(vc_tool, "picked_color", text="")

    def execute(self, context):
        """
        填充选中物体的顶点色

        Returns:
            set: Blender操作结果
        """
        try:
            return _apply_color_and_report(self, context, "填充")
        except Exception as e:
            report_error(self, context, f"填充顶点色时出错: {str(e)}", exc=e)
            return {'CANCELLED'}


class VERTEXCOLOR_OT_ApplySelectedColor(bpy.types.Operator):
    """
    应用已选择的颜色到选中的物体

    功能:
        - 直接使用场景中已选择的颜色
        - 将颜色应用到选中的物体或顶点
        - 不弹出对话框，直接使用面板中选择的颜色
    """
    bl_idname = "vertexcolor.apply_selected_color"
    bl_label = "应用选择的颜色"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        """
        执行应用颜色操作

        Returns:
            set: Blender操作结果
        """
        try:
            return _apply_color_and_report(self, context, "修改")
        except Exception as e:
            report_error(self, context, f"应用颜色时出错: {str(e)}", exc=e)
            return {'CANCELLED'}


class VERTEXCOLOR_OT_ModifyVertexColor(bpy.types.Operator):
    """
    顶点颜色修改工具

    功能:
        - 提供完整的颜色选择器界面
        - 包含调色盘、颜色滑块和透明度调节
        - 可以从选中物体或顶点获取颜色
        - 将选择的颜色填充到选中的物体或顶点
    """
    bl_idname = "vertexcolor.modify_vertex_color"
    bl_label = "顶点颜色修改"
    bl_options = {'REGISTER', 'UNDO'}

    picked_color: bpy.props.FloatVectorProperty(
        name="选择颜色",
        subtype='COLOR',
        size=4,
        min=0.0,
        max=1.0,
        default=(1.0, 1.0, 1.0, 1.0),
        description="选择RGBA颜色值"
    )

    def draw(self, context):
        """绘制颜色修改UI"""
        layout = self.layout
        col = layout.column()

        col.label(text="选择颜色:", icon='COLOR')
        col.prop(self, "picked_color", text="")

    def get_vertex_color_from_selection(self, context):
        """
        从选中物体或顶点获取颜色

        Returns:
            tuple: (r, g, b, a) 颜色值或 None
        """
        from ..utils.vertex_color_utils import get_average_vertex_color_from_selection

        try:
            return get_average_vertex_color_from_selection(context)
        except Exception as e:
            log_error("获取顶点色时出错", exc=e)
            return None

    def execute(self, context):
        """
        执行顶点颜色修改操作

        Returns:
            set: Blender操作结果
        """
        try:
            selected_objects = context.selected_objects

            if not selected_objects:
                self.report({'ERROR'}, "请先选择要修改顶点色的物体")
                return {'CANCELLED'}

            vc_tool = context.scene.vertex_color_tool

            # 保存颜色到场景属性
            vc_tool.picked_color = self.picked_color

            return _apply_color_and_report(self, context, "修改")

        except Exception as e:
            report_error(self, context, f"顶点颜色修改时出错: {str(e)}", exc=e)
            return {'CANCELLED'}

    def invoke(self, context, event):
        """
        调用操作符时自动获取选中顶点的颜色

        取不到颜色时**取消并报错**，不打开对话框:
            取色失败意味着「没有可读的颜色」，此时若照常打开对话框，
            用户看到的是算子属性的默认值——白色 swatch。
            白色会被误读成「读出来就是白色」，确认后就把白色刷满模型。
            错误信息导致的错误操作比直接报错更糟，因此这里必须拒绝。

        与同模块 clear_vertex_colors.invoke() 保持同一套标准:
            两者都是「先读取、再弹窗」型算子，前置条件不满足时都应拒绝。

        Returns:
            set: Blender操作结果
        """
        # 尝试从选中物体或顶点获取颜色。
        # 注意用 `is None` 而非真值判断: 读到的颜色可能是全 0 的
        # (0,0,0,0)，它是合法颜色而非「没读到」。
        picked_color = self.get_vertex_color_from_selection(context)

        if picked_color is None:
            self.report({'ERROR'}, "请先选择要读取颜色的顶点或物体")
            return {'CANCELLED'}

        self.picked_color = picked_color
        vc_tool = context.scene.vertex_color_tool
        vc_tool.picked_color = picked_color

        color_desc = f"R={picked_color[0]:.2f}, G={picked_color[1]:.2f}, B={picked_color[2]:.2f}, A={picked_color[3]:.2f}"
        vc_tool.last_operation = f"已获取选中颜色 ({color_desc})"
        self.report({'INFO'}, f"已获取选中颜色: {color_desc}")

        return context.window_manager.invoke_props_dialog(self)


class VERTEXCOLOR_OT_PreviewChannel(bpy.types.Operator):
    """
    快速预览顶点色通道（重构版）

    功能:
        - 支持传统顶点色层和新式颜色属性
        - 自动创建颜色层（如果不存在）
        - 快速切换显示R/G/B/A通道的黑白预览
        - 一键恢复完整颜色显示

    安全性（P0-3 修复）:
        原始颜色现在通过「网格上的真实备份颜色层」保存，
        而不是序列化成 JSON 字符串塞进 Scene 属性。
        因此即使预览状态下保存 .blend 或 Blender 崩溃，
        原始颜色依然完整保留，可随时恢复，不会再永久丢失。
        备份层名为 __vct_preview_backup__，恢复后会自动删除。
    """
    bl_idname = "vertexcolor.preview_channel"
    bl_label = "预览通道"
    bl_options = {'REGISTER', 'UNDO'}

    channel: bpy.props.EnumProperty(
        name="通道",
        items=[
            ('RGBA', "完整", "显示完整RGBA颜色"),
            ('R', "红色", "仅显示红色通道（灰度）"),
            ('G', "绿色", "仅显示绿色通道（灰度）"),
            ('B', "蓝色", "仅显示蓝色通道（灰度）"),
            ('A', "Alpha", "仅显示Alpha通道（灰度）"),
        ],
        default='RGBA',
        description="选择要预览的通道"
    )

    _CHANNEL_INDEX = {'R': 0, 'G': 1, 'B': 2, 'A': 3}
    _CHANNEL_NAME = {'R': '红色', 'G': '绿色', 'B': '蓝色', 'A': 'Alpha'}

    def execute(self, context):
        try:
            selected_objects = context.selected_objects
            if not selected_objects:
                self.report({'ERROR'}, "请先选择要预览的物体")
                return {'CANCELLED'}

            channel = self.channel
            affected_objects = 0
            failed_objects = 0
            restored_objects = 0
            vc_tool = context.scene.vertex_color_tool

            for obj in selected_objects:
                if obj.type != 'MESH':
                    continue

                mesh = obj.data

                # === 恢复完整颜色 ===
                if channel == 'RGBA':
                    if not has_preview_backup(mesh):
                        # 该物体本来就没有处于预览状态，跳过即可
                        continue

                    # 用备份层自己的名称定位原始层（备份层的 domain/type 与之一致）
                    vcol_layer = get_or_create_active_vcol_layer(obj)
                    if not vcol_layer:
                        log_warning(f"无法为物体 {obj.name} 获取顶点色层，已跳过恢复。")
                        failed_objects += 1
                        continue

                    if restore_color_layer(mesh, vcol_layer):
                        mesh.update()
                        restored_objects += 1
                    else:
                        failed_objects += 1
                    continue

                # === 切换到单通道预览 ===
                vcol_layer = get_or_create_active_vcol_layer(obj)
                if not vcol_layer:
                    log_warning(f"无法为物体 {obj.name} 获取或创建顶点色层，已跳过。")
                    failed_objects += 1
                    continue

                # 首次预览时建立备份；已存在备份则复用，避免连续切通道时层层覆盖
                if not has_preview_backup(mesh):
                    if backup_color_layer(mesh, vcol_layer) is None:
                        log_error(f"为 {obj.name} 创建预览备份失败，已跳过该物体")
                        failed_objects += 1
                        continue

                backup = mesh.color_attributes[PREVIEW_BACKUP_LAYER_NAME]
                channel_index = self._CHANNEL_INDEX[channel]

                # 始终以备份为数据源，保证 R→G→B 反复切换不会累积失真
                count = min(len(vcol_layer.data), len(backup.data))
                for i in range(count):
                    val = backup.data[i].color[channel_index]
                    vcol_layer.data[i].color = (val, val, val, 1.0)

                mesh.update()
                affected_objects += 1

            if channel == 'RGBA':
                if restored_objects > 0:
                    vc_tool.last_operation = f"已恢复 {restored_objects} 个物体的完整颜色显示"
                    self.report({'INFO'}, f"已恢复 {restored_objects} 个物体的完整颜色显示")
                else:
                    vc_tool.last_operation = "没有需要恢复的物体（未处于预览状态）"
                    self.report({'INFO'}, "没有需要恢复的物体（未处于预览状态）")
            else:
                channel_name = self._CHANNEL_NAME[channel]
                vc_tool.last_operation = f"已切换到{channel_name}通道预览"
                message = f"已将 {affected_objects} 个物体切换到{channel_name}通道预览"
                if failed_objects:
                    message += f"（{failed_objects} 个跳过）"
                self.report({'INFO'}, message)

            return {'FINISHED'}

        except Exception as e:
            report_error(self, context, f"预览通道时出错: {str(e)}", exc=e)
            return {'CANCELLED'}
