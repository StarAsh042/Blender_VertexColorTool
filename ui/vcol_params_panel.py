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

        # 法线夹角阈值（阶段 B）：防止薄壁模型跨面取色
        col = layout.column(align=True)
        col.prop(vc_tool, "normal_angle_threshold", text="法线夹角阈值")

        # 取色距离上限（阶段 C）：防止超出源范围的顶点被「拉色」
        col.prop(vc_tool, "pick_distance_percent", text="取色距离上限")

        # 两条约束都只在 Python 路径生效（原生内核未实现），
        # 因此>0 时会自动切到 Python 取色——**主动告知代价**，不是报错：
        # 开关的存在本身意味着用户遇到了取色质量问题，正确性优先于速度。
        # 两个开关可能同时开启，故提示框要合并展示并说明恢复方式，
        # 否则用户只关掉其中一个、发现仍然慢，会以为面板在骗人。
        need_python = (vc_tool.normal_angle_threshold > 0
                       or vc_tool.pick_distance_percent > 0)
        if need_python:
            box = col.box()
            reasons = []
            if vc_tool.normal_angle_threshold > 0:
                reasons.append("法线夹角阈值")
            if vc_tool.pick_distance_percent > 0:
                reasons.append("取色距离上限")
            row = box.row()
            row.label(text="已启用：本次使用 Python 取色", icon='INFO')
            row.label(text="大模型可能变慢")
            box.label(text="生效中：" + "、".join(reasons), icon='BLANK1')
            box.label(text="全部设为 0 可恢复原生加速", icon='BLANK1')

        layout.separator(factor=0.5)

        # 性能优化
        col = layout.column(align=True)
        col.label(text="性能:", icon='MOD_BUILD')
        row = col.row(align=True)
        row.prop(vc_tool, "use_cache", text="缓存", toggle=True)
        row.prop(vc_tool, "optimize_memory", text="内存优化", toggle=True)

        # 缓存内存上限（P0-4）
        # 仅在启用缓存时显示：关掉缓存时该设置无意义。
        if vc_tool.use_cache:
            col.prop(vc_tool, "cache_memory_budget_mb", text="缓存内存上限")
            stats = None
            try:
                from ..core.cache import VertexColorCache
                # 必须传 vc_tool，否则分母是类默认预算而非用户实际设置的上限。
                stats = VertexColorCache.get_cache_stats(vc_tool)
            except Exception:
                stats = None
            if stats is not None:
                col.label(
                    text=f"当前缓存: {stats['cached_mb']:.0f}MB / "
                         f"{stats['max_cache_mb']:.0f}MB ({stats['cache_size']} 条)",
                    icon='INFO',
                )
                # 标明当前实际生效的取色路径。
                #
                # cache._active_color_path 自 1.1.0 起已把两条约束
                # （法线/距离）与 numpy 可用性一并纳入判定，这里的
                # need_python 自算与它是同构判据、双保险；若两侧判据
                # 将来分叉，先改 cache 再对齐这里。
                if need_python:
                    col.label(
                        text="取色路径: Python（约束已启用）", icon='INFO')
                else:
                    path = stats.get('color_path')
                    if path == 'native':
                        col.label(text="取色路径: 原生内核",
                                  icon='CHECKMARK')
                    else:
                        # 成因可能是用户关闭加速、dll 缺失、numpy 缺失，
                        # 统称「Python」，不猜具体原因。
                        col.label(text="取色路径: Python", icon='INFO')

                # 清空缓存：缓存键不含颜色值，改过源颜色后必须手动清一次，
                # 否则会命中旧缓存拿到修改前的颜色（无任何提示）。
                row = col.row(align=True)
                row.operator("vertexcolor.clear_cache",
                             text="清空缓存", icon='TRASH')
                if stats['cache_size'] > 0:
                    col.label(text="改过源顶点色后请清空，否则会用旧颜色",
                              icon='INFO')

        layout.separator(factor=0.5)

        # 原生加速（C++ 内核）
        col = layout.column(align=True)
        col.label(text="原生加速:", icon='CPU')
        col.prop(vc_tool, "use_native_accel", text="使用 C++ 内核", toggle=True)

        # 状态提示：让用户明确知道当前走的是原生还是回退路径
        from ..core import native_backend
        if native_backend.is_available():
            col.label(text="已启用（多线程）", icon='CHECKMARK')
        else:
            col.label(text="不可用，已回退纯 Python", icon='ERROR')
            reason = native_backend.describe_failure()
            if reason:
                box = col.box()
                box.label(text=reason[:60], icon='INFO')
