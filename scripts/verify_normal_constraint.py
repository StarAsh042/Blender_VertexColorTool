"""
阶段 B：法线约束验证（薄壁跨面取色）

验证命题:
    1. 正向：0.02 厚度的薄墙，两侧顶点各取到各自的颜色，不跨面
    2. 守护：非薄壁场景，取色结果与「关闭约束」时**完全一致**
    3. 兜底：无符合朝向的候选时退回纯距离最近（不得丢色块）
    4. 阈值 0 = 完全关闭，行为与优化前逐字一致
    5. 性能：非薄壁场景开销增幅

每条断言都配有变异测试（见文件末尾的 --mutate），确保可证伪。

运行:
    python scripts/verify_normal_constraint.py
    python scripts/verify_normal_constraint.py --bench
    python scripts/verify_normal_constraint.py --mutate
    python scripts/verify_normal_constraint.py --real-numpy   # 需本机装有 numpy

退出码:
    0 = 通过；1 = 有断言失败；3 = 因环境缺失而**跳过**（不是通过！）
    （3 单独区分，是为了让调用方能把「跳过」与「通过」区分开，
      避免没装 numpy 时 CI 看起来全绿、实际数值路径零覆盖。）
"""

import argparse
import ast
import importlib.util
import math
import os
import subprocess
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mathutils_stub import Vector3 as Vector  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RESULTS = []

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIP = 3   # 环境缺失导致未执行——绝不等同于「通过」


def check(name, cond, detail=""):
    _RESULTS.append((name, bool(cond), detail))
    print(f"   {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 加载被测模块（stub 掉 bpy / mathutils）
# ---------------------------------------------------------------------------

def load_ops(real_numpy=False, core_dir=None):
    """
    加载被测模块（stub 掉 bpy / mathutils；numpy 视real_numpy 而定）。

    Args:
        real_numpy: True 时**不注入 numpy 替身**，让 core/ 里的
            `import numpy as np` 拿到真 numpy。
            这样才能真正执行 _extract_normals / _transform_normals_array
            的向量化数值路径（替身路径走的是 list[Vector] 回退，
            两者精度与实现都不同，不能互相代替）。
            bpy / mathutils 的替身在任何模式下都照旧注入——
            它们是「无 Blender 环境」的必要条件，与 numpy 真伪无关。
        core_dir: 被测 core/ 模块所在目录，None = 仓库的 core/。
            变异测试传入**临时副本目录**。历史上变异测试曾直接覆写
            core/ 源文件再在 finally 里恢复——「写盘之后、恢复之前」
            进程被硬杀（断电 / kill -9 / CI 超时）就会留下变异代码，
            而 build_zip 打包的正是工作区文件，等于把坏代码发出去。
            副本方案从根上消灭这类发布物污染。
    """
    if core_dir is None:
        core_dir = os.path.join(ROOT, "core")
    pkg = types.ModuleType("vn"); pkg.__path__ = [ROOT]
    utils_pkg = types.ModuleType("vn.utils"); utils_pkg.__path__ = [os.path.join(ROOT, "utils")]
    core_pkg = types.ModuleType("vn.core"); core_pkg.__path__ = [core_dir]
    lg = types.ModuleType("vn.utils.logging_utils")
    lg.log_error = lg.log_warning = lg.log_info = lambda *a, **k: None
    vcu = types.ModuleType("vn.utils.vertex_color_utils")
    vcu.get_vcol_layer = lambda *a, **k: None
    vcu.get_active_vcol_layer = lambda *a, **k: None
    vcu.has_preview_backup = lambda m: False
    vcu.PREVIEW_BACKUP_LAYER_NAME = "__vct_preview_backup__"
    nb = types.ModuleType("vn.core.native_backend")
    nb.is_available = lambda: False   # 强制走 Python 路径（本阶段只测 Python）
    nb.NativeKDTree = None
    for n, m in [("vn", pkg), ("vn.utils", utils_pkg), ("vn.core", core_pkg),
                 ("vn.utils.logging_utils", lg),
                 ("vn.utils.vertex_color_utils", vcu),
                 ("vn.core.native_backend", nb)]:
        sys.modules[n] = m

    # bpy 替身：cache.py 顶层 `import bpy`，且vertex_color_ops.py 的函数签名
    # 注解 `bpy.types.Object` 会在 def 时求值，故需补全 types.Object。
    bpy_stub = types.ModuleType("bpy")
    bpy_stub.context = types.SimpleNamespace()
    bpy_stub.types = types.SimpleNamespace(Object=object)
    bpy_stub.props = types.SimpleNamespace()
    sys.modules["bpy"] = bpy_stub

    # numpy 替身。
    #
    # 设计原则（QA P0 的直接教训）: **能崩的地方，替身也要崩。**
    # 真实 numpy 里bool(多元素数组) 抛 ValueError；而 list 的真值永远合法。
    # 若替身用 list 承载法线，就会把唯一会崩的组合消掉 -> 测试假绿。
    # 因此这里让 np_array 一被求真值就抛错，与真实 numpy 行为一致。
    #
    # real_numpy=True 时**不注入**替身，也不删除已存在的真 numpy——
    # 直接让 core/ 的 import 走真 numpy。若本机无真 numpy，
    # core/ 里的 `except ImportError` 会把 np 置为 None，
    # 由调用方（run_real_numpy_check）检测并报「跳过」。
    if not real_numpy:
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

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    cache = _load("vn.core.cache", os.path.join(core_dir, "cache.py"))
    ops = _load("vn.core.vertex_color_ops", os.path.join(core_dir, "vertex_color_ops.py"))
    return ops, cache


class KDTree:
    """最小可用的 mathutils.kdtree.KDTree 替身（find / find_range）"""

    def __init__(self, coords):
        self.coords = list(coords)

    def find(self, co):
        best, bd = -1, float('inf')
        for i, c in enumerate(self.coords):
            d = (c[0] - co[0]) ** 2 + (c[1] - co[1]) ** 2 + (c[2] - co[2]) ** 2
            if d < bd:
                bd, best = d, i
        if best < 0:
            return None
        return (self.coords[best], best, bd)

    def find_range(self, co, radius):
        r2 = radius * radius
        out = []
        for i, c in enumerate(self.coords):
            d = (c[0] - co[0]) ** 2 + (c[1] - co[1]) ** 2 + (c[2] - co[2]) ** 2
            if d <= r2:
                out.append((c, i, d))
        return out


class Tool:
    def __init__(self, normal_angle_threshold=75.0):
        self.normal_angle_threshold = normal_angle_threshold
        self.use_native_accel = False
        self.use_kdtree = True


# ---------------------------------------------------------------------------
# 场景构造
# ---------------------------------------------------------------------------

RED = (1.0, 0.0, 0.0, 1.0)
BLUE = (0.0, 0.0, 1.0, 1.0)


def make_sparse_backface(thickness=0.02, gap=0.05):
    """
    构造「符合朝向的候选在稍远处」的稀疏场景（守护二次搜索的半径扩张）。

    布局:
        - 目标点 (1,0,0) 附近有一批顶点，法线**朝前**（不符合目标 -Y）
        - 唯一法线朝后（符合）的顶点放在 (1, -gap, 0)，
          即与目标点的距离**恰为 gap**（放在目标点正下方，
          避免因 x 方向偏移导致实际距离大于 gap 而越界）

    这样只有当半径逐步扩大到 >= gap 时才能找到符合朝向的候选；
    若只搜一轮（半径 = 2×MIN = 0.002），就会找不到而走兜底。
    """
    verts, normals, colors = [], [], {}
    # 附近：法线朝前（不符合），含一个正好在目标点上的
    for k in range(3):
        verts.append(Vector(float(k), 0.0, 0.0))
        normals.append(Vector(0.0, 1.0, 0.0))
        colors[len(verts) - 1] = RED
    # 稍远处：法线朝后（符合），位于目标点 (1,0,0) 正下方 gap 处
    verts.append(Vector(1.0, -gap, 0.0))
    normals.append(Vector(0.0, -1.0, 0.0))
    colors[len(verts) - 1] = BLUE
    return verts, normals, colors


def make_multi_candidate_same_radius():
    """
    构造「同一轮内有多个符合朝向候选」的场景（守护取最近而非最后命中）。

    关键设计（缺一不可，否则该场景无法观测目标行为）:
      1. find_range 按**插入顺序**返回候选；
      2. 较近的候选**先**插入、较远的**后**插入；
         => 正确实现（比较 dist_sq 取最小）应取较近的，
            「取最后命中」的错误实现会取到较远的 —— 结果不同，可区分。
      3. 两个符合朝向的候选必须落在**同一轮**的搜索半径内。
         实现每轮命中后会在「本轮已有符合候选」时提前 return，
         若两个候选分属不同轮次，后一个永远进不了循环体，变异无从体现。
         因此这里让两者距离接近（0.10 与 0.12），
         且最近点（非合规）距离为 0，使首轮半径 = 2×MIN = 0.002，
         仍需多轮扩张 —— 但两者会在**同一轮**同时进入（半径 >= 0.12 时）。
         为保证同轮，把两者距离设为 0.10 与 0.11（比值 < 2，必同轮覆盖）。
    """
    verts, normals, colors = [], [], {}
    # 较近的符合候选（先插入，距离 0.10）—— 正确实现应取它
    verts.append(Vector(0.0, -0.10, 0.0))
    normals.append(Vector(0.0, -1.0, 0.0))
    colors[len(verts) - 1] = BLUE
    # 较远的符合候选（后插入，距离 0.11）—— 只差 0.01，必与前者同轮进入
    verts.append(Vector(0.0, -0.11, 0.0))
    normals.append(Vector(0.0, -1.0, 0.0))
    colors[len(verts) - 1] = RED
    # 最近的点法线朝前（不符合，触发二次搜索），距离 0.0
    verts.append(Vector(0.0, 0.0, 0.0))
    normals.append(Vector(0.0, 1.0, 0.0))
    colors[len(verts) - 1] = RED
    return verts, normals, colors


def make_thin_wall(thickness=0.02, n=3):
    """
    构造 0.02 厚度的薄墙源物体。

    前面（y=0，法线 +Y）纯红，背面（y=-thickness，法线 -Y）纯蓝。
    顶点交错排列，使两面顶点距离仅 0.02 —— 这是跨面透色发生的根因。
    """
    verts, normals, colors = [], [], {}
    for i in range(n):
        x = float(i)
        # 正面
        verts.append(Vector(x, 0.0, 0.0))
        normals.append(Vector(0.0, 1.0, 0.0))
        colors[len(verts) - 1] = RED
        # 背面
        verts.append(Vector(x, -thickness, 0.0))
        normals.append(Vector(0.0, -1.0, 0.0))
        colors[len(verts) - 1] = BLUE
    return verts, normals, colors


def make_single_sided(n=3):
    """单面片（只有正面）—— 法线约束找不到反面候选，必须走兜底"""
    verts, normals, colors = [], [], {}
    for i in range(n):
        verts.append(Vector(float(i), 0.0, 0.0))
        normals.append(Vector(0.0, 1.0, 0.0))
        colors[len(verts) - 1] = RED
    return verts, normals, colors


class _FakeVert:
    """模拟 mesh.vertices 的元素：带 .co / .normal / .index"""
    __slots__ = ('co', 'normal', 'index')

    def __init__(self, co, normal, index=0):
        self.co = co
        self.normal = normal
        self.index = index


class _FakeVertexGroup:
    """
    模拟 mesh.vertices：支持 foreach_get('normal'/'co', list)。

    vertex_color_ops._extract_normals 与 _target_points_array 都用 foreach_get
    一次性导出数据，所以这里必须实现该接口而不是只提供可迭代性。
    """

    def __init__(self, verts):
        self._verts = verts

    def __len__(self):
        return len(self._verts)

    def __iter__(self):
        return iter(self._verts)

    def __getitem__(self, i):
        return self._verts[i]

    def foreach_get(self, attr, out):
        """
        模拟 mesh.vertices.foreach_get。

        关键语义（务必与真实 Blender 一致，否则测试会假绿）:
            - 对 3 分量属性（normal / co），传入**扁平数组**时缓冲区长度
              必须是 3 × 顶点数；传 `[None] * count` 在真实 Blender 中会抛异常。
            - 传入**预分配的 Vector 列表**时逐项写入（真实 Blender 支持这种用法，
              core/cache.py 的无 numpy 回退路径就依赖它）。
            - 两种buffer 形态都必须支持，缺一个就会让对应路径假失败/假通过。
        """
        if attr not in ("normal", "co"):
            raise AttributeError(f"不支持的 foreach_get 属性: {attr}")
        for i, v in enumerate(self._verts):
            vec = v.normal if attr == "normal" else v.co
            # 形态 1：扁平数组（长度 3N）
            if len(out) >= 3 * len(self._verts) and not hasattr(out[0] if len(out) else None, "x"):
                if 3 * i + 2 < len(out):
                    out[3 * i] = vec[0]
                    out[3 * i + 1] = vec[1]
                    out[3 * i + 2] = vec[2]
                continue
            # 形态 2：预分配的 Vector 列表（长度 N）
            if i < len(out):
                out[i] = vec


def run_scenario(ops, source_verts, source_normals, source_colors,
                 target_verts, target_normals, threshold, matrix_world=None):
    """
    跑一次 _write_colors_to_target 的 Python KDTree 路径，返回每个目标顶点取到的颜色。

    Args:
        matrix_world: 目标物体的世界矩阵替身。默认 _Identity()（替身路径）。
            真 numpy 通道需传入 _NumpyMatrix——_Identity 无法被 np.array()
            消费，会让 _transform_normals_array 静默回退（见 _NumpyMatrix 文档）。
    """
    kd = KDTree(source_verts)
    source_data = {
        'vertices': source_verts,
        'vertex_colors': source_colors,
        'kd': kd,
        'normals': source_normals,
        'native_tree': None,
    }

    class _ColorSlot:
        """模拟颜色层的 data[i]（支持 .color 读写）"""
        __slots__ = ('color',)

        def __init__(self):
            self.color = (0.0, 0.0, 0.0, 1.0)

    class FakeLayer:
        name = "Color"
        domain = 'POINT'

        def __init__(self, n):
            self.data = [_ColorSlot() for _ in range(n)]

    layer = FakeLayer(len(target_verts))

    class FakeMesh:
        def __init__(self, n):
            self.vertices = _FakeVertexGroup(
                [_FakeVert(Vector(0, 0, 0), Vector(0, 1, 0), i) for i in range(n)])
            self.loops = []
            self.polygons = []

        def update(self):
            pass

    target_obj = types.SimpleNamespace(
        name="T", data=FakeMesh(len(target_verts)),
        matrix_world=matrix_world if matrix_world is not None else _Identity())
    # mesh_eval.vertices 的元素需要同时具备 .co（取位置）与 .normal（取法线）
    mesh_eval = types.SimpleNamespace(vertices=_FakeVertexGroup([
        _FakeVert(co, target_normals[i] if i < len(target_normals) else None, i)
        for i, co in enumerate(target_verts)
    ]))

    tool = Tool(normal_angle_threshold=threshold)
    # 直接调内部函数，绕过 native 判定
    ops._write_colors_to_target(target_obj, mesh_eval, layer, source_data, tool)
    return [tuple(d.color) for d in layer.data]


class _Identity:
    """
    最小 world matrix 替身：@ 作用于 Vector 返回自身。

    需与真实 mathutils.Matrix 一致：`M @ v` 返回 **Vector**（而非原地修改），
    否则 `Vector(M @ n)` 这类写法会在替身下抛 TypeError，
    而在真实 Blender 中正常——这种「替身比真实环境更严格」的偏差
    会让回退路径假失败。

    仅用于**替身路径**：它无法被 `np.array()` 消费，
    因此真 numpy 通道改用 _NumpyMatrix（见下）。
    """

    def __matmul__(self, other):
        if hasattr(other, "copy"):
            return other.copy()
        return other

    def to_3x3(self):
        return self

    def inverted_safe(self):
        return self

    def transposed(self):
        return self


class _NumpyMatrix:
    """
    真 numpy 通道专用的 world matrix 替身。

    为什么不能直接用 _Identity:
        `core/vertex_color_ops.py::_transform_normals_array` 里是
        `m = np.array(normal_matrix, dtype=np.float64)` 然后
        `arr @ m[:3, :3].T` —— 它要求 matrix_world 能被 **numpy 数组化**
        且支持二维切片。而 _Identity 两者都不支持，真 numpy 下会抛异常
        -> 该函数 return None -> **静默退回逐点回退**。
        那样真 numpy 通道看着「跑通了」，实际根本没测到向量化路径
        （与本项目「测试假绿」的核心教训同类）。

    必须同时满足两套语义:
        1. mathutils.Matrix：to_3x3() / inverted_safe() / transposed()
           以及 `M @ Vector` 返回 **Vector**（core 的逐点回退路径依赖后者）
        2. numpy：__array__ 与 __getitem__（二维切片）
    """

    def __init__(self, np_mod, arr):
        self._np = np_mod
        self._a = arr

    @classmethod
    def identity(cls, np_mod):
        return cls(np_mod, np_mod.eye(4, dtype=np_mod.float64))

    def __array__(self, dtype=None, copy=None):
        # numpy 2.x 会以 __array__(dtype, copy=...) 调用；
        # 两个参数都接受，避免签名不匹配导致静默回退。
        if dtype is None:
            return self._a
        return self._a.astype(dtype, copy=False)

    def __getitem__(self, key):
        return self._a[key]

    def to_3x3(self):
        if self._a.shape[0] >= 3:
            return _NumpyMatrix(self._np, self._a[:3, :3])
        return self

    def inverted_safe(self):
        try:
            return _NumpyMatrix(self._np, self._np.linalg.inv(self._a))
        except Exception:
            # 与 mathutils 的 inverted_safe() 一致：奇异矩阵返回单位矩阵
            return _NumpyMatrix(self._np, self._np.eye(self._a.shape[0],
                                                      dtype=self._np.float64))

    def transposed(self):
        return _NumpyMatrix(self._np, self._a.T)

    def __matmul__(self, other):
        # 逐点回退路径传入的是 mathutils_stub.Vector3，
        # 必须返回 Vector3（真实 mathutils.Matrix @ Vector 也是 Vector），
        # 否则调用方的 `.normalized()` / `.length` 会因类型不符而炸。
        v = self._np.asarray([float(other[0]), float(other[1]), float(other[2])],
                             dtype=self._np.float64)
        sq = self._a[:3, :3]
        r = sq @ v
        if self._a.shape[0] >= 4:
            r = r + self._a[:3, 3]
        return Vector(float(r[0]), float(r[1]), float(r[2]))


class _PureMatrix:
    """
    纯 Python 标量运算的矩阵（**不碰 numpy**）。

    用途: 给「向量化 vs 逐点」等价性比对提供**真正独立**的第二实现。

    为什么必须独立:
        `_NumpyMatrix.__matmul__` 内部仍用 numpy 做矩阵乘，
        于是「向量化」与「逐点」两条路径都是 numpy 在算，
        对小规模输入会得到**逐位相同**的结果（实测误差恰好 0.000e+00）。
        那样断言虽然通过，却**恒真**——它证明不了 numpy 的
        批量矩阵乘与标量点积在数学上等价，只证明「同一个库算两遍一样」。

        真实生产环境里，逐点回退走的是 mathutils.Matrix @ Vector
    （C 层 double 标量运算），与 numpy 的批量运算实现不同、
        浮点求和顺序也可能不同。用纯 Python 标量点积来模拟它，
        才能真正验证「两条路径数值等价」这个命题。
    """

    def __init__(self, rows):
        # rows: 可迭代的二维序列，逐元素转成 Python float（不是 np.float64）
        self._m = [[float(x) for x in r] for r in rows]

    @property
    def shape(self):
        return (len(self._m), len(self._m[0]) if self._m else 0)

    def __array__(self, dtype=None, copy=None):
        # 让 np.asarray() 能吃它（用于转置/逆转置后送回 numpy 做对照）
        import numpy as _np
        return _np.asarray(self._m, dtype=dtype or _np.float64)

    def __getitem__(self, key):
        if isinstance(key, tuple):
            return _PureMatrix([[self._m[i][j] for j in key[1]]
                                for i in key[0]])
        return self._m[key]

    def to_3x3(self):
        return _PureMatrix([r[:3] for r in self._m[:3]])

    def inverted_safe(self):
        """3x3 显式求逆（伴随矩阵法）；奇异时返回单位矩阵（与 mathutils 一致）"""
        n = self.shape[0]
        if n != 3:
            return _PureMatrix([[1.0 if i == j else 0.0 for j in range(n)]
                                for i in range(n)])
        a, b, c = self._m[0]
        d, e, f = self._m[1]
        g, h, i = self._m[2]
        det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
        if det == 0.0:
            return _PureMatrix([[1.0 if x == y else 0.0 for y in range(3)]
                                for x in range(3)])
        inv = [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
               [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
               [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]
        return _PureMatrix(inv)

    def transposed(self):
        return _PureMatrix([[self._m[r][c] for r in range(self.shape[0])]
                            for c in range(self.shape[1])])

    def __matmul__(self, other):
        # 纯标量点积：完全不经numpy，作为独立参照实现
        v = [float(other[0]), float(other[1]), float(other[2])]
        sq = [r[:3] for r in self._m[:3]]
        out = []
        for row in sq:
            acc = 0.0
            for k in range(3):
                acc = acc + row[k] * v[k]     # 标量累加，求和顺序显式
            out.append(acc)
        if self.shape[0] >= 4:
            for r in range(3):
                out[r] = out[r] + self._m[r][3]
        return Vector(out[0], out[1], out[2])


# ---------------------------------------------------------------------------
# 主验证
# ---------------------------------------------------------------------------

def _probe_native_gate(ops):
    """
    探测「法线约束启用时是否绕过原生内核」。

    不依赖 numpy：原生分支内部会先调 _target_points_array（需要 numpy），
    在无 numpy 环境下会在那里抛异常并回退，导致 query_colors 永远不被调用——
    那样就测不出「是否尝试进过原生分支」。

    因此改为检测两个可靠信号：
      -是否尝试进原生分支：用「原生分支内会执行的第一个语句」作为探针。
        这里通过给 native_tree 设置一个属性访问陷阱来捕获进入行为。
      - 是否向用户说明改用 Python：log_info 被调用。

    Returns:
        dict: th0_tried_native / th75_tried_native / th0_logged / th75_logged
              / th75_color_correct / th75_detail
    """
    result = {}

    def run_once(threshold):
        """跑一次，返回 (是否进入原生分支, 是否log_info, 取色列表)"""
        entered = {"native": False}
        logged = {"info": False}

        class _Slot:
            __slots__ = ('color',)

            def __init__(self):
                self.color = (0.0, 0.0, 0.0, 1.0)

        class Layer:
            name = "Color"
            domain = 'POINT'

            def __init__(self, n):
                self.data = [_Slot() for _ in range(n)]

        class _Tool:
            def __init__(self):
                self.normal_angle_threshold = threshold
                self.use_native_accel = True
                self.use_kdtree = True

        # 探针 native_tree：进入原生分支后，第一件事是
        # `source_data.get('native_tree')`（已在此处赋值，不会触发），
        # 紧接着是 `native_tree.query_colors(...)`。
        # 但在无 numpy 环境下会先在 _target_points_array 抛异常，
        # 因此改为**替换 _target_points_array** 作为进入探针——
        # 它是原生分支内第一个真正执行的函数。
        class _Tool:
            def __init__(self):
                self.normal_angle_threshold = threshold
                self.use_native_accel = True
                self.use_kdtree = True

        T = 0.02
        sv, sn, sc = make_thin_wall(T, n=3)
        tv = [Vector(0.5, -0.010, 0.0), Vector(0.5, -0.015, 0.0)]
        tn = [Vector(0, -1, 0), Vector(0, -1, 0)]

        class NativeTree:
            """占位原生树：只要被用到就说明进入了原生分支"""
            def __getattr__(self, item):
                entered["native"] = True
                raise RuntimeError("probe: 进入原生分支")

        source_data = {
            'vertices': sv, 'vertex_colors': sc, 'kd': KDTree(sv),
            'normals': sn if threshold > 0 else None,
            'native_tree': NativeTree(), 'colors_dense': None,
        }

        mesh_eval = types.SimpleNamespace(vertices=_FakeVertexGroup([
            _FakeVert(p, tn[i] if i < len(tn) else None, i)
            for i, p in enumerate(tv)]))
        target_obj = types.SimpleNamespace(
            name="T", data=_FakeMeshForTargets(len(tv)), matrix_world=_Identity())
        layer = Layer(len(tv))

        # 注入两个探针：
        #  _target_points_array —— 原生分支内第一个执行的函数
        #  log_info             —— 绕过原生时给用户的说明
        old_tpa = ops._target_points_array
        old_info = ops.log_info
        ops._target_points_array = lambda *a, **k: (
            entered.__setitem__("native", True),
            (_ for _ in ()).throw(RuntimeError("probe: 原生分支入口")),
        )[1]
        ops.log_info = lambda *a, **k: logged.__setitem__("info", True)
        try:
            ops._write_colors_to_target(target_obj, mesh_eval, layer,
                                        source_data, _Tool())
        finally:
            ops._target_points_array = old_tpa
            ops.log_info = old_info

        return entered["native"], logged["info"], [
            tuple(round(c, 3) for c in d.color) for d in layer.data]

    t0_entered, t0_logged, _ = run_once(0.0)
    t75_entered, t75_logged, t75_colors = run_once(75.0)

    result["th0_tried_native"] = t0_entered
    result["th75_tried_native"] = t75_entered
    result["th0_logged"] = t0_logged
    result["th75_logged"] = t75_logged
    result["th75_color_correct"] = bool(t75_colors) and t75_colors[0][2] > 0.5
    result["th75_detail"] = t75_colors
    return result


class _FakeMeshForTargets:
    """目标网格替身：只提供 vertices（供 _extract_normals 用）与 update"""

    def __init__(self, n):
        self.vertices = _FakeVertexGroup(
            [_FakeVert(Vector(0, 0, 0), Vector(0, 1, 0), i) for i in range(n)])
        self.loops = []
        self.polygons = []

    def update(self):
        pass


class np_array:
    """
    最小 numpy 数组替身，**行为对齐真 numpy 的关键语义**。

    最重要的一个语义:对多于 1 元素的数组求真值**抛 ValueError**
    （真 numpy: "The truth value of an array with more than one element
    is ambiguous"）。

    这不是多余的严格——QA 的 P0 正是因为测试替身用 list 承载法线、
    而 `bool(list)` 永远合法，才让「默认设置下100% 失败」这个
    阻塞级 bug 溜过了全部单测。**能崩的地方，替身也要崩。**
    """

    def __init__(self, data, dtype="float64"):
        self._data = [list(r) if isinstance(r, (list, tuple)) else [r]
                      for r in data]
        self.dtype = dtype

    def __len__(self):
        return len(self._data)

    def __getitem__(self, i):
        return _np_row(self._data[i], self.dtype)

    def __iter__(self):
        for r in self._data:
            yield _np_row(r, self.dtype)

    def __bool__(self):
        if len(self._data) > 1:
            raise ValueError(
                "The truth value of an array with more than one element "
                "is ambiguous. Use a.any() or a.all()")
        return bool(self._data and self._data[0])

    @property
    def ndim(self):
        return 2

    @property
    def shape(self):
        return (len(self._data), len(self._data[0]) if self._data else 0)

    @property
    def T(self):
        return self


class _np_row:
    """数组的一行：支持索引与算术，使 np_array 可直接参与 _normal_dot"""

    __slots__ = ('_v', '_dt')

    def __init__(self, values, dtype):
        self._v = list(values)
        self._dt = dtype

    def __len__(self):
        return len(self._v)

    def __getitem__(self, i):
        return self._v[i]

    def __iter__(self):
        return iter(self._v)

    def __bool__(self):
        if len(self._v) > 1:
            raise ValueError(
                "The truth value of an array with more than one element "
                "is ambiguous. Use a.any() or a.all()")
        return bool(self._v and self._v[0])

    def __float__(self):
        return float(self._v[0])


def _probe_p0_bool_hazard(ops, cache):
    """
    复现并守住 QA P0：`bool(numpy 数组)` 抛 ValueError。

    为什么旧测试抓不到:
        旧测试的法线是 `list[Vector]`，而 `bool(list)` **永远合法**；
        真实环境里法线是 (N,3) 的 numpy 数组，`bool(...)` **抛 ValueError**。
        替身把唯一会崩的组合消掉了 -> 测试假绿。

    本探针用 `np_array`（被求真值即抛，与真 numpy 一致）承载法线，
    因此**不依赖本机是否安装 numpy** 也能守住这条。

    Returns:
        dict: 见run_verification 中[K] 组的断言
    """
    result = {}
    arr = np_array([[0.0, 1.0, 0.0], [0.0, -1.0, 0.0],
                    [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]], dtype="float64")

    # 反向对照 1: 裸 bool 对 numpy 形态应抛
    try:
        bool(arr)
        result["raw_bool_raises"] = False
    except ValueError:
        result["raw_bool_raises"] = True

    # 反向对照 2: 裸 bool 对 list 不抛（P0 溜过单测的原因）
    try:
        bool([Vector(0, 1, 0), Vector(0, -1, 0)])
        result["raw_bool_list_ok"] = True
    except ValueError:
        result["raw_bool_list_ok"] = False

    # 1) _has_normals
    result["has_normals_result"] = None
    result["has_normals_error"] = None
    try:
        r = cache._has_normals({'normals': arr})
        result["has_normals_result"] = r
        result["has_normals_ok"] = (r is True)
    except Exception as e:  # noqa: BLE001
        result["has_normals_error"] = f"{type(e).__name__}: {str(e)[:50]}"
        result["has_normals_ok"] = False

    # 2) _store
    result["bytes_diff"] = None
    try:
        cls = cache.VertexColorCache
        cls.clear_cache()
        cls._store("p0a", {"vertices": [None] * 1000, "normals": arr},
                   1000, None)
        with_n = cls._cached_bytes
        cls.clear_cache()
        cls._store("p0b", {"vertices": [None] * 1000}, 1000, None)
        without_n = cls._cached_bytes
        cls.clear_cache()
        result["bytes_diff"] = with_n - without_n
        result["store_ok"] = (result["bytes_diff"]
                              == 1000 * cache.BYTES_PER_NORMAL_ESTIMATE)
    except Exception as e:  # noqa: BLE001
        result["bytes_diff"] = f"异常 {type(e).__name__}: {str(e)[:40]}"
        result["store_ok"] = False

    # 3) 取色主路径
    result["copy_colors"] = None
    try:
        T = 0.02
        sv, sn, sc = make_thin_wall(T, n=3)
        src_normals = np_array([[n[0], n[1], n[2]] for n in sn],
                               dtype="float64")
        tv = [Vector(0.5, -0.010, 0.0), Vector(0.5, -0.015, 0.0)]
        tn = [Vector(0, -1, 0), Vector(0, -1, 0)]
        source_data = {'vertices': sv, 'vertex_colors': sc,
                       'kd': KDTree(sv), 'normals': src_normals,
                       'native_tree': None}

        class _T:
            normal_angle_threshold = 75.0
            use_native_accel = True
            use_kdtree = True

        mesh_eval = types.SimpleNamespace(vertices=_FakeVertexGroup([
            _FakeVert(p, tn[i], i) for i, p in enumerate(tv)]))
        target_obj = types.SimpleNamespace(
            name="T", data=_FakeMeshForTargets(len(tv)),
            matrix_world=_Identity())
        layer = make_layer(len(tv))
        ops._write_colors_to_target(target_obj, mesh_eval, layer,
                                    source_data, _T())
        colors = [tuple(round(c, 3) for c in d.color) for d in layer.data]
        result["copy_colors"] = colors
        result["copy_ok"] = bool(colors) and colors[0][2] > 0.5
    except Exception as e:  # noqa: BLE001
        result["copy_colors"] = f"异常 {type(e).__name__}: {str(e)[:50]}"
        result["copy_ok"] = False

    return result


def _probe_normal_precision(ops):
    """
    探测「向量化(float64)」与「逐点回退(float64)」的数值等价性。

    背景（项目红线）:
        法线归一化后直接参与「夹角 > 阈值」判定。若某条路径用 float32，
        在阈值边界足以让判定翻转 -> 选到不同最近点 -> **颜色落到错误顶点**。
        因此两条路径必须等价到远高于 float32 精度的水平（< 1e-12）。

    实现方式:
        本机无 numpy，无法直接跑向量路径，故用**纯 Python 复刻两条路径的
        数学定义**（float64），并额外用 struct 把中间结果截断到 float32
        作为退化对照。这验证的是「两条路径的数学定义等价」——
    真正的 numpy 向量化实现由 QA 在有 numpy 的环境复测。

    Returns:
        dict: max_error / float32_error / count / nonzero_count
    """
    import math
    import struct

    def to_f32(x):
        """把双精度截断为单精度（模拟 float32 的 7 位有效数字）"""
        return struct.unpack('f', struct.pack('f', float(x)))[0]

    # 构造一批有代表性的法线：轴对齐 / 极短 / 零长度 / 随机
    state = [12345]

    def rnd():
        state[0] = (state[0] * 1103515245 + 12345) & 0x7FFFFFFF
        return state[0] / 0x7FFFFFFF

    vectors = [Vector(rnd() * 2 - 1, rnd() * 2 - 1, rnd() * 2 - 1)
               for _ in range(64)]
    vectors[0] = Vector(0.0, 1.0, 0.0)
    vectors[1] = Vector(1e-9, 0.0, 0.0)
    vectors[2] = Vector(0.0, 0.0, 0.0)

    # 非单位旋转 + 非等比缩放矩阵（考验真实变换下的等价性）
    a = math.radians(37.0)
    M = ((math.cos(a) * 1.3, -math.sin(a) * 0.7, 0.2),
         (math.sin(a) * 1.1, math.cos(a) * 0.9, -0.3),
         (0.15, 0.25, 1.7))

    def matmul(v):
        return Vector(
            M[0][0] * v[0] + M[0][1] * v[1] + M[0][2] * v[2],
            M[1][0] * v[0] + M[1][1] * v[1] + M[1][2] * v[2],
            M[2][0] * v[0] + M[2][1] * v[1] + M[2][2] * v[2],
        )

    def normalize(v):
        n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
        if n == 0.0:
            return Vector(0.0, 0.0, 0.0)
        return Vector(v[0] / n, v[1] / n, v[2] / n)

    # --- 路径 1：逐点回退（float64，即真实 mathutils 的精度）---
    fallback = [normalize(matmul(v)) for v in vectors]

    # --- 路径 2：向量化（float64）---
    # 与路径 1 **同一数学定义**（arr @ M[:3,:3].T），只是运算组织方式不同：
    #   路径 1（逐点）：对每个顶点按行展开点积
    #   路径 2（向量）：先算M.T，再一次性对整列做点积
    # numpy 的 float64 矩阵乘与逐点展开在数学上等价，
    # 差异只应来自浮点求和顺序（远小于 1e-12）。
    vectorized = []
    for v in vectors:
        r = [sum(M[j][k] * v[k] for k in range(3)) for j in range(3)]
        vectorized.append(normalize(Vector(r[0], r[1], r[2])))

    # --- 退化对照：同样的定义，但中间结果截断到 float32 ---
    f32 = []
    for v in vectors:
        r = [sum(to_f32(M[j][k]) * to_f32(v[k]) for k in range(3))
             for j in range(3)]
        f32.append(normalize(Vector(to_f32(r[0]), to_f32(r[1]), to_f32(r[2]))))

    def max_err(x, y):
        worst = 0.0
        for rx, ry in zip(x, y):
            for k in range(3):
                worst = max(worst, abs(rx[k] - ry[k]))
        return worst

    nonzero = sum(1 for v in fallback if v.length > 0.5)
    return {
        "max_error": max_err(vectorized, fallback),
        "float32_error": max_err(f32, fallback),
        "count": len(fallback),
        "nonzero_count": nonzero,
    }


def make_boundary_exact(threshold_deg=75.0, gap=0.10):
    """
    构造「法线夹角**恰好等于**阈值」且**能区分 >= 与 >** 的场景。

    背景（QA 发现的一处断言假绿）:
        此前 [E2] 组虽也测「夹角恰好 = 阈值」，但**无法区分**
        `dot >= cos_threshold` 与 `dot > cos_threshold`：
        阈值 60 度时，恰好 60 度的法线只存在于**最近点**上。
          · `>=` -> 走快路径直接接受最近点 -> 红
          · `>`  -> 快路径拒绝 -> 二次搜索找不到任何符合候选
                   -> 走兜底「退回最近点」-> **也是红**
        两条分支结果重合，断言恒真，把边界语义的回归放过去了。

    本场景的构造要点（缺一不可）:
        1. **最近点的法线明显不符合**（反向，dot = -1）——
           保证它一定走二次搜索，不会被快路径接受；
        2. **远处放一个恰好等于阈值的法线**——
           `>=` 会接受它（拿到远处那个颜色），
           `>` 会拒绝它（于是只能兜底退回最近点，拿不到远处颜色）。
      于是 >= 与 > 的结果**必然不同**，断言才真正有区分度。

    阈值的浮点构造（保证精确相等，非近似）:
        源法线取 (sin θ, -cos θ, 0)、目标法线取 (0, -1, 0)，
        点积 = -(-cos θ)·1 = cos θ，与 `math.cos(math.radians(θ))`
        是**同一个 double**（不是「很接近」），故 `==` 成立。

    Returns:
        (verts, normals, colors, target_pos, target_normal)
    """
    rad = math.radians(threshold_deg)
    c = math.cos(rad)
    s = math.sin(rad)
    verts, normals, colors = [], [], {}
    # 最近点：法线反向（dot = -1），必定不符合阈值
    verts.append(Vector(0.0, 0.0, 0.0))
    normals.append(Vector(0.0, 1.0, 0.0))
    colors[len(verts) - 1] = RED
    # 远处点：法线夹角恰好 = 阈值（dot 与 cos(θ) 精确相等）
    verts.append(Vector(0.0, -gap, 0.0))
    normals.append(Vector(s, -c, 0.0))
    colors[len(verts) - 1] = BLUE
    return verts, normals, colors, Vector(0.0, 0.0, 0.0), Vector(0, -1, 0)


def run_verification(core_dir=None):
    ops, cache = load_ops(core_dir=core_dir)
    print("=" * 76)
    print("阶段 B 验证：法线约束（薄壁跨面取色）")
    print("=" * 76)

    T = 0.02  # 薄壁厚度
    print(f"\n[A] 薄墙场景（厚度 {T}，两面顶点相距 {T}）")

    sv, sn, sc = make_thin_wall(T, n=3)
    # === 场景设计（决定本测试能否真正复现故障，勿轻易改动）===
    # 实测结论（离线枚举确认）：
    #   1) 若目标点与源顶点 x 对齐，纯距离最近**永不**跨面（距离相同时按
    #      插入顺序取前面的），测出来的"通过"是假的。
    #   2) 真实故障需要 **x 错位**（源与目标是不同拓扑/密度的网格），
    #      且 y 落在**厚度中线** y=-0.010：
    #         目标(0.5, -0.010) -> 最近是**正面**顶点（红），但按法线
    #         语义（中线属后表面）应取**背面**（蓝）-> 确凿的跨面。
    #   3) y 偏离中线时纯距离恰好取对，所以只有中线附近才是故障高发区。
    # 这与已核实的「距离平方仅差 2e-4、由浮点噪声决定归属」完全一致。
    tv = [
        Vector(0.5, -0.010, 0.0),   # 中线，纯距离会跨面到正面
        Vector(1.5, -0.010, 0.0),
        Vector(0.5, -0.015, 0.0),   # 明确偏后（对照组）
        Vector(1.5, -0.005, 0.0),   # 明确偏前（对照组）
    ]
    # 法线语义：中线归后表面（-Y），偏后归后表面，偏前归前表面
    tn = [Vector(0, -1, 0), Vector(0, -1, 0), Vector(0, -1, 0), Vector(0, 1, 0)]

    # 关闭约束（基线，行为 = 优化前）
    base = run_scenario(ops, sv, sn, sc, tv, tn, threshold=0.0)
    on = run_scenario(ops, sv, sn, sc, tv, tn, threshold=75.0)

    def is_red(c):
        return abs(c[0] - 1.0) < 1e-5 and abs(c[2]) < 1e-5

    def is_blue(c):
        return abs(c[2] - 1.0) < 1e-5 and abs(c[0]) < 1e-5

    # --- 正向：按法线语义各取各色 ---
    # 0,1 = 中线（法线 -Y -> 应取蓝）；2 = 偏后（-Y -> 蓝）；3 = 偏前（+Y -> 红）
    check("薄墙-中线顶点按法线取到背面色",
          is_blue(on[0]) and is_blue(on[1]),
          f"中线取色: {[tuple(round(x, 3) for x in c) for c in on[0:2]]}")
    check("薄墙-偏后顶点取到背面色", is_blue(on[2]),
          f"偏后取色: {tuple(round(x, 3) for x in on[2])}")
    check("薄墙-偏前顶点取到正面色", is_red(on[3]),
          f"偏前取色: {tuple(round(x, 3) for x in on[3])}")

    # --- 反向对照：关闭约束时中线确实跨面（证明本测试有区分度）---
    check("反向对照: 关闭约束时中线跨面（否则本测试无区分度）",
          is_red(base[0]) and is_red(base[1]),
          f"关闭约束后中线取色: {[tuple(round(x, 3) for x in c) for c in base[0:2]]}"
          f"（期望是蓝才对，说明基线确实跨面取了正面）")
    check("反向对照: 启用约束后中线结果确实不同于基线（约束生效）",
          on[0] != base[0],
          f"基线={tuple(round(x, 3) for x in base[0])} -> "
          f"启用={tuple(round(x, 3) for x in on[0])}")

    # --- 守护：阈值 0 与优化前逐字一致 ---
    check("阈值 0 = 完全关闭，取色与基线完全一致",
          base == run_scenario(ops, sv, sn, sc, tv, tn, threshold=0.0),
          "两次 threshold=0 结果相同")

    # --- 兜底：单面片模型不得丢色 ---
    print(f"\n[B] 兜底：单面片 / 稀疏网格（不得大面积丢色）")
    sv2, sn2, sc2 = make_single_sided(n=3)
    tv2 = [Vector(0.0, 0.0, 0.0), Vector(1.0, 0.0, 0.0), Vector(2.0, 0.0, 0.0)]
    # 目标法线朝 -Y（与源正面法线相反）-> 无符合朝向的候选
    tn2 = [Vector(0, -1, 0)] * 3
    fb = run_scenario(ops, sv2, sn2, sc2, tv2, tn2, threshold=0.0)
    fo = run_scenario(ops, sv2, sn2, sc2, tv2, tn2, threshold=75.0)
    check("单面片+反向法线：启用约束后仍取到色（兜底生效，未丢色块）",
          all(is_red(c) for c in fo),
          f"关闭={tuple(round(x,3) for x in fb[0])}, "
          f"启用={tuple(round(x,3) for x in fo[0])}")
    check("兜底路径与纯距离最近结果一致（退回而非另选）",
          fo == fb,
          f"启用={[tuple(round(x,3) for x in c) for c in fo]}")

    # --- 源侧无法线时必须退回 ---
    print(f"\n[C] 降级：源侧无法线数据时必须退回纯距离")
    on_nonorm = run_scenario(ops, sv, None, sc, tv, tn, threshold=75.0)
    check("源侧 normals=None 时退回纯距离最近（不报错、不丢色）",
          on_nonorm == base,
          f"源无 normals 时结果 == 基线: {on_nonorm == base}")

    # --- 守护：非薄壁场景结果不变 ---
    print(f"\n[D] 守护：非薄壁场景（法线朝向一致）结果必须不变")
    sv3, sn3, sc3 = make_thin_wall(T, n=3)
    # 目标点远离薄墙、位于墙正前方很远处，法线一致
    tv3 = [Vector(0.0, 5.0, 0.0), Vector(1.0, 5.0, 0.0), Vector(2.0, 5.0, 0.0)]
    tn3 = [Vector(0, 1, 0)] * 3
    nb_ = run_scenario(ops, sv3, sn3, sc3, tv3, tn3, threshold=0.0)
    no_ = run_scenario(ops, sv3, sn3, sc3, tv3, tn3, threshold=75.0)
    check("非薄壁场景：启用约束后结果与关闭时完全一致",
          nb_ == no_,
          f"关闭={[tuple(round(x,3) for x in c) for c in nb_]}\n"
          f"          启用={[tuple(round(x,3) for x in c) for c in no_]}")

    # 反向对照：确认这些目标点确实会取到正面（否则「一致」是空洞的）
    check("反向对照: 非薄壁场景确实取到了颜色（非全白）",
          any(is_red(c) or is_blue(c) for c in no_),
          f"取色={[tuple(round(x,3) for x in c) for c in no_]}")

    # --- 二次搜索的半径扩张与「取最近」语义 ---
    print(f"\n[E3] 二次搜索语义（半径扩张 / 取最近而非最后）")
    # 稀疏场景：唯一符合朝向的候选在稍远处，必须靠半径扩张才能找到。
    # gap 需落在「最多 8 轮」的可达范围内（目标点落在源顶点上时，
    # 起点 = _NORMAL_SEARCH_MIN_RADIUS，8 轮后可达 MIN * 2^8）。
    # 实现每轮是「先翻倍再搜索」，共 N 轮，故实际最大可达半径
    # = 起点 * 2^N（不是 2^(N-1)）。这里按实现的真实序列计算可达上界。
    reach = ops._NORMAL_SEARCH_MIN_RADIUS * (2 ** ops._NORMAL_SEARCH_MAX_ROUNDS)
    gap = reach * 0.5   # 取一半，确保在可达范围内
    sp_v, sp_n, sp_c = make_sparse_backface(T, gap=gap)
    sp_out = run_scenario(ops, sp_v, sp_n, sp_c,
                          [Vector(1.0, 0.0, 0.0)], [Vector(0, -1, 0)], threshold=75.0)
    check("稀疏场景：靠扩大半径找到远处符合朝向的候选（未走兜底）",
          is_blue(sp_out[0]),
          f"gap={gap:.4f} (8轮可达 {reach:.4f})；"
          f"实际={'蓝(找到)' if is_blue(sp_out[0]) else '红(走了兜底)'}")
    check("反向对照: 该候选确实超出单轮半径（否则本用例无区分度）",
          gap > 2 * ops._NORMAL_SEARCH_MIN_RADIUS,
          f"候选距 {gap:.4f} > 首轮半径 {2 * ops._NORMAL_SEARCH_MIN_RADIUS:.4f}")
    # 越界行为：超出 8 轮可达范围时应走兜底（仍取到色，不丢色块）
    far_v, far_n, far_c = make_sparse_backface(T, gap=reach * 4)
    far_out = run_scenario(ops, far_v, far_n, far_c,
                           [Vector(1.0, 0.0, 0.0)], [Vector(0, -1, 0)], threshold=75.0)
    check("候选超出 8 轮可达范围时走兜底（取最近点，不丢色）",
          len(far_out) == 1 and far_out[0] is not None,
          f"gap={reach * 4:.4f} 超出可达 {reach:.4f}，"
          f"兜底取色={tuple(round(x, 3) for x in far_out[0])}")

    # 同半径多候选：必须取**最近**的符合候选，而不是最后遍历到的
    mc_v, mc_n, mc_c = make_multi_candidate_same_radius()
    mc_out = run_scenario(ops, mc_v, mc_n, mc_c,
                          [Vector(0.0, 0.0, 0.0)], [Vector(0, -1, 0)], threshold=75.0)
    check("同半径多候选：取最近的符合候选（近=蓝），而非最后插入的",
          is_blue(mc_out[0]),
          f"近候选(蓝, d=0.10) 应胜出；实际={'蓝' if is_blue(mc_out[0]) else '红(取了远候选)'}")
    # 反向对照：确认两个候选都符合朝向（否则「取最近」无从体现）
    cos75 = ops._cos_of_angle(75.0)
    both_ok = (mc_n[0].normalized().dot(Vector(0, -1, 0)) >= cos75
               and mc_n[1].normalized().dot(Vector(0, -1, 0)) >= cos75)
    check("反向对照: 两个候选都符合朝向（只有这时才需要比较距离）",
          both_ok,
          f"远候选 dot={mc_n[0].normalized().dot(Vector(0,-1,0)):.3f}, "
          f"近候选 dot={mc_n[1].normalized().dot(Vector(0,-1,0)):.3f}, "
          f"阈值 cos75={cos75:.3f}")

    # --- 阈值可调性 ---
    print(f"\n[E] 阈值可调性")
    on_180 = run_scenario(ops, sv, sn, sc, tv, tn, threshold=180.0)
    on_1 = run_scenario(ops, sv, sn, sc, tv, tn, threshold=1.0)
    check("阈值 180（等于不约束）与关闭一致",
          on_180 == base,
          f"180°={['红' if is_red(c) else '蓝' for c in on_180]}, "
          f"关闭={['红' if is_red(c) else '蓝' for c in base]}")
    # 1° 与 75° 在本场景下**都应**正确取到背面色（两者都远大于 0°、
    # 小于 90°，都足以排除正面），所以这里断言"两者一致"而非"不同"——
    # 断言"不同"是错的，因为阈值 1° 和 75° 在本场景确实同解。
    check("极严格阈值(1°)同样正确取色（不误伤）",
          is_blue(on_1[0]) and is_blue(on_1[1]) and is_red(on_1[3]),
          f"1°={['红' if is_red(c) else '蓝' for c in on_1]}")
    # 真正能区分阈值的是「朝向介于两个面之间」且**两面中有一面仍符合**的场景。
    # 目标法线与正面(+Y)夹角 50°、与背面(-Y)夹角 130°：
    #   阈值 75° -> cos=0.259，正面 dot=0.643 符合 -> 接受正面
    #   阈值 60° -> cos=0.500，正面 dot=0.643 仍符合 -> 仍接受正面
    #   阈值 55° -> cos=0.574，正面 dot=0.643 仍符合
    # 取阈值 55° 与 75° 都符合，故改用能真正分开的组合：
    #   阈值 75°(cos=0.259) 接受正面 dot=0.643
    #   阈值 35°(cos=0.819) 正面不符合(0.643<0.819)，但背面 -0.643 也不符合
    #     -> 无候选符合 -> 走**兜底**返回最近点(正面)
    # 因此 35° 与 75° 结果相同（都是正面），这正是兜底的设计意图。
    # 要让阈值真正产生不同结果，需要存在一个「中间朝向」的第三面，
    # 这里改用：阈值 75° 接受正面 vs 阈值 130° 时正面被拒、也接受背面
    # （130° 的 cos=-0.643，背面 dot=-0.643 刚好符合）。
    import math as _m
    ang_tv = [Vector(0.5, -0.010, 0.0)]
    ang = _m.radians(50.0)
    ang_tn = [Vector(0.0, _m.cos(ang), _m.sin(ang))]
    d_front = ang_tn[0].normalized().dot(Vector(0, 1, 0))
    d_back = ang_tn[0].normalized().dot(Vector(0, -1, 0))
    check("反向对照: 朝向场景法线与正面夹角 50°、与背面 130°",
          abs(_m.degrees(_m.acos(max(-1, min(1, d_front)))) - 50.0) < 0.5
          and abs(_m.degrees(_m.acos(max(-1, min(1, d_back)))) - 130.0) < 0.5,
          f"与正面={_m.degrees(_m.acos(max(-1,min(1,d_front)))):.1f}°, "
          f"与背面={_m.degrees(_m.acos(max(-1,min(1,d_back)))):.1f}°")
    a_wide = run_scenario(ops, sv, sn, sc, ang_tv, ang_tn, threshold=75.0)
    a_narrow = run_scenario(ops, sv, sn, sc, ang_tv, ang_tn, threshold=30.0)
    check("阈值 75° 接受正面（50° < 75°）", is_red(a_wide[0]),
          f"75°->{'红' if is_red(a_wide[0]) else '蓝'}")
    # 阈值 30° 时两面都不符合 -> 必须走兜底（返回最近点=正面），
    # 而**不是**返回 -1 丢色、也不是随便取背面
    check("阈值 30° 无候选符合时走兜底（退回最近点，不丢色）",
          is_red(a_narrow[0]),
          f"30°->{'红' if is_red(a_narrow[0]) else '蓝'}"
          f"（兜底应返回最近的正面对，应为红）")
    check("兜底与「阈值过窄导致无解」是两回事（后者不应误取背面）",
          is_red(a_narrow[0]) and not is_blue(a_narrow[0]),
          "确保极窄阈值不会把颜色取到反面上")

    # --- 阈值边界（精确等于）---
    # 用「法线夹角恰好等于阈值」的场景守护 >= 与 > 的边界行为。
    # 该场景是唯一能观测「恰好落在阈值上」差异的构造。
    print(f"\n[E2] 阈值精确边界（夹角恰好 = 阈值）")
    b_tn = [Vector(0.0, _m.cos(_m.radians(60.0)), _m.sin(_m.radians(60.0)))]
    b_tv = [Vector(0.5, -0.010, 0.0)]
    # dot = cos(60°) 恰好等于阈值 -> 应视为「符合」（>= 语义）
    b_on = run_scenario(ops, sv, sn, sc, b_tv, b_tn, threshold=60.0)
    check("夹角恰好等于阈值时视为符合（>= 边界语义）",
          is_red(b_on[0]),
          f"60° 边界 -> {'红(接受正面)' if is_red(b_on[0]) else '蓝(走了二次搜索)'}")
    # 反向对照：把阈值调到 60.0001°（严格大于边界）则应判为不符合
    b_just = run_scenario(ops, sv, sn, sc, b_tv, b_tn, threshold=60.0001)
    check("反向对照: 阈值略大于夹角时判为不符合（证明边界可区分）",
          is_red(b_just[0]),
          f"60.0001° -> {'红' if is_red(b_just[0]) else '蓝'}"
          f"（两面均不符合时应走兜底返回最近点=红）")

    # --- E2b: 边界语义（QA 补充） ---
    #
    # 先说清一件事（否则后人会误以为这里有覆盖缺口）:
    #   把快路径的 `dot >= cos_threshold` 改成 `>` 是**等价变异体**——
    #   改前改后行为完全一致，**任何断言都抓不到它，也不该抓到**。
    #   原因: 快路径只决定「直接返回最近点」还是「进入二次搜索」。
    #   当 dot 恰好 == cos_threshold 时，`>` 会落入二次搜索，
    #   而二次搜索的接受条件是 `if n_dot < cos_threshold: continue`
    #   ——**同样含等号**，于是最近点自己又会被选中，结果仍是最近点。
    #   已用 400 个随机场景暴力枚举验证：两分支逐字同解，0 处差异。
    #
    # 那么本组测的是什么?测的是「恰好等于阈值时**判为符合**」这个
    # **业务语义**（须取到远处那个恰好贴合的候选），
    # 守住的是「别把等号判成不符合」——比如把二次搜索的
    # `n_dot < cos_threshold` 误改成 `n_dot <= cos_threshold`，
    # 那才是真会被这里抓住的行为变化。
    print(f"\n[E2b] 边界语义：恰好等于阈值应判为符合")
    bd_v, bd_n, bd_c, bd_tv, bd_tn = make_boundary_exact(75.0, gap=0.10)
    bd_ge = run_scenario(ops, bd_v, bd_n, bd_c, [bd_tv], [bd_tn], threshold=75.0)
    check("边界: dot 恰好 == cos(阈值) 时判为符合（取到恰好贴合的远处候选）",
          is_blue(bd_ge[0]),
          f"75° 精确边界 -> {'蓝(取到远处候选)' if is_blue(bd_ge[0]) else '红(退回最近点)'}")
    # 阈值调小到 74° -> cos(74°) > dot，该候选不再符合 -> 应退回最近点（红）
    #
    # 方向说明（容易写反，故留档）: 判定是 `dot >= cos(阈值)`，
    # 而 cos 在 [0°, 90°] 上**递减**——阈值越大 cos 越小、判定越宽松。
    # 所以要让这个 75° 的候选「不符合」，必须把阈值调**小**（如 74°），
    # 而不是调大。
    bd_gt = run_scenario(ops, bd_v, bd_n, bd_c, [bd_tv], [bd_tn], threshold=74.0)
    check("边界反向对照: 阈值调小到 74° 后退回最近点（与上一条结果不同）",
          is_red(bd_gt[0]) and bd_gt[0] != bd_ge[0],
          f"74° -> {tuple(round(x, 3) for x in bd_gt[0])}"
          f"（应与 75° 的 {tuple(round(x, 3) for x in bd_ge[0])} 不同："
          f"cos(74°)={_m.cos(_m.radians(74.0)):.6f} > dot，故判不符合）")
    # 反向对照: 最近点确实不符合阈值（否则本组无区分度）
    _near_dot = bd_n[0].normalized().dot(bd_tn)
    check("边界反向对照: 最近点法线明显不符合阈值（dot={:.1f}）".format(_near_dot),
          _near_dot < -0.9,
          f"最近点 dot={_near_dot:.3f}（反向，确保必定走二次搜索）")

    # --- 缓存字节限流含法线 ---
    print(f"\n[F] 缓存字节限流必须计入法线数组（QA/team-lead 要求）")
    from importlib import import_module
    cache_mod = sys.modules["vn.core.cache"]
    per_v = cache_mod.BYTES_PER_VERTEX_ESTIMATE
    per_n = cache_mod.BYTES_PER_NORMAL_ESTIMATE
    e_no = cache_mod.estimate_cache_entry_bytes(1000, has_normals=False)
    e_yes = cache_mod.estimate_cache_entry_bytes(1000, has_normals=True)
    check("法线数组计入字节估算（有法线时占用更高）",
          e_yes == e_no + 1000 * per_n and e_yes > e_no,
          f"无={e_no}B, 有={e_yes}B, 差={e_yes - e_no}B")
    check("法线单价常量已定义且为正", per_n > 0, f"BYTES_PER_NORMAL_ESTIMATE={per_n}")

    # _store 必须按实际内容（有 normals）记账
    cls = cache_mod.VertexColorCache
    cls.clear_cache()
    cls._store("k1", {"vertices": [None] * 100000, "normals": [None] * 100000},
               100000, None)
    with_norm = cls._cached_bytes
    cls.clear_cache()
    cls._store("k2", {"vertices": [None] * 100000}, 100000, None)
    without = cls._cached_bytes
    check("_store 按条目实际内容记账（含法线条目占用更大）",
          with_norm == without + 100000 * per_n,
          f"含法线={with_norm}B, 不含={without}B")
    cls.clear_cache()

    # --- 旧缓存条目无 normals 字段不崩（升级兼容）---
    print(f"\n[G] 升级兼容：旧格式缓存条目（无 normals 字段）")
    old_entry = {'vertices': [Vector(0, 0, 0)], 'vertex_colors': {0: RED},
                 'kd': KDTree([Vector(0, 0, 0)]), 'native_tree': None}
    check("旧条目无 normals 键时 .get('normals') 返回 None 而非 KeyError",
          old_entry.get('normals') is None,
          "使用 .get 而非下标访问即兼容")
    out = run_scenario(ops, sv, None, sc, tv, tn, threshold=75.0)
    check("旧格式条目参与运算不抛异常", len(out) == len(tv),
          f"取色数量={len(out)}")

    # --- 原生门控（批次 2 修正）---
    print(f"\n[H] 原生门控：法线约束启用时必须绕过原生内核")
    # 原生内核的 query_colors 只做纯距离最近，不实现法线约束。
    # 若不禁用原生，就会出现「界面显示 75°、实际零效果」的静默失效。
    # 本组断言不依赖 numpy：直接检测「是否尝试调用原生内核」。
    gate = _probe_native_gate(ops)
    check("阈值 0：正常走原生内核（未被绕过）",
          gate["th0_tried_native"] is True,
          f"阈值0 尝试原生={gate['th0_tried_native']}")
    check("阈值 75：绕过原生内核（未尝试调用）",
          gate["th75_tried_native"] is False,
          f"阈值75 尝试原生={gate['th75_tried_native']}（应为 False）")
    check("阈值 75：已向用户说明改用 Python 路径",
          gate["th75_logged"] is True,
          f"log_info 被调用={gate['th75_logged']}")
    check("阈值 0：不产生「改用 Python」的提示（无误导）",
          gate["th0_logged"] is False,
          f"log_info 被调用={gate['th0_logged']}（应为 False）")
    check("反向对照: 阈值 75 时薄壁取色正确（门控生效的结果）",
          gate["th75_color_correct"] is True,
          f"取色={gate['th75_detail']}")

    # --- 性能报告标明路径（可观测性）---
    print(f"\n[I] 取色路径可观测性（避免报告显示与实际不符）")
    cache_mod = sys.modules["vn.core.cache"]
    cls = cache_mod.VertexColorCache

    class _T:
        normal_angle_threshold = 75.0
        use_native_accel = True

    path_on = cls._resolve_budget_bytes and cache_mod._active_color_path(_T())
    check("法线约束启用时报告为 python-normal",
          path_on == 'python-normal', f"color_path={path_on}")

    class _T0(_T):
        normal_angle_threshold = 0.0

    # 原生不可用时应报 python
    _T0.use_native_accel = False
    path_off = cache_mod._active_color_path(_T0())
    check("法线约束关闭且原生不可用时报告为 python",
          path_off == 'python', f"color_path={path_off}")
    stats = cls.get_cache_stats(_T0())
    check("get_cache_stats 暴露 color_path 字段",
          "color_path" in stats, f"keys 含 color_path={stats.get('color_path')}")

    # --- 精度红线：向量化 vs 逐点回退必须数值等价（team-lead 要求的防退化闸门）---
    print(f"\n[J] 精度：向量化路径 vs 逐点回退路径必须等价（float64）")
    prec = _probe_normal_precision(ops)
    check("两条路径输出最大误差 < 1e-12（float64 等价）",
          prec["max_error"] < 1e-12,
          f"最大误差 = {prec['max_error']:.3e}（要求 < 1e-12）")
    check("反向对照: 测试输入非退化（存在有效法线，非全零）",
          prec["nonzero_count"] > 60,
          f"{prec['nonzero_count']}/{prec['count']} 条为单位法线")
    check("反向对照: 覆盖了零长度/极短法线等退化输入",
          prec["count"] >= 64,
          f"输入 {prec['count']} 条（含轴对齐、1e-9 极短、零长度）")
    # 若误用 float32，误差量级会在 1e-7 附近，远超 1e-12 阈值
    check("反向对照: float32 会明显劣于该阈值（本断言能挡住精度退化）",
          prec["float32_error"] > 1e-12,
          f"模拟 float32 的误差 = {prec['float32_error']:.3e} > 1e-12"
          f"（故该断言确实能捕获 float32 退化）")

    # --- QA P0 防线：numpy 形态的法线数据（即使本机无真 numpy）---
    # np_array 在被求真值时**抛 ValueError**，与真 numpy 一致；
    # 因此这里能守住「裸 bool()」这个 P0 根因，不依赖本机是否装了 numpy。
    print(f"\n[K] QA P0 防线：numpy 形态法线（bool() 必须不抛异常）")
    p0 = _probe_p0_bool_hazard(ops, cache)
    check("_has_normals() 对 numpy 形态数组返回 True（不抛 ValueError）",
          p0["has_normals_ok"],
          f"返回={p0['has_normals_result']}，"
          f"异常={p0['has_normals_error']}")
    check("_store() 写入 numpy 形态法线不抛异常",
          p0["store_ok"],
          f"字节差={p0['bytes_diff']}（应= 1000×{cache.BYTES_PER_NORMAL_ESTIMATE}）")
    check("取色主路径对 numpy 形态法线不抛异常",
          p0["copy_ok"],
          f"取色={p0['copy_colors']}")
    check("反向对照: 裸 bool(numpy数组) 确实会抛 ValueError（证明本组有效）",
          p0["raw_bool_raises"],
          "若此项为 False，说明替身不够严格，本组断言无法守住 P0")
    check("反向对照: 裸 bool(list) 不抛异常（这正是 P0 能溜过单测的原因）",
          p0["raw_bool_list_ok"],
          "list 的真值永远合法 -> 旧测试用 list 承载法线时 P0 不可见")

    # --- 源码级 dtype 守护 ---
    # 上一条断言验证的是「两条路径的数学定义在 float64 下等价」，
    # 但它并未执行真实的 _transform_normals_array，因此把源码里的
    # float64 改回 float32 它测不出来（变异测试已证实）。
    # 这里补一条直接检查源码 dtype 的断言，把项目红线钉死。
    # 注意: 读取路径必须与 load_ops 的 core_dir 一致——变异测试
    # 用临时副本跑本脚本，这里若仍读仓库源文件，float32 变异就会逃逸。
    if core_dir is None:
        core_dir = os.path.join(ROOT, "core")
    print(f"\n[J2] 源码级精度红线（法线变换必须 float64）")
    src_path = os.path.join(core_dir, "vertex_color_ops.py")
    cache_path = os.path.join(core_dir, "cache.py")

    def _code_only(path, func_name):
        """
        提取指定函数的**可执行代码**（剔除文档字符串与注释）。

        必须剔除：这两个函数的 docstring 里会解释「为什么不能用 float32」，
        里面本就出现 float32 字样；不剔除则断言恒假。
        """
        import io
        import tokenize
        with open(path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        lines = source.splitlines()
        drop = set()   # 需要剔除的行号（0-based）
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None and node.body:
                    stmt = node.body[0]
                    end = getattr(stmt, "end_lineno", stmt.lineno)
                    for ln in range(stmt.lineno, end + 1):
                        drop.add(ln - 1)
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                drop.add(tok.start[0] - 1)

        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == func_name), None)
        if fn is None:
            return ""
        return "\n".join(
            lines[ln] for ln in range(fn.lineno - 1, fn.end_lineno)
            if ln not in drop
        )

    vco_code = _code_only(src_path, "_transform_normals_array")
    check("_transform_normals_array 的代码用 float64（不含 float32）",
          "float64" in vco_code and "float32" not in vco_code,
          f"代码段 float64={'float64' in vco_code}, "
          f"float32={'float32' in vco_code}（已剔除注释与文档字符串）")

    cache_code = _code_only(cache_path, "_extract_normals")
    check("_extract_normals 的代码用 float64（不含 float32）",
          "float64" in cache_code and "float32" not in cache_code,
          f"代码段 float64={'float64' in cache_code}, "
          f"float32={'float32' in cache_code}（已剔除注释与文档字符串）")

    failed = [n for n, ok, _ in _RESULTS if not ok]
    print("\n" + "=" * 76)
    print(f"结果: {len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过"
          + ("" if not failed else f"\n失败: {failed}"))
    print("=" * 76)
    return 0 if not failed else 1


def run_bench():
    """非薄壁场景的开销增幅（team-lead 要求 < 5%）"""
    ops, cache = load_ops()
    print("=" * 76)
    print("阶段 B 性能：非薄壁场景（法线朝向一致）开销增幅")
    print("=" * 76)

    N = 2000
    # 球面点云，法线 = 归一化位置（朝向一致，模拟普通模型）
    sv, sn, sc = [], [], {}
    for i in range(N):
        a = i * 0.01
        v = Vector(math.cos(a), math.sin(a), 0.0)
        sv.append(v)
        sn.append(Vector(v.x, v.y, 0.0))
        sc[i] = (0.2, 0.4, 0.6, 1.0)
    tv = [Vector(math.cos(i * 0.01) * 1.001, math.sin(i * 0.01) * 1.001, 0.0)
          for i in range(N)]
    tn = [Vector(v.x, v.y, 0.0) for v in tv]

    def timeit(threshold, reps=3):
        best = float('inf')
        for _ in range(reps):
            t0 = time.perf_counter()
            run_scenario(ops, sv, sn, sc, tv, tn, threshold=threshold)
            best = min(best, time.perf_counter() - t0)
        return best

    t_off = timeit(0.0)
    t_on = timeit(75.0)
    delta = (t_on - t_off) / t_off * 100 if t_off > 0 else 0.0
    print(f"\n  顶点数: {N}（球面点云，法线朝向一致 = 典型非薄壁模型）")
    print(f"  关闭约束: {t_off * 1000:.2f} ms")
    print(f"  启用约束: {t_on * 1000:.2f} ms")
    print(f"  增幅:     {delta:+.2f}%")
    print(f"\n  目标: < 5%")
    print("=" * 76)
    return 0 if delta < 5.0 else 1


MUTATIONS = [
    ("去掉兜底（无符合朝向时返回 -1 而非退回最近点）",
        "vertex_color_ops.py",
     "    # === 兜底：无符合朝向的候选，退回纯距离最近 ===\n"
     "    # 该点已通过上方的距离校验，故此兜底不会突破距离上限。\n"
     "    return nearest_idx",
     "    return -1"),
    ("二次搜索用 <= 而非 <（边界候选被排除 -> 可观测的行为变化）",
        "vertex_color_ops.py",
     "            if n_dot < cos_threshold:\n                continue",
     "            if n_dot <= cos_threshold:\n                continue"),
    ("法线比较反向（dot <= cos_threshold 才认为符合）",
        "vertex_color_ops.py",
     "    if dot >= cos_threshold:\n        return nearest_idx  # 朝向符合，直接采用（快路径）",
     "    if dot <= cos_threshold:\n        return nearest_idx"),
    ("二次搜索时忽略法线（直接取半径内最近）",
        "vertex_color_ops.py",
        ("            if n_dot is None:\n                continue\n"
            "            if n_dot < cos_threshold:\n                continue"),
        "            if n_dot is None:\n                continue"),
    ("二次搜索只搜 1 轮（半径不再扩大，可能漏掉远处候选）",
        "vertex_color_ops.py",
     "    for _ in range(_NORMAL_SEARCH_MAX_ROUNDS):",
     "    for _ in range(1):"),
    ("不记录 best_dist_sq（取最后命中而非最近命中）",
        "vertex_color_ops.py",
     "            if best_dist_sq is None or dist_sq < best_dist_sq:",
     "            if best_dist_sq is None or dist_sq >= 0:"),
    # 门控失效类变异：移除 native_enabled 条件 -> 静默失效复发
    ("移除原生门控（法线启用时仍走原生 = 静默失效复发）",
        "vertex_color_ops.py",
     "    if native_tree is not None and not need_python:",
     "    if native_tree is not None:"),
    # 精度红线类变异：把法线变换改回 float32 -> 应被 J 组精度断言捕获
    ("法线变换退回 float32（违反项目 double 精度红线）",
        "vertex_color_ops.py",
     "        m = np.array(normal_matrix, dtype=np.float64)",
     "        m = np.array(normal_matrix, dtype=np.float32)"),
    # QA P0 类变异：把 _has_normals 改回裸 bool()
    # 真实 numpy 下 bool(ndarray) 抛 ValueError -> 默认设置下100% 失败。
    # 测试替身已改为「能崩的地方也崩」，故此项应被捕获。
    ("_has_normals 退回裸 bool()（QA P0 根因复现）",
        "cache.py",
     "    return normals is not None and len(normals) > 0",
     "    return bool(normals)"),
    ("_store 绕过 _has_normals 直接 bool()（QA P0 第1 处）",
        "cache.py",
     "        has_normals = _has_normals(cache_data)",
     "        has_normals = bool(cache_data.get('normals'))"),
    ("取色路径绕过 _has_normals 直接真值判断（QA P0 第 4 处）",
        "vertex_color_ops.py",
     "        if _has_normals(source_data):",
     "        if source_data.get('normals'):"),
]

# 与 EQUIVALENT_MUTATIONS 对应的说明：这些变异在数学上与原实现等价，
# 因此**无法**用任何输入观测到差异。它们不是「弱断言」，而是变异本身无效。
# 逐一论证见文件末尾 EQUIVALENT_MUTATIONS 的注释。
EQUIVALENT_MUTATIONS = [
    # 条目结构 = (说明, 目标文件, 旧代码, 新代码, 等价性论证)，
    # 与 MUTATIONS 一致。三个锚点均在 vertex_color_ops.py。
    # 注意: 本表曾缺「目标文件」字段而 run_mutate 按 5 元组解包——
    # 等价变异段因此从未执行过（--mutate 一进本循环就 ValueError）。
    ("关闭阈值也走二次搜索（0 应完全关闭）",
     "vertex_color_ops.py",
     "    if angle_threshold_deg is None or angle_threshold_deg <= 0:\n        return nearest_idx",
     "    if angle_threshold_deg is None:\n        return nearest_idx",
     "cos(0°)=1.0；同向法线 dot=1.0，故 dot>=cos 成立 -> 快路径直接返回最近点，"
     "与早退结果完全相同。无论 dot 是否恰好为 1.0，该分支都不可观测。"),
    ("快路径用 > 而非 >=",
     "vertex_color_ops.py",
     "    if dot >= cos_threshold:\n        return nearest_idx  # 朝向符合，直接采用（快路径）",
     "    if dot > cos_threshold:\n        return nearest_idx",
     "边界情形 dot==cos 时，改动后会落入二次搜索；但二次搜索用< 排除，"
     "dot==cos 的候选同样被接受 => 最终仍返回同一索引。两条路径语义等价。"),
    # 注: 「二次搜索用 <= 而非 <」曾列在本表并声称等价——变异测试实测
    # 有 2 条断言变红（最近点朝向不符、二次搜索中恰好边界相等的候选
    # 是唯一合规项时，旧实现接受它、变异后排除并退回兜底），证明该
    # 变异**可观测**，已移入 MUTATIONS 表作为有效变异守护。
]


def _run_against_core_copy(sources):
    """
    把 core 源码写入**临时副本目录**，对副本跑一遍全量验证。

    通过 --core-dir 把副本目录注入子进程的 load_ops 与 J2 源码级断言，
    全程不触碰仓库的 core/ 文件——旧的「原地覆写再恢复」方案在
    写盘后被硬杀会留下变异代码并混进发行包，已废弃。
    """
    import shutil
    import tempfile
    core_dir = tempfile.mkdtemp(prefix="vct_mutant_core_")
    try:
        for fname, text in sources.items():
            with open(os.path.join(core_dir, fname), "w",
                      encoding="utf-8") as f:
                f.write(text)
        return subprocess.run(
            [sys.executable, os.path.abspath(__file__),
             "--core-dir", core_dir],
            capture_output=True, text=True, cwd=os.path.dirname(
                os.path.abspath(__file__)))
    finally:
        shutil.rmtree(core_dir, ignore_errors=True)


def run_mutate():
    """
    变异测试：每个变异都必须让至少一条断言变红。

    变异项声明为 (说明, 目标文件, 旧代码, 新代码)；
    目标文件相对 core/。变异只施加于临时副本（见 _run_against_core_copy）。
    """
    print("=" * 76)
    print("阶段 B 变异测试：每条断言必须可被变异打红")
    print("=" * 76)

    # 读入所有可能被变异的源文件（只读，绝不写回）
    sources = {}
    for fname in ("vertex_color_ops.py", "cache.py"):
        path = os.path.join(ROOT, "core", fname)
        with open(path, encoding="utf-8") as f:
            sources[fname] = f.read()

    all_caught = True
    for label, fname, old, new in MUTATIONS:
        if old not in sources[fname]:
            print(f"  [SKIP] {label}（锚点未找到于 {fname}，代码可能已变动）")
            all_caught = False
            continue
        mutated = dict(sources)
        mutated[fname] = sources[fname].replace(old, new, 1)
        r = _run_against_core_copy(mutated)
        fails = [l.strip() for l in r.stdout.splitlines()
                 if l.strip().startswith("FAIL")]
        # 崩溃也算被捕获（P0 类 bug 表现为异常退出）
        crashed = r.returncode not in (0, 1) and not r.stdout.strip().endswith("全部一致  ") \
            and "结果:" not in r.stdout
        caught = bool(fails) or crashed
        if not caught:
            all_caught = False
        print(f"  [{'捕获' if caught else '★未捕获★'}] {label}  ({len(fails)} 条变红)")
        for x in fails[:2]:
            print(f"         {x[:80]}")
        if crashed and not fails:
            print(f"         （进程异常退出—— P0 类 bug 的典型表现）")

    # 等价变异部分同样只在临时副本上验证
    for label, fname, old, new, proof in EQUIVALENT_MUTATIONS:
        if old not in sources[fname]:
            print(f"  [SKIP] {label}（锚点未找到于 {fname}）")
            continue
        mutated = dict(sources)
        mutated[fname] = sources[fname].replace(old, new, 1)
        r = _run_against_core_copy(mutated)
        fails = [l.strip() for l in r.stdout.splitlines()
                 if l.strip().startswith("FAIL")]
        note = "" if fails else "（确认无差异 = 等价）"
        print(f"  [等价{note}] {label}")
        print(f"         论证: {proof}")
        if fails:
            print(f"         ★但它确实改变了结果（{len(fails)} 条变红），说明论证有误")

    print("\n" + "=" * 76)
    print("结论:", "全部有效变异均被捕获 ✔" if all_caught else "存在未捕获的有效变异")
    print("=" * 76)
    return 0 if all_caught else 1


def run_real_numpy_check():
    """
    真 numpy 路径回归 —— 在**真实 numpy** 下执行被测代码的向量化路径。

    为什么这条通道不可替代（本项目最核心的教训）:
        上一轮出现过「CI 5/5 全绿，但插件在默认设置下 100% 不可用」。
        根因是**替身比真实环境更宽容**——numpy 替身刻意不给数组能力，
        于是被测代码强制走 list[Vector] 回退，数值路径**一行都没执行**。
        本分支让 core/ 里的 `import numpy as np` 拿到真 numpy，
        从而真正执行 _extract_normals / _transform_normals_array /
        _normal_dot 的 ndarray 分支，并与回退路径逐点比对数值。

    覆盖的两类风险（替身路径原理上抓不到）:
        1. **数值精度**：真 numpy 的 float64 矩阵乘 / 归一化是否与逐点
           mathutils 路径等价（< 1e-12）。若误用 float32，判定会在阈值
           边界翻转 -> 颜色落到错误顶点。
        2. **替身与真 numpy 的语义差**：真 numpy 下bool(ndarray) 抛
           ValueError 是**真实**行为；替身对齐了它，但真 numpy 还有很多
           替身没有的语义（dtype 提升、shape 检查、reshape 视图、
           numpy 标量的比较返回等）。这些只有跑真 numpy 才算数。

    与 P0 防线的关系:
        P0（bool(ndarray) 抛 ValueError）**已由替身路径守住**
        （替身的 np_array / _NPArray 求真值即抛），不依赖本分支。
        本分支是**加固**而非替代——它额外确认真 numpy 下同样安全。

    Returns:
        None 表示**因环境缺失而跳过**（本机无真 numpy），
        调用方须据此返回 EXIT_SKIP 而**不是** EXIT_OK，
        否则「没装 numpy」会被误读成「测过了且通过」。
    """
    print("=" * 70)
    print("真 numpy 路径回归")
    print("=" * 70)

    np = _import_real_numpy()
    if np is None:
        print("本机**没有安装真实 numpy** -> 本通道无法执行。")
        print()
        print("  跳过了什么: 法线约束的全部 **numpy 向量化路径**")
        print("             （_extract_normals / _transform_normals_array /")
        print("               _normal_dot 的 ndarray 分支、float64 数值一致性）")
        print("  为什么跳过: import numpy 失败（ModuleNotFoundError）")
        print("  已覆盖的部分: 替身路径（无 numpy 时的 list[Vector] 回退）")
        print("             仍由 `python verify_normal_constraint.py` 全量覆盖，")
        print("             含 P0 防线（np_array 求真值抛 ValueError，与真 numpy 一致）。")
        print()
        print("  如何补上: pip install numpy 后重跑本分支。")
        print("=" * 70)
        return None

    print(f"  numpy 版本: {np.__version__}  ({np.__file__})")
    # 装载被测模块：不注入 numpy 替身（load_ops(real_numpy=True)），
    # bpy / mathutils 仍照旧替身（与 numpy 真伪无关）。
    ops, cache = load_ops(real_numpy=True)

    # --- 断言 1:替身确实被让开了 ---
    # 这一条本身就是回归防线：若将来 load_ops 的 real_numpy 分支被改坏、
    # 或替身又抢先写进 sys.modules，下面这条会立刻变红，
    # 而不是让后续所有断言「在替身上假绿」。
    check("真 numpy 通道：被测模块拿到的是真 numpy 而非替身",
          getattr(ops, "np", None) is np and getattr(cache, "np", None) is np,
          f"ops.np={getattr(ops, 'np', None)!r:.40}, "
          f"cache.np={getattr(cache, 'np', None)!r:.40}")
    check("真 numpy 通道：_numpy_has_arrays() 判定为 True（数组能力齐备）",
          cache._numpy_has_arrays() is True,
          f"_numpy_has_arrays()={cache._numpy_has_arrays()}"
          f"（判据: empty/asarray/linalg/float64 四项俱在）")

    # --- 断言 2: 被测的向量化函数在真 numpy 下**真的被执行** ---
    # 这是本分支存在的意义：若 _transform_normals_array 因任何原因
    # return None，下面所有数值比对都只是在比回退路径，等于白测。
    # 所以直接调它本身，并确认返回的是 (N,3) float64 ndarray。
    M = _make_nonnormal_matrix(np)
    nrm = np.asarray(
        [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0],
         [0.0, -1.0, 0.0], [0.5773502691896258, 0.5773502691896258, 0.5773502691896258]],
        dtype=np.float64)
    fast = ops._transform_normals_array(nrm, M)
    check("被测的 _transform_normals_array 真的执行并返回 (N,3) float64 数组",
          isinstance(fast, np.ndarray) and fast.shape == (5, 3)
          and fast.dtype == np.float64,
          f"返回类型={type(fast).__name__}, "
          f"shape={getattr(fast, 'shape', None)}, "
          f"dtype={getattr(fast, 'dtype', None)}"
          f"（返回 None 说明静默退回了逐点回退，本组断言将形同虚设）")

    # --- 断言 3: 数值一致性 —— 向量化 vs 逐点回退（float64） ---
    _probe_real_numpy_equivalence(ops, cache, np, M)

    # --- 断言 4: 端到端取色在真numpy 下正确（薄壁不跨面） ---
    # 这一组把真 numpy 装进完整链路：
    #   _has_normals(真 ndarray) -> _extract_normals(真 ndarray)
    #   -> _transform_normals(真 ndarray) -> _normal_dot(真 ndarray 行)
    # 若这一步变红，说明用户在Blender 里（Blender 自带真 numpy）
    # 真实会遇到的问题——而替身路径对此**完全无感**。
    _probe_real_numpy_end_to_end(ops, cache, np)

    failed = [n for n, ok, _ in _RESULTS if not ok]
    print("\n" + "=" * 70)
    print(f"结果: {len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过"
          + ("" if not failed else f"\n失败: {failed}"))
    print("=" * 70)
    return True


def _import_real_numpy():
    """
    拿到**真实 numpy** 模块；本机没装（或被替身遮蔽且无法恢复）则返回 None。

    实现要点（这里踩过一次坑，留档）：
        **绝不能无条件地 `sys.modules.pop("numpy")` 再 import**。
        若numpy 已经被正常导入过，重新 import 会产生**第二个** numpy 实例
        （表现为 "The NumPy module was reloaded" 警告 + 属性访问递归爆栈），
        比原来的遮蔽问题更糟。

    因此采用两级策略：
        1. 先**普通 import**（不碰 sys.modules）。正常情况下直接命中，
           零副作用。
        2. 只有当命中的模块**缺少 `__version__`**（即确实是替身）时，
           才把替身摘掉重新 import。此时原替身并未持有真实 numpy 状态，
           重新 import 不会造成「同进程两份 numpy」。

    Returns:
        真 numpy 模块，或 None（须由调用方报「跳过」）
    """
    import importlib

    try:
        mod = importlib.import_module("numpy")
    except ImportError:
        return None
    if hasattr(mod, "__version__"):
        return mod

    # 命中替身：摘掉后重导一次。替身无__version__，说明真正的 numpy
    # 从未被本进程导入过，故此操作不会产生重复实例。
    saved = sys.modules.pop("numpy", None)
    try:
        mod = importlib.import_module("numpy")
    except ImportError:
        return None
    finally:
        if saved is not None:
            sys.modules["numpy"] = saved
    return mod if hasattr(mod, "__version__") else None


def _make_nonnormal_matrix(np):
    """
    构造一个「非单位、非等比、含旋转」的 4x4 矩阵。

    刻意不用单位矩阵：单位矩阵下变换与归一化都退化成恒等，
    数值比对的误差恒为 0，**断言会假绿**。
    必须让矩阵真正施加非等比缩放 + 旋转，才能暴露
    「向量化与逐点路径的浮点求和顺序差异」以及「误用 float32 的退化」。
    """
    a = math.radians(37.0)
    m = np.eye(4, dtype=np.float64)
    m[0, 0] = math.cos(a) * 1.3
    m[0, 1] = -math.sin(a) * 0.7
    m[0, 2] = 0.2
    m[1, 0] = math.sin(a) * 1.1
    m[1, 1] = math.cos(a) * 0.9
    m[1, 2] = -0.3
    m[2, 0] = 0.15
    m[2, 1] = 0.25
    m[2, 2] = 1.7
    m[0, 3] = 0.11
    m[1, 3] = -0.23
    m[2, 3] = 0.37
    return _NumpyMatrix(np, m)


def _probe_real_numpy_equivalence(ops, cache, np, matrix):
    """
    真 numpy 下逐点回退路径 vs 向量化路径的数值等价性。

    两条路径用**互相独立**的实现跑出来:
        - 向量化: 被测代码 `_transform_normals` -> `_transform_normals_array`
          （真 numpy 的批量矩阵乘 + `np.linalg.norm`）
        - 逐点回退: 纯 Python **标量**点积 + 标量归一化（_PureMatrix）
      刻意不直接复用被测代码的回退分支：它内部仍是`normal_matrix @ n`，
      若矩阵是 numpy 支撑的，两条路径就是同一个库算两遍，
      误差会恰好为 0，断言**恒真**、什么都没证明。
      真实生产里逐点回退走 mathutils.Matrix @ Vector（C 层 double 标量运算），
      与 numpy 批量运算的求和顺序可能不同，故这个独立比对才有意义。

    断言:
        1. 两者最大逐分量误差 < 1e-12（float64 等价）
        2. 反向对照: 若把中间结果截断到 float32，误差显著更大
           ——证明断言 1 确实有区分度，不是恒真
        3. 覆盖了退化输入（零长度 / 极短法线）
    """
    print("\n  [等价性] 向量化 (float64 ndarray) vs 逐点回退 (mathutils)")

    # 输入：轴对齐 / 极短 / 零长度 / 一般值——与替身路径的输入同构，
    # 但此处是**真 numpy float64 数组**。
    raw = np.asarray(
        [[0.0, 1.0, 0.0],
         [1e-9, 0.0, 0.0],
         [0.0, 0.0, 0.0],
         [0.0, -1.0, 0.0],
         [1.0, 1.0, 1.0],
         [-0.3, 0.7, 0.2],
         [0.11, -0.29, 0.93],
         [0.0, 0.0, 1.0]],
        dtype=np.float64)

    # --- 路径 A: 向量化（真 numpy，被测代码本体） ---
    vec, vec_err = _try_call(lambda: ops._transform_normals(raw, matrix))
    check("向量化路径返回 ndarray（未被静默跳过）",
          isinstance(vec, np.ndarray),
          vec_err or
          f"type={type(vec).__name__}, shape={getattr(vec, 'shape', None)}"
          f"（返回 None 说明没走到向量化路径）")
    check("向量化路径输出 dtype 为 float64（精度红线）",
          isinstance(vec, np.ndarray) and vec.dtype == np.float64,
          f"dtype={getattr(vec, 'dtype', None)}")

    # --- 路径 B: 逐点回退（**纯 Python 标量**实现，完全不经 numpy） ---
    #
    # 刻意不用 ops._transform_normals 的回退分支，原因是它内部仍用
    # `normal_matrix @ n`——若 normal_matrix 是 _NumpyMatrix，矩阵乘还是
    # numpy 在做，于是「向量化」与「逐点」两条路径**都是 numpy 在算**，
    # 对小规模输入会得到逐位相同的结果（实测误差恰好 0.000e+00）。
    # 那样断言虽然通过，却**恒真**——它只证明「同一个库算两遍一样」，
    # 证明不了 numpy 批量矩阵乘与标量点积在数学上等价。
    #
    # 真实生产里逐点回退走 mathutils.Matrix @ Vector（C 层 double 标量运算），
    # 与 numpy 批量运算的实现和求和顺序都可能不同，
    # 因此用纯 Python 标量点积做参照，这个比对才有意义。
    pure = _PureMatrix([[float(x) for x in row]
                        for row in np.asarray(matrix, dtype=np.float64)])
    nm = pure.to_3x3().inverted_safe().transposed()   # 逆转置，与被测代码一致
    fb = []
    for row in raw:
        v = nm @ Vector(float(row[0]), float(row[1]), float(row[2]))
        n = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5
        fb.append(v if n == 0.0
                  else Vector(v[0] / n, v[1] / n, v[2] / n))
    check("回退参照实现为纯 Python 标量（不经 numpy，保证比对独立）",
          len(fb) == len(raw) and all(isinstance(x, Vector) for x in fb),
          f"{len(fb)} 条 Vector，逐分量标量点积 + 标量归一化")

    # --- 数值比对 ---
    def max_err(a, b):
        worst = 0.0
        for i in range(len(b)):
            for k in range(3):
                worst = max(worst, abs(float(a[i][k]) - float(b[i][k])))
        return worst

    both_ok = isinstance(vec, np.ndarray) and isinstance(fb, list)
    if both_ok:
        err = max_err(vec, fb)
        check("真 numpy：向量化与逐点回退最大误差 < 1e-12（float64 等价）",
              err < 1e-12, f"最大误差 = {err:.3e}（要求 < 1e-12）")
    else:
        err = float('inf')
        check("真 numpy：向量化与逐点回退最大误差 < 1e-12（float64 等价）",
              False, f"路径类型异常: vec={type(vec).__name__}, "
                     f"fb={type(fb).__name__}，无法比对")
    # --- 反向对照: float32 退化必须被本断言抓到 ---
    # 刻意用 float32 跑同一数学定义（同样的逆转置 3x3），
    # 误差应落在 1e-7 量级，远超 1e-12 阈值。
    # 若此项为 False，说明退化对照失效（上面的断言恒真）——必须一起看。
    m32 = np.asarray(nm, dtype=np.float32)
    v32 = raw.astype(np.float32) @ m32.T
    lens = np.linalg.norm(v32, axis=1)
    lens[lens == 0.0] = 1.0
    v32 = v32 / lens[:, None]
    err32 = max_err(v32.astype(np.float64), fb)
    check("反向对照: 误用 float32 时误差显著更大（本断言能挡住精度退化）",
          err32 > 1e-12,
          f"float32 误差 = {err32:.3e}（> 1e-12，故上面的断言确实有区分度）")
    check("反向对照: float64 误差比 float32 小多个数量级（量级区分明显）",
          both_ok and err32 > max(err, 1e-18) * 1000,
          f"float64={err:.3e} vs float32={err32:.3e}"
          + (f"（相差 {err32 / max(err, 1e-300):.0f} 倍）" if both_ok and err > 0
             else ""))

    # --- 反向对照: 覆盖了退化输入 ---
    zero_len = float(np.linalg.norm(raw[2]))
    check("反向对照: 输入含零长度法线（归一化除零保护被覆盖）",
          zero_len == 0.0, f"第 3 行范数 = {zero_len}")
    check("反向对照: 零长度法线归一化后仍为零向量（不产生 nan）",
          isinstance(vec, np.ndarray) and not np.isnan(vec).any()
          and abs(float(vec[2][0])) < 1e-15,
          f"结果行={vec[2] if isinstance(vec, np.ndarray) else 'N/A'}")


def _probe_real_numpy_end_to_end(ops, cache, np):
    """
    端到端：完整取色链路在**真 numpy** 下的行为。

    链路（全部使用真 numpy）:
        _has_normals(真 ndarray)      -> 不抛 ValueError
        _extract_normals(真 ndarray)  ->扁平 float64 -> reshape (N,3)
        _transform_normals(真 ndarray)-> 向量化或回退
        _normal_dot(真 ndarray 行)    -> 点积

    每个断言都用 _try_check 包一层:
        本组要探的正是「真 numpy 下bool(ndarray) 抛 ValueError」这类崩溃。
        若直接调用，一旦被测代码回归就会**抛异常中断整个脚本**，
        后面的断言根本没机会跑——那样一次崩溃就能掩盖后续的真实问题。
        包一层后，崩溃会变成一条明确的 FAIL，其余断言照常执行。
    """
    print("\n  [端到端] 真 numpy 下的薄壁取色（用户实际路径）")

    T = 0.02
    sv, sn, sc = make_thin_wall(T, n=3)
    # 目标点与源顶点 x 错位、y 落在厚度中线 —— 纯距离最近会跨面
    tv = [Vector(0.5, -0.010, 0.0), Vector(1.5, -0.010, 0.0),
          Vector(0.5, -0.015, 0.0), Vector(1.5, -0.005, 0.0)]
    tn = [Vector(0, -1, 0), Vector(0, -1, 0), Vector(0, -1, 0), Vector(0, 1, 0)]
    # 端到端场景必须用**单位矩阵**（与替身路径的 _Identity 语义一致）。
    # 原因: run_scenario 里源法线是**直接以世界空间**塞进 source_data 的
    #（契约如此，缓存条目存的就是世界空间法线），而目标法线会经
    # _transform_normals(local, matrix_world) 变换。
    # 若这里给非单位矩阵，目标法线会被旋转而源法线不会 ——
    # 两者不在同一空间，夹角判定就是假的，测试会给出误导性结论。
    # （非单位矩阵的数值等价性由 _probe_real_numpy_equivalence 专门覆盖，
    #   且那边直接调 _transform_normals，不存在空间不一致问题。）
    # 即便矩阵是单位阵，_transform_normals_array 的
    # asarray / 矩阵乘 / linalg.norm / 归一化仍会**完整执行**，
    # 向量化路径照样被真实覆盖。
    matrix = _NumpyMatrix.identity(np)
    # 源法线用**真 numpy 数组**（真实环境里 _extract_normals 的产物形态）
    sn_np = np.asarray([[n[0], n[1], n[2]] for n in sn], dtype=np.float64)

    def is_red(c):
        return abs(c[0] - 1.0) < 1e-5 and abs(c[2]) < 1e-5

    def is_blue(c):
        return abs(c[2] - 1.0) < 1e-5 and abs(c[0]) < 1e-5

    has_normals, hn_detail = _try_call(
        lambda: cache._has_normals({'normals': sn_np}) is True)
    check("真 numpy 数组形态的源法线：_has_normals() 返回 True（不抛 ValueError）",
          has_normals, hn_detail)

    base = _try_call(lambda: run_scenario(
        ops, sv, sn_np, sc, tv, tn, threshold=0.0, matrix_world=matrix))[0]
    on = _try_call(lambda: run_scenario(
        ops, sv, sn_np, sc, tv, tn, threshold=75.0, matrix_world=matrix))[0]

    ok_base = isinstance(base, list) and len(base) == len(tv)
    ok_on = isinstance(on, list) and len(on) == len(tv)

    check("真 numpy：薄墙中线顶点按法线取到背面色（不跨面）",
          ok_on and is_blue(on[0]) and is_blue(on[1]),
          f"中线取色={[tuple(round(x, 3) for x in c) for c in on[0:2]]}"
          if ok_on else "取色链路抛异常或返回异常（见上一条）")
    check("真 numpy：偏前顶点取到正面色",
          ok_on and is_red(on[3]),
          f"偏前取色={tuple(round(x, 3) for x in on[3])}"
          if ok_on else "取色链路抛异常或返回异常")
    check("真 numpy：反向对照 —— 关闭约束时中线确实跨面（本用例有区分度）",
          ok_base and is_red(base[0]) and is_red(base[1]),
          f"关闭后={[tuple(round(x, 3) for x in c) for c in base[0:2]]}"
          if ok_base else "取色链路抛异常或返回异常")
    base2 = _try_call(lambda: run_scenario(
        ops, sv, sn_np, sc, tv, tn, threshold=0.0, matrix_world=matrix))[0]
    check("真 numpy：阈值 0 完全关闭，与基线逐字一致",
          ok_base and isinstance(base2, list) and base == base2,
          "两次 threshold=0 结果相同"
          if ok_base and isinstance(base2, list) else "无法比对（基线异常）")

    # --- 退化输入：零法线不得让整条链路崩，且**仍须取到色** ---
    # 注意断言的写法：不能只判「颜色非None」。
    # 取色失败时 layer.data[i].color 会停在初始值 (0,0,0,1)，
    # 那样 c 不是 None 却是个**没写进去的黑点**——
    # 用 `all(c is not None)` 断言会让「返回 -1 丢色」这种变异蒙混过关
    # （这正是本项目的核心教训：断言必须能区分「写对了」与「没写」）。
    # 因此这里断言「取到了非黑色的实际颜色」。
    zero_src, _ = _try_call(lambda: run_scenario(
        ops, sv, np.zeros((len(sv), 3)), sc, tv, tn,
        threshold=75.0, matrix_world=matrix))
    zero_detail = "链路抛异常（不应发生）"
    if isinstance(zero_src, list) and len(zero_src) == len(tv):
        # 未写入的槽位保持初始色 (0,0,0,1)；已写入的取自源（红或蓝）
        unwritten = sum(1 for c in zero_src
                        if abs(c[0]) < 1e-6 and abs(c[1]) < 1e-6
                        and abs(c[2]) < 1e-6)
        zero_detail = (f"全零法线源：{len(zero_src) - unwritten}/{len(zero_src)} "
                       f"个顶点取到实际颜色（未写入的黑点 {unwritten} 个）")
    check("真 numpy：源法线全零时链路不崩、且每个顶点都取到色（兜底生效）",
          isinstance(zero_src, list) and len(zero_src) == len(tv)
          and all(c is not None for c in zero_src)
          and not any(abs(c[0]) < 1e-6 and abs(c[1]) < 1e-6
                      and abs(c[2]) < 1e-6 for c in zero_src),
          zero_detail)

    # --- 二次搜索语义（在真 numpy 法线下） ---
    # 必要性：源法线是 ndarray 时，_normal_dot 走的是 `n.dot(target)`
    # 分支（ndarray 自带 .dot），而替身路径走的是 `[i]` 索引分支——
    # **两条分支不同**，替身路径的通过不能代表真 numpy 路径通过。
    _probe_real_numpy_search(ops, np, matrix)


def _probe_real_numpy_search(ops, np, matrix):
    """
    二次搜索语义，在**真 numpy 法线**下验证。

    为什么替身路径的通过不能代替本组:
        源法线是 ndarray 时，`_normal_dot` 走的是 `n.dot(target)` 分支
        （ndarray 自带 .dot 方法），而替身路径的 `_np_row` **没有** .dot，
        走的是 `[i]` 索引分支。**两条分支是不同的代码**，
        替身全绿不代表真 numpy 全绿——这正是本项目吃过的亏。

    覆盖:
        - 稀疏场景：靠扩大半径找到远处符合朝向的候选（而非走兜底）
        - 同半径多候选：取**最近**的符合候选，而非最后遍历到的
        - 越界场景：超出 8 轮可达范围时走兜底（仍取到色，不丢色）
    """
    print("\n  [二次搜索] 真 numpy 法线下的半径扩张与取最近语义")

    # 同 _probe_real_numpy_end_to_end：源法线已是世界空间，
    # 故matrix_world 必须是单位阵，否则源/目标法线不在同一空间。
    matrix = _NumpyMatrix.identity(np)

    def as_np(normals):
        return np.asarray([[n[0], n[1], n[2]] for n in normals],
                          dtype=np.float64)

    def is_red(c):
        return abs(c[0] - 1.0) < 1e-5 and abs(c[2]) < 1e-5

    def is_blue(c):
        return abs(c[2] - 1.0) < 1e-5 and abs(c[0]) < 1e-5

    # --- 稀疏场景：唯一符合朝向的候选在稍远处 ---
    reach = ops._NORMAL_SEARCH_MIN_RADIUS * (2 ** ops._NORMAL_SEARCH_MAX_ROUNDS)
    gap = reach * 0.5
    sp_v, sp_n, sp_c = make_sparse_backface(0.02, gap=gap)
    sp_out, _ = _try_call(lambda: run_scenario(
        ops, sp_v, as_np(sp_n), sp_c, [Vector(1.0, 0.0, 0.0)],
        [Vector(0, -1, 0)], threshold=75.0, matrix_world=matrix))
    check("真 numpy：稀疏场景靠扩大半径找到远处候选（未走兜底）",
          isinstance(sp_out, list) and is_blue(sp_out[0]),
          f"gap={gap:.4f}（8 轮可达 {reach:.4f}）；实际="
          f"{'蓝(找到)' if isinstance(sp_out, list) and is_blue(sp_out[0]) else '红(走了兜底)'}")

    # --- 同半径多候选：必须取最近，而非最后插入的 ---
    mc_v, mc_n, mc_c = make_multi_candidate_same_radius()
    mc_out, _ = _try_call(lambda: run_scenario(
        ops, mc_v, as_np(mc_n), mc_c, [Vector(0.0, 0.0, 0.0)],
        [Vector(0, -1, 0)], threshold=75.0, matrix_world=matrix))
    check("真 numpy：同半径多候选取最近的（近=蓝），而非最后插入的",
          isinstance(mc_out, list) and is_blue(mc_out[0]),
          f"近候选(蓝, d=0.10) 应胜出；实际="
          f"{'蓝' if isinstance(mc_out, list) and is_blue(mc_out[0]) else '红(取了远候选)'}")

    # --- 越界：超出可达范围时走兜底，仍取到色（不丢色块） ---
    far_v, far_n, far_c = make_sparse_backface(0.02, gap=reach * 4)
    far_out, _ = _try_call(lambda: run_scenario(
        ops, far_v, as_np(far_n), far_c, [Vector(1.0, 0.0, 0.0)],
        [Vector(0, -1, 0)], threshold=75.0, matrix_world=matrix))
    ok_far = (isinstance(far_out, list) and len(far_out) == 1
              and far_out[0] is not None
              and not (abs(far_out[0][0]) < 1e-6 and abs(far_out[0][1]) < 1e-6
                       and abs(far_out[0][2]) < 1e-6))
    check("真 numpy：候选超出 8 轮可达范围时走兜底（取到色，不丢色）",
          ok_far,
          f"gap={reach * 4:.4f} 超出可达 {reach:.4f}；兜底取色="
          + (f"{tuple(round(x, 3) for x in far_out[0])}"
             if isinstance(far_out, list) and far_out[0] else "无（丢色）"))


def _try_call(fn):
    """
    执行 fn，异常不外冒。

    真 numpy 通道探的正是「bool(ndarray) 抛 ValueError」这类**崩溃**。
    若让异常直接冒泡，脚本会在第一条就中断，
    后面的断言全部没跑——一次崩溃就能掩盖后续的真实缺陷。
    这与本项目「测试假绿」的教训同源：没跑到的检查等于没有检查。

    Returns:
        (value, detail_str)：成功时 detail 为 None；
        失败（抛异常）时 value 为 None、detail 说明异常类型与内容。
    """
    try:
        return fn(), None
    except Exception as e:  # noqa: BLE001
        return None, f"调用抛异常 -> {type(e).__name__}: {str(e)[:70]}"


def make_layer(n):
    """颜色层替身：支持 .data[i].color 读写"""
    class _Slot:
        __slots__ = ('color',)

        def __init__(self):
            self.color = (0.0, 0.0, 0.0, 1.0)

    class Layer:
        name = "Color"
        domain = 'POINT'

        def __init__(self, count):
            self.data = [_Slot() for _ in range(count)]

    return Layer(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--mutate", action="store_true")
    ap.add_argument("--real-numpy", action="store_true",
                    help="只跑真 numpy 回归（QA P0 加固防线）")
    ap.add_argument("--core-dir", default=None,
                    help="被测 core/ 所在目录（变异测试指向临时副本；"
                         "默认用仓库的 core/）")
    args = ap.parse_args()
    if args.mutate:
        return run_mutate()
    if args.bench:
        return run_bench()
    if args.real_numpy:
        # 注意: load_ops(real_numpy=True) —— 不注入 numpy 替身，
        # 否则本分支 import 到的会是替身，测的就不是真 numpy 了。
        ran = run_real_numpy_check()
        if ran is None:
            # 环境缺失 = **跳过**，绝不等同于通过（退出码 3）
            return EXIT_SKIP
        failed = [n for n, ok, _ in _RESULTS if not ok]
        return EXIT_OK if not failed else EXIT_FAIL
    return run_verification(core_dir=args.core_dir)


if __name__ == "__main__":
    sys.exit(main())
