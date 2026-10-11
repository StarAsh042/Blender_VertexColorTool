"""
匹配参数子面板
"""

import bpy


class VERTEXCOLOR_PT_MatchParamsPanel(bpy.types.Panel):
    """配置物体匹配算法参数"""
    bl_label = "匹配参数"
    bl_idname = "VERTEXCOLOR_PT_MatchParamsPanel"
    bl_parent_id = "VERTEXCOLOR_PT_MainPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "顶点色复制"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        vc_tool = context.scene.vertex_color_tool

        # 预设选择（按钮样式）
        col = layout.column(align=True)
        col.label(text="匹配预设:", icon='PRESET')
        row = col.row(align=True)
        row.prop(vc_tool, "match_preset", expand=True)

        layout.separator(factor=0.5)

        # 基本参数
        col = layout.column(align=True)
        col.label(text="基本参数:", icon='MODIFIER')
        col.prop(vc_tool, "distance_threshold", text="最大匹配距离")

        row = col.row(align=True)
        row.prop(vc_tool, "size_weight", text="尺寸", slider=True)
        row.prop(vc_tool, "volume_weight", text="体积", slider=True)

        row = col.row(align=True)
        row.prop(vc_tool, "vertex_count_weight", text="顶点数", slider=True)
        row.prop(vc_tool, "distance_weight", text="距离", slider=True)

        layout.separator(factor=0.5)

        # 高级参数
        col = layout.column(align=True)
        col.label(text="高级参数:", icon='TOOL_SETTINGS')
        col.prop(vc_tool, "match_similarity_threshold", text="相似度阈值", slider=True)
        col.prop(vc_tool, "position_decay_factor", text="位置衰减系数", slider=True)
        col.prop(vc_tool, "min_confidence_score", text="最小置信度", slider=True)

        col.separator(factor=0.5)
        col.prop(vc_tool, "use_clustering", text="使用聚类统一顶点色")
        if vc_tool.use_clustering:
            col.prop(vc_tool, "clustering_threshold", text="聚类阈值", slider=True)
