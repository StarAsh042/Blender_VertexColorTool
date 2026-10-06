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

        # 递归范围说明（常驻，不依赖用户是否点过「分析集合」）
        #
        # 为什么必须常驻: 匹配与分析都按「本集合 + 所有层级子集合」取物体。
        # 依赖「用子集合做隔离」的用户升级后会发现隔离失效了——
        # 那是行为变更，不是 bug。仅在统计数字里写「含子集合」不够，
        # 因为用户往往是先配好集合、隔很久才点一次分析，
        # 数字变大时早已想不起来当初配的是什么结构。
        # 因此把范围定义摆在选集合的地方，与操作本身同处一屏。
        col = layout.column(align=True)
        col.label(text="范围含各级子集合", icon='INFO')

        # 统计信息
        if vc_tool.collection_a_stats or vc_tool.collection_b_stats:
            layout.separator(factor=0.5)
            col = layout.column(align=True)
            if vc_tool.collection_a_stats:
                col.label(text=vc_tool.collection_a_stats)
            if vc_tool.collection_b_stats:
                col.label(text=vc_tool.collection_b_stats)
