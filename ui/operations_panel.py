"""
自动处理工具子面板
"""

import bpy


class VERTEXCOLOR_PT_OperationsPanel(bpy.types.Panel):
    """自动处理工作流：分析 -> 匹配 -> 复制"""
    bl_label = "自动处理工具"
    bl_idname = "VERTEXCOLOR_PT_OperationsPanel"
    bl_parent_id = "VERTEXCOLOR_PT_MainPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "顶点色复制"

    def draw(self, context):
        layout = self.layout

        col = layout.column(align=True)

        row = col.row(align=True)
        row.scale_y = 1.5
        row.operator("vertexcolor.analyze_groups", text="1. 分析集合", icon='VIEWZOOM')

        row = col.row(align=True)
        row.scale_y = 1.5
        row.operator("vertexcolor.find_matches", text="2. 查找匹配", icon='ZOOM_ALL')

        # 提示：匹配结果可在下方「匹配结果」面板中查看与剔除（P1-6）
        if not context.scene.vertex_color_tool.match_results:
            col.label(text="匹配后可在「匹配结果」面板查看", icon='INFO')

        row = col.row(align=True)
        row.scale_y = 1.5
        row.operator("vertexcolor.copy_colors", text="3. 复制顶点色", icon='COPYDOWN')
