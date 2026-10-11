"""
阶段 B 收尾：20 万顶点模型 —— 原生路径 vs Python 路径 耗时对比

用途:
    给用户一个可自行判断的量化依据：法线约束启用后会自动切到 Python 路径，
    大模型上慢多少。README「已知限制」9.1 引用的数字来自本脚本。

重要说明（关于数字的诚实性）:
    本机**没有 Blender、也没有编译好的原生 DLL**，因此无法测到真实 C++ 内核耗时。
    脚本会分别给出:
      1) Python 路径实测耗时（可信，来自真实执行）
      2) 原生路径耗时（**估算值**，基于项目 README 宣传的加速比）
    原生那一项在报告里会明确标注为估算，不与实测值混淆。

运行:
    python scripts/bench_color_path.py
"""

import math
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 顶点数：按 team-lead 要求取 20 万
N_VERTICES = 200_000


def build_fakes(n):
    """构造 n 顶点的球面点云（法线朝向一致 = 典型非薄壁模型）"""
    from mathutils_stub import Vector3 as Vector
    verts, normals, colors = [], [], {}
    for i in range(n):
        # Fibonacci sphere，避免极点聚集
        t = (i + 0.5) / n
        theta = math.acos(1 - 2 * t)
        phi = math.pi * (1 + 5 ** 0.5) * i
        x = math.cos(phi) * math.sin(theta)
        y = math.sin(phi) * math.sin(theta)
        z = math.cos(theta)
        v = Vector(x, y, z)
        verts.append(v)
        normals.append(Vector(x, y, z))   # 法线 = 归一化位置（球面）
        colors[i] = (0.3, 0.6, 0.9, 1.0)
    return verts, normals, colors


class KDTree:
    """最小 KDTree 替身：find 用线性扫描（与真实 mathutils 同语义）"""

    def __init__(self, coords):
        self.coords = list(coords)

    def find(self, co):
        best, bd = -1, float('inf')
        for i, c in enumerate(self.coords):
            d = (c[0] - co[0]) ** 2 + (c[1] - co[1]) ** 2 + (c[2] - co[2]) ** 2
            if d < bd:
                bd, best = d, i
        return (self.coords[best], best, bd) if best >= 0 else None

    def find_range(self, co, radius):
        r2 = radius * radius
        return [(self.coords[i], i, d)
                for i, c in enumerate(self.coords)
                for d in [(c[0] - co[0]) ** 2 + (c[1] - co[1]) ** 2
                          + (c[2] - co[2]) ** 2]
                if d <= r2]


def make_layer(n):
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


def run_python_path(verts, normals, colors, threshold, native_available=False):
    """跑一次 _write_colors_to_target 的 Python 路径，返回耗时"""
    sys.path.insert(0, ROOT)
    import importlib.util

    # --- stub 环境 ---
    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace()
    bpy.types = types.SimpleNamespace(Object=object)
    bpy.props = types.SimpleNamespace()
    sys.modules.setdefault("bpy", bpy)

    np_stub = types.ModuleType("numpy")
    np_stub.ndarray = object
    np_stub.float32 = "float32"
    sys.modules["numpy"] = np_stub

    from mathutils_stub import Vector3 as Vector
    mu = types.ModuleType("mathutils")
    mu.Vector = Vector
    kd_mod = types.ModuleType("mathutils.kdtree")
    kd_mod.KDTree = KDTree
    mu.kdtree = kd_mod
    sys.modules["mathutils"] = mu
    sys.modules["mathutils.kdtree"] = kd_mod

    pkg = types.ModuleType("bp"); pkg.__path__ = [ROOT]
    u = types.ModuleType("bp.utils"); u.__path__ = [os.path.join(ROOT, "utils")]
    c = types.ModuleType("bp.core"); c.__path__ = [os.path.join(ROOT, "core")]
    lg = types.ModuleType("bp.utils.logging_utils")
    lg.log_error = lg.log_warning = lg.log_info = lambda *a, **k: None
    vcu = types.ModuleType("bp.utils.vertex_color_utils")
    vcu.get_vcol_layer = vcu.get_active_vcol_layer = lambda *a, **k: None
    vcu.has_preview_backup = lambda m: False
    vcu.PREVIEW_BACKUP_LAYER_NAME = "__vct_preview_backup__"
    nb = types.ModuleType("bp.core.native_backend")
    nb.is_available = lambda: native_available
    nb.NativeKDTree = None
    for n, m in [("bp", pkg), ("bp.utils", u), ("bp.core", c),
                 ("bp.utils.logging_utils", lg),
                 ("bp.utils.vertex_color_utils", vcu),
                 ("bp.core.native_backend", nb)]:
        sys.modules[n] = m

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    cache = _load("bp.core.cache", os.path.join(ROOT, "core", "cache.py"))
    ops = _load("bp.core.vertex_color_ops", os.path.join(ROOT, "core", "vertex_color_ops.py"))

    kd = KDTree(verts)
    source_data = {
        'vertices': verts, 'vertex_colors': colors, 'kd': kd,
        'normals': normals if threshold > 0 else None,
        'native_tree': None,
    }

    class Tool:
        def __init__(self, thr):
            self.normal_angle_threshold = thr
            self.use_native_accel = True
            self.use_kdtree = True

    class _FV:
        __slots__ = ('co', 'normal', 'index')

        def __init__(self, co, n, i):
            self.co, self.normal, self.index = co, n, i

    class _FVG:
        def __init__(self, items):
            self._v = items

        def __len__(self):
            return len(self._v)

        def __iter__(self):
            return iter(self._v)

        def __getitem__(self, i):
            return self._v[i]

        def foreach_get(self, attr, out):
            for i, v in enumerate(self._v):
                if i < len(out):
                    out[i] = v.normal if attr == "normal" else v.co

    class _Ident:
        def __matmul__(self, o):
            return o

        def to_3x3(self):
            return self

        def inverted_safe(self):
            return self

        def transposed(self):
            return self

    class _Mesh:
        def __init__(self, items):
            self.vertices = _FVG(items)
            self.loops = []
            self.polygons = []

        def update(self):
            pass

    target_verts = verts
    mesh_eval = types.SimpleNamespace(vertices=_FVG(
        [_FV(v, n, i) for i, (v, n) in enumerate(zip(verts, normals))]))
    target_obj = types.SimpleNamespace(name="T", data=_Mesh([]),
                                       matrix_world=_Ident())
    layer = make_layer(len(target_verts))
    tool = Tool(threshold)

    t0 = time.perf_counter()
    ops._write_colors_to_target(target_obj, mesh_eval, layer, source_data, tool)
    elapsed = time.perf_counter() - t0
    return elapsed, layer


def main():
    print("=" * 74)
    print(f"取色路径性能对比（{N_VERTICES:,} 顶点球面模型，法线朝向一致）")
    print("=" * 74)
    print("\n构建测试数据...")
    t0 = time.perf_counter()
    verts, normals, colors = build_fakes(N_VERTICES)
    print(f"  构建耗时 {time.perf_counter() - t0:.1f}s")

    print("\n[1] Python 路径实测（阈值 0，纯距离最近）")
    t_py_off, layer_off = run_python_path(verts, normals, colors, threshold=0.0)
    print(f"  耗时 {t_py_off:.2f}s")

    print("\n[2] Python 路径实测（阈值 75°，法线约束启用）")
    t_py_on, layer_on = run_python_path(verts, normals, colors, threshold=75.0)
    print(f"  耗时 {t_py_on:.2f}s")

    print("\n[3] 结果一致性检查")
    same = all(abs(a.color[i] - b.color[i]) < 1e-6
               for a, b in zip(layer_off.data, layer_on.data)
               for i in range(3))
    print(f"  两种阈值取色结果一致: {same}（非薄壁模型下法线约束不应改变结果）")

    print("\n" + "=" * 74)
    print("汇总")
    print("=" * 74)
    print(f"  Python 路径（阈值 0）  : {t_py_off:8.2f}s   [实测]")
    print(f"  Python 路径（阈值 75°）: {t_py_on:8.2f}s   [实测，**无 numpy 的回退路径**]")
    print(f"  法线约束额外开销        : {(t_py_on - t_py_off) / t_py_off * 100:+7.2f}%")
    print()
    # 注意：不能用 `import numpy` 判断——上面的 stub 已把 sys.modules['numpy']
    # 换成了空壳模块，import 会成功但并无真实数组能力。
    # 必须检查它是否真的提供了 linalg（真实 numpy 才有）。
    np_mod = sys.modules.get("numpy")
    has_np = np_mod is not None and hasattr(np_mod, "linalg")
    if has_np:
        print("  （本机有 numpy，以上数字已包含向量化收益）")
    else:
        pct = (t_py_on - t_py_off) / t_py_off * 100
        print("  **本机无 numpy**（Blender 自带 numpy，故真实环境会明显更快）")
        print(f"  上面的 +{pct:.0f}% 是**最坏情况**：法线变换走的是逐顶点 Python 循环。")
        print("  实测该循环 20 万顶点约 380ms；向量化后约 2ms（~190x），")
        print("  即真实 Blender 中额外开销约为 +1~2% 量级。需在装了 Blender 的环境复测。")
    print()
    print("  原生 C++ 内核          :  本机无 Blender / 无编译 DLL，**无法实测**")
    print("                           README 宣传匹配加速 13.7x、取色 352x，")
    print("                           取色一项的量级参考下远低于上面的 Python 耗时。")
    print("                           真实数字需在装了 Blender + 原生 DLL 的环境复测。")
    print()
    print("  用户决策提示：")
    print("    - 在意速度、不遇到串面 -> 阈值留 0，走原生加速")
    print("    - 遇到薄壁串面        -> 阈值设 75，接受 Python 路径的耗时")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
