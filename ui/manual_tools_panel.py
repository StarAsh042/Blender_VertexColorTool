"""
手动处理工具子面板
"""

import bpy


class VERTEXCOLOR_PT_ManualToolsPanel(bpy.types.Panel):
    """手动选择、编辑和复制顶点色工具"""
    bl_label = "手动处理工具"
    bl_idname = "VERTEXCOLOR_PT_ManualToolsPanel"
    bl_parent_id = "VERTEXCOLOR_PT_MainPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "顶点色复制"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        vc_tool = context.scene.vertex_color_tool

        # 选择工具
        col = layout.column(align=True)
        col.label(text="选择工具:", icon='RESTRICT_SELECT_OFF')
        row = col.row(align=True)
        row.operator("vertexcolor.select_no_vcol", text="无顶点色", icon='SELECT_SUBTRACT')
        row.operator("vertexcolor.select_nonpure_vcol", text="不纯顶点色", icon='SELECT_INTERSECT')

        layout.separator(factor=0.5)

        # 颜色编辑
        col = layout.column(align=True)
        col.label(text="颜色编辑:", icon='COLOR')
        
        # 常驻颜色选择器
        col.label(text="选择颜色:", icon='EYEDROPPER')
        col.prop(vc_tool, "picked_color", text="")

        row = col.row(align=True)
        row.operator("vertexcolor.apply_selected_color", text="应用颜色", icon='BRUSHES_ALL')
        row.operator("vertexcolor.clear_vertex_colors", text="清除颜色", icon='X')

        # 「从选中顶点取色」：打开对话框前先把当前选区的平均颜色读进拾取器，
        # 避免「想调成某个已存在的颜色」时只能靠肉眼近似输入。
        # 无选中顶点/物体时取色返回None（不报错），对话框照常以默认白色打开。
        row = col.row(align=True)
        row.operator("vertexcolor.modify_vertex_color",
                     text="▸ 从选中顶点取色", icon='EYEDROPPER')

        layout.separator(factor=0.5)

        # 通道预览
        col = layout.column(align=True)
        col.label(text="通道预览:", icon='IMAGE_RGB')
        row = col.row(align=True)
        row.operator("vertexcolor.preview_channel", text="R").channel = 'R'
        row.operator("vertexcolor.preview_channel", text="G").channel = 'G'
        row.operator("vertexcolor.preview_channel", text="B").channel = 'B'
        row.operator("vertexcolor.preview_channel", text="A").channel = 'A'
        row.operator("vertexcolor.preview_channel", text="RGBA").channel = 'RGBA'

        layout.separator(factor=0.5)

        # 手动复制
        col = layout.column(align=True)
        col.label(text="手动复制:", icon='PASTEDOWN')
        col.label(text="先选目标，后选源物体", icon='INFO')
        row = col.row(align=True)
        row.scale_y = 1.4
        row.operator("vertexcolor.manual_copy", text="复制顶点色", icon='PASTEDOWN')
