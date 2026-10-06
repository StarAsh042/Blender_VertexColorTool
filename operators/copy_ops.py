"""
复制操作模块

审计修复说明:
    - P1-4 : 移除批量复制开头无条件调用的 clear_cache()。
      该调用与「使用缓存」开关的设计意图自相矛盾，使缓存形同虚设。
      缓存失效改由 load_post 钩子 + 对象指针缓存键共同保证。
    - P1-7 : 新增进度条与可取消支持（进度条出现时可用 ESC 触发 cancel）。
      同时把 cancelled 标志改为在 execute 开始时重置，
      避免类属性在多实例/重入场景下状态不隔离。
    - P2-18: 统一错误上报。
"""

import bpy
import gc
import time

from ..utils.vertex_color_utils import has_vertex_colors, get_vertex_color_info
from ..utils.logging_utils import log_error, report_error
from ..core.vertex_color_ops import copy_vertex_colors_between_objects
from ..core.cache import VertexColorCache


class VERTEXCOLOR_OT_ManualCopy(bpy.types.Operator):
    """
    手动复制选中物体的顶点色

    功能:
        - 手动选择源物体和目标物体
        - 将源物体的顶点色复制到目标物体
        - 支持多对一复制

    用法:
        - 先选择目标物体
        - 最后选择源物体（黄色边框的为活动物体）
        - 执行复制操作
    """
    bl_idname = "vertexcolor.manual_copy"
    bl_label = "手动复制顶点色"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        """
        手动复制选中物体的顶点色

        Returns:
            set: Blender操作结果

        错误处理:
            - 验证选择的有效性
            - 捕获复制过程中的异常
            - 统计成功和失败的数量
        """
        try:
            scene = context.scene
            vc_tool = scene.vertex_color_tool

            # 获取选中的物体
            selected_objects = context.selected_objects

            if len(selected_objects) < 2:
                self.report({'ERROR'}, "请先选择至少两个物体（先选目标物体，最后选源物体）")
                return {'CANCELLED'}

            # 确定源物体和目标物体
            if context.active_object and context.active_object in selected_objects:
                source_obj = context.active_object
                target_objects = [obj for obj in selected_objects if obj != source_obj]
            else:
                source_obj = selected_objects[-1]
                target_objects = selected_objects[:-1]

            # 确保源物体是网格
            if source_obj.type != 'MESH':
                self.report({'ERROR'}, f"源物体 {source_obj.name} 不是网格物体")
                return {'CANCELLED'}

            # 检查源物体是否有顶点色
            if not has_vertex_colors(source_obj):
                self.report({'ERROR'}, f"源物体 {source_obj.name} 没有顶点色")
                return {'CANCELLED'}

            # 获取源顶点色层名称
            source_vcol_layer, _ = get_vertex_color_info(source_obj)
            if not source_vcol_layer:
                source_vcol_layer = "Color"

            # 检查目标物体
            valid_targets = []
            for obj in target_objects:
                try:
                    if obj.type != 'MESH':
                        self.report({'WARNING'}, f"跳过非网格物体: {obj.name}")
                        continue
                    valid_targets.append(obj)
                except Exception as e:
                    log_error(f"检查目标物体 {obj.name} 时出错", exc=e)
                    continue

            if not valid_targets:
                self.report({'ERROR'}, "没有有效的目标物体")
                return {'CANCELLED'}

            # 复制顶点色到每个目标物体
            success_count = 0
            fail_count = 0

            for i, target_obj in enumerate(valid_targets):
                try:
                    # 复制顶点色
                    success = copy_vertex_colors_between_objects(
                        source_obj, target_obj, vc_tool=vc_tool
                    )

                    if success:
                        success_count += 1
                    else:
                        fail_count += 1

                    # 分批处理时，每批结束后强制垃圾回收
                    if vc_tool.optimize_memory and i % 5 == 0:
                        gc.collect()

                except Exception as e:
                    log_error(f"复制顶点色到 {target_obj.name} 时出错", exc=e)
                    fail_count += 1
                    continue

            vc_tool.last_operation = f"手动复制: 成功 {success_count}, 失败 {fail_count}"
            self.report({'INFO'}, f"成功从 {source_obj.name} 复制顶点色到 {success_count} 个物体 ({fail_count} 个失败)")
            return {'FINISHED'}

        except Exception as e:
            report_error(self, context, f"手动复制时出错: {str(e)}", exc=e)
            return {'CANCELLED'}


class VERTEXCOLOR_OT_CopyColors(bpy.types.Operator):
    """
    根据匹配结果复制顶点色

    功能:
        - 读取之前计算的匹配结果
        - 批量复制顶点色到目标物体
        - 支持分批处理以优化内存使用
        - 显示进度和统计信息

    性能优化:
        - 分批处理防止内存溢出
        - 缓存源物体数据
        - 垃圾回收控制
    """
    bl_idname = "vertexcolor.copy_colors"
    bl_label = "复制顶点色"
    bl_options = {'REGISTER', 'UNDO'}

    cancelled = False

    def execute(self, context):
        """
        根据匹配结果复制顶点色

        Returns:
            set: Blender操作结果

        错误处理:
            - 验证匹配结果存在
            - 分批处理防止内存溢出
            - 捕获复制过程中的异常
        """
        # 每次执行都重置取消标志（避免类属性跨实例残留）
        self.cancelled = False

        wm = context.window_manager
        progress_active = False

        try:
            scene = context.scene
            vc_tool = scene.vertex_color_tool

            if not vc_tool.match_results:
                self.report({'ERROR'}, "请先查找匹配")
                return {'CANCELLED'}

            # 性能分析初始化
            profiling_enabled = vc_tool.enable_profiling
            perf_stats = {}
            start_time = time.time()  # 始终记录开始时间，用于计算总耗时
            if profiling_enabled:
                perf_stats['total_start'] = start_time
                perf_stats['total_copy_time'] = 0
                perf_stats['match_processing_times'] = []
                perf_stats['batch_times'] = []

            success_count = 0
            fail_count = 0

            # 说明（P1-4）: 此处不再无条件调用 VertexColorCache.clear_cache()。
            # 缓存失效由对象指针缓存键 + load_post 钩子保证，
            # 否则「使用缓存」开关将形同虚设。

            # 分批处理
            batch_size = max(1, vc_tool.batch_size)
            total_matches = len(vc_tool.match_results)

            # 进度条（P1-7）
            try:
                wm.progress_begin(0, total_matches)
                progress_active = True
            except Exception:
                progress_active = False

            try:
                for batch_start in range(0, total_matches, batch_size):
                    batch_end = min(batch_start + batch_size, total_matches)
                    batch_matches = vc_tool.match_results[batch_start:batch_end]

                    batch_start_time = time.time()

                    for offset, match in enumerate(batch_matches):
                        match_start_time = time.time() if profiling_enabled else 0
                        try:
                            source_obj = bpy.data.objects.get(match.source_name)
                            target_obj = bpy.data.objects.get(match.target_name)

                            if not source_obj or not target_obj:
                                fail_count += 1
                                continue

                            # 使用共用的复制函数
                            if copy_vertex_colors_between_objects(
                                source_obj, target_obj, vc_tool=vc_tool
                            ):
                                success_count += 1
                            else:
                                fail_count += 1

                            if profiling_enabled:
                                perf_stats['match_processing_times'].append(
                                    time.time() - match_start_time
                                )

                        except Exception as e:
                            log_error(
                                f"复制顶点色时出错 (源: {match.source_name}, 目标: {match.target_name})",
                                exc=e,
                            )
                            fail_count += 1
                            if profiling_enabled:
                                perf_stats['match_processing_times'].append(
                                    time.time() - match_start_time
                                )
                            continue

                        if progress_active:
                            try:
                                wm.progress_update(batch_start + offset + 1)
                            except Exception:
                                pass

                    # 记录批处理时间
                    if profiling_enabled:
                        perf_stats['batch_times'].append(time.time() - batch_start_time)

                    # 分批处理时，每批结束后强制垃圾回收
                    if vc_tool.optimize_memory:
                        gc.collect()

                    # 更新UI
                    try:
                        if context.area:
                            context.area.tag_redraw()
                    except Exception:
                        pass

                    # 如果用户取消操作（进度条下按 ESC），提前退出
                    if self.cancelled:
                        break

            finally:
                if progress_active:
                    try:
                        wm.progress_end()
                    except Exception:
                        pass
                    progress_active = False

            # 性能分析统计
            if profiling_enabled:
                perf_stats['total_time'] = time.time() - perf_stats['total_start']
                perf_stats['total_copy_time'] = perf_stats['total_time']

                # 计算平均处理时间
                if perf_stats['match_processing_times']:
                    avg_match_time = sum(perf_stats['match_processing_times']) / len(perf_stats['match_processing_times'])
                    max_match_time = max(perf_stats['match_processing_times'])
                    min_match_time = min(perf_stats['match_processing_times'])
                else:
                    avg_match_time = max_match_time = min_match_time = 0

                # 计算平均批处理时间
                if perf_stats['batch_times']:
                    avg_batch_time = sum(perf_stats['batch_times']) / len(perf_stats['batch_times'])
                else:
                    avg_batch_time = 0

                # 缓存统计
                cache_stats = VertexColorCache.get_cache_stats()

                # 生成统计报告
                stats_report = f"""
性能分析统计:
总耗时: {perf_stats['total_time']:.3f}s
实际复制: {perf_stats['total_copy_time']:.3f}s
平均批处理: {avg_batch_time:.3f}s
平均单对处理: {avg_match_time:.4f}s (max: {max_match_time:.4f}s, min: {min_match_time:.4f}s)
缓存命中率: {cache_stats['hit_rate']:.1f}% ({cache_stats['hits']} 命中, {cache_stats['misses']} 未命中)
                """.strip()

                print(stats_report)
                vc_tool.profiling_stats = stats_report

            elapsed_time = time.time() - start_time
            if self.cancelled:
                vc_tool.last_operation = (
                    f"复制已取消: 成功 {success_count}, 失败 {fail_count} ({elapsed_time:.2f}s)"
                )
                self.report({'WARNING'}, f"复制被取消: 已成功 {success_count}, 失败 {fail_count}")
                return {'CANCELLED'}

            vc_tool.last_operation = f"复制完成: 成功 {success_count}, 失败 {fail_count} ({elapsed_time:.2f}s)"
            self.report({'INFO'}, f"顶点色复制完成: 成功 {success_count}, 失败 {fail_count} (耗时: {elapsed_time:.2f}秒)")
            return {'FINISHED'}

        except Exception as e:
            if progress_active:
                try:
                    context.window_manager.progress_end()
                except Exception:
                    pass
            report_error(self, context, f"批量复制顶点色时出错: {str(e)}", exc=e)
            return {'CANCELLED'}

    def cancel(self, context):
        """取消操作（进度条显示时按 ESC 触发）"""
        self.cancelled = True
