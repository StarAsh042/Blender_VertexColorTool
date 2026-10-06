"""
主面板UI模块

主面板作为容器，仅显示状态信息。
所有功能内容由子面板承载。
"""

import bpy


class VERTEXCOLOR_PT_MainPanel(bpy.types.Panel):
    """顶点色复制工具 - 主面板"""
    bl_label = "顶点色复制工具"
    bl_idname = "VERTEXCOLOR_PT_MainPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "顶点色复制"

    def draw(self, context):
        layout = self.layout
        vc_tool = context.scene.vertex_color_tool

        if vc_tool.last_operation:
            row = layout.row()
            row.alignment = 'LEFT'
            row.label(text=vc_tool.last_operation, icon='INFO')
