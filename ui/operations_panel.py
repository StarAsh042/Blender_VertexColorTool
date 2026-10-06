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
        vc_tool = context.scene.vertex_color_tool

        col = layout.column(align=True)

        # poll() 会让前置条件不满足的按钮变灰。这里补一行说明，
        # 让用户知道「为什么灰」以及「下一步该做什么」，
        # 否则变灰本身会被误读成按钮坏了。
        if not (vc_tool.collection_a and vc_tool.collection_b):
            col.label(text="请先在上方「集合选择」指定参考组与目标组", icon='INFO')

        row = col.row(align=True)
        row.scale_y = 1.5
        row.operator("vertexcolor.analyze_groups", text="1. 分析集合", icon='VIEWZOOM')

        row = col.row(align=True)
        row.scale_y = 1.5
        row.operator("vertexcolor.find_matches", text="2. 查找匹配", icon='ZOOM_ALL')

        # 提示：匹配结果可在下方「匹配结果」面板中查看与剔除（P1-6）
        if not vc_tool.match_results:
            col.label(text="匹配后可在「匹配结果」面板查看", icon='INFO')

        row = col.row(align=True)
        row.scale_y = 1.5
        row.operator("vertexcolor.copy_colors", text="3. 复制顶点色", icon='COPYDOWN')

        # copy_colors 的 poll() 依赖 match_results 非空，这里说明灰掉的原因。
        # 注意:不能写「需先执行 2. 查找匹配才能复制」——
        # 用户刚执行完查找匹配、但结果为 0 条时，这句话与事实矛盾，
        # 且指向用户已经做过的事。这里只陈述「当前无匹配结果」这个事实
        # 并给出两种情况下都有效的下一步（先执行；已执行仍为空则调低阈值）。
        if not vc_tool.match_results:
            col.label(text="无匹配结果：可调低「相似度阈值」后重试", icon='INFO')
