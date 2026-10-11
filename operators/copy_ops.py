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
    - P0-5 : 批量复制的失败原因聚合。
      原实现只统计「成功 N, 失败 M」，用户无法得知那M 个为何失败
      （原因仅在 Blender 控制台）。现收集失败明细并在结束时
      通过 self.report + 面板状态栏展示前 3 条。
"""

import bpy
import gc
import time

from ..utils.vertex_color_utils import has_vertex_colors, get_vertex_color_info
from ..utils.logging_utils import log_error, report_error
from ..core.vertex_color_ops import copy_vertex_colors_between_objects
from ..core.cache import VertexColorCache


# 失败明细最多展示的条数（P0-5）。
# 超过 3 条时再给出总数，避免 self.report 弹出超长文本刷屏。
_FAILURE_DETAIL_LIMIT = 3

# 失败明细最多**收集**的条数（P0-5）。
# 批量复制可能有上千个目标，全量收集明细本身会占用内存；
# 超出后只保留计数，不再追加文本（展示时统一用失败总数补足）。
_FAILURE_COLLECT_LIMIT = 50

# 取色路径标识 -> 性能报告里的中文标签（阶段 B / 阶段 C）。
# 两条约束启用时都会绕过原生内核（原生暂未实现这两个约束），
# 报告里如实显示当前路径，避免「显示与实际不符」。
_COLOR_PATH_LABELS = {
    'native': '原生内核（C++）',
    'python': 'Python',
    'python-normal': 'Python（法线约束已启用）',
    'python-distance': 'Python（取色距离上限已启用）',
    'python-constraints': 'Python（法线约束、取色距离上限已启用）',
}


class _BoundedFailureList(list):
    """
    带长度上限 + 相同原因合并的失败原因列表（P0-5 / QA 复核项 C、D）。

    直接传给 copy_vertex_colors_between_objects 的 failure_reasons 参数，
    使核心层的每一处失败追加都自动受 _FAILURE_COLLECT_LIMIT 约束，
    无需在核心层为「是否有上限」写任何分支。

    两种压缩手段：
        1) 去重合并：P0-1 触发时整批目标都会失败，且原因字符串**完全相同**
           （都指向同一个源物体）。若不去重，用户会看到 3 条一模一样的文字。
           这里把相同原因折叠为 1 条并记录重复次数。
        2) 长度上限：超出 _FAILURE_COLLECT_LIMIT 后只计数，不再追加文本。

    合并与丢弃的条数分别记录在 merged / dropped 中，
    展示时用「另有 N 项失败」统一补全总数。
    """

    def __init__(self, limit=_FAILURE_COLLECT_LIMIT):
        super().__init__()
        self.limit = limit
        self.merged = 0    # 因原因重复而被合并的条数
        self.dropped = 0   # 因超出长度上限而被丢弃的条数

    @property
    def total(self):
        """本列表代表的失败总条数（含合并与丢弃的）"""
        return len(self) + self.merged + self.dropped

    def append(self, reason):
        # 去重合并：相同原因只保留一条，重复次数累加到 merged
        if reason in self:
            self.merged += 1
            return
        if len(self) < self.limit:
            super().append(reason)
        else:
            self.dropped += 1


def _format_failure_details(failures, total_failures=None):
    """
    把失败明细列表格式化为一行可读文本（P0-5）。

    Args:
        failures: 失败原因列表（_BoundedFailureList 或普通 list）
        total_failures: 实际失败总数。为 None 时取 len(failures)。
            与 len(failures) 不一致时（因收集上限被丢弃），
            补充说明还有多少项失败未展开。

    Returns:
        str: 形如 "A: 原因1； B: 原因2； C: 原因3（另有 57 项失败）" 的文本，
             无失败时返回空字符串
    """
    if not failures:
        return ""

    shown = list(failures)[:_FAILURE_DETAIL_LIMIT]
    detail = "； ".join(shown)

    if total_failures is None:
        total_failures = len(failures)
    remaining = int(total_failures) - len(shown)
    if remaining > 0:
        detail += f"（另有 {remaining} 项失败）"
    return detail


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
            # P0-5: 收集失败原因，结束时聚合展示
            failures = _BoundedFailureList()

            for i, target_obj in enumerate(valid_targets):
                try:
                    # 复制顶点色
                    success = copy_vertex_colors_between_objects(
                        source_obj, target_obj, vc_tool=vc_tool,
                        failure_reasons=failures,
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
                    failures.append(f"{target_obj.name}: {e}")
                    fail_count += 1
                    continue

            # P0-5: 把失败明细展示给用户（面板状态栏 + 状态栏报告）
            failure_detail = _format_failure_details(failures, total_failures=fail_count)
            summary = f"手动复制: 成功 {success_count}, 失败 {fail_count}"
            if failure_detail:
                summary += f" | 失败原因: {failure_detail}"
            vc_tool.last_operation = summary
            self.report(
                {'INFO'},
                f"成功从 {source_obj.name} 复制顶点色到 {success_count} 个物体 ({fail_count} 个失败)"
            )
            if failure_detail:
                self.report({'WARNING'}, f"部分物体复制失败，原因: {failure_detail}")
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

    @classmethod
    def poll(cls, context):
        """
        仅在已有匹配结果时可用（否则按钮变灰）。

        为什么需要: 复制完全依赖上一步「2. 查找匹配」写入的match_results，
        没有结果时点按钮只会得到「请先查找匹配」的报错 toast。
        变灰能让用户立刻看出是前置步骤缺失，而不是自己点错了。

        注意: poll 由 Blender 每帧调用。match_results 是 CollectionProperty，
        取 len() 为 O(1)，不遍历元素，也不做任何异常抛出。
        """
        scene = getattr(context, "scene", None)
        vc_tool = getattr(scene, "vertex_color_tool", None)
        if vc_tool is None:
            return False
        return len(vc_tool.match_results) > 0

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
            # P0-5: 收集失败原因，结束时聚合展示
            failures = _BoundedFailureList()

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
                                missing = []
                                if not source_obj:
                                    missing.append(f"源物体 '{match.source_name}' 不存在")
                                if not target_obj:
                                    missing.append(f"目标物体 '{match.target_name}' 不存在")
                                failures.append("; ".join(missing))
                                continue
                            # 使用共用的复制函数
                            if copy_vertex_colors_between_objects(
                                source_obj, target_obj, vc_tool=vc_tool,
                                failure_reasons=failures,
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
                            failures.append(
                                f"{match.source_name} -> {match.target_name}: {e}"
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
                cache_stats = VertexColorCache.get_cache_stats(vc_tool)

                # 生成统计报告
                stats_report = f"""
性能分析统计:
总耗时: {perf_stats['total_time']:.3f}s
实际复制: {perf_stats['total_copy_time']:.3f}s
平均批处理: {avg_batch_time:.3f}s
平均单对处理: {avg_match_time:.4f}s (max: {max_match_time:.4f}s, min: {min_match_time:.4f}s)
缓存命中率: {cache_stats['hit_rate']:.1f}% ({cache_stats['hits']} 命中, {cache_stats['misses']} 未命中)
缓存内存: {cache_stats['cached_mb']:.1f}MB / {cache_stats['max_cache_mb']:.0f}MB ({cache_stats['cache_size']} 条)
取色路径: {_COLOR_PATH_LABELS.get(cache_stats.get('color_path'), 'Python')}
                """.strip()

                print(stats_report)
                vc_tool.profiling_stats = stats_report

            elapsed_time = time.time() - start_time

            # P0-5: 把失败明细展示给用户（面板状态栏 + 状态栏报告）
            failure_detail = _format_failure_details(failures, total_failures=fail_count)

            if self.cancelled:
                summary = (
                    f"复制已取消: 成功 {success_count}, 失败 {fail_count} ({elapsed_time:.2f}s)"
                )
                if failure_detail:
                    summary += f" | 失败原因: {failure_detail}"
                vc_tool.last_operation = summary
                self.report({'WARNING'}, f"复制被取消: 已成功 {success_count}, 失败 {fail_count}")
                if failure_detail:
                    self.report({'WARNING'}, f"部分物体复制失败，原因: {failure_detail}")
                return {'CANCELLED'}

            summary = f"复制完成: 成功 {success_count}, 失败 {fail_count} ({elapsed_time:.2f}s)"
            if failure_detail:
                summary += f" | 失败原因: {failure_detail}"
            vc_tool.last_operation = summary
            self.report({'INFO'}, f"顶点色复制完成: 成功 {success_count}, 失败 {fail_count} (耗时: {elapsed_time:.2f}秒)")
            if failure_detail:
                self.report({'WARNING'}, f"部分物体复制失败，原因: {failure_detail}")
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


class VERTEXCOLOR_OT_ClearCache(bpy.types.Operator):
    """
    清空顶点色缓存

    存在的理由:
        缓存键是「对象地址 + 层名 + 顶点数」，**不包含颜色值**，
        因此改完源物体的顶点色后再复制会直接命中旧缓存，
        拿到修改前的颜色，且没有任何提示（见 docs/LIMITATIONS.md 限制第 12 条）。
        此前唯一的绕过办法是重开 .blend 文件。

    注意:
        - 不声明 UNDO：清缓存不可撤销（也不需要撤销，缓存本就是可重建的派生数据）。
        - 同时重置命中率统计（clear_cache 内部会归零 hits/misses）。
    """
    bl_idname = "vertexcolor.clear_cache"
    bl_label = "清空缓存"
    bl_options = {'REGISTER'}

    def execute(self, context):
        """
        清空顶点色缓存并反馈清理结果

        Returns:
            set: Blender操作结果
        """
        try:
            vc_tool = context.scene.vertex_color_tool

            # 先取清理前的快照用于反馈。必须传 vc_tool：
            # 不传时 get_cache_stats 读的是类默认预算，而非用户实际设置的上限。
            before = VertexColorCache.get_cache_stats(vc_tool)
            cleared_count = before.get('cache_size', 0)
            freed_mb = before.get('cached_mb', 0.0)

            VertexColorCache.clear_cache()

            if cleared_count > 0:
                message = (
                    f"已清空缓存：{cleared_count} 条条目，"
                    f"释放约 {freed_mb:.1f}MB；命中率统计已重置"
                )
            else:
                message = "缓存已是空的，无需清理（命中率统计已重置）"

            vc_tool.last_operation = message
            self.report({'INFO'}, message)
            return {'FINISHED'}

        except Exception as e:
            report_error(self, context, f"清空缓存时出错: {str(e)}", exc=e)
            return {'CANCELLED'}
