"""
分析操作模块
"""

import bpy
from ..utils.vertex_color_utils import has_vertex_colors
from ..utils.collection_utils import (
    get_collection_objects,
    format_collection_stats,
)
from ..utils.logging_utils import log_error, report_error


class VERTEXCOLOR_OT_AnalyzeGroups(bpy.types.Operator):
    """
    分析参考组和目标组的顶点色情况

    功能:
        - 统计参考组中的网格物体数量和顶点色数量
        - 统计目标组中的网格物体数量和顶点色数量
        - 显示分析结果供用户参考

    统计范围（v1.1.0 起变更）:
        统计**递归包含所有层级的子集合**（旧实现只看集合直属物体，
        会漏掉嵌套子集合里的模型）。因此升级后数字可能变大——
        那是补上了原先的漏算，不是算错。
    """
    bl_idname = "vertexcolor.analyze_groups"
    bl_label = "分析组"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        """
        仅在参考组与目标组都已指定时可用（否则按钮变灰）。

        为什么需要: 没有这一步，用户点按钮后只会收到一句
        「请先选择参考组和目标组集合」的报错 toast，
        容易让人以为自己点错了地方。按钮变灰表达的是
        「前置步骤还没完成」，与「操作出错」是两回事。

        注意: poll 由 Blender 每帧调用，因此只做常量时间的属性读取，
        不做任何集合遍历、bpy.data 查询或异常抛出。
        """
        scene = getattr(context, "scene", None)
        vc_tool = getattr(scene, "vertex_color_tool", None)
        if vc_tool is None:
            return False
        return bool(vc_tool.collection_a) and bool(vc_tool.collection_b)

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

            # 分析参考组（递归含所有子集合）
            objects_a = get_collection_objects(collection_a)
            mesh_count_a = 0
            vcol_count_a = 0
            for obj in objects_a:
                try:
                    if obj.type == 'MESH':
                        mesh_count_a += 1
                        if has_vertex_colors(obj):
                            vcol_count_a += 1
                except Exception as e:
                    log_error(f"分析物体 {obj.name} 时出错", exc=e)
                    continue

            # 分析目标组（递归含所有子集合）
            objects_b = get_collection_objects(collection_b)
            mesh_count_b = 0
            vcol_count_b = 0
            for obj in objects_b:
                try:
                    if obj.type == 'MESH':
                        mesh_count_b += 1
                        if has_vertex_colors(obj):
                            vcol_count_b += 1
                except Exception as e:
                    log_error(f"分析物体 {obj.name} 时出错", exc=e)
                    continue

            # 统计文字由共用函数生成，面板与 toast 永远是同一份文案。
            # 文案含「含子集合」标注：数字因递归变大时，用户能立刻知道原因。
            stats_a = format_collection_stats(
                "参考组", len(objects_a), mesh_count_a, vcol_count_a)
            stats_b = format_collection_stats(
                "目标组", len(objects_b), mesh_count_b, vcol_count_b)

            # 更新缓存统计信息
            vc_tool.collection_a_stats = stats_a
            vc_tool.collection_b_stats = stats_b

            result = f"{stats_a}\n{stats_b}"

            vc_tool.last_operation = "分析完成"
            self.report({'INFO'}, result)
            return {'FINISHED'}

        except Exception as e:
            report_error(self, context, f"分析过程中出错: {str(e)}", exc=e)
            return {'CANCELLED'}
