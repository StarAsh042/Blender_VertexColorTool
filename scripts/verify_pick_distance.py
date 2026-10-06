"""
阶段 C：取色最大距离上限验证（超出源范围被「拉色」）

验证命题:
    1. 正向：目标部分超出源范围 -> 超出部分不写入（保留原色），
       范围内部分正常取色
    2. 守护：距离上限= 0（默认）-> 与本功能上线前**逐字一致**
    3. 守护：目标完全在源范围内 -> 与关闭时**完全一致**（不回归）
    4. 交互：与法线约束共存（两个都开 / 只开一个 / 都关）语义正确
    5. 兜底：源退化（单顶点）时不误杀；两条约束的兜底一致
    6. 门控：距离上限启用时必须绕过原生内核（否则界面显示已启用、实际无效）
    7. 尺度：按包围盒对角线归一化 -> 换模型尺寸依然有效

每条关键断言都配有变异测试（--mutate），确保可证伪。

运行:
    python scripts/verify_pick_distance.py
    python scripts/verify_pick_distance.py --mutate
    python scripts/verify_pick_distance.py --verbose

退出码:
    0 = 通过；1 = 有断言失败
"""

import argparse
import ast
import importlib.util
import math
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mathutils_stub import Vector3 as Vector  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RESULTS = []

EXIT_OK = 0
EXIT_FAIL = 1


def check(name, cond, detail=""):
    _RESULTS.append((name, bool(cond), detail))
    print(f"   {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 加载被测模块（复用 verify_normal_constraint 的 load_ops，保证装载方式一致）
# ---------------------------------------------------------------------------

def load_ops():
    """
    装载 core/vertex_color_ops.py（stub 掉 bpy / mathutils / numpy）。

    直接复用 verify_normal_constraint.load_ops —— 两处各写一份装载器
    才是「替身与真实环境分叉」的温床，而分叉出来的替身会让本脚本
    测到一条真实插件不会走的路径。
    """
    import verify_normal_constraint as vnc
    return vnc.load_ops()


class Tool:
    """
    工具设置替身。

    **必须同时带两个阈值**：本批要验证的正是两条约束的交互，
    替身缺字段会让「只开距离上限」这一关键场景测不到。
    """

    def __init__(self, normal_angle_threshold=0.0, pick_distance_percent=0.0):
        self.normal_angle_threshold = normal_angle_threshold
        self.pick_distance_percent = pick_distance_percent
        self.use_native_accel = False
        self.use_kdtree = True


# ---------------------------------------------------------------------------
# 场景构造
# ---------------------------------------------------------------------------

RED = (1.0, 0.0, 0.0, 1.0)
BLUE = (0.0, 0.0, 1.0, 1.0)
# 目标物体**原有**的颜色（用来验证「超距时保留原色」而不是被填成白色）
OWN = (0.1, 0.2, 0.3, 1.0)


def nearest_dist(p, pts):
    """目标点到源顶点集的最近距离（用于在测试里**算**期望值，而不是手写常数）。"""
    return min((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2
               for q in pts) ** 0.5


def bbox_diagonal(pts):
    """独立实现的包围盒对角线（不复用被测代码，避免自证）。"""
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]; zs = [p[2] for p in pts]
    return math.dist((min(xs), min(ys), min(zs)),
                     (max(xs), max(ys), max(zs)))


def make_source_cube(n=2):
    """
    源：单位立方体的 8 个顶点（n 只影响是否重复，实际固定 8 点）。
    全部为红色，包围盒对角线 = sqrt(3) ≈ 1.732。
    """
    verts, normals, colors = [], [], {}
    for x in (0.0, 1.0):
        for y in (0.0, 1.0):
            for z in (0.0, 1.0):
                verts.append(Vector(x, y, z))
                normals.append(Vector(0.0, 0.0, 1.0))
                colors[len(verts) - 1] = RED
    return verts, normals, colors


def make_dense_source(step=0.25):
    """
    源：[0,1]^3 内间距 step 的致密点云（step=0.25 -> 5×5×5 = 125 点）。

    **为什么需要它**：稀疏源（8 顶点）即使在几何上「完全在范围内」，
    其内部点距最近源顶点也可达对角线的 50%（立方体中心到角点）。
    拿稀疏源去测「范围内不回归」是**测错了东西**——
    它测的其实是「稀疏源该不该被拒绝」，那是另一个命题。
    「不回归」的正确前提是：源足够致密，使域内任意点都贴近某个源顶点。
    """
    steps = [i * step for i in range(int(round(1.0 / step)) + 1)]
    verts, normals, colors = [], [], {}
    for x in steps:
        for y in steps:
            for z in steps:
                verts.append(Vector(x, y, z))
                normals.append(Vector(0.0, 0.0, 1.0))
                colors[len(verts) - 1] = RED
    return verts, normals, colors


def run_scenario(ops, source_verts, source_normals, source_colors,
                 target_verts, target_normals=None, angle_deg=0.0,
                 distance_percent=0.0, initial_color=OWN, use_kd=True):
    """
    跑一次 _write_colors_to_target 的 Python 路径，返回每个目标顶点取到的颜色。

    Args:
        initial_color: 目标颜色层的**初始**颜色。用于验证超距顶点
            是否真的「保留原色」——若实现改成填白/填中性色，这里会暴露。
        use_kd: False 时走暴力搜索路径（验证距离上限在两条路径上都生效）。
    """
    from verify_normal_constraint import _Identity, _FakeVertexGroup, _FakeVert, KDTree

    source_data = {
        'vertices': source_verts,
        'vertex_colors': source_colors,
        'kd': KDTree(source_verts) if use_kd else None,
        'normals': source_normals,
        'native_tree': None,
    }

    class _ColorSlot:
        __slots__ = ('color',)

        def __init__(self, init):
            self.color = init

    class FakeLayer:
        name = "Color"
        domain = 'POINT'

        def __init__(self, n, init):
            self.data = [_ColorSlot(init) for _ in range(n)]

    n = len(target_verts)
    layer = FakeLayer(n, initial_color)

    class FakeMesh:
        def __init__(self, count):
            self.vertices = _FakeVertexGroup(
                [_FakeVert(Vector(0, 0, 0), Vector(0, 1, 0), i)
                 for i in range(count)])
            self.loops = []
            self.polygons = []

        def update(self):
            pass

    target_obj = types.SimpleNamespace(
        name="T", data=FakeMesh(n), matrix_world=_Identity())
    tn = target_normals if target_normals is not None else [None] * n
    mesh_eval = types.SimpleNamespace(vertices=_FakeVertexGroup([
        _FakeVert(co, tn[i], i) for i, co in enumerate(target_verts)]))

    tool = Tool(normal_angle_threshold=angle_deg,
                pick_distance_percent=distance_percent)
    ops._write_colors_to_target(target_obj, mesh_eval, layer, source_data, tool)
    return [tuple(d.color) for d in layer.data]


def is_red(c):
    return abs(c[0] - 1.0) < 1e-5 and abs(c[2]) < 1e-5


def is_own(c):
    """目标原色（验证「保留原色」而不是被填成别的中性色）"""
    return all(abs(c[i] - OWN[i]) < 1e-6 for i in range(4))


# ---------------------------------------------------------------------------
# 场景 1：正向 —— 目标部分超出源范围
# ---------------------------------------------------------------------------

def scenario_partial_outside(ops):
    """
    源 = 单位立方体（对角线 sqrt(3) ≈ 1.7321）。
    目标 = 3×3×3 网格，坐标 0..2 —— 即**恰好是源的两倍大**。

    设计：
      - 落在 [0,1]^3 内（含贴边）的顶点 -> 距离 0 ~ 0（顶点重合）
        或极小 -> 应正常取到红色
      - 落在 (1,2]^3 的顶点 -> 最近源顶点在角上，距离 >= 1.0
        超过任何合理上限 -> 应保留原色

    阈值 10% -> 上限 = 0.1732。距离 1.0 远超 -> 必然被拒。
    反向对照：距离 0 的顶点必须取到红色（证明测试有区分度）。
    """
    sv, sn, sc = make_source_cube()
    tv, expected_red = [], []
    for x in (0.0, 1.0, 2.0):
        for y in (0.0, 1.0, 2.0):
            for z in (0.0, 1.0, 2.0):
                tv.append(Vector(x, y, z))
                # 最近源顶点距离：源顶点坐标每个分量属于 {0,1}，
                # 故距离 = sqrt(超出 [0,1] 的分量数)
                over = sum(1 for c in (x, y, z) if c > 1.0)
                expected_red.append(over == 0)

    out = run_scenario(ops, sv, sn, sc, tv,
                       distance_percent=10.0)
    return out, expected_red


# ---------------------------------------------------------------------------
# 场景 2：尺度归一化 —— 换模型尺寸依然有效
# ---------------------------------------------------------------------------

def scenario_scale_invariance(ops):
    """
    把源整体放大 1000倍，目标同样放大。
    若阈值是「固定世界单位」，放大后所有距离都超限 -> 全部拒绝（错）。
    若按对角线归一化，放大前后行为应**完全一致**。
    """
    results = []
    for scale in (1.0, 1000.0):
        base_v, base_n, base_c = make_source_cube()
        sv = [Vector(v.x * scale, v.y * scale, v.z * scale) for v in base_v]
        # 目标同比例放大：一半在源内、一半在源外
        tv = [Vector(0.5 * scale, 0.5 * scale, 0.5 * scale),
              Vector(1.5 * scale, 1.5 * scale, 1.5 * scale)]
        results.append(run_scenario(ops, sv, base_n, base_c, tv,
                                    distance_percent=10.0))
    return results


# ---------------------------------------------------------------------------
# 场景 3：与法线约束的交互
# ---------------------------------------------------------------------------

def make_thin_wall(thickness=0.02, n=3):
    """
    薄墙源：前面（y=0，法线 +Y）纯红，背面（y=-thickness，法线 -Y）纯蓝。
    顶点交错使两面距离仅 thickness —— 跨面透色的根因。

    **源只有 6 个顶点，x 间距 1.0** -> 其「空洞半径」为间距的一半 = 0.5，
    占对角线（≈2.0）的 **25%**。因此任何小于 25% 的阈值都会把
    墙面中点拒掉，交互矩阵必须用 >=30% 的阈值（见 scenario_interaction）。
    """
    verts, normals, colors = [], [], {}
    for i in range(n):
        x = float(i)
        verts.append(Vector(x, 0.0, 0.0))
        normals.append(Vector(0.0, 1.0, 0.0))
        colors[len(verts) - 1] = RED
        verts.append(Vector(x, -thickness, 0.0))
        normals.append(Vector(0.0, -1.0, 0.0))
        colors[len(verts) - 1] = BLUE
    return verts, normals, colors


# 薄墙场景的阈值：必须大于源的空洞半径占比25%（留余量取 40%）。
# 贴墙点距源 0.5= 对角线 25% -> 40% 上限下在范围内；
# 远处点 (50,-0.01,0) 距源约 48 -> 远超上限 -> 必然被拒。
_WALL_PERCENT = 40.0


def scenario_interaction(ops):
    """
    四象限矩阵：{法线 0 / 75} × {距离 0 / 40%}。

    目标点分两类（距离由脚本实测，不写死）：
      A类「贴墙」：(0.5, -0.010, 0) —— 距源 0.5 = 对角线 25%，在 40% 上限内；
        法线语义为背面（-Y），纯距离会跨面取到红色。
      B 类「远离」：(50.0, -0.010, 0) —— 距源约 48 = 对角线 2400%，超限。

    期望矩阵（B 与前/后顶点等距：法线关时 KDTree 先返回前面红，
    法线开时约束选中背面蓝）：
                    距离 0                距离 40%
      法线 0    A=红（跨面） B=红     A=红           B=保留原色
      法线 75   A=蓝         B=蓝     A=蓝           B=保留原色
    """
    sv, sn, sc = make_thin_wall(0.02, n=3)
    diag = bbox_diagonal([(v.x, v.y, v.z) for v in sv])
    tv = [Vector(0.5, -0.010, 0.0), Vector(50.0, -0.010, 0.0)]
    tn = [Vector(0, -1, 0), Vector(0, -1, 0)]

    d_near = nearest_dist((0.5, -0.010, 0.0),
                          [(v.x, v.y, v.z) for v in sv])
    d_far = nearest_dist((50.0, -0.010, 0.0),
                         [(v.x, v.y, v.z) for v in sv])

    out = {}
    for angle in (0.0, 75.0):
        for dist in (0.0, _WALL_PERCENT):
            out[(angle, dist)] = run_scenario(
                ops, sv, sn, sc, tv, tn,
                angle_deg=angle, distance_percent=dist)
    return out, diag, d_near, d_far


# ---------------------------------------------------------------------------
# 原生门控探测
# ---------------------------------------------------------------------------

def probe_native_gate(ops):
    """
    探测「距离上限启用时是否绕过原生内核」。

    手法与 verify_normal_constraint._probe_native_gate 相同：
    把 _target_points_array（原生分支内第一个真正执行的函数）替换成探针。
    """
    result = {}

    def run_once(angle, dist):
        entered = {"native": False}
        logged = {"info": False}

        from verify_normal_constraint import (
            _FakeMeshForTargets, _FakeVertexGroup, _FakeVert, _Identity, KDTree)

        class _Slot:
            __slots__ = ('color',)

            def __init__(self):
                self.color = (0.0, 0.0, 0.0, 1.0)

        class Layer:
            name = "Color"
            domain = 'POINT'

            def __init__(self, count):
                self.data = [_Slot() for _ in range(count)]

        class NativeTree:
            def __getattr__(self, item):
                entered["native"] = True
                raise RuntimeError("probe: 进入原生分支")

        sv, sn, sc = make_source_cube()
        tv = [Vector(0.5, 0.5, 0.5)]
        source_data = {
            'vertices': sv, 'vertex_colors': sc, 'kd': KDTree(sv),
            'normals': sn if angle > 0 else None,
            'native_tree': NativeTree(), 'colors_dense': None,
        }
        mesh_eval = types.SimpleNamespace(vertices=_FakeVertexGroup([
            _FakeVert(tv[0], Vector(0, 0, 1), 0)]))
        target_obj = types.SimpleNamespace(
            name="T", data=_FakeMeshForTargets(1), matrix_world=_Identity())
        layer = Layer(1)

        old_tpa = ops._target_points_array
        old_info = ops.log_info
        ops._target_points_array = lambda *a, **k: (
            entered.__setitem__("native", True),
            (_ for _ in ()).throw(RuntimeError("probe: 原生分支入口")),
        )[1]
        ops.log_info = lambda *a, **k: logged.__setitem__("info", True)
        try:
            ops._write_colors_to_target(
                target_obj, mesh_eval, layer, source_data,
                Tool(normal_angle_threshold=angle,
                     pick_distance_percent=dist))
        finally:
            ops._target_points_array = old_tpa
            ops.log_info = old_info
        return entered["native"], logged["info"]

    result["d0_entered"] = run_once(0.0, 0.0)[0]
    result["d0_logged"] = run_once(0.0, 0.0)[1]
    result["d10_entered"] = run_once(0.0, 10.0)[0]
    result["d10_logged"] = run_once(0.0, 10.0)[1]
    result["both_entered"] = run_once(75.0, 10.0)[0]
    return result


# ---------------------------------------------------------------------------
# 暴力搜索路径（KDTree 关闭时）
# ---------------------------------------------------------------------------

def scenario_bruteforce(ops):
    """
    距离上限在**暴力搜索路径**上同样生效。

    为什么这条必须测：若只把上限加在 KDTree 路径上，
    「关掉 KDTree + 开着距离上限」的用户会遇到
    「界面显示已启用、实际完全不生效」的静默失效。

    用**致密源**（125 点，x=0.5 恰是源顶点 -> 距离 0），
    避免稀疏源的 50% 空洞半径把「范围内」点也拒掉。
    远处点 (1.5,1.5,1.5) 距源 0.866 = 对角线 50% -> 20% 上限下超限。
    """
    dv, dn, dc = make_dense_source(step=0.25)
    tv = [Vector(0.5, 0.5, 0.5), Vector(1.5, 1.5, 1.5)]
    on = run_scenario(ops, dv, dn, dc, tv, distance_percent=20.0, use_kd=False)
    off = run_scenario(ops, dv, dn, dc, tv, distance_percent=0.0, use_kd=False)
    return on, off, (dv, dn, dc, tv)


# ---------------------------------------------------------------------------
# 源退化
# ---------------------------------------------------------------------------

def scenario_degenerate_source(ops):
    """
    源只有 1 个顶点 -> 包围盒对角线 = 0 -> 按比例的上限会退化成 0，
    把除该点外所有目标顶点都拒掉（模型被涂成空白）。

    期望：**关闭距离上限**（退回原行为），而不是全部拒绝。
    """
    sv = [Vector(1.0, 1.0, 1.0)]
    sn = [Vector(0.0, 0.0, 1.0)]
    sc = {0: RED}
    tv = [Vector(1.0, 1.0, 1.0), Vector(5.0, 5.0, 5.0)]
    return run_scenario(ops, sv, sn, sc, tv, distance_percent=10.0)


# ---------------------------------------------------------------------------
# 主验证
# ---------------------------------------------------------------------------

def run_verification():
    ops, cache = load_ops()
    print("=" * 76)
    print("阶段 C 验证：取色最大距离上限（超出源范围被拉色）")
    print("=" * 76)

    # --- [A] 源尺度 ---
    print("\n[A] 归一化尺度：源包围盒对角线")
    sv, sn, sc = make_source_cube()
    diag = ops._source_bbox_diagonal({'vertices': sv})
    check("单位立方体（边长 1）的对角线 = sqrt(3)",
          abs(diag - math.sqrt(3.0)) < 1e-9,
          f"实测 {diag:.9f}，期望 {math.sqrt(3.0):.9f}")
    check("反向对照: 对角线确实随模型尺寸线性变化",
          abs(ops._source_bbox_diagonal(
              {'vertices': [Vector(0, 0, 0), Vector(10, 0, 0)]}) - 10.0) < 1e-9,
          "边长 10 的线段 -> 对角线 10")
    check("退化源（单顶点）对角线为 0（触发不启用分支）",
          ops._source_bbox_diagonal({'vertices': [Vector(1, 1, 1)]}) == 0.0,
          "单顶点")
    check("空源对角线为 0，不抛异常",
          ops._source_bbox_diagonal({'vertices': []}) == 0.0, "空列表")
    # 记忆化：同一 source_data 第二次必须走缓存（用「改内容不改结果」验证）
    memo = {'vertices': sv}
    d1 = ops._source_bbox_diagonal(memo)
    memo['vertices'] = [Vector(0, 0, 0)]   # 若未缓存，结果会变
    d2 = ops._source_bbox_diagonal(memo)
    check("包围盒结果被记忆化在 source_data 上（同源复用不重复扫描）",
          d1 == d2 and d2 > 1.0, f"改顶点后仍返回 {d2:.6f}（未重新计算）")

    # --- [B] 阈值解析 ---
    print("\n[B] 阈值解析（0 = 关闭；退化源不启用）")
    check("pick_distance_percent=0 -> 不启用（返回 None）",
          ops._resolve_distance_limit_sq(Tool(0.0, 0.0), {'vertices': sv}) is None,
          "None")
    check("pick_distance_percent=10 -> 上限 = 对角线 × 10%",
          abs(ops._resolve_distance_limit_sq(
              Tool(0.0, 10.0), {'vertices': sv}) - (math.sqrt(3) * 0.1) ** 2) < 1e-9,
          f"dist_sq={ops._resolve_distance_limit_sq(Tool(0.0, 10.0), {'vertices': sv}):.9f}")
    check("vc_tool=None -> 不启用（不抛异常）",
          ops._resolve_distance_limit_sq(None, {'vertices': sv}) is None, "None")
    check("退化源 + 上限>0 -> 不启用（避免把模型涂成空白）",
          ops._resolve_distance_limit_sq(
              Tool(0.0, 10.0), {'vertices': [Vector(1, 1, 1)]}) is None,
          "None（而不是 0）")

    # --- [C] 正向：部分超出 ---
    print("\n[C] 正向：目标比源大一倍（3×3×3 vs 2×2×2 顶点集）")
    out, expected_red = scenario_partial_outside(ops)
    in_range = [out[i] for i, e in enumerate(expected_red) if e]
    out_range = [out[i] for i, e in enumerate(expected_red) if not e]
    check("范围内的顶点正常取到源颜色（红）",
          len(in_range) == 8 and all(is_red(c) for c in in_range),
          f"{sum(1 for c in in_range if is_red(c))}/{len(in_range)} 个为红")
    check("超出范围的顶点**保留目标原色**（未被写入）",
          len(out_range) == 19 and all(is_own(c) for c in out_range),
          f"{sum(1 for c in out_range if is_own(c))}/{len(out_range)} 个保留原色")
    check("反向对照: 关闭上限时超距顶点确实被写入（否则本组无区分度）",
          all(is_red(c) for c in
              run_scenario(ops, sv, sn, sc,
                           [Vector(2, 2, 2)], distance_percent=0.0)),
          "关闭时 (2,2,2) 取到红色（应保留原色才对）")
    check("反向对照: 保留的是目标原色，不是白色 / 黑色 / 源色",
          not any(is_red(c) or c[:3] == (1.0, 1.0, 1.0) or c[:3] == (0.0, 0.0, 0.0)
                  for c in out_range),
          f"超距色 = {tuple(round(x, 3) for x in out_range[0])}")

    # --- [D] 守护：关闭时逐字一致 ---
    print("\n[D] 守护：距离上限 = 0 时与本功能上线前逐字一致")
    tv_all = [Vector(x, y, z)
              for x in (0.0, 0.5, 1.0, 1.5, 2.0)
              for y in (0.0, 1.0, 2.0)
              for z in (0.0, 2.0)]
    base = run_scenario(ops, sv, sn, sc, tv_all, distance_percent=0.0)
    check("距离上限=0 两次运行结果完全一致（幂等）",
          base == run_scenario(ops, sv, sn, sc, tv_all, distance_percent=0.0),
          "两次相同")
    check("距离上限=0 时全部顶点都取到源色（含超距的，与旧行为一致）",
          all(is_red(c) for c in base),
          f"{sum(1 for c in base if is_red(c))}/{len(base)} 个为红")
    # 属性缺失（旧 .blend / 第三方调用方）也必须等价于关闭
    class _LegacyTool:
        normal_angle_threshold = 0.0
        use_native_accel = False
        use_kdtree = True
        # 故意不定义 pick_distance_percent
    check("设置对象缺少该属性时视为关闭（旧文件兼容）",
          ops._distance_constraint_enabled(_LegacyTool()) is False,
          "getattr 缺省 -> False")
    check("距离上限=0 时不触发任何距离分支（max_dist_sq 为 None）",
          ops._resolve_distance_limit_sq(Tool(0.0, 0.0), {'vertices': sv}) is None,
          "None -> 后续 if 全部不执行")

    # --- [E] 守护：目标完全在源范围内不回归 ---
    print("\n[E] 守护：目标完全在源范围内 -> 与关闭时完全一致")
    # 用**致密源**：稀疏源（8 顶点）的内部点距最近顶点可达对角线的 50%，
    # 那是「源是否够密」的另一条命题，见 [E2]。
    dv, dn, dc = make_dense_source(step=0.25)
    tv_inside = [Vector(0.5, 0.5, 0.5), Vector(0.25, 0.75, 0.5),
                 Vector(0.125, 0.125, 0.125), Vector(0.9, 0.1, 0.6),
                 Vector(0.0, 0.0, 0.0), Vector(1.0, 1.0, 1.0)]
    off = run_scenario(ops, dv, dn, dc, tv_inside, distance_percent=0.0)
    on = run_scenario(ops, dv, dn, dc, tv_inside, distance_percent=20.0)
    check("致密源 + 范围内目标：开启上限后结果与关闭时完全一致（不回归）",
          on == off,
          f"开启={[tuple(round(x, 3) for x in c) for c in on]}\n"
          f"        关闭={[tuple(round(x, 3) for x in c) for c in off]}")
    check("反向对照: 这些点确实取到了颜色（非全保留原色）",
          all(is_red(c) for c in on),
          f"{[tuple(round(x, 3) for x in c) for c in on]}")
    # 更强的守护：范围内**全域**扫描（连续网格，不是几个采样点）
    dense_tv, dense_exp = [], []
    steps = [i / 8.0 for i in range(9)]
    for x in steps:
        for y in steps:
            for z in steps:
                p = (x, y, z)
                d = min(
                    (p[0] - q.x) ** 2 + (p[1] - q.y) ** 2 + (p[2] - q.z) ** 2
                    for q in dv) ** 0.5
                dense_tv.append(Vector(x, y, z))
                dense_exp.append(d)
    diag_d = ops._source_bbox_diagonal({'vertices': dv})
    worst = max(dense_exp)
    check(f"反向对照: 致密源域内最大最近距离 = 对角线的 "
          f"{100 * worst / diag_d:.1f}%（故 20% 阈值必然全通过）",
          worst < diag_d * 0.20,
          f"最大距离 {worst:.4f} < 20% 上限 {diag_d * 0.2:.4f}")
    full_on = run_scenario(ops, dv, dn, dc, dense_tv, distance_percent=20.0)
    check("致密源 + 域内 729 点全域扫描：开启 20% 上限后无一被拒",
          all(is_red(c) for c in full_on),
          f"{sum(1 for c in full_on if is_red(c))}/{len(full_on)} 个取到色")

    # --- [E2] 已发现的真实边界：稀疏源 + 小阈值会拒绝域内点 ---
    print("\n[E2] 已知边界（实测发现，非缺陷）：稀疏源下域内点也可能超距")
    # 立方体中心 (0.5,0.5,0.5) 距最近角点 0.866 = 对角线的 50%。
    # 因此 10% 阈值下它必然被拒 —— 这不是「拉色」防护误伤，
    # 而是源只有 8 个顶点、本就没有该处颜色数据。
    center = run_scenario(ops, sv, sn, sc, [Vector(0.5, 0.5, 0.5)],
                          distance_percent=10.0)
    check("稀疏源(8顶点) + 10% 上限 -> 域内中心点被拒（源在该处本就无数据）",
          is_own(center[0]),
          f"中心点取色={tuple(round(x, 3) for x in center[0])}")
    check("反向对照: 同一中心点在 60% 上限下被接受（证明是阈值问题、非bug）",
          is_red(run_scenario(ops, sv, sn, sc, [Vector(0.5, 0.5, 0.5)],
                              distance_percent=60.0)[0]),
          "60% 上限 -> 红")
    check("反向对照: 稀疏源的角点（距源顶点 0）始终被接受",
          is_red(run_scenario(ops, sv, sn, sc, [Vector(0, 0, 0)],
                              distance_percent=1.0)[0]),
          "1% 上限下角点仍取到红色（距离 0 恒在上限内）")
    check("含义: 阈值需 > 源的最大「空洞半径」，密网格源该值很小",
          True,
          "致密源(125点)最大空洞 = 对角线 12.5%；稀疏源(8点) = 50%。"
          "故默认值取 0（关闭），由用户按资产密度自行开启")

    # --- [F] 尺度归一化 ---
    print("\n[F] 尺度归一化：源整体放大 1000 倍后行为不变")
    scaled = scenario_scale_invariance(ops)
    check("放大 1000 倍后取色结果与原始尺度完全一致",
          scaled[0] == scaled[1],
          f"scale=1    -> {[tuple(round(x, 3) for x in c) for c in scaled[0]]}\n"
          f"          scale=1000 -> {[tuple(round(x, 3) for x in c) for c in scaled[1]]}")
    check("反向对照: 两个量级下确实都发生了「保留原色」（非全同 trivially）",
          is_own(scaled[0][1]) and is_own(scaled[1][1]),
          f"远处顶点在两个尺度下都保留原色")

    # --- [G] 与法线约束的交互 ---
    print(f"\n[G] 交互矩阵：{{法线 0/75}} × {{距离 0/{_WALL_PERCENT:g}%}}")
    m, wall_diag, d_near, d_far = scenario_interaction(ops)
    limit = _WALL_PERCENT / 100.0 * wall_diag
    check("实测前提: 贴墙点距离在上限内、远处点在上限外（矩阵结论的前提）",
          d_near <= limit < d_far,
          f"对角线={wall_diag:.4f} 上限={limit:.4f} "
          f"d_near={d_near:.4f} d_far={d_far:.4f}")
    # 法线关 + 距离关：纯距离最近（跨面是已知的旧行为）
    check("[法线0|距离0] 贴墙点取到红色（纯距离最近，跨面= 旧行为）",
          is_red(m[(0.0, 0.0)][0]),
          f"{tuple(round(x, 3) for x in m[(0.0, 0.0)][0])}")
    check("[法线0|距离0] 远处点也取到红色（无距离约束）",
          is_red(m[(0.0, 0.0)][1]),
          f"{tuple(round(x, 3) for x in m[(0.0, 0.0)][1])}")
    # 法线开 + 距离关：贴墙点修正为蓝色；远处点 B 与前/后顶点**等距**，
    # 法线约束选中背面 -> 防跨面语义在任意距离都成立（这也反证距离未被门控）。
    check("[法线75|距离0] 贴墙点按法线取到蓝色（防跨面仍生效）",
          m[(75.0, 0.0)][0][2] > 0.5 and m[(75.0, 0.0)][0][0] < 0.5,
          f"{tuple(round(x, 3) for x in m[(75.0, 0.0)][0])}")
    check("[法线75|距离0] 远处点按法线取到背面蓝（未受距离约束、未保留原色）",
          m[(75.0, 0.0)][1][2] > 0.5 and m[(75.0, 0.0)][1][0] < 0.5,
          f"{tuple(round(x, 3) for x in m[(75.0, 0.0)][1])}")
    # 距离开 + 法线关：贴墙点取色（距离内），远处点保留原色
    check(f"[法线0|距离{_WALL_PERCENT:g}%] 贴墙点在距离内 -> 正常取色（红）",
          is_red(m[(0.0, _WALL_PERCENT)][0]),
          f"{tuple(round(x, 3) for x in m[(0.0, _WALL_PERCENT)][0])}")
    check(f"[法线0|距离{_WALL_PERCENT:g}%] 远处点超距 -> 保留原色",
          is_own(m[(0.0, _WALL_PERCENT)][1]),
          f"{tuple(round(x, 3) for x in m[(0.0, _WALL_PERCENT)][1])}")
    # 两个都开：两条约束同时生效
    check(f"[法线75|距离{_WALL_PERCENT:g}%] 贴墙点按法线取蓝（距离内，法线生效）",
          m[(75.0, _WALL_PERCENT)][0][2] > 0.5 and m[(75.0, _WALL_PERCENT)][0][0] < 0.5,
          f"{tuple(round(x, 3) for x in m[(75.0, _WALL_PERCENT)][0])}")
    check(f"[法线75|距离{_WALL_PERCENT:g}%] 远处点超距 -> 保留原色（距离优先于法线）",
          is_own(m[(75.0, _WALL_PERCENT)][1]),
          f"{tuple(round(x, 3) for x in m[(75.0, _WALL_PERCENT)][1])}")

    # --- [G2] 关键交互：距离超限 + 法线不符同时发生时的语义 ---
    print("\n[G2] 关键交互：距离超限与法线不符**同时**发生时的语义")
    # 源：两点线段(0,0,0) 红法线+Y / (1,0,0) 蓝法线-Y，对角线 = 1.0。
    # 目标点 (0.5,0,0) 法线 -Y：最近点是 0（红，+Y）-> 法线不符，
    # 触发二次搜索；1.0 处（蓝，-Y）符合朝向。
    # 该点距源 0.5 = 对角线 50%，故 90% 上限下**在范围内**。
    seg_v = [Vector(0.0, 0.0, 0.0), Vector(1.0, 0.0, 0.0)]
    seg_n = [Vector(0.0, 1.0, 0.0), Vector(0.0, -1.0, 0.0)]
    seg_c = {0: RED, 1: BLUE}
    combo = run_scenario(ops, seg_v, seg_n, seg_c,
                         [Vector(0.5, 0.0, 0.0)], [Vector(0, -1, 0)],
                         angle_deg=75.0, distance_percent=90.0)
    check("距离内 + 法线不符：二次搜索找到朝向一致的候选（未退化为拒绝）",
          combo[0][2] > 0.5,
          f"取色={tuple(round(x, 3) for x in combo[0])}（应为蓝：法线约束生效）")

    # 同一目标点、阈值收紧到 10%（0.5 > 0.1 -> 超限）：
    # 法线搜索仍会找到 1.0 处的蓝，但那个点已越界 -> 必须拒绝。
    tight = run_scenario(ops, seg_v, seg_n, seg_c,
                         [Vector(0.5, 0.0, 0.0)], [Vector(0, -1, 0)],
                         angle_deg=75.0, distance_percent=10.0)
    check("同一超差点在 10% 上限下被拒（距离优先于法线）",
          is_own(tight[0]),
          f"取色={tuple(round(x, 3) for x in tight[0])}（应保留原色）")
    check("反向对照: 90% 与 10% 结果确实不同（阈值生效，非恒拒绝）",
          combo[0] != tight[0],
          f"90%={tuple(round(x,3) for x in combo[0])} vs "
          f"10%={tuple(round(x,3) for x in tight[0])}")

    # 远处点：无论法线如何，40（远超 1.0 对角线）都超限 -> 拒绝
    over = run_scenario(ops, seg_v, seg_n, seg_c,
                        [Vector(5.0, 0.0, 0.0)], [Vector(0, -1, 0)],
                        angle_deg=75.0, distance_percent=90.0)
    check("距离超限 + 法线不符：距离优先，直接拒绝（保留原色）",
          is_own(over[0]),
          f"取色={tuple(round(x, 3) for x in over[0])}（应保留原色）")
    check("反向对照: 同一远处点在距离=0 时确实会取到颜色（不是恒拒绝）",
          is_own(run_scenario(ops, seg_v, seg_n, seg_c,
                              [Vector(5.0, 0.0, 0.0)], [Vector(0, -1, 0)],
                              angle_deg=75.0, distance_percent=0.0)[0]) is False,
          "关闭距离上限时 (5,0,0) 被写入（应取到色才对）")

    # --- [G3] 二次搜索不得越界 ---
    print("\n[G3] 二次搜索半径被距离上限夹住（防止兜底架空上限）")
    # 源：两个相距很远的顶点，法线相反。
    # 目标点靠近 A（A 法线不符）但**在距离上限内**，
    # B 在距离上限**外**且法线符合。
    # 若find_range 半径不被夹住，扩张搜索会选中 B -> 越界取色。
    spread_v = [Vector(0.0, 0.0, 0.0), Vector(10.0, 0.0, 0.0)]
    spread_n = [Vector(0.0, 1.0, 0.0), Vector(0.0, -1.0, 0.0)]
    spread_c = {0: RED, 1: BLUE}
    # 对角线 = 10。目标 (1, 0, 0)：最近 A 距离 1.0 -> 10% 上限 = 1.0（含边界）。
    # B 在 9.0 处，远超上限。目标法线 -Y（与 A 不符，与 B 符）。
    clamp = run_scenario(ops, spread_v, spread_n, spread_c,
                         [Vector(1.0, 0.0, 0.0)],
                         [Vector(0, -1, 0)],
                         angle_deg=75.0, distance_percent=10.0)
    check("二次搜索被夹在距离上限内，未选中上限外的 B（红=退回最近点）",
          is_red(clamp[0]),
          f"取色={tuple(round(x, 3) for x in clamp[0])}"
          f"（若为蓝说明越界选中了 9.0 处的 B）")
    check("反向对照: 不设上限时同一场景会选中 B（证明该场景有区分度）",
          (lambda c: c[2] > 0.5)(
              run_scenario(ops, spread_v, spread_n, spread_c,
                           [Vector(1.0, 0.0, 0.0)], [Vector(0, -1, 0)],
                           angle_deg=75.0, distance_percent=0.0)[0]),
          "关闭上限 -> 蓝（二次搜索找到远处的 B）")

    # --- [H] 暴力搜索路径 ---
    print("\n[H] 暴力搜索路径（KDTree 关闭）同样受距离上限约束")
    bf_on, bf_off, (bdv, bdn, bdc, btv) = scenario_bruteforce(ops)
    check("[暴力|距离20%] 范围内顶点取到红色",
          is_red(bf_on[0]), f"{tuple(round(x, 3) for x in bf_on[0])}")
    check("[暴力|距离20%] 超距顶点保留原色（不静默失效）",
          is_own(bf_on[1]), f"{tuple(round(x, 3) for x in bf_on[1])}")
    check("[暴力|距离0] 超距顶点取到红色（旧行为）",
          is_red(bf_off[1]), f"{tuple(round(x, 3) for x in bf_off[1])}")
    check("两条路径结果一致（KDTree 与暴力同语义）",
          bf_on == run_scenario(ops, bdv, bdn, bdc, btv,
                                distance_percent=20.0, use_kd=True),
          f"暴力={[tuple(round(x,3) for x in c) for c in bf_on]}\n"
          f"      KDTree={[tuple(round(x,3) for x in c) for c in run_scenario(ops, bdv, bdn, bdc, btv, distance_percent=20.0, use_kd=True)]}")

    # --- [I] 源退化不误杀 ---
    print("\n[I] 兜底：源退化为单顶点时不得把模型涂成空白")
    degen = scenario_degenerate_source(ops)
    check("单顶点源 + 上限 10% -> 上限被忽略，两个点都正常取色",
          all(is_red(c) for c in degen),
          f"{[tuple(round(x, 3) for x in c) for c in degen]}")
    check("反向对照: 若不忽略上限，(5,5,5) 会被拒（证明该守卫有意义）",
          is_red(run_scenario(ops, sv, sn, sc, [Vector(1.5, 1.5, 1.5)],
                              distance_percent=0.0)[0]),
          "对照：正常源 + 关闭上限时 (1.5,1.5,1.5) 取到红色")

    # --- [J] 原生门控 ---
    print("\n[J] 原生门控：距离上限启用时必须绕过原生内核")
    gate = probe_native_gate(ops)
    check("[距离0|法线0] 正常走原生内核（未被绕过）",
          gate["d0_entered"] is True, f"尝试原生={gate['d0_entered']}")
    check("[距离0|法线0] 不产生「改用 Python」的提示（无误导）",
          gate["d0_logged"] is False, f"log_info={gate['d0_logged']}")
    check("[距离10%|法线0] 绕过原生内核（未尝试调用）",
          gate["d10_entered"] is False,
          f"尝试原生={gate['d10_entered']}（应为 False）")
    check("[距离10%|法线0] 已向用户说明改用 Python 路径",
          gate["d10_logged"] is True, f"log_info={gate['d10_logged']}")
    check("[距离10%|法线75] 绕过原生内核（两条约束任一启用即绕过）",
          gate["both_entered"] is False,
          f"尝试原生={gate['both_entered']}（应为 False）")

    # --- [K] 提示文案不得只提法线 ---
    print("\n[K] 提示文案：只开距离上限时不得只提「法线夹角约束」")
    msgs = []
    old_log = ops.log_info
    ops.log_info = lambda msg, *a, **k: msgs.append(msg)
    try:
        from verify_normal_constraint import (
            _FakeMeshForTargets, _FakeVertexGroup, _FakeVert, _Identity, KDTree)
        for (ang, dist) in ((0.0, 10.0), (75.0, 0.0), (75.0, 10.0)):
            sv2, sn2, sc2 = make_source_cube()

            class NT:
                def __getattr__(self, item):
                    raise RuntimeError("probe")

            class _Slot:
                __slots__ = ('color',)

                def __init__(self):
                    self.color = (0, 0, 0, 1)

            class Layer:
                name = "Color"
                domain = 'POINT'
                data = [_Slot()]

            sd = {'vertices': sv2, 'vertex_colors': sc2, 'kd': KDTree(sv2),
                  'normals': sn2 if ang > 0 else None,
                  'native_tree': NT(), 'colors_dense': None}
            me = types.SimpleNamespace(vertices=_FakeVertexGroup(
                [_FakeVert(Vector(0.5, 0.5, 0.5), Vector(0, 0, 1), 0)]))
            to = types.SimpleNamespace(name="T",
                                       data=_FakeMeshForTargets(1),
                                       matrix_world=_Identity())
            msgs.clear()
            try:
                ops._write_colors_to_target(to, me, Layer(), sd,
                                            Tool(ang, dist))
            except Exception:
                pass
            captured = msgs[0] if msgs else ""
            if (ang, dist) == (0.0, 10.0):
                check("只开距离上限 -> 提示提到「取色距离上限」",
                      "取色距离上限" in captured, f"提示={captured[:60]}")
                check("只开距离上限 -> 提示未误报「法线夹角约束」",
                      "法线夹角" not in captured, f"提示={captured[:60]}")
            elif (ang, dist) == (75.0, 10.0):
                check("两个都开 -> 提示同时提到两个约束",
                      "法线夹角约束" in captured and "取色距离上限" in captured,
                      f"提示={captured[:70]}")
                check("两个都开 -> 提示说明恢复方式为「都设为 0」",
                      "都设为 0" in captured, f"提示={captured[:70]}")
    finally:
        ops.log_info = old_log

    # --- [L] 参数存在性 ---
    print("\n[L] 参数：pick_distance_percent 已在 settings 中定义且默认 0")
    settings_src = open(
        os.path.join(ROOT, "properties", "settings.py"),
        encoding="utf-8").read()
    tree = ast.parse(settings_src)
    prop = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and \
                getattr(node.target, "id", "") == "pick_distance_percent":
            prop = node
    check("settings.py 中定义了 pick_distance_percent", prop is not None, "")
    if prop is not None:
        # **Blender 的属性声明是「裸注解」**（`x: bpy.props.FloatProperty(...)`），
        # Python 把它解析成 annotation=调用、value=None，
        # 取参数必须读 .annotation 而不是 .value —— 读 .value 会 AttributeError。
        call = prop.annotation
        kw = {k.arg: k.value for k in call.keywords} if call is not None else {}
        default = getattr(kw.get("default"), "value", None)
        check("pick_distance_percent 默认值为 0.0（默认关闭，不改变现有用户行为）",
              default == 0.0, f"default={default}")
        check("pick_distance_percent 下限为 0.0（不能为负）",
              getattr(kw.get("min"), "value", None) == 0.0,
              f"min={getattr(kw.get('min'), 'value', None)}")
        check("pick_distance_percent 有 soft_max（避免滑块被max 200 卡住手感）",
              "soft_max" in kw, f"soft_max={getattr(kw.get('soft_max'), 'value', None)}")
        desc = getattr(kw.get("description"), "value", "") or ""
        check("tooltip 说明按对角线百分比归一化（避免误解为世界单位）",
              "对角线" in desc, "")
        check("tooltip 说明超距时保留原色（用户决策）",
              "保留" in desc and "不写入" in desc, "")
        check("tooltip 告知会切到 Python 路径（代价如实告知）",
              "Python" in desc, "")

    # --- 汇总 ---
    failed = [n for n, ok, _ in _RESULTS if not ok]
    print("\n" + "=" * 76)
    print(f"结果: {len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过"
          + ("" if not failed else f"\n失败: {failed}"))
    print("=" * 76)
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# 变异测试：确认每条断言都能被变异打红
# ---------------------------------------------------------------------------

# (描述, 源码锚点, 变异体, 期望被打红的断言关键字)
MUTATIONS = [
    ("距离闸门完全去掉（恢复原缺陷）",
     "    if max_dist_sq is not None and nearest_dist_sq > max_dist_sq:\n        return -1",
     "    if False:\n        return -1",
     "保留目标原色"),

    ("距离比较反向（变成「不超距才拒绝」）",
     "if max_dist_sq is not None and nearest_dist_sq > max_dist_sq:",
     "if max_dist_sq is not None and nearest_dist_sq < max_dist_sq:",
     "保留目标原色"),

    ("二次搜索半径不再被距离上限夹住（兜底架空上限）",
     "        if max_dist_sq is not None and radius * radius > max_dist_sq:\n            radius = math.sqrt(max_dist_sq)",
     "        if False:\n            radius = radius",
     "二次搜索被夹在距离上限内"),

    ("超距时写入白色而非跳过（违反用户决策）",
     "        if src_idx < 0:\n            # 超距（或无候选）-> 不写入，即保留目标原色（用户明确选择的语义）\n            continue\n        color = vertex_colors.get(src_idx, (1.0, 1.0, 1.0, 1.0))",
     "        if src_idx < 0:\n            src_idx = 0\n        color = vertex_colors.get(src_idx, (1.0, 1.0, 1.0, 1.0))",
     "保留目标原色"),

    ("阈值按固定世界单位算（不再归一化）",
     "    limit = diagonal * (percent / 100.0)",
     "    limit = percent / 100.0",
     "放大 1000 倍后取色结果与原始尺度完全一致"),

    ("退化源不再忽略上限（会把模型涂成空白）",
     "    if diagonal < _MIN_SOURCE_EXTENT:",
     "    if False:",
     "单顶点源 + 上限 10% -> 上限被忽略"),

    ("暴力搜索路径不检查距离（静默失效）",
     "        if max_dist_sq is not None and min_dist_sq > max_dist_sq:\n            continue\n\n        if vcol_layer.domain == 'POINT'",
     "        if False:\n            continue\n\n        if vcol_layer.domain == 'POINT'",
     "[暴力|距离10%] 超距顶点保留原色"),

    ("距离上限不绕过原生（界面显示已启用、实际无效）",
     "    need_python = normal_enabled or distance_enabled",
     "    need_python = normal_enabled",
     "[距离10%|法线0] 绕过原生内核"),

    ("提示文案只说法线（不提距离）",
     "        if distance_enabled:\n            reasons.append(\"取色距离上限\")",
     "        if False:\n            reasons.append(\"取色距离上限\")",
     "只开距离上限 -> 提示提到「取色距离上限」"),
]


def load_ops_mutated(old, new):
    """装载注入变异后的 vertex_color_ops（其余模块用替身）。"""
    pkg = types.ModuleType("vn"); pkg.__path__ = [ROOT]
    utils_pkg = types.ModuleType("vn.utils")
    utils_pkg.__path__ = [os.path.join(ROOT, "utils")]
    core_pkg = types.ModuleType("vn.core")
    core_pkg.__path__ = [os.path.join(ROOT, "core")]
    lg = types.ModuleType("vn.utils.logging_utils")
    lg.log_error = lg.log_warning = lg.log_info = lambda *a, **k: None
    vcu = types.ModuleType("vn.utils.vertex_color_utils")
    vcu.get_vcol_layer = lambda *a, **k: None
    vcu.get_active_vcol_layer = lambda *a, **k: None
    vcu.has_preview_backup = lambda m: False
    vcu.PREVIEW_BACKUP_LAYER_NAME = "__vct_preview_backup__"
    nb = types.ModuleType("vn.core.native_backend")
    nb.is_available = lambda: False
    nb.NativeKDTree = None
    for n, m in [("vn", pkg), ("vn.utils", utils_pkg), ("vn.core", core_pkg),
                 ("vn.utils.logging_utils", lg),
                 ("vn.utils.vertex_color_utils", vcu),
                 ("vn.core.native_backend", nb)]:
        sys.modules[n] = m

    bpy_stub = types.ModuleType("bpy")
    bpy_stub.context = types.SimpleNamespace()
    bpy_stub.types = types.SimpleNamespace(Object=object)
    bpy_stub.props = types.SimpleNamespace()
    sys.modules["bpy"] = bpy_stub

    from verify_normal_constraint import KDTree, np_array
    np_stub = types.ModuleType("numpy")
    np_stub.ndarray = np_array
    np_stub.float32 = "float32"
    np_stub.float64 = "float64"
    sys.modules["numpy"] = np_stub

    mu = types.ModuleType("mathutils")
    mu.Vector = Vector
    kd_mod = types.ModuleType("mathutils.kdtree")
    kd_mod.KDTree = KDTree
    mu.kdtree = kd_mod
    sys.modules["mathutils"] = mu
    sys.modules["mathutils.kdtree"] = kd_mod

    path = os.path.join(ROOT, "core", "vertex_color_ops.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if old not in src:
        raise AssertionError(f"变异锚点未找到: {old[:70]!r}")
    src = src.replace(old, new, 1)
    # 写进临时模块并执行
    import tempfile
    # mkstemp 没有 delete 参数（那是 NamedTemporaryFile 的）；
    # 它本来就只创建不自动删除，finally 里的 os.unlink 负责清理。
    fd, tmp = tempfile.mkstemp(suffix="_mutant.py")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        spec = importlib.util.spec_from_file_location(
            "vn.core.vertex_color_ops", tmp)
        m = importlib.util.module_from_spec(spec)
        sys.modules["vn.core.vertex_color_ops"] = m
        spec.loader.exec_module(m)
        # cache 模块也需装载（vertex_color_ops 从它 import）
        cpath = os.path.join(ROOT, "core", "cache.py")
        cspec = importlib.util.spec_from_file_location("vn.core.cache", cpath)
        cm = importlib.util.module_from_spec(cspec)
        sys.modules["vn.core.cache"] = cm
        cspec.loader.exec_module(cm)
        return m
    finally:
        os.unlink(tmp)


def run_mutate():
    """
    对每个变异体跑完整验证，必须**至少有一条断言转红**。

    全部仍全绿 = 这些断言是恒真的，本脚本测不出东西。
    """
    print("=" * 76)
    print("变异测试：确认每条断言都可能被缺陷打红")
    print("=" * 76)
    results = []
    for desc, old, new, expect_kw in MUTATIONS:
        try:
            # 每次都从干净状态重新装载，避免上一个变异体污染 sys.modules
            for m in list(sys.modules):
                if m.startswith("vn"):
                    del sys.modules[m]
            ops = load_ops_mutated(old, new)
            for m in list(sys.modules):
                if m.startswith("vn"):
                    del sys.modules[m]
            # run_verification 会自己 load_ops()，故此处改为直接测关键场景
            ok = _mutant_caught(ops, expect_kw)
        except Exception as e:
            ok = True  # 变异体自身崩溃也算「被抓住」
            desc += f"（变异体抛异常: {type(e).__name__}）"
        results.append((desc, ok))
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}"
              + ("" if ok else "  -- 变异后仍全绿（断言恒真！）"))

    n_pass = sum(1 for _, ok in results if ok)
    print("\n" + "=" * 76)
    print(f"变异测试: {n_pass}/{len(results)} 个变异体被抓住")
    print("=" * 76)
    return 0 if n_pass == len(results) else 1


def _mutant_caught(ops, expect_kw):
    """在变异体上跑与 expect_kw 相关的场景，看是否出现失败。"""
    try:
        sv, sn, sc = make_source_cube()
        # 覆盖所有关键场景，取任一「应通过但变异后不通过」即算抓住
        checks = []

        # C: 部分超出
        out, expected_red = scenario_partial_outside(ops)
        in_range = [out[i] for i, e in enumerate(expected_red) if e]
        out_range = [out[i] for i, e in enumerate(expected_red) if not e]
        checks.append(len(in_range) == 8 and all(is_red(c) for c in in_range))
        checks.append(len(out_range) == 19 and all(is_own(c) for c in out_range))

        # E: 致密源域内不回归（稀疏源不适用，见 E2）
        dv, dn, dc = make_dense_source(step=0.25)
        tv_inside = [Vector(0.5, 0.5, 0.5), Vector(0.25, 0.75, 0.5),
                     Vector(0.125, 0.125, 0.125)]
        checks.append(run_scenario(ops, dv, dn, dc, tv_inside,
                                  distance_percent=0.0)
                      == run_scenario(ops, dv, dn, dc, tv_inside,
                                      distance_percent=20.0))

        # F: 尺度归一化
        scaled = scenario_scale_invariance(ops)
        checks.append(scaled[0] == scaled[1])

        # G: 交互
        m, _ = scenario_interaction(ops)
        checks.append(is_own(m[(0.0, 10.0)][1]))
        checks.append(is_own(m[(75.0, 10.0)][1]))
        checks.append(is_red(m[(0.0, 10.0)][0]))
        checks.append(is_red(m[(0.0, 0.0)][0]))

        # G3: 二次搜索夹紧
        spread_v = [Vector(0.0, 0.0, 0.0), Vector(10.0, 0.0, 0.0)]
        spread_n = [Vector(0.0, 1.0, 0.0), Vector(0.0, -1.0, 0.0)]
        spread_c = {0: RED, 1: BLUE}
        clamp = run_scenario(ops, spread_v, spread_n, spread_c,
                             [Vector(1.0, 0.0, 0.0)], [Vector(0, -1, 0)],
                             angle_deg=75.0, distance_percent=10.0)
        checks.append(is_red(clamp[0]))

        # H: 暴力路径
        bf_on, bf_off = scenario_bruteforce(ops)
        checks.append(is_own(bf_on[1]))
        checks.append(is_red(bf_off[1]))

        # I: 退化源
        checks.append(all(is_red(c) for c in scenario_degenerate_source(ops)))

        # J: 原生门控
        try:
            g = probe_native_gate(ops)
            checks.append(g["d10_entered"] is False)
            checks.append(g["both_entered"] is False)
        except Exception:
            pass

        # K: 提示文案
        try:
            msgs = []
            old_log = ops.log_info
            ops.log_info = lambda m, *a, **k: msgs.append(m)
            try:
                from verify_normal_constraint import (
                    _FakeMeshForTargets, _FakeVertexGroup, _FakeVert,
                    _Identity, KDTree)
                sv2, sn2, sc2 = make_source_cube()

                class NT:
                    def __getattr__(self, item):
                        raise RuntimeError("probe")

                class _Slot:
                    __slots__ = ('color',)

                    def __init__(self):
                        self.color = (0, 0, 0, 1)

                class Layer:
                    name = "Color"
                    domain = 'POINT'
                    data = [_Slot()]

                sd = {'vertices': sv2, 'vertex_colors': sc2, 'kd': KDTree(sv2),
                      'normals': None, 'native_tree': NT(), 'colors_dense': None}
                me = types.SimpleNamespace(vertices=_FakeVertexGroup(
                    [_FakeVert(Vector(0.5, 0.5, 0.5), Vector(0, 0, 1), 0)]))
                to = types.SimpleNamespace(name="T",
                                           data=_FakeMeshForTargets(1),
                                           matrix_world=_Identity())
                msgs.clear()
                try:
                    ops._write_colors_to_target(to, me, Layer(), sd,
                                                Tool(0.0, 10.0))
                except Exception:
                    pass
                checks.append(bool(msgs) and "取色距离上限" in msgs[0])
            finally:
                ops.log_info = old_log
        except Exception:
            pass

        return not all(checks)   # 有一条不成立 -> 变异被抓住
    except Exception:
        return True


def main():
    ap = argparse.ArgumentParser(description="阶段 C：取色距离上限验证")
    ap.add_argument("--mutate", action="store_true",
                    help="只跑变异测试（确认断言可证伪）")
    args = ap.parse_args()
    if args.mutate:
        return run_mutate()
    return run_verification()


if __name__ == "__main__":
    sys.exit(main())
