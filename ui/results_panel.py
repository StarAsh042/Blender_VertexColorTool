"""
匹配结果预览面板

审计修复说明（P1-6）:
    匹配结果（match_results）此前在 UI 中完全没有呈现：用户点完「查找匹配」后
    看不到匹配了谁、置信度多少、是否存在错配，只能盲目点「复制顶点色」。
    本面板补上这一关键的可观测性缺口，并提供手动剔除错误匹配的能力。
"""

import bpy


class VERTEXCOLOR_UL_MatchResults(bpy.types.UIList):
    """匹配结果列表"""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)

        # 左侧：源物体 → 目标物体
        sub = row.row(align=True)
        sub.label(text=item.source_name, icon='OBJECT_DATA')
        sub.label(text="→")
        sub.label(text=item.target_name, icon='MESH_DATA')

        # 右侧：置信度（带状态图标）+ 聚类标识
        confidence = item.confidence
        if confidence >= 80:
            conf_icon = 'CHECKMARK'
        elif confidence >= 60:
            conf_icon = 'INFO'
        else:
            conf_icon = 'ERROR'

        tail = row.row(align=True)
        tail.alignment = 'RIGHT'
        tail.label(text=f"{confidence:.0f}%", icon=conf_icon)
        if item.cluster_id >= 0:
            tail.label(text=f"组{item.cluster_id}")


class VERTEXCOLOR_OT_ClearMatchResults(bpy.types.Operator):
    """清空全部匹配结果"""
    bl_idname = "vertexcolor.clear_match_results"
    bl_label = "清空匹配结果"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        vc_tool = context.scene.vertex_color_tool
        count = len(vc_tool.match_results)
        vc_tool.match_results.clear()
        vc_tool.match_results_index = 0
        vc_tool.last_operation = f"已清空 {count} 条匹配结果"
        self.report({'INFO'}, f"已清空 {count} 条匹配结果")
        return {'FINISHED'}


class VERTEXCOLOR_OT_RemoveMatchResult(bpy.types.Operator):
    """移除列表中选中的那一条匹配（用于剔除错误匹配）"""
    bl_idname = "vertexcolor.remove_match_result"
    bl_label = "移除选中匹配"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        vc_tool = context.scene.vertex_color_tool
        index = vc_tool.match_results_index

        if not vc_tool.match_results or index < 0 or index >= len(vc_tool.match_results):
            # QA 复核建议: 未选中列表项属轻微情况，用 WARNING 而非 ERROR，
            # 避免 bpy.ops 调用方被 RuntimeError 打断。
            self.report({'WARNING'}, "请先在列表中选中一条匹配")
            return {'CANCELLED'}

        removed = vc_tool.match_results[index]
        description = f"{removed.source_name} → {removed.target_name}"
        vc_tool.match_results.remove(index)

        # 修正选中索引，避免越界
        new_index = min(index, len(vc_tool.match_results) - 1)
        vc_tool.match_results_index = max(0, new_index)

        vc_tool.last_operation = f"已移除匹配: {description}"
        self.report({'INFO'}, f"已移除匹配: {description}")
        return {'FINISHED'}


class VERTEXCOLOR_PT_ResultsPanel(bpy.types.Panel):
    """
    匹配结果预览面板

    展示「谁匹配到谁 + 置信度 + 聚类分组」，并提供剔除与清空能力。
    """
    bl_label = "匹配结果"
    bl_idname = "VERTEXCOLOR_PT_ResultsPanel"
    bl_parent_id = "VERTEXCOLOR_PT_MainPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "顶点色复制"

    def draw(self, context):
        layout = self.layout
        vc_tool = context.scene.vertex_color_tool

        if not vc_tool.match_results:
            layout.label(text="尚无匹配结果，请先执行「查找匹配」", icon='INFO')
            return

        # 概要统计
        total = len(vc_tool.match_results)
        high_count = sum(1 for m in vc_tool.match_results if m.confidence >= 80)
        review_count = total - high_count

        box = layout.box()
        col = box.column(align=True)
        col.label(text=f"共 {total} 条匹配", icon='ZOOM_ALL')
        col.label(text=f"高置信度(≥80%): {high_count} ／ 建议复核: {review_count}")

        # 列表
        row = layout.row()
        row.template_list(
            "VERTEXCOLOR_UL_MatchResults", "",
            vc_tool, "match_results",
            vc_tool, "match_results_index",
            rows=6,
        )

        # 操作
        col = layout.column(align=True)
        col.operator("vertexcolor.remove_match_result", text="移除选中匹配", icon='REMOVE')
        col.operator("vertexcolor.clear_match_results", text="清空匹配结果", icon='TRASH')
