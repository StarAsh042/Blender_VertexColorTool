"""
集合遍历工具模块

背景（为什么需要这个模块）:
    「参考组 / 目标组」在旧实现里遍历的是 `collection.objects`，
    而 `Collection.objects` 的语义是「**直接**位于本集合中的物体」，
    **不含子集合**。于是集合树下还有嵌套子集合时，嵌套里的模型
    既不会被匹配、也不会被统计——用户看到的是一组偏小的数字，
    却查不出原因。

    本模块统一改用 `Collection.all_objects`
    （官方文档: "Objects that are in this collection and its child
    collections"），即「本集合 + 所有层级子集合」，语义与用户在大纲
    视图里展开集合树看到的范围一致。

`all_objects` 语义核实结论:
    1. **只往下递归，不往上**: 含本集合与全部后代子集合，
       不含父集合里那些没被链到本集合的兄弟物体。
    2. **不去重由 Blender 侧保证**, 但 Blender 允许同一物体被同时
       链接进多个集合（大纲视图里 Ctrl 拖拽即可），这是合法用法。
       因此本模块仍显式按物体名去重（见 get_collection_objects），
       不把正确性押在 Blender 版本的实现细节上。

为什么要显式去重（不是多余的防御）:
    下游以 `obj.name` 为键组织特征字典，且「目标物体 → 匹配结果」
    是**逐条追加**的。若同一物体在列表里出现两次：
      · 目标侧会为同一个物体写入**两条** match_results，
        复制阶段就会对同一模型执行两次顶点色写入；
      · 源侧会让 len(source_objects) 虚高，进度条与统计数字失真。
    去重成本是一个 set 的 O(1) 查找，换掉一整类重复写入风险。

已知例外（按名去重的边界）:
    去重键是 `obj.name`。单个 .blend 内物体名唯一，安全；但来自
    **不同链接库（linked library）的物体允许同名**，此时两个不同物体会
    被当作重复、第二个被静默丢弃。这与下游「以名字为键」的设计一致——
    同名物体本就会在特征字典里互相覆盖——本模块只是把这一既有局限
    显式化，不引入新问题。
"""

import bpy  # noqa: F401  (保持与包内其他 utils 模块一致的导入约定)


def get_collection_objects(collection):
    """
    递归获取集合自身与所有层级子集合中的物体（按物体名去重）。

    相比 `collection.objects` 的变化:
        会纳入嵌套子集合里的物体。统计数字因此可能变大——
        这是**修正漏算**，不是新引入的错误。

    Args:
        collection: bpy.types.Collection 实例

    Returns:
        list[bpy.types.Object]: 去重后的物体列表，顺序遵循 Blender
            `all_objects` 的返回顺序。非网格物体也包含在内，
            由调用方按 `type == 'MESH'` 过滤。
    """
    # all_objects 自 Blender 2.8 起提供，本插件要求 3.2+，
    # 因此这里不做 hasattr 兜底：真出问题时应该让上层报错，
    # 而不是静默返回一个空列表、让用户看到「参考组里没有网格物体」
    # 这种与真实原因无关的误导性提示。
    objects = collection.all_objects

    # 显式去重: 同一物体被链接进多个集合时（Blender 的合法用法），
    # 一旦重复进入列表会导致目标侧重复写匹配结果。
    # 用 name 而非对象身份做键: 与下游 source_features/target_features
    # 的键保持一致，且不依赖 bpy_struct 的 __hash__ 行为。
    seen = set()
    unique = []
    for obj in objects:
        name = obj.name
        if name in seen:
            continue
        seen.add(name)
        unique.append(obj)

    return unique


def format_collection_stats(label, total_count, mesh_count, vcol_count):
    """
    生成集合统计文字（面板显示与 toast 提示共用同一份文案）。

    文案里必须写明「含子集合」:
        递归后统计数字会大于用户原先看到的值（原先漏算了嵌套子集合）。
        若不解释，用户会以为插件算错了。诚实标注是这类行为变更的底线。

    Args:
        label: 组名，如 "参考组"
        total_count: 该集合树内的物体总数（已去重，含非网格）
        mesh_count: 其中网格物体的数量
        vcol_count: 其中带顶点色的网格物体数量

    Returns:
        str: 形如「参考组: 12个物体（含子集合） (8个网格, 5个有顶点色)」
    """
    return (
        f"{label}: {total_count}个物体（含子集合） "
        f"({mesh_count}个网格, {vcol_count}个有顶点色)"
    )