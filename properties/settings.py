"""
工具设置数据模型
"""

import bpy
from .match_item import VertexColorMatchItem
from ..utils.logging_utils import log_error


# 匹配预设参数表（数据驱动，P2-14）。
# 新增一套预设只需在此追加一项；新增一个参数也只需在各预设中补一个键，
# 不再需要修改 on_preset_changed 的 if-elif 链。
MATCH_PRESETS = {
    'FAST': {
        'distance_threshold': 50.0,
        'size_weight': 0.3,
        'volume_weight': 0.3,
        'vertex_count_weight': 0.2,
        'distance_weight': 0.2,
        'match_similarity_threshold': 0.7,
        'position_decay_factor': 0.5,
        'min_confidence_score': 0.6,
        'use_clustering': False,
        'clustering_threshold': 0.8,
    },
    'PRECISE': {
        'distance_threshold': 30.0,
        'size_weight': 0.4,
        'volume_weight': 0.4,
        'vertex_count_weight': 0.3,
        'distance_weight': 0.1,
        'match_similarity_threshold': 0.8,
        'position_decay_factor': 0.3,
        'min_confidence_score': 0.7,
        'use_clustering': False,
        'clustering_threshold': 0.9,
    },
    'LOOSE': {
        'distance_threshold': 100.0,
        'size_weight': 0.2,
        'volume_weight': 0.2,
        'vertex_count_weight': 0.1,
        'distance_weight': 0.3,
        'match_similarity_threshold': 0.5,
        'position_decay_factor': 0.8,
        'min_confidence_score': 0.4,
        'use_clustering': False,
        'clustering_threshold': 0.6,
    },
    'CLUSTER': {
        'distance_threshold': 60.0,
        'size_weight': 0.25,
        'volume_weight': 0.25,
        'vertex_count_weight': 0.25,
        'distance_weight': 0.25,
        'match_similarity_threshold': 0.6,
        'position_decay_factor': 0.6,
        'min_confidence_score': 0.5,
        'use_clustering': True,
        'clustering_threshold': 0.7,
    },
}


class VertexColorToolSettings(bpy.types.PropertyGroup):
    """
    工具设置 - 存储插件的所有配置参数

    分类:
        - 集合选择: 参考组和目标组
        - 匹配参数: 控制物体匹配的算法参数
        - 顶点色参数: 控制顶点色复制的参数
        - UI控制: 控制面板显示状态
        - 匹配结果: 存储匹配结果
        - 状态信息: 显示操作状态
    """
    # ===== 集合选择 =====
    collection_a: bpy.props.StringProperty(
        name="参考组",
        description="包含源物体的集合（带有顶点色的物体）"
    )
    collection_b: bpy.props.StringProperty(
        name="目标组",
        description="包含目标物体的集合（需要复制顶点色的物体）"
    )

    # 缓存统计信息
    collection_a_stats: bpy.props.StringProperty(
        name="参考组统计",
        description="参考组的统计信息"
    )
    collection_b_stats: bpy.props.StringProperty(
        name="目标组统计",
        description="目标组的统计信息"
    )

    # ===== 匹配参数 =====
    distance_threshold: bpy.props.FloatProperty(
        name="最大匹配距离",
        default=50.0,
        min=0.0,
        soft_max=200.0,
        description="两个物体中心点之间的最大距离，超过此距离的物体不会被匹配"
    )

    size_weight: bpy.props.FloatProperty(
        name="尺寸权重",
        default=0.3,
        min=0.0,
        max=1.0,
        description="物体尺寸相似性的重要性。值越高，尺寸相似性对匹配影响越大"
    )

    volume_weight: bpy.props.FloatProperty(
        name="体积权重",
        default=0.3,
        min=0.0,
        max=1.0,
        description="物体体积相似性的重要性。值越高，体积相似性对匹配影响越大"
    )

    vertex_count_weight: bpy.props.FloatProperty(
        name="顶点数权重",
        default=0.2,
        min=0.0,
        max=1.0,
        description="顶点数量相似性的重要性。值越高，顶点数相似性对匹配影响越大"
    )

    distance_weight: bpy.props.FloatProperty(
        name="距离权重",
        default=0.2,
        min=0.0,
        max=1.0,
        description="位置距离的重要性。值越高，距离越近的物体越可能匹配"
    )
    
    # 匹配预设
    match_preset_items = [
        ('FAST', "快速匹配", "快速匹配，适合大多数情况", 'PLAY', 0),
        ('PRECISE', "精确匹配", "精确匹配，提高匹配质量", 'ZOOM_IN', 1),
        ('LOOSE', "宽松匹配", "宽松匹配，增加匹配数量", 'ZOOM_OUT', 2),
        ('CLUSTER', "聚类匹配", "使用聚类优化", 'GROUP', 3),
        ('CUSTOM', "自定义", "自定义参数", 'SETTINGS', 4),
    ]
    
    match_preset: bpy.props.EnumProperty(
        name="匹配预设",
        items=match_preset_items,
        default='FAST',
        description="选择匹配预设配置",
        update=lambda self, context: self.on_preset_changed()
    )
    
    def on_preset_changed(self):
        """
        当预设改变时自动应用对应的参数配置（数据驱动版，P2-14）。

        参数表定义在模块级 MATCH_PRESETS 中：新增预设或新增参数
        都不需要再修改本函数。

        支持的预设:
            - FAST: 快速匹配，适合大多数情况
            - PRECISE: 精确匹配，提高匹配质量
            - LOOSE: 宽松匹配，增加匹配数量
            - CLUSTER: 聚类匹配，对相似物体统一处理
            - CUSTOM: 自定义参数（不覆盖任何值）

        说明: 预设激活时会保持高亮显示，用户可以进一步微调参数
        """
        preset = self.match_preset

        # CUSTOM 表示用户自行调参，不覆盖任何已有值
        if preset == 'CUSTOM':
            self.last_operation = "已切换到自定义参数"
            return

        preset_values = MATCH_PRESETS.get(preset)
        if not preset_values:
            return

        try:
            for attr_name, value in preset_values.items():
                setattr(self, attr_name, value)

            # 不再自动切换到CUSTOM模式，保持当前预设高亮显示
            # 用户可以手动切换到CUSTOM来进一步调整参数
            self.last_operation = f"已应用 {preset} 预设配置"

        except Exception as e:
            log_error(f"应用预设 {preset} 时出错", exc=e)
            self.last_operation = "应用预设失败"
    
    # ===== 高级匹配参数 =====
    match_similarity_threshold: bpy.props.FloatProperty(
        name="相似度阈值",
        default=0.7,
        min=0.0,
        max=1.0,
        description="最小相似度阈值。只有相似度超过此值的匹配才会被接受"
    )

    position_decay_factor: bpy.props.FloatProperty(
        name="位置衰减系数",
        default=0.5,
        min=0.1,
        max=2.0,
        description="距离影响的衰减速度。值越小，距离越远的相似物体越可能被匹配"
    )

    min_confidence_score: bpy.props.FloatProperty(
        name="最小置信度",
        default=0.6,
        min=0.0,
        max=1.0,
        description="最小匹配置信度。只有置信度超过此值的匹配才会被接受"
    )

    use_clustering: bpy.props.BoolProperty(
        name="使用聚类",
        default=False,
        description="对目标组内相似物体进行聚类，使它们使用相同的顶点色"
    )

    clustering_threshold: bpy.props.FloatProperty(
        name="聚类阈值",
        default=0.8,
        min=0.5,
        max=1.0,
        description="目标物体之间的相似度阈值，高于此值的物体会被归为一类"
    )

    # ===== 顶点色参数 =====
    target_vcol_name: bpy.props.StringProperty(
        name="目标顶点色层名称",
        default="Color",
        description="要复制到的顶点色层名称"
    )

    use_active_vcol: bpy.props.BoolProperty(
        name="使用激活顶点色层",
        default=True,
        description="复制到目标物体的激活顶点色层，否则使用指定名称的层"
    )

    use_kdtree: bpy.props.BoolProperty(
        name="使用KDTree加速",
        default=True,
        description="使用KDTree加速最近点搜索，提高大模型的复制速度"
    )

    batch_size: bpy.props.IntProperty(
        name="分批处理数量",
        default=10,
        min=1,
        max=100,
        description="每次处理的物体数量，减少内存使用和避免崩溃"
    )

    use_cache: bpy.props.BoolProperty(
        name="使用缓存",
        default=True,
        description="缓存源物体的顶点色数据，提高重复访问的性能"
    )

    optimize_memory: bpy.props.BoolProperty(
        name="优化内存使用",
        default=True,
        description="在处理过程中优化内存使用，减少内存峰值"
    )
    
    # ===== 匹配结果和状态 =====
    match_results: bpy.props.CollectionProperty(
        type=VertexColorMatchItem,
        description="存储所有匹配结果"
    )

    # 匹配结果列表在 UI 中的选中项索引（供结果面板的 UIList 使用，P1-6）。
    # 注: Blender 的 IntProperty 无法设置「随集合长度变化」的动态上限，
    # 因此不设静态 max（静态上限无意义），改为在消费端做越界防护：
    # template_list 自身会钳制索引，remove_match_result 也做了显式边界检查。
    match_results_index: bpy.props.IntProperty(
        name="选中匹配",
        default=0,
        min=0,
        description="匹配结果列表中当前选中的条目索引"
    )

    last_operation: bpy.props.StringProperty(
        name="最后操作",
        description="显示最后一次操作的状态信息"
    )

    picked_color: bpy.props.FloatVectorProperty(
        name="拾取颜色",
        subtype='COLOR',
        size=4,
        min=0.0,
        max=1.0,
        default=(1.0, 1.0, 1.0, 1.0),
        description="拾取的顶点色RGBA值 (红, 绿, 蓝, Alpha透明度)"
    )

    # 说明（P0-3 修复）:
    # 早期版本用 preview_original_colors 这个 StringProperty 以 JSON 形式
    # 存放通道预览前的原始顶点色，一旦 JSON 解析失败会被静默清空，
    # 导致用户原始顶点色永久丢失。现改为在网格上创建真实备份颜色层
    # （见 utils/vertex_color_utils.py 的 backup_color_layer / restore_color_layer），
    # 该属性已废弃并移除。

    # ===== 性能分析 =====
    enable_profiling: bpy.props.BoolProperty(
        name="启用性能分析",
        default=False,
        description="启用后记录各操作的执行时间，用于性能优化"
    )

    profiling_stats: bpy.props.StringProperty(
        name="性能统计",
        default="",
        description="存储性能分析统计信息"
    )
