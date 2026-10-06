"""
集合选择子面板
"""

import bpy


class VERTEXCOLOR_PT_CollectionPanel(bpy.types.Panel):
    """选择参考组和目标组集合"""
    bl_label = "集合选择"
    bl_idname = "VERTEXCOLOR_PT_CollectionPanel"
    bl_parent_id = "VERTEXCOLOR_PT_MainPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "顶点色复制"

    def draw(self, context):
        layout = self.layout
        vc_tool = context.scene.vertex_color_tool

        col = layout.column(align=True)
        col.prop_search(vc_tool, "collection_a", bpy.data, "collections",
                        text="参考组", icon='OBJECT_DATA')
        col.prop_search(vc_tool, "collection_b", bpy.data, "collections",
                        text="目标组", icon='OBJECT_DATA')

        # 统计信息
        if vc_tool.collection_a_stats or vc_tool.collection_b_stats:
            layout.separator(factor=0.5)
            col = layout.column(align=True)
            if vc_tool.collection_a_stats:
                col.label(text=vc_tool.collection_a_stats)
            if vc_tool.collection_b_stats:
                col.label(text=vc_tool.collection_b_stats)
