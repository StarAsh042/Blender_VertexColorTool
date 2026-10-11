"""
原生加速后端 —— 通过 ctypes 加载纯 C ABI 动态库

设计要点:
    1. **不依赖 Python 版本**：只走纯 C ABI（extern "C" + 基本类型指针），
       因此同一个 DLL 可同时服务 Blender 3.4（Python 3.10）与 4.x（Python 3.11+）。
       若用 CPython 扩展（.pyd），一旦用户升级 Blender 就会失效。
    2. **静默降级**：库缺失、ABI 版本不符、加载失败时一律回退纯 Python 实现。
       插件在任何平台都必须可用，二进制只是加速项而非硬依赖。
    3. **零拷贝传参**：几何数组以连续 numpy 数组传入，直接暴露其数据指针；
       特征与参数走 `repr(C)` 结构体数组，与 Rust 侧布局逐字段对应。
    4. **生命周期安全**：DLL 若崩溃/未加载，Python 侧绝不解引用空指针；
       所有 ctypes 调用前都做句柄与形状校验。
"""

import ctypes
import os
import sys

import numpy as np

from ..utils.logging_utils import log_warning

# 必须与 native/vct-rs 中的 ABI_VERSION 一致
_EXPECTED_API_VERSION = 2

_lib = None
_load_attempted = False
_load_error = None
_load_path = None


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def _library_filename():
    if sys.platform.startswith("win"):
        return "vct_native.dll"
    if sys.platform == "darwin":
        return "libvct_native.dylib"
    return "libvct_native.so"


def _candidate_paths():
    """按优先级返回候选库路径"""
    here = os.path.dirname(os.path.abspath(__file__))
    package_root = os.path.dirname(here)
    filename = _library_filename()
    return (
        os.path.join(package_root, "native", "bin", filename),
        os.path.join(package_root, "native", filename),
    )


class ObjectFeatures(ctypes.Structure):
    """与 Rust 侧 `#[repr(C)] struct ObjectFeatures` 逐字段对应。

    布局为 3+3+1+1+1 个 f64 = 72 字节，`repr(C)` 下无重排、无填充，
    因此 Rust 侧可直接按下标解读，Python 侧也无需任何解析。
    """

    _fields_ = [
        ("location", ctypes.c_double * 3),
        ("dimensions", ctypes.c_double * 3),
        ("volume", ctypes.c_double),
        ("vertex_count", ctypes.c_double),
        ("bounding_radius", ctypes.c_double),
    ]


class MatchParams(ctypes.Structure):
    """与 Rust 侧 `MatchParams` 对应。字段顺序即 ABI 契约，不可调换。"""

    _fields_ = [
        ("distance_threshold", ctypes.c_double),
        ("position_decay_factor", ctypes.c_double),
        ("w_distance", ctypes.c_double),
        ("w_size", ctypes.c_double),
        ("w_volume", ctypes.c_double),
        ("w_vertex", ctypes.c_double),
        ("similarity_threshold", ctypes.c_double),
    ]


class ClusterParams(ctypes.Structure):
    """与 Rust 侧 `ClusterParams` 对应。"""

    _fields_ = [
        ("distance_threshold", ctypes.c_double),
        ("clustering_threshold", ctypes.c_double),
    ]


def _features_ptr(buf):
    """结构体数组的首元素地址。"""
    return ctypes.cast(buf, ctypes.POINTER(ObjectFeatures))


def _configure_signatures(lib):
    """声明各导出函数的参数与返回类型，避免 ctypes 误判导致内存错误"""
    f32_p = ctypes.POINTER(ctypes.c_float)
    f64_p = ctypes.POINTER(ctypes.c_double)
    i32_p = ctypes.POINTER(ctypes.c_int)
    feat_p = ctypes.POINTER(ObjectFeatures)

    lib.vct_api_version.restype = ctypes.c_int
    lib.vct_api_version.argtypes = []

    lib.vct_self_test.restype = ctypes.c_int
    lib.vct_self_test.argtypes = []

    lib.vct_kdtree_create.restype = ctypes.c_void_p
    lib.vct_kdtree_create.argtypes = [f32_p, ctypes.c_int]

    lib.vct_kdtree_free.restype = None
    lib.vct_kdtree_free.argtypes = [ctypes.c_void_p]

    lib.vct_kdtree_query_indices.restype = ctypes.c_int
    lib.vct_kdtree_query_indices.argtypes = [
        ctypes.c_void_p, f32_p, ctypes.c_int, i32_p, f32_p,
    ]

    # max_dist_sq < 0 表示关闭距离上限；0 是合法上限值（恰好命中），
    # 所以不能用 0 表示「关闭」。
    lib.vct_kdtree_pick_constrained.restype = ctypes.c_int
    lib.vct_kdtree_pick_constrained.argtypes = [
        ctypes.c_void_p, f64_p, f32_p, f64_p, ctypes.c_int,
        ctypes.c_double, ctypes.c_double, i32_p,
    ]

    lib.vct_match_best.restype = ctypes.c_int
    lib.vct_match_best.argtypes = [
        feat_p, ctypes.c_int, feat_p, ctypes.c_int,
        ctypes.POINTER(MatchParams), i32_p, f64_p,
    ]

    lib.vct_cluster_targets.restype = ctypes.c_int
    lib.vct_cluster_targets.argtypes = [
        feat_p, ctypes.c_int, ctypes.POINTER(ClusterParams),
        i32_p, i32_p, i32_p,
    ]


def ensure_loaded(force_reload=False):
    """
    尝试加载原生库（幂等，只真正尝试一次）。

    Returns:
        bool: 是否可用
    """
    global _lib, _load_attempted, _load_error, _load_path

    if _lib is not None and not force_reload:
        return True
    if _load_attempted and not force_reload:
        return False

    _load_attempted = True
    _load_error = None
    _load_path = None

    # Windows 上先加入 DLL 所在目录，便于解析潜在依赖
    candidates = _candidate_paths()
    if sys.platform.startswith("win"):
        for candidate in candidates:
            directory = os.path.dirname(candidate)
            if os.path.isdir(directory) and hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(directory)
                except OSError:
                    pass

    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        try:
            lib = ctypes.CDLL(candidate)
            _configure_signatures(lib)

            version = lib.vct_api_version()
            if version != _EXPECTED_API_VERSION:
                _load_error = (
                    f"ABI 版本不匹配（库={version}, 期望={_EXPECTED_API_VERSION}）"
                )
                continue

            # 构建后自检，避免加载到损坏/不完整的库
            if lib.vct_self_test() != 0:
                _load_error = "自检未通过（库可能损坏或与当前平台不符）"
                continue

            _lib = lib
            _load_path = candidate
            return True

        except OSError as exc:
            _load_error = f"加载失败: {exc}"
        except Exception as exc:  # noqa: BLE001  兜底：任何异常都不能影响插件可用性
            _load_error = f"初始化失败: {exc}"

    if _load_error is None:
        _load_error = "未找到原生库文件"

    return False


def is_available():
    """原生后端是否可用"""
    return ensure_loaded()


def unload():
    """
    卸载原生库并重置加载状态（插件注销时调用）。

    为什么必须显式释放:
        Windows 上被 ctypes 加载的 DLL 文件处于锁定状态；
        不 FreeLibrary 的话，「卸载插件」删除安装目录时会因
        「文件被占用」直接报错。Linux/macOS 允许删除已打开的库文件，
        此调用同样安全（幂等；非 Windows 无需额外处理）。

    调用时机: 必须在所有 NativeKDTree 句柄关闭**之后**
    （close() 依赖模块级 _lib 调用 vct_kdtree_free）。
    """
    global _lib, _load_attempted, _load_error, _load_path
    if _lib is None:
        return
    lib, _lib = _lib, None
    # 重置为「未尝试」，保证同一会话内禁用 -> 重新启用后原生仍可再次加载
    _load_attempted = False
    _load_error = None
    _load_path = None

    handle = getattr(lib, "_handle", None)
    if not handle:
        return
    try:
        if sys.platform.startswith("win"):
            if not ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(handle)):
                log_warning(
                    "FreeLibrary 释放原生库失败（不影响卸载流程，"
                    "重启 Blender 后可完全清理残留文件）"
                )
        # 非 Windows: 删除打开中的库文件本身合法，无需显式 dlclose
    except Exception as exc:  # noqa: BLE001  释放失败绝不能打断卸载
        log_warning(f"释放原生库时出错（不影响卸载，重启 Blender 后可完全清理）: {exc}")


def status():
    """返回可读的状态描述，用于 UI 展示与诊断"""
    if is_available():
        return f"原生加速已启用 ({os.path.basename(_load_path or '')})"
    return f"原生加速不可用，已回退纯 Python（{_load_error}）"


def describe_failure():
    """返回加载失败原因（可用时为 None）"""
    ensure_loaded()
    return None if _lib is not None else _load_error


# ---------------------------------------------------------------------------
# 数组辅助
# ---------------------------------------------------------------------------

def _as_float32_2d(array, width):
    """确保为 C 连续的 float32 二维数组"""
    arr = np.ascontiguousarray(array, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] != width:
        raise ValueError(f"期望形状 (N, {width})，实际 {arr.shape}")
    return arr


def _as_float32_1d(array):
    return np.ascontiguousarray(array, dtype=np.float32)


def _float_ptr(array):
    return array.ctypes.data_as(ctypes.POINTER(ctypes.c_float))


def _float64_ptr(array):
    """f64 数组的指针。array 为 None 时返回空指针。"""
    if array is None:
        return None
    return array.ctypes.data_as(ctypes.POINTER(ctypes.c_double))


def _int_ptr(array):
    return array.ctypes.data_as(ctypes.POINTER(ctypes.c_int))


# ---------------------------------------------------------------------------
# KDTree 封装
# ---------------------------------------------------------------------------

class NativeKDTree:
    """
    原生 KDTree 句柄包装。

    构建后为只读，可安全地被多次查询复用（对应 Python 侧的缓存策略）。
    实例销毁时自动释放原生内存。
    """

    __slots__ = ("_handle", "_count")

    def __init__(self, points):
        if not ensure_loaded():
            raise RuntimeError("原生库不可用")

        pts = _as_float32_2d(points, 3)
        count = pts.shape[0]
        if count == 0:
            raise ValueError("点数不能为 0")

        handle = _lib.vct_kdtree_create(_float_ptr(pts), count)
        if not handle:
            raise RuntimeError("原生 KDTree 创建失败")

        self._handle = handle
        self._count = count

    @property
    def count(self):
        return self._count

    def query_colors(self, colors, target_points):
        """
        批量最近邻取色。

        Args:
            colors: (N, 4) float32 源颜色，需稠密（缺失处由调用方填默认色）
            target_points: (M, 3) float32 目标坐标

        Returns:
            (M, 4) float32 numpy 数组
        """
        if self._handle is None:
            raise RuntimeError("KDTree 句柄已释放")

        src_colors = _as_float32_2d(colors, 4)
        if src_colors.shape[0] != self._count:
            raise ValueError(
                f"颜色数量 {src_colors.shape[0]} 与树内点数 {self._count} 不一致"
            )

        indices, _ = self.query_indices(target_points)
        out = np.zeros((indices.shape[0], 4), dtype=np.float32)
        # 下标为 -1 表示内核判定「不写入该点」，保持原色（全 0 即未写入）。
        hit = indices >= 0
        out[hit] = src_colors[indices[hit]]
        return out

    def query_indices(self, target_points):
        """批量最近邻查询，返回 (indices, dist_sq)"""
        if self._handle is None:
            raise RuntimeError("KDTree 句柄已释放")

        tgt = _as_float32_2d(target_points, 3)
        target_count = tgt.shape[0]
        indices = np.empty(target_count, dtype=np.int32)
        dist_sq = np.empty(target_count, dtype=np.float32)
        if target_count == 0:
            return indices, dist_sq

        rc = _lib.vct_kdtree_query_indices(
            self._handle, _float_ptr(tgt), target_count,
            _int_ptr(indices), _float_ptr(dist_sq),
        )
        if rc != 0:
            raise RuntimeError(f"原生查询失败（返回码 {rc}）")
        return indices, dist_sq

    def pick_indices(self, src_normals, target_points, target_normals,
                     angle_threshold_deg=0.0, max_dist_sq=None):
        """带距离硬边界与法线精修的取色，返回每个目标点的源顶点下标。

        Args:
            src_normals: (N, 3) 源法线，f64。传 None 表示不启用法线约束。
            target_points: (M, 3) 目标坐标，f32
            target_normals: (M, 3) 目标法线，f64。传 None 表示不启用法线约束。
            angle_threshold_deg: 法线夹角阈值（度）。<= 0 关闭约束。
            max_dist_sq: 取色距离上限（**平方**）。None 表示不启用。

        Returns:
            (M,) int32 数组；-1 表示判定为「不写入」。
        """
        if self._handle is None:
            raise RuntimeError("KDTree 句柄已释放")

        tgt = _as_float32_2d(target_points, 3)
        target_count = tgt.shape[0]
        out = np.full(target_count, -1, dtype=np.int32)
        if target_count == 0:
            return out

        normals = None
        if src_normals is not None:
            normals = np.ascontiguousarray(src_normals, dtype=np.float64).ravel()
            if normals.size != self._count * 3:
                raise ValueError(
                    f"法线数量 {normals.size // 3} 与树内点数 {self._count} 不一致"
                )
        tnorms = None
        if target_normals is not None:
            tnorms = np.ascontiguousarray(
                target_normals, dtype=np.float64
            ).ravel()

        limit = -1.0 if max_dist_sq is None else float(max_dist_sq)
        rc = _lib.vct_kdtree_pick_constrained(
            self._handle,
            _float64_ptr(normals) if normals is not None else None,
            _float_ptr(tgt),
            _float64_ptr(tnorms) if tnorms is not None else None,
            target_count,
            float(angle_threshold_deg), limit, _int_ptr(out),
        )
        if rc != 0:
            raise RuntimeError(f"原生受约束取色失败（返回码 {rc}）")
        return out

    def close(self):
        if self._handle is not None:
            try:
                _lib.vct_kdtree_free(self._handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = None

    def __del__(self):
        self.close()


# ---------------------------------------------------------------------------
# 物体匹配
# ---------------------------------------------------------------------------

def _pack_features(features):
    """把特征字典列表打包成连续的 ObjectFeatures 数组。

    字段全部走 f64：评分要参与 0.7 这类阈值比较，用 f32 会在边界引入舍入。
    """
    count = len(features)
    buf = (ObjectFeatures * count)()
    for i, feat in enumerate(features):
        item = buf[i]
        for axis in range(3):
            item.location[axis] = float(feat['location'][axis])
            item.dimensions[axis] = float(feat['dimensions'][axis])
        item.volume = float(feat['volume'])
        item.vertex_count = float(feat['vertex_count'])
        item.bounding_radius = float(feat['bounding_sphere_radius'])
    return buf


def match_best(source_features, target_features, vc_tool):
    """
    为每个目标物体计算最佳源物体（原生内核）。

    Args:
        source_features: 源特征字典列表
        target_features: 目标特征字典列表
        vc_tool: 工具设置对象（提供权重与阈值）

    Returns:
        (best_indices, best_scores) 两个 numpy 数组；
        原生不可用时返回 None，由调用方回退 Python 实现。
    """
    if not ensure_loaded():
        return None
    if not source_features or not target_features:
        return None

    src = _pack_features(source_features)
    tgt = _pack_features(target_features)

    params = MatchParams(
        distance_threshold=float(vc_tool.distance_threshold),
        position_decay_factor=float(vc_tool.position_decay_factor),
        w_distance=float(vc_tool.distance_weight),
        w_size=float(vc_tool.size_weight),
        w_volume=float(vc_tool.volume_weight),
        w_vertex=float(vc_tool.vertex_count_weight),
        similarity_threshold=float(vc_tool.match_similarity_threshold),
    )

    target_count = len(target_features)
    best_index = np.empty(target_count, dtype=np.int32)
    best_score = np.empty(target_count, dtype=np.float64)

    rc = _lib.vct_match_best(
        _features_ptr(src), len(source_features),
        _features_ptr(tgt), target_count,
        ctypes.byref(params),
        _int_ptr(best_index), _float64_ptr(best_score),
    )
    if rc != 0:
        log_warning(f"原生匹配内核返回错误码 {rc}，本次回退 Python 实现")
        return None

    return best_index, best_score


# ---------------------------------------------------------------------------
# 目标物体聚类
# ---------------------------------------------------------------------------

def cluster_targets(features, distance_threshold, clustering_threshold):
    """
    对目标物体做贪心聚类（原生内核）。

    Args:
        features: 特征字典列表
        distance_threshold: 距离阈值；聚类半径取其一半
        clustering_threshold: 簇内相似度门槛

    Returns:
        嵌套列表（每簇一个成员下标列表）；原生不可用时返回 None，由调用方回退。
    """
    if not ensure_loaded():
        return None
    if not features:
        return []

    count = len(features)
    packed = _pack_features(features)
    params = ClusterParams(
        distance_threshold=float(distance_threshold),
        clustering_threshold=float(clustering_threshold),
    )
    members = np.empty(count, dtype=np.int32)
    offsets = np.empty(count + 1, dtype=np.int32)
    cluster_count = ctypes.c_int(0)

    rc = _lib.vct_cluster_targets(
        _features_ptr(packed), count, ctypes.byref(params),
        _int_ptr(members), _int_ptr(offsets), ctypes.byref(cluster_count),
    )
    if rc != 0:
        log_warning(f"原生聚类内核返回错误码 {rc}，本次回退 Python 实现")
        return None

    total = cluster_count.value
    bounds = offsets[:total + 1]
    return [members[bounds[i]:bounds[i + 1]].tolist() for i in range(total)]
