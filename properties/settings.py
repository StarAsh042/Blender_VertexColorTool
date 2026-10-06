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
        description=(
            "源物体所在的集合名称——这些物体**已有**顶点色，颜色将从它们复制出去。"
            "只有网格物体会参与；本项记录的是集合名，"
            "若该集合随后被删除，此处会留下失效名称，需重新指定。"
        )
    )
    collection_b: bpy.props.StringProperty(
        name="目标组",
        description=(
            "目标物体所在的集合名称——这些物体将被**写入**顶点色。"
            "只有网格物体会参与；本项记录的是集合名，"
            "若该集合随后被删除，此处会留下失效名称，需重新指定。"
        )
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
        description=(
            "两个物体**原点（Object Origin）**之间的最大距离，超出则不参与匹配。"
            "注意：这里量的是**物体原点**，不是包围盒中心。"
            "若模型原点在底部、或与另一组原点的位置约定不同"
            "（导出、FBX 导入、手动设原点后常见），"
            "即使两者并排也会因距离超限而匹配不上。"
            "此时可调大本值，或先执行 Object > Transform > Origin to Geometry 对齐原点"
        )
    )

    size_weight: bpy.props.FloatProperty(
        name="尺寸权重",
        default=0.3,
        min=0.0,
        max=1.0,
        description=(
            "包围盒三边尺寸的相似度在综合得分中的权重（尺寸差异越大得分越低）。"
            "调高=更看重体型是否一致，调低=更依赖位置邻近。"
            "权重是**相对值**，内部会按四项之和归一化，因此无需凑成 1.0；"
            "把本项调大等价于按比例调大其余三项。"
            "注意：四项权重全为 0 时总权重为 0，将导致**完全匹配不上任何物体**。"
            "勾选「使用聚类」时本项失效（聚类内部固定等权）。"
        )
    )

    volume_weight: bpy.props.FloatProperty(
        name="体积权重",
        default=0.3,
        min=0.0,
        max=1.0,
        description=(
            "包围盒体积的相似度在综合得分中的权重。"
            "「体积」= 包围盒三边之积（x*y*z），"
            "因此它与「尺寸权重」是**同一信息的重复计分**——"
            "两者同时调高等同于把体型差异算两遍，会让结果偏向体型相近者。"
            "权重是**相对值**，内部会归一化；四项全为 0 会导致**完全匹配不上**。"
            "勾选「使用聚类」时本项失效（聚类内部固定等权）。"
        )
    )

    vertex_count_weight: bpy.props.FloatProperty(
        name="顶点数权重",
        default=0.2,
        min=0.0,
        max=1.0,
        description=(
            "顶点数相似度在综合得分中的权重。"
            "调高=更看重网格密度是否接近（如 LOD 匹配），调低=对细分差异更宽容。"
            "注意：取的是**修改器之前**的顶点数（原始网格 obj.data），"
            "不是求值后的网格——细分/布尔/镜像不会计入。"
            "因此「低模配高模」场景建议调低本项，否则会把正确匹配拉低。"
            "权重是**相对值**，内部会归一化；四项全为 0 会导致**完全匹配不上**。"
            "勾选「使用聚类」时本项失效（聚类内部固定等权）。"
        )
    )

    distance_weight: bpy.props.FloatProperty(
        name="距离权重",
        default=0.2,
        min=0.0,
        max=1.0,
        description=(
            "位置邻近度在综合得分中的权重（距离按指数衰减计分）。"
            "调高=更信任「挨得近的就是同一个」，是形状差异大时最有效的兜底手段。"
            "距离先按两者尺寸归一化，所以不同大小的模型可以合理比较远近。"
            "权重是**相对值**，内部会归一化；四项全为 0 会导致**完全匹配不上**。"
            "勾选「使用聚类」时本项失效（聚类内部固定等权）。"
        )
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
        description=(
            "一键套用一组匹配参数（距离、四个权重、两个阈值、聚类开关）。"
            "「自定义」=不套用任何值，你自己调。"
            "切换预设会**覆盖下方所有参数**（含「使用聚类」），"
            "但不会自动切到「自定义」——微调后记得留意当前预设名可能已与实际值不符。"
            "预设偏保守，日常精调建议切到「自定义」后逐项调整"
        ),
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
        description=(
            "候选的准入门槛：综合相似度**低于**本值的物体直接出局，不进入后续比较。"
            "调高=匹配更少更准，调低=匹配更多但更容易错配。"
            "这是**第一道**过滤，决定了哪些配对有机会被选中。"
        )
    )

    position_decay_factor: bpy.props.FloatProperty(
        name="位置衰减系数",
        default=0.5,
        min=0.1,
        max=2.0,
        description=(
            "距离得分的衰减速度：距离按物体尺寸归一化后，"
            "以 exp(-距离/尺寸 × 本系数) 计分。"
            "值越小→远处的物体分数掉得越快（只认紧邻的）；"
            "值越大→分数越平缓（远处物体仍有竞争力）。"
            "只影响「距离」这一项的计分曲线，不影响最大匹配距离。"
            "注意：勾选「使用聚类」时本项**不生效**（聚类路径固定按 1.0 计算）。"
        )
    )

    min_confidence_score: bpy.props.FloatProperty(
        name="最小置信度",
        default=0.6,
        min=0.0,
        max=1.0,
        description=(
            "结果准入门槛：最佳匹配的得分**低于**本值则不写入匹配结果。"
            "调高=结果更少更保守，调低=保留更多低分匹配（会引入错配）。"
            "重要：这是**第二道**过滤，候选在「相似度阈值」那一步已被淘汰。"
            "因此**单独调低本值通常没有效果**——"
            "必须把「相似度阈值」一起调低才会真正多出匹配。"
            "注意：结果面板上的 80% 分界线只是配色阈值，与本参数无关。"
        )
    )

    use_clustering: bpy.props.BoolProperty(
        name="使用聚类",
        default=False,
        description=(
            "把目标组内相似的物体归为一类，同一类共用同一个源物体的顶点色，"
            "适合成堆的同类资产（砖块/柱子/散件）。"
            "代价：此模式下**尺寸/体积/顶点数/距离四个权重全部失效**"
            "（聚类内部固定等权），「位置衰减系数」也不生效；"
            "分组粒度只能靠「聚类阈值」调整。"
            "注意：切换匹配预设会**静默覆盖**本项（每个预设都带该值）。"
        )
    )

    clustering_threshold: bpy.props.FloatProperty(
        name="聚类阈值",
        default=0.8,
        min=0.5,
        max=1.0,
        description=(
            "聚类模式下的分组粒度：目标物体之间的相似度**达到**本值即归入同一类。"
            "调高=分得更细（类更多更杂），调低=合并得更粗（整堆共用一个源颜色）。"
            "仅在「使用聚类」勾选时可用；"
            "聚类时的距离上限是「最大匹配距离」的一半。"
        )
    )

    # ===== 顶点色参数 =====
    target_vcol_name: bpy.props.StringProperty(
        name="目标顶点色层名称",
        default="Color",
        description=(
            "要复制到的顶点色层名称。目标物体没有该层时会自动创建。"
            "仅在取消勾选「使用激活顶点色层」时生效。"
        )
    )

    use_active_vcol: bpy.props.BoolProperty(
        name="使用激活顶点色层",
        default=True,
        description=(
            "勾选=沿用目标物体当前激活的颜色层（推荐，逐物体可控）；"
            "取消勾选=统一使用下方「指定层名」，没有则自动创建。"
            "注意：本项同时决定**源**物体的取色层——"
            "取消勾选时，参考组里没有该同名层的物体将被跳过（不参与匹配）。"
        )
    )

    use_kdtree: bpy.props.BoolProperty(
        name="使用KDTree加速",
        default=True,
        description="使用KDTree加速最近点搜索，提高大模型的复制速度"
    )

    normal_angle_threshold: bpy.props.FloatProperty(
        name="法线夹角阈值",
        default=75.0,
        min=0.0,
        max=180.0,
        soft_max=120.0,
        description=(
            "防止薄壁模型跨面取色。"
            "取色时若最近顶点与目标点的法线夹角超过该阈值，"
            "会在附近改找朝向一致的顶点，避免墙背面染上正面的颜色。"
            "0 = 完全关闭（退回纯距离最近）。"
            "若某些模型取色变差，可调大该值或直接设为 0 关闭"
        )
    )

    pick_distance_percent: bpy.props.FloatProperty(
        name="取色距离上限",
        default=0.0,
        min=0.0,
        soft_max=30.0,
        max=200.0,
        subtype='PERCENTAGE',
        description=(
            "防止目标超出源范围时把源边缘颜色「拉」出去。"
            "取色时若最近源顶点的距离超过「源包围盒对角线 × 本值」，"
            "该目标顶点**不写入**颜色——即保留它原来的颜色，"
            "让你一眼看出这块超出了源范围，而不是颜色被凭空改掉。"
            "按对角线的百分比而非固定单位，因此换模型尺寸依然有效。"
            "0 = 完全关闭（默认；退回纯距离最近，与未提供该参数时的行为一致）。"
            "参考值：10~20% 通常足够容纳 LOD 差异；"
            "目标与源几乎完全重合时 1~5% 就够。"
            "调太小会漏掉本该上色的顶点（表现为色块缺口），"
            "调太大则等于没开。若某些模型取色变差，可调大或直接设为 0 关闭。"
            "⚠ 开启后会自动改用 Python 取色路径（原生内核暂不支持距离上限），"
            "大模型会变慢"
        )
    )

    use_native_accel: bpy.props.BoolProperty(
        name="原生加速",
        default=True,
        description=(
            "使用 C++ 原生内核加速「物体匹配」与「顶点色复制」"
            "（多线程）。原生库不可用时自动回退纯 Python，不影响功能"
        )
    )

    batch_size: bpy.props.IntProperty(
        name="分批处理数量",
        default=10,
        min=1,
        max=100,
        description=(
            "每批处理的匹配条目数，仅影响「优化内存」开启时的垃圾回收节奏"
            "（每批结束回收一次）与进度条刷新粒度，**不影响匹配或复制结果**。"
            "调小更省内存、界面刷新更频繁；调大更快但内存峰值更高。"
            "大模型批量复制时若感觉界面卡顿，可调小观察。"
        )
    )

    use_cache: bpy.props.BoolProperty(
        name="使用缓存",
        default=True,
        description=(
            "缓存源物体的顶点/颜色/KDTree，重复复制到多个目标时避免重复构建"
            "（大批量复制的主要提速点）。"
            "注意：缓存键**不随源颜色变化而失效**——"
            "改完源物体的顶点色后直接再复制，会命中旧缓存拿到修改前的颜色，"
            "且没有任何提示。此时需在「优化处理」面板点「清空缓存」。"
        )
    )

    optimize_memory: bpy.props.BoolProperty(
        name="优化内存使用",
        default=True,
        description=(
            "分批复制过程中每批结束强制垃圾回收，降低内存峰值"
            "（大模型 + 大量匹配条目时有助于避免 Blender 被系统终止）。"
            "代价是每批多一次回收耗时；关闭可略微提速。"
        )
    )

    cache_memory_budget_mb: bpy.props.IntProperty(
        name="缓存内存上限",
        default=256,
        min=0,
        max=8192,
        soft_max=1024,
        description=(
            "顶点色缓存的内存上限（MB）。"
            "默认 256MB。缓存会为每个源物体保留顶点坐标与颜色，"
            "大模型下单条即可占用数百 MB，本插件运行在你的 Blender 进程内，"
            "上限过高可能被系统直接终止。设为 0 表示使用默认值 256MB。"
            "内存充足时可调高以提升重复复制的速度。"
        )
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
        description=(
            "启用后记录「批量复制」各阶段的耗时与缓存命中率，"
            "结果输出到 Blender 系统控制台，并存入下方「性能统计」文本。"
            "仅在排查性能问题时开启；正常出图无需开启。"
        )
    )

    profiling_stats: bpy.props.StringProperty(
        name="性能统计",
        default="",
        description="存储性能分析统计信息"
    )
