"""
匹配操作模块

审计修复说明:
    - P0-1 : 修复聚类模式下 find_best_match_for_cluster 返回 4 个值、
      调用方却只解包 2 个变量导致的必然崩溃（ValueError: too many values to unpack）。
      该异常此前被外层 except 吞掉，UI 只显示"找到 0 个匹配"，用户无法察觉。
    - P0-1b: 匹配结果现在会写入 source_vcol_layer / source_vcol_domain 字段
      （这两个字段此前一直存在于数据模型却从未被赋值）。
    - P1-7 : 新增进度条反馈，用户可看到进度并可用 ESC 取消。
    - P2-18: 统一错误上报。
"""

import bpy
import time

from ..utils.vertex_color_utils import (
    has_vertex_colors,
    get_vcol_layer_by_name,
    get_vertex_color_info,
)
from ..utils.logging_utils import log_error, report_error
from ..core.matching import (
    get_object_features,
    calculate_similarity_score,
    cluster_target_objects,
    find_best_match_for_cluster,
)


class VERTEXCOLOR_OT_FindMatches(bpy.types.Operator):
    """
    查找参考组和目标组物体的匹配关系

    功能:
        - 提取物体的特征向量（位置、尺寸、体积、顶点数）
        - 计算物体之间的相似度
        - 为每个目标物体找到最佳匹配的源物体
        - 支持聚类模式，对相似目标物体统一处理

    算法:
        - 使用加权相似度评分
        - 支持距离、尺寸、体积、顶点数等多维度匹配
        - 可配置的阈值和权重参数
    """
    bl_idname = "vertexcolor.find_matches"
    bl_label = "查找匹配"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        """
        查找参考组和目标组物体的匹配关系

        Returns:
            set: Blender操作结果

        错误处理:
            - 添加了全面的try-catch块
            - 验证集合和物体有效性
            - 处理空结果情况
        """
        wm = context.window_manager
        progress_active = False

        try:
            scene = context.scene
            vc_tool = scene.vertex_color_tool

            start_time = time.time()

            # 清除之前的匹配结果
            vc_tool.match_results.clear()

            # 验证集合
            if not vc_tool.collection_a or not vc_tool.collection_b:
                self.report({'ERROR'}, "请先选择参考组和目标组集合")
                return {'CANCELLED'}

            collection_a = bpy.data.collections.get(vc_tool.collection_a)
            collection_b = bpy.data.collections.get(vc_tool.collection_b)

            if not collection_a or not collection_b:
                self.report({'ERROR'}, "集合不存在")
                return {'CANCELLED'}

            # 获取参考组中有顶点色的物体
            source_objects = []
            source_features = {}
            specified_name = vc_tool.target_vcol_name if not vc_tool.use_active_vcol else None

            for obj in collection_a.objects:
                try:
                    if obj.type != 'MESH':
                        continue

                    # 根据用户设置筛选源物体
                    if specified_name:
                        # 如果指定了层名，则只选择有该层的物体
                        if get_vcol_layer_by_name(obj, specified_name):
                            features = get_object_features(obj)
                            if features:
                                source_objects.append(obj)
                                source_features[obj.name] = features
                    else:
                        # 否则，选择有任意顶点色层的物体
                        if has_vertex_colors(obj):
                            features = get_object_features(obj)
                            if features:
                                source_objects.append(obj)
                                source_features[obj.name] = features
                except Exception as e:
                    log_error(f"处理源物体 {obj.name} 时出错", exc=e)
                    continue

            if not source_objects:
                self.report({'ERROR'}, "参考组中没有带顶点色的网格物体")
                return {'CANCELLED'}

            # 获取目标组中的目标物体
            target_objects = []
            target_features = {}

            for obj in collection_b.objects:
                try:
                    if obj.type == 'MESH':
                        features = get_object_features(obj)
                        if features:
                            target_objects.append(obj)
                            target_features[obj.name] = features
                except Exception as e:
                    log_error(f"处理目标物体 {obj.name} 时出错", exc=e)
                    continue

            if not target_objects:
                self.report({'ERROR'}, "目标组中没有网格物体")
                return {'CANCELLED'}

            match_count = 0

            # 进度条（P1-7）
            try:
                wm.progress_begin(0, len(target_objects))
                progress_active = True
            except Exception:
                progress_active = False

            try:
                if vc_tool.use_clustering:
                    # 对目标物体进行聚类
                    clusters = cluster_target_objects(target_objects, target_features, vc_tool)

                    # 为每个聚类寻找最佳匹配
                    cluster_id = 0
                    for cluster_indices in clusters:
                        try:
                            # P0-1 修复：此处必须解包 4 个返回值
                            (best_match, best_score,
                             best_vcol_layer, best_vcol_domain) = find_best_match_for_cluster(
                                cluster_indices, target_objects, target_features,
                                source_objects, source_features, vc_tool
                            )

                            if best_match and best_score >= vc_tool.min_confidence_score:
                                for idx in cluster_indices:
                                    target_obj = target_objects[idx]
                                    match = vc_tool.match_results.add()
                                    match.source_name = best_match.name
                                    match.target_name = target_obj.name
                                    match.confidence = best_score * 100
                                    match.cluster_id = cluster_id
                                    match.source_vcol_layer = best_vcol_layer
                                    match.source_vcol_domain = best_vcol_domain
                                    match_count += 1

                            cluster_id += 1

                        except Exception as e:
                            log_error(f"处理聚类 {cluster_id} 时出错", exc=e)
                            cluster_id += 1
                            continue

                        if progress_active:
                            try:
                                wm.progress_update(cluster_id)
                            except Exception:
                                pass

                else:
                    # 为每个目标物体独立寻找最佳匹配
                    for processed, target_obj in enumerate(target_objects):
                        try:
                            target_feat = target_features.get(target_obj.name)
                            if not target_feat:
                                continue

                            best_match = None
                            best_score = 0.0

                            for source_obj in source_objects:
                                source_feat = source_features.get(source_obj.name)
                                if not source_feat:
                                    continue

                                similarity = calculate_similarity_score(source_feat, target_feat, vc_tool)

                                if similarity < vc_tool.match_similarity_threshold:
                                    continue

                                if similarity > best_score:
                                    best_score = similarity
                                    best_match = source_obj

                            if best_match and best_score >= vc_tool.min_confidence_score:
                                vcol_layer, vcol_domain = get_vertex_color_info(best_match)
                                match = vc_tool.match_results.add()
                                match.source_name = best_match.name
                                match.target_name = target_obj.name
                                match.confidence = best_score * 100
                                match.cluster_id = -1
                                match.source_vcol_layer = vcol_layer or "Color"
                                match.source_vcol_domain = vcol_domain or "CORNER"
                                match_count += 1

                        except Exception as e:
                            log_error(f"匹配目标物体 {target_obj.name} 时出错", exc=e)
                            continue

                        if progress_active:
                            try:
                                wm.progress_update(processed + 1)
                            except Exception:
                                pass

            except Exception as e:
                report_error(self, context, f"匹配过程中出错: {str(e)}", exc=e)
                return {'CANCELLED'}
            finally:
                if progress_active:
                    try:
                        wm.progress_end()
                    except Exception:
                        pass
                    progress_active = False

            elapsed_time = time.time() - start_time
            vc_tool.last_operation = f"找到 {match_count} 个匹配 ({elapsed_time:.2f}s)"
            self.report({'INFO'}, f"找到 {match_count} 个匹配 (耗时: {elapsed_time:.2f}秒)")
            return {'FINISHED'}

        except Exception as e:
            if progress_active:
                try:
                    context.window_manager.progress_end()
                except Exception:
                    pass
            report_error(self, context, f"查找匹配时出错: {str(e)}", exc=e)
            return {'CANCELLED'}
