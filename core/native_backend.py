"""
原生加速后端 —— 通过 ctypes 加载纯 C ABI 动态库

设计要点:
    1. **不依赖 Python 版本**：只走纯 C ABI（extern "C" + 基本类型指针），
       因此同一个 DLL 可同时服务 Blender 3.4（Python 3.10）与 4.x（Python 3.11+）。
       若用 CPython 扩展（.pyd），一旦用户升级 Blender 就会失效。
    2. **静默降级**：库缺失、ABI 版本不符、加载失败时一律回退纯 Python 实现。
       插件在任何平台都必须可用，二进制只是加速项而非硬依赖。
    3. **零拷贝传参**：所有数组以连续 float32 的 numpy 数组传入，
       直接暴露其数据指针，不产生中间拷贝。
    4. **生命周期安全**：DLL 若崩溃/未加载，Python 侧绝不解引用空指针；
       所有 ctypes 调用前都做句柄与形状校验。
"""

import ctypes
import os
import sys

import numpy as np

from ..utils.logging_utils import log_warning

# 必须与 native/vct_native.h 中的 VCT_API_VERSION 一致
_EXPECTED_API_VERSION = 1

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


def _configure_signatures(lib):
    """声明各导出函数的参数与返回类型，避免 ctypes 误判导致内存错误"""
    f32_p = ctypes.POINTER(ctypes.c_float)
    i32_p = ctypes.POINTER(ctypes.c_int)

    lib.vct_api_version.restype = ctypes.c_int
    lib.vct_api_version.argtypes = []

    lib.vct_self_test.restype = ctypes.c_int
    lib.vct_self_test.argtypes = []

    lib.vct_kdtree_create.restype = ctypes.c_void_p
    lib.vct_kdtree_create.argtypes = [f32_p, ctypes.c_int]

    lib.vct_kdtree_free.restype = None
    lib.vct_kdtree_free.argtypes = [ctypes.c_void_p]

    lib.vct_kdtree_query_colors.restype = ctypes.c_int
    lib.vct_kdtree_query_colors.argtypes = [
        ctypes.c_void_p, f32_p, f32_p, ctypes.c_int, f32_p,
    ]

    lib.vct_kdtree_query_indices.restype = ctypes.c_int
    lib.vct_kdtree_query_indices.argtypes = [
        ctypes.c_void_p, f32_p, ctypes.c_int, i32_p, f32_p,
    ]

    lib.vct_match_best.restype = ctypes.c_int
    lib.vct_match_best.argtypes = [
        f32_p, f32_p, f32_p, f32_p, f32_p, ctypes.c_int,
        f32_p, f32_p, f32_p, f32_p, f32_p, ctypes.c_int,
        ctypes.c_float, ctypes.c_float,
        ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
        ctypes.c_float,
        i32_p, f32_p,
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
        tgt = _as_float32_2d(target_points, 3)

        if src_colors.shape[0] != self._count:
            raise ValueError(
                f"颜色数量 {src_colors.shape[0]} 与树内点数 {self._count} 不一致"
            )

        target_count = tgt.shape[0]
        out = np.empty((target_count, 4), dtype=np.float32)
        if target_count == 0:
            return out

        rc = _lib.vct_kdtree_query_colors(
            self._handle, _float_ptr(src_colors), _float_ptr(tgt),
            target_count, _float_ptr(out),
        )
        if rc != 0:
            raise RuntimeError(f"原生取色失败（返回码 {rc}）")
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

def _stack_features(features):
    """把特征字典列表压成并列的 numpy 数组"""
    count = len(features)
    loc = np.empty((count, 3), dtype=np.float32)
    dim = np.empty((count, 3), dtype=np.float32)
    vol = np.empty(count, dtype=np.float32)
    vcount = np.empty(count, dtype=np.float32)
    radius = np.empty(count, dtype=np.float32)

    for i, feat in enumerate(features):
        loc[i, 0] = feat['location'][0]
        loc[i, 1] = feat['location'][1]
        loc[i, 2] = feat['location'][2]
        dim[i, 0] = feat['dimensions'][0]
        dim[i, 1] = feat['dimensions'][1]
        dim[i, 2] = feat['dimensions'][2]
        vol[i] = feat['volume']
        vcount[i] = feat['vertex_count']
        radius[i] = feat['bounding_sphere_radius']

    return loc, dim, vol, vcount, radius


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

    s_loc, s_dim, s_vol, s_vc, s_rad = _stack_features(source_features)
    t_loc, t_dim, t_vol, t_vc, t_rad = _stack_features(target_features)

    target_count = len(target_features)
    best_index = np.empty(target_count, dtype=np.int32)
    best_score = np.empty(target_count, dtype=np.float32)

    rc = _lib.vct_match_best(
        _float_ptr(s_loc), _float_ptr(s_dim), _float_ptr(s_vol),
        _float_ptr(s_vc), _float_ptr(s_rad), len(source_features),
        _float_ptr(t_loc), _float_ptr(t_dim), _float_ptr(t_vol),
        _float_ptr(t_vc), _float_ptr(t_rad), target_count,
        float(vc_tool.distance_threshold), float(vc_tool.position_decay_factor),
        float(vc_tool.distance_weight), float(vc_tool.size_weight),
        float(vc_tool.volume_weight), float(vc_tool.vertex_count_weight),
        float(vc_tool.match_similarity_threshold),
        _int_ptr(best_index), _float_ptr(best_score),
    )
    if rc != 0:
        log_warning(f"原生匹配内核返回错误码 {rc}，本次回退 Python 实现")
        return None

    return best_index, best_score
