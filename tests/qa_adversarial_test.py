"""
对抗性验证测试（QA 独立编写，不修改任何插件源码）

运行方式:
    "<Blender安装路径>/blender.exe" \
        --background --factory-startup --python tests/qa_adversarial_test.py

覆盖目标（针对审计修复的薄弱点主动攻击）:
    1. P0-2 边界：源带修改器且顶点数变化（Subsurf）+ 逐顶点渐变色的独立 oracle 比对
    2. P0-3 边界：R->G->B->A->RGBA 连续切换不累积失真；两物体先后预览互不干扰
    3. P1-5 边界：缓存身份校验 + 内存地址复用时 Python wrapper 是否会被复用（决定论证是否成立）
    4. P0-1 边界：>=3 个聚类时 cluster_id 是否合理递增
    5. 注册/注销/再注册幂等
    6. results_panel UIList 注册 + match_results_index 越界健壮性
    7. fill_vertex_colors 编辑模式「只填充选中顶点」行为
"""

import os
import sys
import traceback

import bpy
import bmesh


_RESULTS = []


def check(name, condition, detail=""):
    _RESULTS.append((name, bool(condition), detail))
    status = "PASS" if condition else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line)


def info(name, detail=""):
    _RESULTS.append((name, None, detail))  # None = 仅信息，不计入通过/失败
    print(f"[INFO] {name}  -- {detail}")


def run_case(name, func):
    try:
        func()
    except Exception as e:
        _RESULTS.append((name, False, f"异常: {e}"))
        print(f"[FAIL] {name}  -- 抛出异常: {e}")
        traceback.print_exc()


# ---------------------------------------------------------------------------
# 场景辅助
# ---------------------------------------------------------------------------

def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh)


def make_grid_object(name, location, collection, n=4, color_fn=None):
    """创建 n×n 网格平面；color_fn(i, total) 返回该顶点颜色"""
    mesh = bpy.data.meshes.new(f"{name}_mesh")
    verts = []
    for j in range(n):
        for i in range(n):
            verts.append((float(i), float(j), 0.0))
    faces = []
    for j in range(n - 1):
        for i in range(n - 1):
            a = j * n + i
            b = j * n + i + 1
            c = (j + 1) * n + i + 1
            d = (j + 1) * n + i
            faces.append((a, b, c, d))
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    attr = mesh.color_attributes.new(name="Color", type='FLOAT_COLOR', domain='POINT')
    total = len(mesh.vertices)
    for i, d in enumerate(attr.data):
        if color_fn is None:
            t = i / (total - 1) if total > 1 else 0.0
            d.color = (t, 1.0 - t, 0.5, 1.0)
        else:
            d.color = color_fn(i, total)

    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    collection.objects.link(obj)
    return obj


def get_layer_colors(obj, layer_name="Color"):
    mesh = obj.data
    if layer_name in mesh.color_attributes:
        return [tuple(round(c, 4) for c in d.color) for d in mesh.color_attributes[layer_name].data]
    return []


def evaluated_positions_and_colors(obj, layer_name="Color"):
    """独立 oracle：取求值网格的世界坐标顶点 + 同网格上的颜色"""
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    try:
        pos = [obj.matrix_world @ v.co for v in me.vertices]
        colors = {}
        if layer_name in me.color_attributes:
            attr = me.color_attributes[layer_name]
            for i, d in enumerate(attr.data):
                colors[i] = tuple(d.color)
        return pos, colors
    finally:
        ev.to_mesh_clear()


def nearest_color_oracle(target_positions, src_positions, src_colors):
    """独立实现：对每个目标点，在源求值网格里找最近点并返回其颜色"""
    out = []
    for tp in target_positions:
        best_d = float('inf')
        best_c = (1.0, 1.0, 1.0, 1.0)
        for si, sp in enumerate(src_positions):
            d = (sp - tp).length_squared
            if d < best_d:
                best_d = d
                best_c = src_colors.get(si, (1.0, 1.0, 1.0, 1.0))
        out.append(best_c)
    return out


# ---------------------------------------------------------------------------
# 1. P0-2 边界：修改器改变顶点数 + 渐变色 + 独立 oracle
# ---------------------------------------------------------------------------

def test_p0_2_gradient_subsurf():
    from Blender_VertexColorTool.core.vertex_color_ops import copy_vertex_colors_between_objects

    clear_scene()
    coll_a = bpy.data.collections.new("RefGroup")
    coll_b = bpy.data.collections.new("TargetGroup")
    bpy.context.scene.collection.children.link(coll_a)
    bpy.context.scene.collection.children.link(coll_b)

    # 源：4x4 网格，逐顶点渐变色（每个顶点颜色都不同）
    src = make_grid_object("grad_src", (0, 0, 0), coll_a, n=4)
    # 加细分修改器：顶点数从 16 变为更多（改变拓扑）
    mod = src.modifiers.new(name="Subdiv", type='SUBSURF')
    mod.levels = 2
    mod.render_levels = 2

    # 目标：同位置 4x4 网格（无修改器），先涂成纯白便于识别未写入项
    tgt = make_grid_object("grad_tgt", (0, 0, 0), coll_b, n=4,
                           color_fn=lambda i, t: (1.0, 1.0, 1.0, 1.0))

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.use_active_vcol = True
    vc_tool.use_kdtree = True
    vc_tool.use_cache = False  # 避免缓存干扰

    # 记录源原始顶点数 vs 求值顶点数
    src_eval_pos, src_eval_colors = evaluated_positions_and_colors(src, "Color")
    info("P0-2 顶点数对比",
         f"原始 {len(src.data.vertices)} 顶点 / 求值 {len(src_eval_pos)} 顶点")

    ok = copy_vertex_colors_between_objects(src, tgt, vc_tool=vc_tool)
    check("P0-2 带修改器复制返回成功", ok)

    tgt_colors = get_layer_colors(tgt, "Color")

    # 独立 oracle：用同一份求值网格位置/颜色，对每个目标顶点做最近邻
    tgt_eval_pos, _ = evaluated_positions_and_colors(tgt, "Color")
    oracle = nearest_color_oracle(tgt_eval_pos, src_eval_pos, src_eval_colors)

    # 逐顶点比对
    max_err = 0.0
    mismatch = 0
    for i, (got, exp) in enumerate(zip(tgt_colors, oracle)):
        err = max(abs(got[k] - exp[k]) for k in range(4))
        max_err = max(max_err, err)
        if err > 1e-3:
            mismatch += 1
    check("P0-2 逐顶点颜色与独立 oracle 一致（Subsurf 改变顶点数）",
          mismatch == 0, f"不匹配 {mismatch}/{len(tgt_colors)} 项, 最大误差 {max_err:.5f}")

    # 强否定检查：不应出现「未写入的纯白」残留
    white = [c for c in tgt_colors if c == (1.0, 1.0, 1.0, 1.0)]
    check("P0-2 无未写入的纯白残留（旧 bug 会因索引错位留下白点）",
          len(white) == 0, f"纯白顶点数 {len(white)}")

    # 颜色应有变化（渐变被保留，而非被压成单色）
    distinct = len(set(tgt_colors))
    check("P0-2 渐变被保留（目标颜色多样）", distinct >= 3,
          f"目标不同颜色数 {distinct}")


# ---------------------------------------------------------------------------
# 2. P0-3 边界：连续切换不累积失真 + 两物体互不干扰
# ---------------------------------------------------------------------------

def _select_only(obj):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def test_p0_3_full_cycle_and_two_objects():
    from Blender_VertexColorTool.utils.vertex_color_utils import has_preview_backup

    clear_scene()
    coll = bpy.data.collections.new("PreviewGroup")
    bpy.context.scene.collection.children.link(coll)

    obj1 = make_grid_object("prev_1", (0, 0, 0), coll, n=3)
    obj2 = make_grid_object("prev_2", (10, 0, 0), coll, n=3)

    orig1 = get_layer_colors(obj1, "Color")
    orig2 = get_layer_colors(obj2, "Color")

    # --- 单物体 R->G->B->A 连续切换，每步都应以最初色为源 ---
    _select_only(obj1)
    seq = ['R', 'G', 'B', 'A']
    for ch in seq:
        bpy.ops.vertexcolor.preview_channel(channel=ch)
    after_cycle = get_layer_colors(obj1, "Color")
    # 最后一步是 A 通道，期望等于 orig1 的 alpha（此处均为 1.0）灰度
    expected_a = [tuple([orig1[i][3]] * 3 + [1.0]) for i in range(len(orig1))]
    ok_a = all(
        max(abs(after_cycle[i][k] - expected_a[i][k]) for k in range(4)) < 1e-3
        for i in range(len(orig1))
    )
    check("P0-3 R->G->B->A 连续切换后仍以最初色为源（不累积失真）", ok_a,
          f"A通道首色={after_cycle[0] if after_cycle else 'N/A'}, 期望={expected_a[0] if expected_a else 'N/A'}")

    # --- 两物体先后预览，备份互不干扰 ---
    # 先把 obj1 切到 R（建立备份）
    _select_only(obj1)
    bpy.ops.vertexcolor.preview_channel(channel='R')
    check("P0-3 物体1 预览后存在备份层", has_preview_backup(obj1.data))

    # 再单独预览 obj2（G）
    _select_only(obj2)
    bpy.ops.vertexcolor.preview_channel(channel='G')
    check("P0-3 物体2 预览后存在备份层", has_preview_backup(obj2.data))
    check("P0-3 物体2 预览未污染物体1（物体1 仍有备份）",
          has_preview_backup(obj1.data))

    # 物体2 的备份应等于其最初色
    backup2 = obj2.data.color_attributes["__vct_preview_backup__"]
    b2_colors = [tuple(round(c, 4) for c in d.color) for d in backup2.data]
    check("P0-3 物体2 的备份内容 == 其最初色", b2_colors == orig2,
          f"备份首色={b2_colors[0] if b2_colors else 'N/A'} vs 最初={orig2[0] if orig2 else 'N/A'}")

    # 分别恢复
    _select_only(obj2)
    bpy.ops.vertexcolor.preview_channel(channel='RGBA')
    _select_only(obj1)
    bpy.ops.vertexcolor.preview_channel(channel='RGBA')

    check("P0-3 物体1 恢复后 == 最初色（多轮切换后）", get_layer_colors(obj1, "Color") == orig1,
          f"恢复={get_layer_colors(obj1, 'Color')[0] if get_layer_colors(obj1,'Color') else 'N/A'} vs 最初={orig1[0] if orig1 else 'N/A'}")
    check("P0-3 物体2 恢复后 == 最初色", get_layer_colors(obj2, "Color") == orig2)
    check("P0-3 恢复后两物体均无残留备份层",
          not has_preview_backup(obj1.data) and not has_preview_backup(obj2.data))


# ---------------------------------------------------------------------------
# 3. P1-5 缓存身份校验 + 内存地址复用时 wrapper 身份
# ---------------------------------------------------------------------------

def test_cache_identity_and_wrapper_reuse():
    from Blender_VertexColorTool.core.cache import VertexColorCache

    clear_scene()
    coll = bpy.data.collections.new("CacheGroup")
    bpy.context.scene.collection.children.link(coll)

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.use_cache = True

    # --- 3a. 确定性注入：同键但身份不符 ---
    obj = make_grid_object("cache_obj", (0, 0, 0), coll, n=3,
                           color_fn=lambda i, t: (0.0, 1.0, 0.0, 1.0))  # 绿色
    key = VertexColorCache._make_cache_key(obj, "Color")
    VertexColorCache._cache[key] = {
        'obj': None,
        'vertices': [],
        'vertex_colors': {0: (1.0, 0.0, 0.0, 1.0)},  # 投毒：红色
        'kd': None,
    }
    got = VertexColorCache.get_source_data(obj, "Color", vc_tool)
    got_color = list(got['vertex_colors'].values())[0] if got else None
    check("P1-5 同键身份不符条目不被误用", got_color is not None and abs(got_color[1] - 1.0) < 1e-3,
          f"返回={got_color}（应为绿色）")

    # --- 3b. 内存地址复用 + wrapper 身份（决定论证是否成立的关键实验） ---
    VertexColorCache.clear_cache()
    victim = make_grid_object("victim", (0, 0, 0), coll, n=3,
                              color_fn=lambda i, t: (1.0, 0.0, 0.0, 1.0))  # 红色
    VertexColorCache.get_source_data(victim, "Color", vc_tool)
    victim_ptr = victim.as_pointer()
    victim_wrapper = victim
    bpy.data.objects.remove(victim, do_unlink=True)

    # 关键前置事实：对象删除后其 Python wrapper 是否被失效？
    # 若被失效（访问属性抛 ReferenceError），则即使内存地址被复用，
    # 新对象也会拿到全新 wrapper，`is` 校验必然失败 -> 论证成立。
    wrapper_dead = False
    try:
        _ = victim_wrapper.name
    except ReferenceError:
        wrapper_dead = True
    except Exception:
        pass
    check("P1-5 删除对象后其 Python wrapper 被失效（身份校验因此可靠）",
          wrapper_dead, f"访问已删除对象属性是否抛 ReferenceError={wrapper_dead}")

    reused_ptr = False
    wrapper_reused = False
    false_hit = False
    probe = None
    for k in range(1500):
        probe = make_grid_object(f"probe_{k}", (0, 0, 0), coll, n=3,
                                 color_fn=lambda i, t: (0.0, 1.0, 0.0, 1.0))  # 绿色
        if probe.as_pointer() == victim_ptr:
            reused_ptr = True
            wrapper_reused = (probe is victim_wrapper)
            # 若 wrapper 被复用，身份校验会误判为命中 -> 返回红色（错误）
            d = VertexColorCache.get_source_data(probe, "Color", vc_tool)
            c = list(d['vertex_colors'].values())[0] if d else None
            false_hit = (c is not None and abs(c[0] - 1.0) < 1e-3 and abs(c[1]) < 1e-3)
            bpy.data.objects.remove(probe, do_unlink=True)
            break
        bpy.data.objects.remove(probe, do_unlink=True)

    info("P1-5 内存地址复用实验",
         f"1500 次探测中地址是否被复用={reused_ptr}, wrapper 是否被复用={wrapper_reused}")
    if reused_ptr:
        check("P1-5 地址复用场景下未返回错误数据", not false_hit,
              f"是否误命中旧缓存={false_hit}")
    else:
        info("P1-5 结论", "本次运行未能触发地址复用，3b 的真实复用路径未被运行时验证"
                          "（仅逻辑上由 `is` 校验兜底）")

    # --- 3c. LRU 容量约束（内存泄漏上界） ---
    VertexColorCache.clear_cache()
    for i in range(VertexColorCache._max_cache_size + 20):
        o = make_grid_object(f"lru_{i}", (0, 0, 0), coll, n=2,
                             color_fn=lambda i2, t: (0.1, 0.1, 0.1, 1.0))
        VertexColorCache.get_source_data(o, "Color", vc_tool)
    size = len(VertexColorCache._cache)
    check("P1-5 缓存大小受 _max_cache_size 约束（内存泄漏有上界）",
          size <= VertexColorCache._max_cache_size,
          f"缓存条目={size}, 上限={VertexColorCache._max_cache_size}")

    # --- 3d. 对象删除后缓存是否仍持有引用（泄漏存在性） ---
    VertexColorCache.clear_cache()
    leak_obj = make_grid_object("leak_obj", (0, 0, 0), coll, n=3)
    VertexColorCache.get_source_data(leak_obj, "Color", vc_tool)
    n_before = len(VertexColorCache._cache)
    bpy.data.objects.remove(leak_obj, do_unlink=True)
    n_after = len(VertexColorCache._cache)
    still_referenced = any(e.get('obj') is not None for e in VertexColorCache._cache.values())
    info("P1-5 删除对象后的缓存",
         f"删除前条目={n_before}, 删除后条目={n_after}, 仍持有对象引用={still_referenced}")
    check("P1-5 删除对象后缓存条目不会自动释放（有界泄漏确实存在）",
          n_after == n_before and still_referenced,
          "需靠 LRU 淘汰 / load_post 清空，非自动")


# ---------------------------------------------------------------------------
# 4. P0-1 边界：>=3 个聚类
# ---------------------------------------------------------------------------

def test_p0_1_multi_cluster():
    from Blender_VertexColorTool.core.matching import (
        get_object_features, cluster_target_objects,
    )

    clear_scene()
    coll_a = bpy.data.collections.new("RefGroup")
    coll_b = bpy.data.collections.new("TargetGroup")
    bpy.context.scene.collection.children.link(coll_a)
    bpy.context.scene.collection.children.link(coll_b)

    # 3 个源物体，分别放在 3 个相距很远的区域
    for i in range(3):
        make_grid_object(f"msrc_{i}", (i * 100.0, 0, 0), coll_a, n=3,
                         color_fn=lambda idx, t: (1.0, 0.0, 0.0, 1.0))
    # 3 个目标簇，每簇 2 个靠近的物体
    for i in range(3):
        make_grid_object(f"mtgt_{i}a", (i * 100.0, 1, 0), coll_b, n=3)
        make_grid_object(f"mtgt_{i}b", (i * 100.0, 1.5, 0), coll_b, n=3)

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.collection_a = "RefGroup"
    vc_tool.collection_b = "TargetGroup"
    vc_tool.use_clustering = True
    vc_tool.distance_threshold = 10.0
    vc_tool.match_similarity_threshold = 0.0
    vc_tool.min_confidence_score = 0.0

    bpy.ops.vertexcolor.find_matches()

    results = vc_tool.match_results
    check("P0-1 多聚类产生匹配结果", len(results) > 0, f"结果数={len(results)}")

    cluster_ids = sorted({r.cluster_id for r in results})
    info("P0-1 聚类 ID 集合", f"{cluster_ids}")

    # cluster_id 应从 0 开始合理递增（允许有空簇导致跳号，但必须是升序非负）
    check("P0-1 cluster_id 均为非负且升序", all(c >= 0 for c in cluster_ids)
          and cluster_ids == sorted(cluster_ids),
          f"cluster_ids={cluster_ids}")
    check("P0-1 至少覆盖 2 个不同聚类（多簇场景）", len(cluster_ids) >= 2,
          f"不同聚类数={len(cluster_ids)}")
    check("P0-1 每条结果都写入了源层名",
          all(r.source_vcol_layer for r in results),
          f"示例={results[0].source_vcol_layer if len(results) else 'N/A'}")


# ---------------------------------------------------------------------------
# 5. 注册 / 注销 / 再注册 幂等
# ---------------------------------------------------------------------------

def test_register_unregister_cycle(package_name):
    import importlib
    addon = importlib.import_module(package_name)

    ok_cycles = True
    detail = []
    # 模拟真实「Reload Scripts」场景：反复 unregister -> register
    for cycle in range(3):
        try:
            if hasattr(bpy.types.Scene, "vertex_color_tool"):
                addon.unregister()
            addon.register()
            registered = hasattr(bpy.types.Scene, "vertex_color_tool")
            count = len(addon._registered_classes)
            addon.unregister()
            unregistered = not hasattr(bpy.types.Scene, "vertex_color_tool")
            ok_cycles = ok_cycles and registered and unregistered
            detail.append(f"c{cycle+1}: 类数={count} 注册OK={registered} 注销OK={unregistered}")
        except Exception as e:
            ok_cycles = False
            detail.append(f"c{cycle+1}: 异常 {e}")

    # 最终恢复注册状态供后续测试
    addon.register()
    check("注册/注销/再注册 重复执行不报错（幂等，3 轮）", ok_cycles, "; ".join(detail))
    check("重复注册后仍可正常使用", hasattr(bpy.types.Scene, "vertex_color_tool"))


# ---------------------------------------------------------------------------
# 6. results_panel UIList + match_results_index 越界
# ---------------------------------------------------------------------------

def test_results_panel_and_index():
    check("UIList VERTEXCOLOR_UL_MatchResults 已注册",
          hasattr(bpy.types, "VERTEXCOLOR_UL_MatchResults"))
    check("Panel VERTEXCOLOR_PT_ResultsPanel 已注册",
          hasattr(bpy.types, "VERTEXCOLOR_PT_ResultsPanel"))

    vc_tool = bpy.context.scene.vertex_color_tool
    check("match_results_index 属性存在",
          hasattr(vc_tool, "match_results_index"))

    # 越界写入不应崩溃
    try:
        vc_tool.match_results_index = 9999
        val = vc_tool.match_results_index
        info("越界写入 match_results_index", f"设置 9999 -> 实际 {val}")
        check("越界索引写入不崩溃", True)
    except Exception as e:
        check("越界索引写入不崩溃", False, f"异常 {e}")

    # 空列表时执行 remove 算子应优雅取消。
    # 注意: 算子内 self.report({'ERROR'}, ...) + 返回 CANCELLED 时，
    # bpy.ops 在 Python 侧会抛 RuntimeError（Blender 标准行为，非崩溃）。
    def _invoke_remove():
        try:
            res = bpy.ops.vertexcolor.remove_match_result()
            return res, None
        except RuntimeError as e:
            return 'RAISED', str(e)

    vc_tool.match_results.clear()
    vc_tool.match_results_index = 9999
    res, msg = _invoke_remove()
    check("空列表+越界索引时 remove 算子优雅取消（无崩溃）",
          res in ('RAISED', {'CANCELLED'}), f"返回 {res} {msg or ''}")

    # 有结果但索引越界
    vc_tool.match_results.add()
    vc_tool.match_results_index = 9999
    res, msg = _invoke_remove()
    check("有结果但索引越界时 remove 算子优雅取消（无崩溃）",
          res in ('RAISED', {'CANCELLED'}), f"返回 {res} {msg or ''}")

    # 正常移除
    vc_tool.match_results.add()
    vc_tool.match_results_index = 0
    before = len(vc_tool.match_results)
    res, msg = _invoke_remove()
    check("正常索引可移除一条", len(vc_tool.match_results) == before - 1,
          f"{before} -> {len(vc_tool.match_results)} (返回 {res})")


# ---------------------------------------------------------------------------
# 7. fill_vertex_colors 编辑模式「只填充选中顶点」
# ---------------------------------------------------------------------------

def test_edit_mode_fill_selected_only():
    from Blender_VertexColorTool.core.vertex_color_ops import fill_vertex_colors

    clear_scene()
    coll = bpy.data.collections.new("EditGroup")
    bpy.context.scene.collection.children.link(coll)

    obj = make_grid_object("edit_obj", (0, 0, 0), coll, n=3,
                           color_fn=lambda i, t: (0.2, 0.4, 0.6, 1.0))
    original = get_layer_colors(obj, "Color")

    _select_only(obj)
    bpy.ops.object.mode_set(mode='EDIT')

    # 只选中 3 个顶点（先彻底反选，再仅向上刷）
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    for v in bm.verts:
        v.select = False
    for e in bm.edges:
        e.select = False
    for f in bm.faces:
        f.select = False
    selected_idx = [0, 1, 2]
    for i in selected_idx:
        bm.verts[i].select = True
    bm.select_flush(False)
    bmesh.update_edit_mesh(obj.data)

    vc_tool = bpy.context.scene.vertex_color_tool
    fill_color = (1.0, 0.0, 0.0, 1.0)

    filled_count, selected_count = fill_vertex_colors(bpy.context, [obj], fill_color, vc_tool)
    info("编辑模式填充统计",
         f"filled_object_count={filled_count}, selected_component_count={selected_count}")

    # 关键: 编辑模式下 mesh.color_attributes[x].data 读取为空是 Blender 行为，
    # 必须回到物体模式再读，否则会误判为「数据丢失」。
    mode_still_edit = (obj.mode == 'EDIT')
    bpy.ops.object.mode_set(mode='OBJECT')
    after = get_layer_colors(obj, "Color")

    check("编辑模式：操作后仍处于编辑模式", mode_still_edit, f"fill 后 mode={'EDIT' if mode_still_edit else 'OBJECT'}")

    # 选中顶点应变红
    sel_ok = all(abs(after[i][0] - 1.0) < 1e-3 and abs(after[i][1]) < 1e-3 for i in selected_idx)
    # 未选中顶点应保持原色
    unsel_ok = all(
        abs(after[i][k] - original[i][k]) < 1e-3
        for i in range(len(after)) if i not in selected_idx
        for k in range(4)
    )
    check("编辑模式：选中顶点被填充", sel_ok,
          f"选中顶点颜色={[after[i] for i in selected_idx]}")
    check("编辑模式：未选中顶点保持不变", unsel_ok,
          f"未选中示例={after[5] if len(after) > 5 else 'N/A'}, 原={original[5] if len(original) > 5 else 'N/A'}")

    bpy.ops.object.mode_set(mode='OBJECT')


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package_name = os.path.basename(addon_dir)
    parent_dir = os.path.dirname(addon_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    print("=" * 70)
    print(f"对抗性验证测试  Blender {bpy.app.version_string}")
    print(f"包名: {package_name}")
    print("=" * 70)

    import importlib
    addon = importlib.import_module(package_name)
    addon.register()

    run_case("P0-2 修改器改变顶点数+渐变逐顶点比对", test_p0_2_gradient_subsurf)
    run_case("P0-3 连续切换+双物体互不干扰", test_p0_3_full_cycle_and_two_objects)
    run_case("P1-5 缓存身份校验+wrapper复用+泄漏", test_cache_identity_and_wrapper_reuse)
    run_case("P0-1 多聚类", test_p0_1_multi_cluster)
    run_case("注册/注销幂等", lambda: test_register_unregister_cycle(package_name))
    run_case("results_panel + 越界索引", test_results_panel_and_index)
    run_case("编辑模式只填充选中顶点", test_edit_mode_fill_selected_only)

    total = sum(1 for _, ok, _ in _RESULTS if ok is not None)
    passed = sum(1 for _, ok, _ in _RESULTS if ok is True)
    failed = sum(1 for _, ok, _ in _RESULTS if ok is False)

    print("=" * 70)
    print(f"对抗性测试汇总: {passed}/{total} 通过, {failed} 失败")
    if failed:
        print("失败项:")
        for name, ok, detail in _RESULTS:
            if ok is False:
                print(f"  - {name}  ({detail})")
    print("=" * 70)


if __name__ == "__main__":
    main()
