"""
选择操作模块
"""

import bpy
from ..utils.vertex_color_utils import has_vertex_colors, is_impure_vertex_color
from ..utils.logging_utils import log_error, log_warning, report_error


class VERTEXCOLOR_OT_SelectNoVCol(bpy.types.Operator):
    """在所有可见物体中选择没有顶点色的物体"""
    bl_idname = "vertexcolor.select_no_vcol"
    bl_label = "选择无顶点色物体"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        """
        在所有可见物体中选择没有顶点色的物体
        """
        try:
            scene = context.scene
            vc_tool = scene.vertex_color_tool

            # 改为从场景所有可见物体中查找
            search_space = context.view_layer.objects
            if not search_space:
                self.report({'INFO'}, "场景中没有可见的物体。")
                return {'CANCELLED'}

            # 清空当前选择
            try:
                bpy.ops.object.select_all(action='DESELECT')
            except Exception as e:
                # 在某些上下文中（如未激活视图），此操作可能失败，但可以继续
                log_warning(f"清空选择时出错（可忽略）: {e}")

            # 选择没有顶点色的网格物体
            selected_count = 0
            first_selected = None

            for obj in search_space:
                try:
                    # 确保物体是可见的，并且是网格
                    if obj.visible_get() and obj.type == 'MESH' and not has_vertex_colors(obj):
                        obj.select_set(True)
                        selected_count += 1
                        if first_selected is None:
                            first_selected = obj
                except Exception as e:
                    log_error(f"检查物体 {obj.name} 时出错", exc=e)
                    continue

            # 如果选择了物体，设置活动物体
            if selected_count > 0 and first_selected:
                try:
                    context.view_layer.objects.active = first_selected
                except Exception as e:
                    log_error(f"设置活动物体时出错", exc=e)

            vc_tool.last_operation = f"选择了 {selected_count} 个无顶点色物体"
            self.report({'INFO'}, f"在场景中选择了 {selected_count} 个无顶点色物体")
            return {'FINISHED'}

        except Exception as e:
            report_error(self, context, f"选择无顶点色物体时出错: {str(e)}", exc=e)
            return {'CANCELLED'}


class VERTEXCOLOR_OT_SelectNonPureVCol(bpy.types.Operator):
    """在所有可见物体中选择顶点色不纯的物体"""
    bl_idname = "vertexcolor.select_nonpure_vcol"
    bl_label = "选择不纯顶点色"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        """
        在所有可见物体中选择顶点色不纯的物体
        """
        try:
            scene = context.scene
            vc_tool = scene.vertex_color_tool

            # 改为从场景所有可见物体中查找
            search_space = context.view_layer.objects
            if not search_space:
                self.report({'INFO'}, "场景中没有可见的物体。")
                return {'CANCELLED'}

            # 清空当前选择
            try:
                bpy.ops.object.select_all(action='DESELECT')
            except Exception as e:
                # 在某些上下文中（如未激活视图），此操作可能失败，但可以继续
                log_warning(f"清空选择时出错（可忽略）: {e}")

            # 选择顶点色不纯的网格物体
            selected_count = 0
            first_selected = None

            for obj in search_space:
                try:
                    if obj.visible_get() and obj.type == 'MESH' and has_vertex_colors(obj) and is_impure_vertex_color(obj):
                        obj.select_set(True)
                        selected_count += 1
                        if first_selected is None:
                            first_selected = obj
                except Exception as e:
                    log_error(f"检查物体 {obj.name} 时出错", exc=e)
                    continue

            # 设置活动物体
            if selected_count > 0 and first_selected:
                try:
                    context.view_layer.objects.active = first_selected
                except Exception as e:
                    log_error(f"设置活动物体时出错", exc=e)

            vc_tool.last_operation = f"选择了 {selected_count} 个不纯顶点色物体"
            self.report({'INFO'}, f"在场景中选择了 {selected_count} 个不纯顶点色物体")
            return {'FINISHED'}

        except Exception as e:
            report_error(self, context, f"选择不纯顶点色物体时出错: {str(e)}", exc=e)
            return {'CANCELLED'}


class VERTEXCOLOR_OT_ClearSelection(bpy.types.Operator):
    """
    清空当前选择

    功能:
        - 清空所有物体的选择状态
        - 移除活动物体
    """
    bl_idname = "vertexcolor.clear_selection"
    bl_label = "清空选择"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        """
        清空当前选择

        Returns:
            set: Blender操作结果
        """
        try:
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = None

            context.scene.vertex_color_tool.last_operation = "已清空选择"
            self.report({'INFO'}, "已清空选择")
            return {'FINISHED'}

        except Exception as e:
            report_error(self, context, f"清空选择时出错: {str(e)}", exc=e)
            return {'CANCELLED'}
