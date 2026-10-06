"""
分析操作模块
"""

import bpy
from ..utils.vertex_color_utils import has_vertex_colors
from ..utils.logging_utils import log_error, report_error


class VERTEXCOLOR_OT_AnalyzeGroups(bpy.types.Operator):
    """
    分析参考组和目标组的顶点色情况

    功能:
        - 统计参考组中的网格物体数量和顶点色数量
        - 统计目标组中的网格物体数量和顶点色数量
        - 显示分析结果供用户参考
    """
    bl_idname = "vertexcolor.analyze_groups"
    bl_label = "分析组"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        """
        分析参考组和目标组的顶点色情况

        Returns:
            set: Blender操作结果

        错误处理:
            - 添加了try-catch块捕获异常
            - 验证集合是否存在
            - 防止访问无效属性
        """
        try:
            scene = context.scene
            vc_tool = scene.vertex_color_tool

            # 验证输入
            if not vc_tool.collection_a or not vc_tool.collection_b:
                self.report({'ERROR'}, "请先选择参考组和目标组集合")
                return {'CANCELLED'}

            collection_a = bpy.data.collections.get(vc_tool.collection_a)
            collection_b = bpy.data.collections.get(vc_tool.collection_b)

            if not collection_a or not collection_b:
                self.report({'ERROR'}, "集合不存在")
                return {'CANCELLED'}

            # 分析参考组
            mesh_count_a = 0
            vcol_count_a = 0
            for obj in collection_a.objects:
                try:
                    if obj.type == 'MESH':
                        mesh_count_a += 1
                        if has_vertex_colors(obj):
                            vcol_count_a += 1
                except Exception as e:
                    log_error(f"分析物体 {obj.name} 时出错", exc=e)
                    continue

            # 分析目标组
            mesh_count_b = 0
            vcol_count_b = 0
            for obj in collection_b.objects:
                try:
                    if obj.type == 'MESH':
                        mesh_count_b += 1
                        if has_vertex_colors(obj):
                            vcol_count_b += 1
                except Exception as e:
                    log_error(f"分析物体 {obj.name} 时出错", exc=e)
                    continue

            # 更新缓存统计信息
            vc_tool.collection_a_stats = f"参考组: {len(collection_a.objects)}个物体 ({mesh_count_a}个网格, {vcol_count_a}个有顶点色)"
            vc_tool.collection_b_stats = f"目标组: {len(collection_b.objects)}个物体 ({mesh_count_b}个网格, {vcol_count_b}个有顶点色)"

            result = (f"参考组: {len(collection_a.objects)}个物体 ({mesh_count_a}个网格, {vcol_count_a}个有顶点色)\n"
                     f"目标组: {len(collection_b.objects)}个物体 ({mesh_count_b}个网格, {vcol_count_b}个有顶点色)")

            vc_tool.last_operation = "分析完成"
            self.report({'INFO'}, result)
            return {'FINISHED'}

        except Exception as e:
            report_error(self, context, f"分析过程中出错: {str(e)}", exc=e)
            return {'CANCELLED'}
