"""
优化处理子面板
"""

import bpy


class VERTEXCOLOR_PT_VColorParamsPanel(bpy.types.Panel):
    """顶点色复制的优化选项"""
    bl_label = "优化处理"
    bl_idname = "VERTEXCOLOR_PT_VColorParamsPanel"
    bl_parent_id = "VERTEXCOLOR_PT_MainPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "顶点色复制"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        vc_tool = context.scene.vertex_color_tool

        # 顶点色层选择
        col = layout.column(align=True)
        col.prop(vc_tool, "use_active_vcol", text="使用当前激活层", toggle=True, icon='TEXTURE')
        if not vc_tool.use_active_vcol:
            col.prop(vc_tool, "target_vcol_name", text="指定层名")

        layout.separator(factor=0.5)

        # 复制选项
        col = layout.column(align=True)
        col.prop(vc_tool, "use_kdtree", text="使用KDTree加速", toggle=True, icon='AUTO')
        col.prop(vc_tool, "batch_size", text="分批处理数量")

        layout.separator(factor=0.5)

        # 性能优化
        col = layout.column(align=True)
        col.label(text="性能:", icon='MOD_BUILD')
        row = col.row(align=True)
        row.prop(vc_tool, "use_cache", text="缓存", toggle=True)
        row.prop(vc_tool, "optimize_memory", text="内存优化", toggle=True)
