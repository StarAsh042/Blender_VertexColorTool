"""
Blender 3.4 无头冒烟测试

用途:
    在真实 Blender 中加载插件、注册、并跑通核心流程，验证审计修复是否生效。

运行方式:
    "<Blender安装路径>/blender.exe" \
        --background --factory-startup --python tests/blender_smoke_test.py

    例如 Windows 下可能是:
    "C:/Program Files/Blender Foundation/Blender 3.4/blender.exe" ...
    注意必须使用 blender.exe（命令行版），而非 blender-launcher.exe（GUI 启动器）。

覆盖的修复项:
    - 插件注册 / 注销（含自动收集类，P2-15）
    - P0-1 聚类模式返回值解包（原先必然崩溃）
    - P0-2 顶点与颜色取自同一网格，复制结果正确
    - P0-3 通道预览备份层可安全恢复原始色
    - P1-5 缓存键改用对象指针，不同物体不互相污染
    - P1-8 大网格自动启用 KDTree（不退化暴力搜索）
"""

import os
import sys
import traceback

import bpy


# ---------------------------------------------------------------------------
# 测试框架（极简）
# ---------------------------------------------------------------------------

_RESULTS = []


def check(name, condition, detail=""):
    _RESULTS.append((name, bool(condition), detail))
    status = "PASS" if condition else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line)


def run_case(name, func):
    """执行一个测试用例，捕获异常并记为 FAIL"""
    try:
        func()
    except Exception as e:
        _RESULTS.append((name, False, f"异常: {e}"))
        print(f"[FAIL] {name}  -- 抛出异常: {e}")
        traceback.print_exc()


# ---------------------------------------------------------------------------
# 场景搭建辅助
# ---------------------------------------------------------------------------

def make_mesh_object(name, location, color, collection, vertex_count=8):
    """创建一个带顶点色的立方体风格网格物体"""
    verts = [
        (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
        (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
    ][:vertex_count]
    faces = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]

    mesh = bpy.data.meshes.new(f"{name}_mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    # 使用新式颜色属性（Blender 3.2+）
    attr = mesh.color_attributes.new(name="Color", type='FLOAT_COLOR', domain='POINT')
    for data in attr.data:
        data.color = (color[0], color[1], color[2], color[3])

    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    collection.objects.link(obj)
    return obj


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh)


def get_vertex_colors_of(obj):
    """返回物体激活颜色层的颜色列表"""
    mesh = obj.data
    attr = mesh.color_attributes.active_color
    if not attr:
        return []
    return [tuple(round(c, 4) for c in d.color) for d in attr.data]


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

def test_registration(package_name):
    """注册 / 注销 是否正常"""
    import importlib
    addon = importlib.import_module(package_name)

    addon.register()
    check("注册后 Scene 拥有 vertex_color_tool 属性",
          hasattr(bpy.types.Scene, "vertex_color_tool"))

    # 关键算子可用性检查
    # 注意: Blender 会把 Operator 注册到 bpy.types 下「由 bl_idname 派生」的名字，
    # 而非类的 __name__，因此这里统一通过 bpy.ops 命名空间验证。
    for bl_id in ("vertexcolor.find_matches", "vertexcolor.copy_colors",
                  "vertexcolor.clear_vertex_colors", "vertexcolor.preview_channel",
                  "vertexcolor.analyze_groups", "vertexcolor.manual_copy",
                  "vertexcolor.clear_match_results", "vertexcolor.remove_match_result"):
        namespace, _, func_name = bl_id.rpartition(".")
        check(f"算子已注册: {bl_id}",
              hasattr(getattr(bpy.ops, namespace, None), func_name))

    # 面板 / UIList 使用类名注册
    for type_name in ("VERTEXCOLOR_PT_ResultsPanel", "VERTEXCOLOR_UL_MatchResults",
                      "VERTEXCOLOR_PT_MainPanel", "VERTEXCOLOR_PT_OperationsPanel"):
        check(f"类型已注册: {type_name}", hasattr(bpy.types, type_name))

    # 自动收集到的类数量应大于手工列表曾有的 20 个
    check("自动收集的类数量 >= 20", len(addon._registered_classes) >= 20,
          f"实际 {len(addon._registered_classes)} 个")


def test_cluster_match_no_crash():
    """
    P0-1: 聚类模式下查找匹配不得崩溃，且必须产出匹配结果。

    修复前: find_best_match_for_cluster 返回 4 值，调用方只解包 2 个
    → ValueError，被吞异常后 match_results 恒为空。
    """
    import Blender_VertexColorTool  # noqa: F401  确保包已加载
    from Blender_VertexColorTool.core.matching import (
        get_object_features, find_best_match_for_cluster,
    )

    clear_scene()

    coll_a = bpy.data.collections.new("RefGroup")
    coll_b = bpy.data.collections.new("TargetGroup")
    bpy.context.scene.collection.children.link(coll_a)
    bpy.context.scene.collection.children.link(coll_b)

    # 参考组：带颜色的物体
    make_mesh_object("src_1", (0, 0, 0), (1.0, 0.0, 0.0, 1.0), coll_a)
    make_mesh_object("src_2", (0, 0, 0), (0.0, 1.0, 0.0, 1.0), coll_a)

    # 目标组：位置接近的物体
    make_mesh_object("tgt_1", (1, 0, 0), (1.0, 1.0, 1.0, 1.0), coll_b)
    make_mesh_object("tgt_2", (1.2, 0, 0), (1.0, 1.0, 1.0, 1.0), coll_b)

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.collection_a = "RefGroup"
    vc_tool.collection_b = "TargetGroup"
    vc_tool.use_clustering = True
    vc_tool.match_similarity_threshold = 0.0
    vc_tool.min_confidence_score = 0.0
    vc_tool.distance_threshold = 100.0

    # 直接验证函数契约：必须能解包 4 个值
    src_objs = [bpy.data.objects["src_1"], bpy.data.objects["src_2"]]
    tgt_objs = [bpy.data.objects["tgt_1"], bpy.data.objects["tgt_2"]]
    src_feats = {o.name: get_object_features(o) for o in src_objs}
    tgt_feats = {o.name: get_object_features(o) for o in tgt_objs}

    result = find_best_match_for_cluster([0], tgt_objs, tgt_feats, src_objs, src_feats, vc_tool)
    check("find_best_match_for_cluster 返回 4 个值", len(result) == 4,
          f"实际返回 {len(result)} 个")

    # 再通过算子跑完整聚类流程
    bpy.ops.vertexcolor.find_matches()
    check("聚类模式下产生匹配结果（P0-1 修复）", len(vc_tool.match_results) > 0,
          f"match_results = {len(vc_tool.match_results)} 条")

    if len(vc_tool.match_results) > 0:
        first = vc_tool.match_results[0]
        check("匹配结果写入了源顶点色层字段", bool(first.source_vcol_layer),
              f"source_vcol_layer='{first.source_vcol_layer}'")
        check("聚类模式下 cluster_id >= 0", first.cluster_id >= 0,
              f"cluster_id={first.cluster_id}")


def test_noncluster_match():
    """非聚类模式仍能正常工作"""
    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.use_clustering = False

    bpy.ops.vertexcolor.find_matches()
    check("非聚类模式下产生匹配结果", len(vc_tool.match_results) > 0,
          f"match_results = {len(vc_tool.match_results)} 条")


def test_copy_colors_correct():
    """
    P0-2: 复制后目标物体的颜色应等于源物体的颜色。

    修复前: 顶点取自 to_mesh()、颜色取自原始 mesh，带修改器时索引错位。
    """
    from Blender_VertexColorTool.core.vertex_color_ops import copy_vertex_colors_between_objects

    clear_scene()

    coll_a = bpy.data.collections.new("RefGroup")
    coll_b = bpy.data.collections.new("TargetGroup")
    bpy.context.scene.collection.children.link(coll_a)
    bpy.context.scene.collection.children.link(coll_b)

    # 源：纯红色
    src = make_mesh_object("src_red", (0, 0, 0), (1.0, 0.0, 0.0, 1.0), coll_a)
    # 目标：先给白色
    tgt = make_mesh_object("tgt_white", (0, 0, 0), (1.0, 1.0, 1.0, 1.0), coll_b)

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.use_active_vcol = True
    vc_tool.use_kdtree = True
    vc_tool.use_cache = True

    ok = copy_vertex_colors_between_objects(src, tgt, vc_tool=vc_tool)
    check("复制函数返回成功", ok)

    target_colors = get_vertex_colors_of(tgt)
    all_red = all(abs(c[0] - 1.0) < 1e-3 and abs(c[1]) < 1e-3 and abs(c[2]) < 1e-3
                  for c in target_colors)
    check("目标物体颜色被正确复制为源颜色（红色）", all_red,
          f"首色={target_colors[0] if target_colors else 'N/A'}")

    # --- 带修改器场景（P0-2 的核心场景）---
    clear_scene()
    coll_a2 = bpy.data.collections.new("RefGroup")
    coll_b2 = bpy.data.collections.new("TargetGroup")
    bpy.context.scene.collection.children.link(coll_a2)
    bpy.context.scene.collection.children.link(coll_b2)

    src2 = make_mesh_object("src_mod", (0, 0, 0), (0.0, 0.0, 1.0, 1.0), coll_a2)
    # 给源物体加一个改变顶点数的修改器（细分）
    mod = src2.modifiers.new(name="Subdiv", type='SUBSURF')
    mod.levels = 1
    tgt2 = make_mesh_object("tgt_mod", (0, 0, 0), (1.0, 1.0, 1.0, 1.0), coll_b2)

    ok2 = copy_vertex_colors_between_objects(src2, tgt2, vc_tool=vc_tool)
    check("带修改器的源物体复制不崩溃", ok2)

    colors2 = get_vertex_colors_of(tgt2)
    all_blue = all(abs(c[2] - 1.0) < 1e-3 and abs(c[0]) < 1e-3 and abs(c[1]) < 1e-3
                   for c in colors2)
    check("带修改器时颜色仍正确（未静默错配）", all_blue,
          f"首色={colors2[0] if colors2 else 'N/A'}")


def test_preview_channel_backup_restore():
    """
    P0-3: 通道预览后必须能完整恢复原始颜色，且不留残余备份层。
    """
    from Blender_VertexColorTool.utils.vertex_color_utils import (
        PREVIEW_BACKUP_LAYER_NAME, has_preview_backup,
    )

    clear_scene()
    coll = bpy.data.collections.new("PreviewGroup")
    bpy.context.scene.collection.children.link(coll)

    obj = make_mesh_object("preview_obj", (0, 0, 0), (0.2, 0.6, 0.9, 1.0), coll)
    original = get_vertex_colors_of(obj)
    check("预览测试：初始颜色已写入", len(original) > 0)

    # 选中并激活
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    # 切到 R 通道
    bpy.ops.vertexcolor.preview_channel(channel='R')
    after_r = get_vertex_colors_of(obj)
    check("切换到 R 通道后备份层已创建", has_preview_backup(obj.data),
          f"备份层 {PREVIEW_BACKUP_LAYER_NAME}")
    gray_r = all(abs(c[0] - c[1]) < 1e-4 and abs(c[1] - c[2]) < 1e-4 for c in after_r)
    check("R 通道预览为灰度（R=G=B）", gray_r,
          f"首色={after_r[0] if after_r else 'N/A'}")

    # 再切到 G 通道，验证不会层层覆盖
    bpy.ops.vertexcolor.preview_channel(channel='G')
    after_g = get_vertex_colors_of(obj)
    if after_g:
        expected_g = original[0][1] if original else 0
        check("连续切换通道后仍以原始色为数据源",
              abs(after_g[0][0] - expected_g) < 1e-3,
              f"G通道值={after_g[0][0]:.4f}, 原始G={expected_g:.4f}")

    # 恢复完整颜色
    bpy.ops.vertexcolor.preview_channel(channel='RGBA')
    restored = get_vertex_colors_of(obj)
    check("恢复后颜色与原始色一致（P0-3 修复）", restored == original,
          f"恢复={restored[0] if restored else 'N/A'} vs 原始={original[0] if original else 'N/A'}")
    check("恢复后备份层已被清理", not has_preview_backup(obj.data))


def test_cache_key_isolation():
    """
    P1-5: 不同物体的缓存不得互相污染。
    """
    from Blender_VertexColorTool.core.cache import VertexColorCache

    clear_scene()
    coll = bpy.data.collections.new("CacheGroup")
    bpy.context.scene.collection.children.link(coll)

    obj_a = make_mesh_object("cache_a", (0, 0, 0), (1.0, 0.0, 0.0, 1.0), coll)
    obj_b = make_mesh_object("cache_b", (5, 0, 0), (0.0, 1.0, 0.0, 1.0), coll)

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.use_cache = True

    # 说明: Blender 3.4 的 Object 没有 session_uid，且 bpy_struct 不支持 weakref，
    # 因此缓存正确性依赖「条目内对象引用 + is 身份校验」，见下方确定性回归测试。

    data_a = VertexColorCache.get_source_data(obj_a, "Color", vc_tool)
    data_b = VertexColorCache.get_source_data(obj_b, "Color", vc_tool)

    check("两个物体的缓存数据都不是 None", data_a is not None and data_b is not None)

    if data_a and data_b:
        color_a = list(data_a['vertex_colors'].values())[0]
        color_b = list(data_b['vertex_colors'].values())[0]
        check("不同物体的缓存颜色互相隔离（P1-5 修复）",
              abs(color_a[0] - 1.0) < 1e-3 and abs(color_b[1] - 1.0) < 1e-3,
              f"A={color_a}, B={color_b}")

    # --- 回归测试：对象删除后内存地址可能被复用，缓存绝不能误命中 ---
    VertexColorCache.clear_cache()
    coll2 = bpy.data.collections.new("CacheGroup2")
    bpy.context.scene.collection.children.link(coll2)

    temp = make_mesh_object("cache_temp", (0, 0, 0), (1.0, 0.0, 0.0, 1.0), coll2)  # 红色
    VertexColorCache.get_source_data(temp, "Color", vc_tool)
    temp_pointer = temp.as_pointer()
    bpy.data.objects.remove(temp, do_unlink=True)

    fresh = make_mesh_object("cache_fresh", (9, 9, 9), (0.0, 1.0, 0.0, 1.0), coll2)  # 绿色
    fresh_data = VertexColorCache.get_source_data(fresh, "Color", vc_tool)
    fresh_color = list(fresh_data['vertex_colors'].values())[0] if fresh_data else None
    check("对象删除重建后缓存不误命中（as_pointer 复用回归）",
          fresh_color is not None and abs(fresh_color[1] - 1.0) < 1e-3,
          f"新物体颜色={fresh_color}（应为绿色）; "
          f"地址被复用={fresh.as_pointer() == temp_pointer}")

    # --- 确定性回归：手工向缓存投放「同键但身份不符」的条目，验证不会被误用 ---
    # 这直接模拟「地址被复用」的最坏情况，不依赖运行时运气。
    poisoned_key = VertexColorCache._make_cache_key(fresh, "Color")
    VertexColorCache._cache[poisoned_key] = {
        'obj': None,                                   # 身份不匹配
        'vertices': [],
        'vertex_colors': {0: (1.0, 0.0, 0.0, 1.0)},    # 假数据：红色
        'kd': None,
    }
    poisoned = VertexColorCache.get_source_data(fresh, "Color", vc_tool)
    poisoned_color = list(poisoned['vertex_colors'].values())[0] if poisoned else None
    check("同键但身份不符的缓存条目不会被误用（P1-5 确定性回归）",
          poisoned_color is not None and abs(poisoned_color[1] - 1.0) < 1e-3,
          f"返回={poisoned_color}（应为绿色，而非被投毒的红色）")


def test_large_mesh_forces_kdtree():
    """
    P1-8: 顶点数超过阈值时应强制构建 KDTree，避免退化为 O(N×M) 暴力搜索。
    """
    from Blender_VertexColorTool.core.cache import (
        VertexColorCache, BRUTEFORCE_VERTEX_LIMIT,
    )

    clear_scene()
    coll = bpy.data.collections.new("BigGroup")
    bpy.context.scene.collection.children.link(coll)

    # 构造超过阈值的网格
    count = BRUTEFORCE_VERTEX_LIMIT + 10
    mesh = bpy.data.meshes.new("big_mesh")
    verts = [(float(i % 100), float((i // 100) % 100), 0.0) for i in range(count)]
    mesh.from_pydata(verts, [], [])
    mesh.update()
    attr = mesh.color_attributes.new(name="Color", type='FLOAT_COLOR', domain='POINT')
    for d in attr.data:
        d.color = (1.0, 0.0, 0.0, 1.0)

    obj = bpy.data.objects.new("big_obj", mesh)
    coll.objects.link(obj)

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.use_cache = False
    vc_tool.use_kdtree = False   # 故意关闭，验证兜底逻辑

    data = VertexColorCache.get_source_data(obj, "Color", vc_tool)
    check("大网格即使关闭 KDTree 也会构建 KDTree（P1-8 兜底）",
          data is not None and data.get('kd') is not None,
          f"kd={'已构建' if (data and data.get('kd')) else 'None'}，顶点数={count}")


def test_match_result_selection_sync():
    """
    结果联动（新功能）：选中「匹配结果」列表的一行时，场景同步选中
    该条匹配的源/目标物体，且目标物体为活动物体。

    防御路径同样覆盖：物体缺失（改名/删除）保留原选择；索引越界不崩溃。
    注: RNA 的 update 只在值**变化**时触发，故先把索引拨到越界值再拨回，
    保证必然触发两次回调。
    """
    clear_scene()
    coll = bpy.data.collections.new("SyncTest")
    bpy.context.scene.collection.children.link(coll)
    src = make_mesh_object("sync_src", (0, 0, 0), (1.0, 0.0, 0.0, 1.0), coll)
    tgt = make_mesh_object("sync_tgt", (2, 0, 0), (1.0, 1.0, 1.0, 1.0), coll)
    other = make_mesh_object("sync_other", (4, 0, 0), (0.0, 0.0, 1.0, 1.0), coll)

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.match_results.clear()
    entry = vc_tool.match_results.add()
    entry.source_name = "sync_src"
    entry.target_name = "sync_tgt"

    # 前置：选择与活动物体都指向无关的第三方，联动后必须被替换
    bpy.ops.object.select_all(action='DESELECT')
    other.select_set(True)
    bpy.context.view_layer.objects.active = other

    # 触发联动（0 -> 1 越界 no-op，1 -> 0 必触发 update）
    vc_tool.match_results_index = 1
    vc_tool.match_results_index = 0

    check("联动: 源物体被选中", src.select_get())
    check("联动: 目标物体被选中", tgt.select_get())
    check("联动: 目标物体为活动物体",
          bpy.context.view_layer.objects.active is tgt,
          f"active={getattr(bpy.context.view_layer.objects.active, 'name', None)}")
    check("联动: 无关物体被取消选中", not other.select_get())

    # 防御 1: 源/目标都被改名（物体缺失）-> 保留用户当前选择，不误清
    ghost = vc_tool.match_results.add()
    ghost.source_name = "sync_ghost"
    ghost.target_name = "sync_ghost_too"
    vc_tool.match_results_index = 0
    vc_tool.match_results_index = 1   # 0 -> 1 必触发 update，命中缺失分支
    check("防御: 源/目标缺失时保留当前选择",
          src.select_get() and tgt.select_get()
          and bpy.context.view_layer.objects.active is tgt)

    # 防御 2: 索引越界 -> 不崩溃、不动选择
    vc_tool.match_results_index = 99
    check("防御: 索引越界不崩溃且保留原选择",
          src.select_get() and tgt.select_get())

    vc_tool.match_results.clear()


def test_collection_recursion():
    """
    v1.1.0 行为变更：匹配 / 统计范围递归含所有层级子集合。

    直接验证 utils/collection_utils.get_collection_objects 的三条语义：
      1. 递归：嵌套子集合里的物体必须被纳入（旧行为会漏掉）；
      2. 去重：同一物体链接进多个集合时只出现一次；
      3. 文案：统计字符串必须标明「含子集合」，否则用户会以为算错。
    """
    from Blender_VertexColorTool.utils.collection_utils import (
        get_collection_objects, format_collection_stats,
    )

    # 清理可能的同名残留（重跑场景）
    for name in ("RecursionRoot", "RecursionChild", "RecursionGrandchild"):
        coll = bpy.data.collections.get(name)
        if coll:
            bpy.data.collections.remove(coll)

    root = bpy.data.collections.new("RecursionRoot")
    bpy.context.scene.collection.children.link(root)
    child = bpy.data.collections.new("RecursionChild")
    root.children.link(child)
    grandchild = bpy.data.collections.new("RecursionGrandchild")
    child.children.link(grandchild)

    def _cube(name):
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        obj = bpy.context.active_object
        obj.name = name
        return obj

    in_root = _cube("RecursionInRoot")
    in_grandchild = _cube("RecursionInGrandchild")
    in_both = _cube("RecursionInBoth")   # 同时链接进 root 与 grandchild

    # 清掉 primitive 自动链接进 scene collection 的关系，按测试意图重新链接
    for obj in (in_root, in_grandchild, in_both):
        for coll in list(obj.users_collection):
            coll.objects.unlink(obj)
    root.objects.link(in_root)
    grandchild.objects.link(in_grandchild)
    root.objects.link(in_both)
    grandchild.objects.link(in_both)

    names = [o.name for o in get_collection_objects(root)]
    check("递归: 孙集合中的物体被纳入（旧行为会漏掉）",
          "RecursionInGrandchild" in names, f"names={names}")
    check("递归: 根集合直属物体被纳入",
          "RecursionInRoot" in names, f"names={names}")
    check("去重: 同时链接进两个集合的物体只出现一次",
          names.count("RecursionInBoth") == 1, f"names={names}")
    check("总数: 恰为 3 个去重后的物体",
          len(names) == 3, f"names={names}")

    text = format_collection_stats("参考组", 3, 3, 0)
    check("统计文案标明「含子集合」", "含子集合" in text, f"text={text!r}")

    # 清理
    for name in ("RecursionRoot", "RecursionChild", "RecursionGrandchild"):
        coll = bpy.data.collections.get(name)
        if coll:
            bpy.data.collections.remove(coll)
    for obj in (in_root, in_grandchild, in_both):
        bpy.data.objects.remove(obj, do_unlink=True)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    # __file__ 指向 <addon_dir>/tests/blender_smoke_test.py
    # 包名 = addon 目录名；sys.path 需要的是它的「父目录」
    addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package_name = os.path.basename(addon_dir)
    parent_dir = os.path.dirname(addon_dir)

    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    print("=" * 70)
    print(f"Blender {bpy.app.version_string} 无头冒烟测试")
    print(f"包名: {package_name}")
    print(f"插件路径: {addon_dir}")
    print("=" * 70)

    run_case("插件注册", lambda: test_registration(package_name))

    # 后续用例依赖已注册状态
    if not hasattr(bpy.types.Scene, "vertex_color_tool"):
        print("!! 注册失败，跳过后续用例")
    else:
        run_case("P0-1 聚类匹配不崩溃", test_cluster_match_no_crash)
        run_case("非聚类匹配", test_noncluster_match)
        run_case("P0-2 颜色复制正确性", test_copy_colors_correct)
        run_case("P0-3 通道预览备份恢复", test_preview_channel_backup_restore)
        run_case("P1-5 缓存隔离", test_cache_key_isolation)
        run_case("P1-8 大网格 KDTree 兜底", test_large_mesh_forces_kdtree)
        run_case("v1.1.0 集合递归与去重", test_collection_recursion)
        run_case("结果联动选中场景物体", test_match_result_selection_sync)

    # 注销验证
    try:
        addon = sys.modules[package_name]
        addon.unregister()
        check("注销后 Scene 属性被清理",
              not hasattr(bpy.types.Scene, "vertex_color_tool"))
    except Exception as e:
        check("注销插件", False, f"异常: {e}")

    # 汇总
    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = total - passed

    print("=" * 70)
    print(f"测试汇总: {passed}/{total} 通过, {failed} 失败")
    if failed:
        print("失败项:")
        for name, ok, detail in _RESULTS:
            if not ok:
                print(f"  - {name}  ({detail})")
    print("=" * 70)


if __name__ == "__main__":
    main()
