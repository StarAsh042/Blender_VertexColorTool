"""
P0 修复回归测试（批次 1）

运行方式:
    "<Blender安装路径>/blender.exe" \
        --background --factory-startup --python tests/test_p0_fixes.py

覆盖本批次修复的 5 个 P0:
    P0-1 预览状态下复制 -> 灰色被当原色复制到全场（跨集合静默数据损坏）
    P0-2 备份层激活索引漂移 -> RGBA 恢复时把备份删掉，原始色永久丢失
    P0-3 to_mesh() 在异常分支漏掉 to_mesh_clear() -> 内存泄漏
    P0-4 缓存内存上限过宽（实测单条可达数百 MB）-> 被 OS 杀掉
    P0-5 31 处错误日志只有 1 处传 context -> 用户看不到失败原因

设计原则:
    每个用例都尽量「先复现旧bug，再验证修复」，
    避免写出「无论代码对错都会通过」的假阳性断言。
"""

import os
import sys
import traceback

import bpy


_RESULTS = []


def check(name, condition, detail=""):
    """记录并打印一条断言结果"""
    _RESULTS.append((name, bool(condition), detail))
    status = "PASS" if condition else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line)


def info(name, detail=""):
    """记录一条仅供参考的信息，不计入通过/失败"""
    _RESULTS.append((name, None, detail))
    print(f"[INFO] {name}  -- {detail}")


def run_case(name, func):
    """执行一个用例，捕获异常并记为失败"""
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
    """清空场景"""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh)


def make_grid_object(name, location, collection, n=4, color_fn=None):
    """
    创建 n×n 网格平面，带名为 "Color" 的 POINT 域颜色属性。

    Args:
        name: 物体名
        location: 位置
        collection: 所属集合
        n: 每边顶点数（实际 (n-1)×(n-1) 个四边形）
        color_fn: (i, total) -> RGBA

    Returns:
        bpy.types.Object
    """
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
    """读取指定颜色层的全部颜色"""
    mesh = obj.data
    if layer_name in mesh.color_attributes:
        attr = mesh.color_attributes[layer_name]
    else:
        attr = mesh.color_attributes.active_color
    return [tuple(d.color) for d in attr.data]


def set_only_selection(context, obj):
    """只选中 obj 并设为活动物体"""
    for other in context.selected_objects:
        other.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


# ===========================================================================
# P0-1 预览状态下复制 -> 灰色被当原色复制到全场
# ===========================================================================

def test_p0_1_copy_blocked_in_preview_state():
    """
    源物体处于通道预览状态时，复制必须被拒绝，且不得污染目标物体。

    这是本批次最严重的问题：预览算子把灰度写进真实颜色层、原始色留在
    __vct_preview_backup__，而复制走激活层，于是灰度被当作原色
    静默扩散到全场，且函数仍返回 True。
    """
    from Blender_VertexColorTool.core.vertex_color_ops import (
        copy_vertex_colors_between_objects,
    )
    from Blender_VertexColorTool.utils.vertex_color_utils import (
        has_preview_backup, PREVIEW_BACKUP_LAYER_NAME,
    )

    clear_scene()
    coll = bpy.data.collections.new("P0_1")
    bpy.context.scene.collection.children.link(coll)

    # 源物体为纯红色，便于识别污染
    src = make_grid_object(
        "P0_1_Source", (0, 0, 0), coll,
        color_fn=lambda i, total: (1.0, 0.0, 0.0, 1.0),
    )
    targets = [
        make_grid_object(f"P0_1_Target_{k}", (k * 10, 0, 0), coll)
        for k in range(3)
    ]
    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.use_active_vcol = True

    # 先验证正常情况下复制是成功的（建立前置基线，避免「因为别的原因失败」）
    ok_baseline = copy_vertex_colors_between_objects(src, targets[0], vc_tool=vc_tool)
    check("P0-1 前置基线: 非预览态复制成功", ok_baseline,
          f"返回 {ok_baseline}")
    # 基线断言确实生效：targets[0] 应已被复制成源物体的纯红
    baseline0 = get_layer_colors(targets[0])
    baseline0_red = all(abs(c[0] - 1.0) < 1e-6 and abs(c[1]) < 1e-6
                        for c in baseline0)
    check("P0-1 前置基线: targets[0] 确实被写成源物体颜色", baseline0_red,
          f"首色 {tuple(round(c, 4) for c in baseline0[0])}")

    # 关键：记录targets[1] **自己**复制前的颜色。
    # 必须做「同一物体前后对比」——拿A 物体的颜色去比B 物体是跨物体比对，
    # 两物体初始颜色本就不同，断言会恒为 False（假失败），
    # 或在两者恰好相同时恒为 True（假通过），两种都是无效断言。
    target1_before = get_layer_colors(targets[1])
    check("P0-1 记录基线: targets[1] 复制前为原始渐变色",
          target1_before != baseline0,
          f"targets[1] 首色 {tuple(round(c, 4) for c in target1_before[0])}, "
          f"targets[0] 首色 {tuple(round(c, 4) for c in baseline0[0])}")

    # 现在把源物体切到 R 通道预览（灰度写进真实层，原始色留在备份层）
    vcol_layer = src.data.color_attributes["Color"]
    backup = src.data.color_attributes.new(
        name=PREVIEW_BACKUP_LAYER_NAME, type='FLOAT_COLOR', domain='POINT',
    )
    for i in range(len(backup.data)):
        backup.data[i].color = vcol_layer.data[i].color
    # 模拟 edit_ops.PreviewChannel 的灰度写入
    for i in range(len(vcol_layer.data)):
        val = vcol_layer.data[i].color[0]
        vcol_layer.data[i].color = (val, val, val, 1.0)

    check("P0-1 预览态已建立备份层", has_preview_backup(src.data))
    grey = get_layer_colors(src, "Color")[0]
    check("P0-1 真实层确已被写成灰度", abs(grey[0] - grey[1]) < 1e-6,
          f"首个颜色 {tuple(round(c, 4) for c in grey)}")

    # 逐个目标尝试复制，全部必须被拒绝
    all_blocked = True
    for tgt in targets:
        ok = copy_vertex_colors_between_objects(src, tgt, vc_tool=vc_tool)
        if ok:
            all_blocked = False
    check("P0-1 预览态下复制被拒绝（不返回 True）", all_blocked)

    # 关键断言：targets[1] 颜色**与它自己复制前**完全一致（未被灰度污染）
    after = get_layer_colors(targets[1])
    untouched = len(after) == len(target1_before) and all(
        all(abs(a[k] - b[k]) < 1e-6 for k in range(4))
        for a, b in zip(after, target1_before)
    )
    check("P0-1 目标物体未被灰度污染（同物体前后对比）", untouched,
          f"复制前首色 {tuple(round(c, 4) for c in target1_before[0])} -> "
          f"复制后首色 {tuple(round(c, 4) for c in after[0])}")

    # 反向对照：若修复失效，targets[1] 会被写成灰色。
    # 这里显式验证「灰度与原色确实不同」，确保上一条断言不是恒真的。
    # 只比 RGB：原色与灰度的 alpha 恒为 1.0，把 alpha 纳入比较会让
    # all(...) 永不成立、断言恒假（该用例自写下起就不可能通过）。
    grey_would_be = (1.0, 1.0, 1.0)  # 源为纯红，R 通道预览后为全白灰度
    distinguishable = any(
        all(abs(c[k] - grey_would_be[k]) > 1e-6 for k in range(3))
        for c in target1_before
    )
    check("P0-1 断言可证伪: 原色与污染灰度可区分", distinguishable,
          "源为纯红，R 通道预览应产生 (1,1,1,1)，与原渐变色不同")

    # 提示信息必须对用户可见（面板状态栏），而不是只在控制台
    last_op = bpy.context.scene.vertex_color_tool.last_operation
    check("P0-1 失败原因写入面板状态栏", "预览" in last_op,
          f"last_operation = {last_op!r}")


# ===========================================================================
# P0-2 备份层激活索引漂移 -> 原始色永久丢失
# ===========================================================================

def test_p0_2_backup_activation_by_name_not_index():
    """
    残留备份存在时再次备份，激活层必须仍是真实层，不能变成备份层。

    旧bug 时序：备份在索引 0、真实层在索引 1 -> 记下 active_index=1
    -> 删除备份，真实层移到索引 0 -> 新建备份落在索引 1
    -> 用旧索引恢复，激活的变成备份层自身。
    """
    from Blender_VertexColorTool.utils.vertex_color_utils import (
        backup_color_layer, PREVIEW_BACKUP_LAYER_NAME,
    )

    clear_scene()
    coll = bpy.data.collections.new("P0_2")
    bpy.context.scene.collection.children.link(coll)
    obj = make_grid_object(
        "P0_2_Obj", (0, 0, 0), coll,
        color_fn=lambda i, total: (0.0, 1.0, 0.0, 1.0),
    )
    mesh = obj.data

    # 人为构造「残留备份在索引 0、真实层在索引 1」的危险布局
    stale = mesh.color_attributes.new(
        name=PREVIEW_BACKUP_LAYER_NAME, type='FLOAT_COLOR', domain='POINT',
    )
    for i in range(len(stale.data)):
        stale.data[i].color = (0.123, 0.456, 0.789, 1.0)
    mesh.color_attributes.active_index = 0  # 激活的是「残留备份」
    # 再激活真实层，模拟用户正常操作后的状态
    mesh.color_attributes.active_index = 1
    info("P0-2 布局", f"layers={[a.name for a in mesh.color_attributes]}, "
                       f"active_index={mesh.color_attributes.active_color_index}")

    real = mesh.color_attributes["Color"]
    backup_color_layer(mesh, real)

    active_name = mesh.color_attributes.active_color.name
    check("P0-2 备份后激活层仍是真实层", active_name == "Color",
          f"激活层 = {active_name!r}（若为备份层名则RGBA 恢复会删掉原始色）")

    # 再验证原始色确实被完整备份
    backup = mesh.color_attributes[PREVIEW_BACKUP_LAYER_NAME]
    same = all(
        abs(backup.data[i].color[1] - 1.0) < 1e-6
        for i in range(len(backup.data))
    )
    check("P0-2 备份层内容为原始绿色", same)


def test_p0_2_restore_refuses_backup_as_target():
    """
    restore_color_layer 必须拒绝以备份层为恢复目标（第二道防线）。

    若不拒绝，会执行「把备份拷给自己再删除自己」，
    真实层里只剩灰色、原始色随备份层一起消失，且跨 .blend 持久化、无法撤销。
    """
    from Blender_VertexColorTool.utils.vertex_color_utils import (
        backup_color_layer, restore_color_layer, has_preview_backup,
        PREVIEW_BACKUP_LAYER_NAME,
    )

    clear_scene()
    coll = bpy.data.collections.new("P0_2B")
    bpy.context.scene.collection.children.link(coll)
    obj = make_grid_object(
        "P0_2B_Obj", (0, 0, 0), coll,
        color_fn=lambda i, total: (0.0, 0.0, 1.0, 1.0),
    )
    mesh = obj.data
    real = mesh.color_attributes["Color"]
    backup_color_layer(mesh, real)

    backup = mesh.color_attributes[PREVIEW_BACKUP_LAYER_NAME]
    ok = restore_color_layer(mesh, backup)

    check("P0-2 拒绝以备份层为恢复目标", ok is False, f"返回 {ok}")
    check("P0-2 拒绝后备份层仍在（原始色未丢）", has_preview_backup(mesh))
    colors = get_layer_colors(mesh and obj, "Color")
    still_blue = all(abs(c[2] - 1.0) < 1e-6 for c in colors)
    check("P0-2 拒绝后真实层颜色未被破坏", still_blue,
          f"首色 {tuple(round(c, 4) for c in colors[0])}")


def test_p0_2_normal_restore_still_works():
    """回归：正常路径下的 RGBA 恢复必须仍然可用（防止修复过头）"""
    from Blender_VertexColorTool.utils.vertex_color_utils import (
        backup_color_layer, restore_color_layer, has_preview_backup,
    )

    clear_scene()
    coll = bpy.data.collections.new("P0_2C")
    bpy.context.scene.collection.children.link(coll)
    obj = make_grid_object(
        "P0_2C_Obj", (0, 0, 0), coll,
        color_fn=lambda i, total: (1.0, 0.5, 0.25, 1.0),
    )
    mesh = obj.data
    real = mesh.color_attributes["Color"]
    original = get_layer_colors(obj, "Color")

    backup_color_layer(mesh, real)
    # 模拟预览：写成灰度
    for i in range(len(real.data)):
        v = real.data[i].color[0]
        real.data[i].color = (v, v, v, 1.0)
    grey = get_layer_colors(obj, "Color")
    check("P0-2C 预览确实改写了颜色", grey[0] != original[0])

    ok = restore_color_layer(mesh, real)
    restored = get_layer_colors(obj, "Color")
    check("P0-2C 正常恢复成功", ok is True, f"返回 {ok}")
    check("P0-2C 恢复后颜色与原始一致", restored == original)
    check("P0-2C 恢复后备份层已清理", not has_preview_backup(mesh))
    check("P0-2C 恢复后激活层为真实层",
          mesh.color_attributes.active_color.name == "Color",
          f"激活层 = {mesh.color_attributes.active_color.name!r}")


# ===========================================================================
# P0-3 to_mesh() 异常分支漏掉 to_mesh_clear()
# ===========================================================================

class _FakeEvalObject:
    """
    模拟 bpy.types.Object 的 evaluated_get() 返回值。

    精确统计 to_mesh() 与 to_mesh_clear() 的配对情况，
    这是 P0-3 唯一可靠的自动化验证手段。
    """

    def __init__(self, mesh=None, fail_clear=False):
        self._mesh = mesh
        self.fail_clear = fail_clear
        self.to_mesh_calls = 0
        self.to_mesh_clear_calls = 0

    def to_mesh(self):
        self.to_mesh_calls += 1
        return self._mesh

    def to_mesh_clear(self):
        self.to_mesh_clear_calls += 1
        if self.fail_clear:
            raise RuntimeError("模拟 to_mesh_clear 失败")


class _FakeMesh:
    """最小可用的求值网格：空顶点集"""

    def __init__(self):
        self.vertices = []
        self.loops = []
        self.polygons = []
        self.updated = False

    def update(self):
        self.updated = True


class _FakeObject:
    """最小可用的物体：仅提供本测试路径真正用到的属性"""

    def __init__(self, name, eval_obj, mesh):
        self.name = name
        self.type = 'MESH'
        self.data = mesh
        self._eval_obj = eval_obj

    def evaluated_get(self, depsgraph):
        return self._eval_obj


def _run_with_fakes(monkey_targets, source_obj, target_obj, vc_tool):
    """
    在替换掉 vco 的 bpy 依赖后调用 copy_vertex_colors_between_objects。

    Returns:
        (返回值, 源求值对象, 目标求值对象)
    """
    from Blender_VertexColorTool.core import vertex_color_ops as vco

    class FakeContext:
        def evaluated_depsgraph_get(self):
            return "DEPSGRAPH"

        class _Scene:
            vertex_color_tool = None

        scene = _Scene()

    class FakeBpy:
        context = FakeContext()

    originals = {
        'bpy': vco.bpy,
        'get_vcol_layer': vco.get_vcol_layer,
        'get_active_vcol_layer': vco.get_active_vcol_layer,
        'has_preview_backup': vco.has_preview_backup,
        'get_source_data': vco.VertexColorCache.get_source_data,
    }
    vco.bpy = FakeBpy
    vco.has_preview_backup = lambda mesh: False
    vco.get_active_vcol_layer = lambda obj: None
    vco.VertexColorCache.get_source_data = staticmethod(
        lambda obj, layer_name, tool: {
            'vertices': [], 'vertex_colors': {}, 'kd': None, 'native_tree': None,
        }
    )
    try:
        result = vco.copy_vertex_colors_between_objects(
            source_obj, target_obj, vc_tool=vc_tool
        )
    finally:
        vco.bpy = originals['bpy']
        vco.get_vcol_layer = originals['get_vcol_layer']
        vco.get_active_vcol_layer = originals['get_active_vcol_layer']
        vco.has_preview_backup = originals['has_preview_backup']
        vco.VertexColorCache.get_source_data = originals['get_source_data']
    return result


def test_p0_3_to_mesh_cleared_on_exception():
    """
    目标侧：写入阶段抛异常时，求值网格必须仍然被释放（P0-3）。

    旧实现的三处 to_mesh_clear() 都会被外层 except 跳过，
    每个失败目标泄漏一个求值网格（20 万顶点 ≈ 12MB）。
    """
    clear_scene()
    coll = bpy.data.collections.new("P0_3")
    bpy.context.scene.collection.children.link(coll)
    make_grid_object("P0_3_Src", (0, 0, 0), coll)
    make_grid_object("P0_3_Tgt", (10, 0, 0), coll)

    from Blender_VertexColorTool.core import vertex_color_ops as vco

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.use_active_vcol = True

    # 目标层给一个最小可用对象，使流程能走到 _write_colors_to_target
    class FakeLayer:
        name = "Color"
        domain = 'POINT'
        data = []

    original_write = vco._write_colors_to_target
    vco._write_colors_to_target = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("模拟写入阶段异常")
    )

    try:
        leaks = 0
        non_false_results = []
        total_calls = 0
        for _ in range(20):
            mesh = _FakeMesh()
            eval_obj = _FakeEvalObject(mesh)
            src_mesh = _FakeMesh()
            src_eval = _FakeEvalObject(src_mesh)
            src = _FakeObject("Src", src_eval, src_mesh)
            tgt = _FakeObject("Tgt", eval_obj, mesh)

            original_get_vcol = vco.get_vcol_layer
            vco.get_vcol_layer = lambda obj, name=None, create_if_missing=False: FakeLayer()
            try:
                result = _run_with_fakes(None, src, tgt, vc_tool)
            finally:
                vco.get_vcol_layer = original_get_vcol

            total_calls += 1
            if result is not False:
                non_false_results.append(result)
            # 核心断言：to_mesh 被调用了，就必须被 clear 掉。
            # 同时要求 to_mesh 确实被调用过，否则「clear 次数 == 0 == to_mesh 次数」
            # 会让配对断言空转恒真。
            if eval_obj.to_mesh_calls == 0:
                leaks += 1
            elif eval_obj.to_mesh_clear_calls != eval_obj.to_mesh_calls:
                leaks += 1
    finally:
        vco._write_colors_to_target = original_write

    check("P0-3 写入阶段异常时复制返回 False",
          not non_false_results and total_calls == 20,
          f"{total_calls} 次调用中，非 False 返回 {len(non_false_results)} 次: "
          f"{non_false_results[:3]}")
    check("P0-3 目标求值网格在异常路径下仍被释放", leaks == 0,
          f"20 次调用中泄漏 {leaks} 次（to_mesh 未调用或 to_mesh/to_mesh_clear 未配对）")


def test_p0_3_to_mesh_none_not_cleared():
    """
    to_mesh() 返回 None 时不应调用 to_mesh_clear()（P0-3 附带修复）。

    原实现在此处调用 to_mesh_clear()，属于依赖未定义行为
    （对未成功 to_mesh 的对象调用）。
    """
    clear_scene()
    coll = bpy.data.collections.new("P0_3B")
    bpy.context.scene.collection.children.link(coll)
    make_grid_object("P0_3B_Src", (0, 0, 0), coll)
    make_grid_object("P0_3B_Tgt", (10, 0, 0), coll)

    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.use_active_vcol = True

    from Blender_VertexColorTool.core import vertex_color_ops as vco

    class FakeLayer:
        name = "Color"
        domain = 'POINT'
        data = []

    mesh = _FakeMesh()
    eval_obj = _FakeEvalObject(mesh=None)  # to_mesh() 返回 None
    src_mesh = _FakeMesh()
    src = _FakeObject("Src", _FakeEvalObject(src_mesh), src_mesh)
    tgt = _FakeObject("Tgt", eval_obj, mesh)

    original_get_vcol = vco.get_vcol_layer
    vco.get_vcol_layer = lambda obj, name=None, create_if_missing=False: FakeLayer()
    try:
        result = _run_with_fakes(None, src, tgt, vc_tool)
    finally:
        vco.get_vcol_layer = original_get_vcol

    check("P0-3 to_mesh 返回 None 时复制返回 False", result is False, f"返回 {result}")
    check("P0-3 to_mesh 返回 None 时不调用 to_mesh_clear",
          eval_obj.to_mesh_clear_calls == 0,
          f"to_mesh_clear 被调用 {eval_obj.to_mesh_clear_calls} 次（应为 0）")


def test_p0_3_source_to_mesh_cleared_on_exception():
    """
    源侧：_extract_vertex_color_map 抛异常时求值网格也必须释放（P0-3）。

    cache.py 原实现的三处 to_mesh_clear() 同样会被外层 except 跳过。
    """
    from Blender_VertexColorTool.core import cache as cache_mod

    VertexColorCache = cache_mod.VertexColorCache
    VertexColorCache.clear_cache()

    original_extract = cache_mod._extract_vertex_color_map

    def boom(*args, **kwargs):
        raise RuntimeError("模拟颜色提取异常")

    cache_mod._extract_vertex_color_map = boom

    eval_obj = _FakeEvalObject(_FakeMesh())

    class FakeEval:
        def to_mesh(self):
            return _FakeMesh()

        def to_mesh_clear(self):
            eval_obj.to_mesh_clear_calls += 1

    class FakeDepsgraph:
        pass

    class FakeContext:
        def evaluated_depsgraph_get(self):
            return FakeDepsgraph()

    class FakeBpy:
        context = FakeContext()

    class FakeVert:
        co = (0.0, 0.0, 0.0)

    class FakeSrcEvalMesh:
        vertices = [FakeVert()]

    class FakeMatrix:
        def __matmul__(self, other):
            return other

    class FakeSourceObj:
        name = "Src"
        type = 'MESH'

        class data:
            vertices = []

        matrix_world = FakeMatrix()

        def evaluated_get(self, depsgraph):
            return FakeEval()

    class FakeTool:
        use_cache = False
        use_kdtree = False
        use_native_accel = False

    original_bpy = cache_mod.bpy
    cache_mod.bpy = FakeBpy()
    try:
        result = VertexColorCache.get_source_data(
            FakeSourceObj(), "Color", FakeTool()
        )
    finally:
        cache_mod.bpy = original_bpy
        cache_mod._extract_vertex_color_map = original_extract

    check("P0-3源侧 提取异常时返回 None", result is None, f"返回 {result}")
    check("P0-3 源侧异常路径下求值网格仍被释放",
          eval_obj.to_mesh_clear_calls == 1,
          f"to_mesh_clear 被调用 {eval_obj.to_mesh_clear_calls} 次（应为 1）")


# ===========================================================================
# P0-4 缓存内存上限
# ===========================================================================

def test_p0_4_cache_byte_budget():
    """
    缓存必须按估算字节数限流，而不是按顶点数。

    这是纯逻辑校验：直接调用 _store 并检查 _cached_bytes 不超过预算。
    不依赖真实大网格，可在无 Blender 数据的情况下快速验证。
    """
    from Blender_VertexColorTool.core.cache import (
        VertexColorCache, estimate_cache_entry_bytes,
        BYTES_PER_VERTEX_ESTIMATE, CACHE_MEMORY_BUDGET_BYTES,
    )

    VertexColorCache.clear_cache()
    budget = VertexColorCache._max_cache_bytes

    # 注意：这里断言的是「类默认预算 == 模块常量」这一**契约**，
    # 而不是把某个魔法数字固化为基线。
    # BYTES_PER_VERTEX_ESTIMATE 是可重新标定的估算值（QA 复核项 A 要求
    # 不把266/370 之类数字写死成断言），因此只校验它的量级合理性：
    # 必须显著大于单顶点最小开销，且能装下至少十万级顶点。
    check("P0-4 类默认预算与模块常量一致",
          budget == CACHE_MEMORY_BUDGET_BYTES,
          f"类默认 = {budget} B, 模块常量 = {CACHE_MEMORY_BUDGET_BYTES} B")
    check("P0-4 默认预算处于合理量级（128MB ~ 1GB）",
          128 * 1024 * 1024 <= budget <= 1024 * 1024 * 1024,
          f"预算 = {budget / 1024 / 1024:.0f} MB")
    check("P0-4 每顶点估算值量级合理（含 float 载荷，300~500B）",
          300 <= BYTES_PER_VERTEX_ESTIMATE <= 500,
          f"估算 = {BYTES_PER_VERTEX_ESTIMATE} B/顶点"
          f"（QA 实测 Python 侧 260B + numpy/KDTree ≈ 320B，故 370B 保守合理）")
    check("P0-4 估算值不是早期低估版本（必须 >= 300，防止漏算 float 载荷）",
          BYTES_PER_VERTEX_ESTIMATE >= 300,
          f"早期误值为 216/266（漏算 4 个 float 载荷共 96B），"
          f"当前 {BYTES_PER_VERTEX_ESTIMATE}")
    capacity = budget // BYTES_PER_VERTEX_ESTIMATE
    check("P0-4 预算可容纳十万级以上顶点",
          capacity >= 100_000,
          f"约 {capacity:,} 顶点")

    # 关键可证伪断言：写入量远超预算时，总量必须真的被压到预算内。
    # 若限流逻辑失效（例如仍按顶点数而非字节），此断言必然失败。
    entries_written = 20
    verts_each = 200_000
    for i in range(entries_written):
        VertexColorCache._store(
            f"p0_4_obj_{i}", {"vertices": [None] * verts_each}, verts_each, None
        )
    requested = entries_written * verts_each * BYTES_PER_VERTEX_ESTIMATE
    check("P0-4 缓存字节数不超过预算",
          VertexColorCache._cached_bytes <= budget,
          f"请求写入 {requested / 1024 / 1024:.0f}MB，"
          f"实际 {VertexColorCache._cached_bytes / 1024 / 1024:.1f} MB "
          f"/ 预算 {budget / 1024 / 1024:.0f} MB")
    check("P0-4 确实发生了淘汰（请求量远超预算，缓存非空且被压缩）",
          0 < len(VertexColorCache._cache) < entries_written,
          f"写入 {entries_written} 条，实际保留 {len(VertexColorCache._cache)} 条")
    # 反向对照：请求量必须真的超预算，否则「未超预算」这个断言会空转恒真。
    # 若限流逻辑退化为按顶点数（200 万）而非字节，上面的断言即会失败。
    vertex_cap = 2_000_000
    check("P0-4 反向对照: 请求顶点数与字节数都远超预算（旧逻辑会失效）",
          entries_written * verts_each > vertex_cap
          and requested > budget,
          f"请求顶点 {entries_written * verts_each:,} > 旧顶点上限 {vertex_cap:,}；"
          f"请求字节 {requested / 1024 / 1024:.0f}MB > 预算 "
          f"{budget / 1024 / 1024:.0f}MB")
    check("P0-4 字节计数非负且与条目数自洽",
          VertexColorCache._cached_bytes >= 0,
          f"字节={VertexColorCache._cached_bytes}")
    # 交叉核对：字节数必须等于「条目数 × 单条字节」之和。
    # 早期版本只比 _cached_bytes >= 0，那是恒真的（计数只会累加，不会有负数），
    # 无法发现记账被破坏。这里改为与理论值精确比对。
    expected_bytes = (len(VertexColorCache._cache)
                      * verts_each * BYTES_PER_VERTEX_ESTIMATE)
    check("P0-4 字节计数与实际条目数精确自洽（非恒真的>=0 检查）",
          VertexColorCache._cached_bytes == expected_bytes,
          f"记账={VertexColorCache._cached_bytes}, "
          f"理论={expected_bytes}（{len(VertexColorCache._cache)} 条 × "
          f"{verts_each} 顶点 × {BYTES_PER_VERTEX_ESTIMATE}B）")
    check("P0-4 顶点计数与实际条目数自洽",
          VertexColorCache._cached_vertices
          == len(VertexColorCache._cache) * verts_each,
          f"顶点={VertexColorCache._cached_vertices}, "
          f"理论={len(VertexColorCache._cache) * verts_each}")

    # 单个超大条目必须被直接跳过（不缓存）
    before = VertexColorCache._cached_bytes
    huge_verts = 5_000_000
    VertexColorCache._store("p0_4_huge", {"vertices": [None] * huge_verts}, huge_verts, None)
    check("P0-4 超预算单条目不缓存",
          VertexColorCache._cached_bytes == before
          and "p0_4_huge" not in VertexColorCache._cache,
          f"字节数未变化 = {VertexColorCache._cached_bytes == before}，"
          f"该条目单条即{huge_verts * BYTES_PER_VERTEX_ESTIMATE / 1024 / 1024:.0f}MB "
          f"> 预算 {budget / 1024 / 1024:.0f}MB")
    # 反向对照：显式验证「若不跳过则必然超预算」，
    # 否则上面的断言在「估算值偏小到单条也放得下」时会空转恒真。
    huge_bytes = huge_verts * BYTES_PER_VERTEX_ESTIMATE
    check("P0-4 反向对照: 该条目确实单条就超预算（跳过逻辑有意义）",
          huge_bytes > budget,
          f"单条 {huge_bytes / 1024 / 1024:.0f}MB vs 预算 "
          f"{budget / 1024 / 1024:.0f}MB")

    # 条目数上限仍作为第二道防线存在（不硬编码具体值，只校验其为正整数量级）
    check("P0-4 条目数上限仍是有效约束",
          isinstance(VertexColorCache._max_cache_size, int)
          and 1 <= VertexColorCache._max_cache_size <= 1000,
          f"_max_cache_size = {VertexColorCache._max_cache_size}")

    # 统计接口应暴露字节口径，且分母反映**用户实际预算**（QA 复核项 B）
    stats = VertexColorCache.get_cache_stats()
    check("P0-4 统计接口暴露字节信息",
          "cached_bytes" in stats and "max_cache_mb" in stats,
          f"keys 含 {[k for k in stats if 'byte' in k or 'mb' in k]}")

    class BigBudgetTool:
        cache_memory_budget_mb = 1024

    big_stats = VertexColorCache.get_cache_stats(BigBudgetTool())
    check("P0-4 统计分母反映用户实际预算（非类默认值）",
          big_stats["max_cache_mb"] == 1024
          and stats["max_cache_mb"] != big_stats["max_cache_mb"],
          f"默认={stats['max_cache_mb']}MB, 用户设为 1024MB 时={big_stats['max_cache_mb']}MB")

    VertexColorCache.clear_cache()
    check("P0-4 clear_cache 后字节计数归零",
          VertexColorCache._cached_bytes == 0)

    # 估算函数边界：用 BYTES_PER_VERTEX_ESTIMATE 推导期望值，
    # 而不是硬编码 266000 —— 否则重新标定估算值时这条会变成假失败。
    check("P0-4 估算函数边界正确",
          estimate_cache_entry_bytes(0) == 0
          and estimate_cache_entry_bytes(-5) == 0
          and estimate_cache_entry_bytes(1000) == 1000 * BYTES_PER_VERTEX_ESTIMATE,
          f"0->{estimate_cache_entry_bytes(0)}, "
          f"-5->{estimate_cache_entry_bytes(-5)}, "
          f"1000->{estimate_cache_entry_bytes(1000)} "
          f"(= 1000 × {BYTES_PER_VERTEX_ESTIMATE})")


def test_p0_4_budget_override_from_settings():
    """用户设置 cache_memory_budget_mb 应能覆盖默认预算"""
    from Blender_VertexColorTool.core.cache import VertexColorCache

    class FakeTool:
        cache_memory_budget_mb = 64

    resolved = VertexColorCache._resolve_budget_bytes(FakeTool())
    check("P0-4 用户设置可覆盖预算", resolved == 64 * 1024 * 1024,
          f"解析为 {resolved / 1024 / 1024:.0f} MB")

    class BadTool:
        cache_memory_budget_mb = 0

    resolved_default = VertexColorCache._resolve_budget_bytes(BadTool())
    check("P0-4 设置为 0 时回退默认值",
          resolved_default == VertexColorCache._max_cache_bytes,
          f"解析为 {resolved_default / 1024 / 1024:.0f} MB")

    resolved_none = VertexColorCache._resolve_budget_bytes(None)
    check("P0-4 vc_tool 为 None 时回退默认值",
          resolved_none == VertexColorCache._max_cache_bytes)


def test_p0_4_setting_property_registered():
    """新增的 cache_memory_budget_mb 属性必须已注册到 PropertyGroup"""
    # 不能用 bpy.types.VertexColorToolSettings 查找：Python 注册的
    # PropertyGroup 在部分 Blender 版本（实测 3.4.1）不进 bpy.types
    # 命名空间，尽管 RNA 结构体已注册（scene.vertex_color_tool 可用、
    # rna_type.identifier 正确）。从场景指针的实例类型取，等价且跨版本稳定。
    settings_cls = type(bpy.context.scene.vertex_color_tool)
    has_prop = "cache_memory_budget_mb" in settings_cls.bl_rna.properties
    check("P0-4 cache_memory_budget_mb 已注册", has_prop)
    if has_prop:
        default = settings_cls.bl_rna.properties["cache_memory_budget_mb"].default
        check("P0-4 默认值为 256MB", default == 256, f"默认值 = {default}")


# ===========================================================================
# P0-5 错误日志未传 context -> 用户看不到原因
# ===========================================================================

def test_p0_5_log_error_without_context_writes_panel():
    """
    不传 context 调用 log_error，也必须写入面板状态栏。

    这是 P0-5 的核心：全项目 31 处 log_error 中 30 处未传 context，
    若无回退，这些错误只进控制台，美术用户完全看不到。
    """
    from Blender_VertexColorTool.utils.logging_utils import log_error

    clear_scene()
    coll = bpy.data.collections.new("P0_5")
    bpy.context.scene.collection.children.link(coll)
    vc_tool = bpy.context.scene.vertex_color_tool
    vc_tool.last_operation = "初始状态"

    # 不传 context（模拟 core/ 与 operators/ 内的绝大多数调用点）
    log_error("这是一条未传 context 的错误")
    check("P0-5 未传 context 时仍写入面板状态栏",
          "这是一条未传 context 的错误" in vc_tool.last_operation,
          f"last_operation = {vc_tool.last_operation!r}")
    check("P0-5 面板状态带错误标记", vc_tool.last_operation.startswith("⚠"),
          f"last_operation = {vc_tool.last_operation!r}")

    # 传 context 的老路径不能被破坏
    vc_tool.last_operation = "初始状态"
    log_error("这是一条传了 context 的错误", context=bpy.context)
    check("P0-5 传 context 的老路径仍生效",
          "这是一条传了 context 的错误" in vc_tool.last_operation,
          f"last_operation = {vc_tool.last_operation!r}")


def test_p0_5_failure_details_aggregated():
    """批量复制的失败原因必须聚合展示给用户（P0-5 第2 条）"""
    from Blender_VertexColorTool.operators.copy_ops import (
        _format_failure_details, _BoundedFailureList,
    )

    # 空列表
    check("P0-5 无失败时返回空串", _format_failure_details([]) == "")

    # 少量失败：全部展示
    details = _format_failure_details(["A: 无颜色层", "B: 层不存在"])
    check("P0-5 少量失败全部展示",
          "A: 无颜色层" in details and "B: 层不存在" in details,
          details)

    # 大量失败：只展示 3 条 + 总数
    # 断言方式：用「实际入选的前 3 条」逐条核对，而不是数分隔符个数。
    #分隔符计数是弱断言（原因文本里本身可能含分隔符，会误判；
    # 而文本里不含分隔符时 count 恒为 0，条件 0 <= 3 恒真）。
    many = [f"obj_{i}: 原因{i}" for i in range(20)]
    details = _format_failure_details(many, total_failures=20)
    expected_head = "； ".join(many[:3])
    check("P0-5 大量失败只展示前 3 条",
          details.startswith(expected_head)
          and many[3] not in details
          and "obj_19" not in details,
          f"期望前缀={expected_head!r} 实际={details!r}")
    check("P0-5 展示条数恰为 3（不多不少）",
          details.count("；") == 2,
          f"分隔符 '；' 出现 {details.count('；')} 次（3 条 => 2 次）；实际={details!r}")
    check("P0-5 大量失败补充了剩余数量", "另有 17 项失败" in details, details)

    # 收集上限：防止上千目标时无限累积
    bounded = _BoundedFailureList(limit=5)
    for i in range(50):
        bounded.append(f"x{i}")
    check("P0-5 失败明细收集受上限约束", len(bounded) == 5, f"实际 {len(bounded)} 条")
    check("P0-5 被丢弃的条数被记账", bounded.dropped == 45,
          f"dropped = {bounded.dropped}")
    details = _format_failure_details(bounded, total_failures=50)
    check("P0-5 上限截断后总数仍正确", "另有 47 项失败" in details, details)

    # === QA 复核项 C/D：相同原因必须合并 ===
    # P0-1 触发时整批目标失败，原因字符串完全相同（都指向同一源物体）。
    # 若不去重，用户会看到 3 条一模一样的文字。
    dup = _BoundedFailureList(limit=50)
    same = "Src: 源物体处于通道预览状态，请先点「RGBA」恢复"
    for _ in range(30):
        dup.append(same)
    check("P0-5 相同原因被合并为 1 条", len(dup) == 1, f"实际 {len(dup)} 条")
    check("P0-5 合并次数被记账", dup.merged == 29, f"merged = {dup.merged}")
    check("P0-5 合并后 total 仍等于真实失败数", dup.total == 30,
          f"total = {dup.total}（1 条 + 29 合并）")
    dup_details = _format_failure_details(dup, total_failures=30)
    check("P0-5 合并后展示不重复且总数正确",
          dup_details.count(same) == 1 and "另有 29 项失败" in dup_details,
          dup_details)
    # 不同原因必须各自保留，不能被误合并
    distinct = _BoundedFailureList(limit=50)
    distinct.append("A: 原因1")
    distinct.append("B: 原因2")
    distinct.append("A: 原因1")  # 与第一条相同 -> 应被合并
    check("P0-5 去重不影响不同原因共存",
          len(distinct) == 2 and distinct.merged == 1 and distinct.total == 3,
          f"保留 {len(distinct)} 条, 合并 {distinct.merged} 次, total={distinct.total}")


def test_p0_5_operator_reports_preview_failure():
    """
    端到端: 源物体处于预览态时执行手动复制，
    失败原因必须出现在面板状态栏与 self.report 中。
    """
    clear_scene()
    coll = bpy.data.collections.new("P0_5B")
    bpy.context.scene.collection.children.link(coll)

    src = make_grid_object(
        "P0_5B_Src", (0, 0, 0), coll,
        color_fn=lambda i, total: (1.0, 0.0, 0.0, 1.0),
    )
    tgt = make_grid_object("P0_5B_Tgt", (10, 0, 0), coll)

    from Blender_VertexColorTool.utils.vertex_color_utils import (
        PREVIEW_BACKUP_LAYER_NAME,
    )
    vcol_layer = src.data.color_attributes["Color"]
    backup = src.data.color_attributes.new(
        name=PREVIEW_BACKUP_LAYER_NAME, type='FLOAT_COLOR', domain='POINT',
    )
    for i in range(len(backup.data)):
        backup.data[i].color = vcol_layer.data[i].color
        vcol_layer.data[i].color = (0.5, 0.5, 0.5, 1.0)

    context = bpy.context
    for other in context.selected_objects:
        other.select_set(False)
    src.select_set(True)
    tgt.select_set(True)
    # 源物体需为活动物体（手动复制的约定：最后选中的为源）
    context.view_layer.objects.active = src

    tgt_before = get_layer_colors(tgt, "Color")
    vc_tool = context.scene.vertex_color_tool

    # 执行手动复制。预览态下应被拒绝并返回 CANCELLED/FINISHED，
    # 关键是 last_operation 必须包含失败原因（而不是只写「成功 X, 失败 Y」）。
    try:
        result = bpy.ops.vertexcolor.manual_copy()
        info("P0-5B 算子返回值", str(result))
    except Exception as e:
        info("P0-5B 算子抛出异常", f"{type(e).__name__}: {e}")

    last_op = vc_tool.last_operation
    check("P0-5B 预览态复制后状态栏含失败原因",
          "失败" in last_op and "预览" in last_op,
          f"last_operation = {last_op!r}")

    # 计数断言：两个计数必须**同时**正确。
    # 原写法 `A or B` 只满足其一也会通过（半个断言恒真），
    # 例如失败数对但成功数错、或反之，都会漏检。
    check("P0-5B 状态栏计数完整（成功数与失败数同时正确）",
          "成功 0" in last_op and "失败 1" in last_op,
          f"期望同时含 '成功 0' 与 '失败 1'，实际 = {last_op!r}")

    # 反向对照：目标物体颜色未被污染（端到端）
    tgt_after = get_layer_colors(tgt, "Color")
    check("P0-5B 目标物体未被污染（端到端）",
          tgt_after == tgt_before,
          f"复制前首色 {tuple(round(c, 4) for c in tgt_before[0])} -> "
          f"复制后首色 {tuple(round(c, 4) for c in tgt_after[0])}")


# ===========================================================================
# 阶段 A：聚类优化等价性（第 1、2 步：去切片冗余 + 空间网格粗筛）
# ===========================================================================

def test_cluster_optimization_equivalence():
    """
    聚类优化后分组结果必须与优化前完全一致（含簇内顺序）。

    优化点：
        第 1 步去掉 `target_objects[i+1:]` 切片 + 复用粗筛距离
        第 2 步用空间网格哈希把粗筛从 O(n) 降到近似 O(1)

    这里内嵌一份「优化前原实现」作为黄金参考做逐例比对。
    完整压力测试（882 个随机场景 + 11 个边界场景）见
    scripts/verify_cluster_equivalence.py，本用例是其在
    Blender 测试套件中的最小回归版本。

    特别覆盖：部分特征缺失。
        该场景曾暴露一个真实 bug——空间索引返回的是 positions 下标，
        而非 target_objects 下标，特征齐备时两者相同因而掩盖了错误。
    """
    import math
    from mathutils import Vector
    from Blender_VertexColorTool.core.matching import cluster_target_objects

    class _Obj:
        def __init__(self, name):
            self.name = name

    class _Tool:
        def __init__(self, distance_threshold=50.0, clustering_threshold=0.8):
            self.distance_threshold = distance_threshold
            self.clustering_threshold = clustering_threshold

    def _ref_cluster(objs, feats, tool):
        """优化前原实现（黄金参考）"""
        clusters = []
        assigned = set()
        radius = tool.distance_threshold * 0.5
        for i, obj in enumerate(objs):
            if i in assigned:
                continue
            cluster = [i]
            assigned.add(i)
            fi = feats.get(obj.name)
            if not fi:
                continue
            loc_i = fi['location']
            for j, other in enumerate(objs[i + 1:], i + 1):
                if j in assigned:
                    continue
                fj = feats.get(other.name)
                if not fj:
                    continue
                dist = (loc_i - fj['location']).length
                if dist > radius:
                    continue
                avg = fi['bounding_sphere_radius'] + fj['bounding_sphere_radius']
                ds = math.exp(-(dist / avg)) if avg > 0.001 else 1.0
                sd = 0
                for k in range(3):
                    md = max(fi['dimensions'][k], fj['dimensions'][k])
                    if md > 0.001:
                        sd += abs(fi['dimensions'][k] - fj['dimensions'][k]) / md
                size_s = 1.0 - sd / 3.0
                mv = max(fi['volume'], fj['volume'], 0.001)
                vol_s = 1.0 - abs(fi['volume'] - fj['volume']) / mv
                mvt = max(fi['vertex_count'], fj['vertex_count'], 1)
                vtx_s = 1.0 - abs(fi['vertex_count'] - fj['vertex_count']) / mvt
                if min(1.0, (ds + size_s + vol_s + vtx_s) / 4.0) >= tool.clustering_threshold:
                    cluster.append(j)
                    assigned.add(j)
            clusters.append(cluster)
        return clusters

    def _build(n, spread, dim):
        """
        构造 n 个物体，其中每3 个一组放在几乎相同的位置（形成多成员簇），
        组与组之间按 spread 拉开。

        注意: 场景必须**非退化**——若每个物体都单独成簇，
        则空间索引与线性扫描必然给出相同结果，等价性断言就毫无意义。
        因此这里刻意制造重复组，并由下方「场景非退化」断言守护。
        """
        objs, feats = [], {}
        for k in range(n):
            name = f"c{k:04d}"
            objs.append(_Obj(name))
            group = k // 3
            member = k % 3
            jitter = 0.0 if member == 0 else 0.01
            # mathutils.Vector 只接受单个序列参数（Vector((x, y, z))），
            # 传多个位置参数在任何 Blender 版本都会 TypeError。
            objs_loc = Vector((group * spread + jitter, jitter, 0.0))
            feats[name] = {
                'name': name,
                'location': objs_loc,
                'dimensions': Vector((dim, dim, dim)),
                'vertex_count': 100,
                'volume': dim ** 3,
                'bounding_sphere_radius': dim * 0.5,
            }
        return objs, feats

    cases = [
        ("空列表", [], {}, _Tool()),
        ("单元素", *_build(1, 1.0, 1.0), _Tool()),
        ("全部重合于一点", *_build(12, 0.0, 1.0), _Tool()),
        ("标准分布", *_build(60, 2.0, 1.0), _Tool()),
        ("稀疏分布", *_build(60, 60.0, 1.0), _Tool()),
        ("负坐标", *_build(40, 3.0, 1.0), _Tool()),
        ("相似度阈值=0", *_build(30, 2.0, 1.0), _Tool(clustering_threshold=0.0)),
        ("相似度阈值=1", *_build(30, 2.0, 1.0), _Tool(clustering_threshold=1.0)),
    ]

    # 部分特征缺失：曾暴露 positions 下标 vs物体下标 混用的真实 bug
    objs, feats = _build(20, 2.0, 1.0)
    for k in (2, 5, 7, 11):
        feats.pop(objs[k].name, None)
    cases.append(("部分特征缺失", objs, feats, _Tool()))

    # 全部特征缺失
    objs, _ = _build(8, 2.0, 1.0)
    cases.append(("全部特征缺失", objs, {}, _Tool()))

    # 距离阈值为 0（空间索引退化为不建）
    objs, feats = _build(20, 2.0, 1.0)
    cases.append(("距离阈值为 0", objs, feats, _Tool(distance_threshold=0.0)))

    mismatches = []
    for label, objs, feats, tool in cases:
        ref = _ref_cluster(objs, feats, tool)
        cur = cluster_target_objects(objs, feats, tool)
        if ref != cur:
            mismatches.append((label, ref, cur))

    check("聚类优化后分组结果与优化前完全一致", not mismatches,
          f"{len(cases) - len(mismatches)}/{len(cases)} 个用例一致"
          + (f"；不一致: {[m[0] for m in mismatches]}" if mismatches else ""))
    for label, ref, cur in mismatches:
        info(f"聚类差异: {label}", f"ref={ref} vs cur={cur}")

    # 反向对照：确认测试场景本身非退化
    #（若所有物体都单独成簇，比对毫无意义——空间索引与线性扫描都会得到同样结果）
    objs, feats = _build(60, 2.0, 1.0)
    cur = cluster_target_objects(objs, feats, _Tool())
    ref = _ref_cluster(objs, feats, _Tool())
    multi = sum(1 for c in cur if len(c) > 1)
    total_members = sum(len(c) for c in cur)
    check("聚类测试场景非退化（存在多成员簇）", multi > 0 and total_members == len(objs),
          f"簇数={len(cur)}, 多成员簇={multi}, 覆盖成员={total_members}/{len(objs)}")
    check("聚类结果覆盖全部物体且无重复",
          sorted(i for c in cur for i in c) == list(range(len(objs))),
          f"参考实现簇数={len(ref)}，当前实现簇数={len(cur)}")


# ===========================================================================
# 汇总
# ===========================================================================

def main():
    addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package_name = os.path.basename(addon_dir)
    parent_dir = os.path.dirname(addon_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    print("=" * 70)
    print(f"P0 回归测试  Blender {bpy.app.version_string}")
    print(f"包名: {package_name}")
    print("=" * 70)

    import importlib
    addon = importlib.import_module(package_name)
    addon.register()

    run_case("P0-1 预览态复制被拒绝且不污染全场", test_p0_1_copy_blocked_in_preview_state)
    run_case("P0-2 备份按名称锚定激活层", test_p0_2_backup_activation_by_name_not_index)
    run_case("P0-2 拒绝以备份层为恢复目标", test_p0_2_restore_refuses_backup_as_target)
    run_case("P0-2 正常恢复路径未回归", test_p0_2_normal_restore_still_works)
    run_case("P0-3 异常路径不跳过清理", test_p0_3_to_mesh_cleared_on_exception)
    run_case("P0-3 to_mesh 返回 None 不调用 clear", test_p0_3_to_mesh_none_not_cleared)
    run_case("P0-3 源侧异常路径仍释放求值网格", test_p0_3_source_to_mesh_cleared_on_exception)
    run_case("P0-4 缓存按字节限流", test_p0_4_cache_byte_budget)
    run_case("P0-4 预算可被用户设置覆盖", test_p0_4_budget_override_from_settings)
    run_case("P0-4 新增设置项已注册", test_p0_4_setting_property_registered)
    run_case("P0-5 未传 context 也写面板", test_p0_5_log_error_without_context_writes_panel)
    run_case("P0-5 失败原因聚合与上限", test_p0_5_failure_details_aggregated)
    run_case("P0-5 算子端到端报告失败原因", test_p0_5_operator_reports_preview_failure)
    run_case("阶段A 聚类优化等价性（第1、2步）", test_cluster_optimization_equivalence)

    total = sum(1 for _, ok, _ in _RESULTS if ok is not None)
    passed = sum(1 for _, ok, _ in _RESULTS if ok is True)
    failed = sum(1 for _, ok, _ in _RESULTS if ok is False)

    print("=" * 70)
    print(f"P0 回归测试汇总: {passed}/{total} 通过, {failed} 失败")
    if failed:
        print("失败项:")
        for name, ok, detail in _RESULTS:
            if ok is False:
                print(f"  - {name}  ({detail})")
    print("=" * 70)


if __name__ == "__main__":
    main()
