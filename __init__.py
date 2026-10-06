"""
Blender顶点色复制工具
版本: 1.1.0
作者: StarAsh042

插件入口模块，负责注册和注销所有类。

兼容性:
    - Blender 3.2+（因使用 mesh.color_attributes，该 API 自 3.2 引入）
    - Python 3.7+

审计修复说明:
    - P2-15: 注册改为「按模块自动收集」，不再手工维护易漏改的 classes 列表。
      新增一个算子/面板时无需再改动本文件，漏注册导致的「面板静默消失」问题被消除。
    - P2-11: 版本号由开发期编号 22.0.0 重置为语义化版本，并配套 CHANGELOG。
    - P2-16: 最低 Blender 版本修正为 3.2。
    - P1-5 : 新增 load_post 钩子，加载 .blend 时清空顶点色缓存，
      避免跨文件会话读到错误的缓存数据。
    - R6   : 注册失败时回滚已注册的类，避免留下半注册状态。

1.1.0 新增:
    - 原生加速内核（C++ / 多线程），通过 ctypes 加载纯 C ABI 动态库。
      库缺失时自动回退纯 Python，不影响任何平台上的可用性。
    - 修复 6 个 P0（详见 CHANGELOG [1.1.0]），其中预览备份层索引漂移
      可导致原始色不可逆丢失、to_mesh 异常泄漏可累积至数十 GB。
    - 法线夹角阈值：防止薄壁模型跨面取色（默认 75°，设为 0 = 关闭）。
      代价是开启后自动切Python 取色路径（原生内核尚未实现该约束）。
    - 取色距离上限 pick_distance_percent：防超范围「拉色」，
      超距顶点保留原色（默认 0 = 关闭，行为与未提供该参数时一致）。
    - 聚类优化：空间网格索引 +簇内匹配原生化，分组结果与优化前完全一致。
    - 匹配 / 统计范围改为递归含所有层级子集合（行为变更，
      详见 CHANGELOG「行为变更」）；移除与 Alt+A 重复的
      VERTEXCOLOR_OT_ClearSelection 算子。
    - UI 可用性：19 条参数 tooltip 重写、3 个关键算子加 poll()、
      补齐隐藏算子入口、新增「清空缓存」按钮。
"""

bl_info = {
    "name": "顶点色复制工具",
    "author": "StarAsh042",
    "version": (1, 1, 0),
    "blender": (3, 2, 0),
    "location": "3D View > Sidebar > 顶点色复制",
    "description": "根据位置接近和相似性在两组模型间复制顶点色，支持编辑模式选择填充和通道预览",
    "category": "Mesh",
}

import importlib

import bpy
from bpy.app.handlers import load_post

from .utils.compatibility import check_blender_compatibility
from .utils.logging_utils import log_error, log_warning
from .core.cache import VertexColorCache


# 需要收集类的模块，顺序即注册顺序（依赖关系已排好）。
# 类在模块内的定义顺序会被保留。
_CLASS_MODULE_ORDER = (
    # PropertyGroup 类（无依赖，先注册）
    "properties.match_item",
    "properties.settings",
    # 主面板（必须在子面板之前注册）
    "ui.main_panel",
    # 子面板（依赖主面板）
    "ui.collection_panel",
    "ui.match_params_panel",
    "ui.operations_panel",
    "ui.manual_tools_panel",
    "ui.results_panel",
    "ui.vcol_params_panel",
    # Operators
    "operators.analyze_ops",
    "operators.match_ops",
    "operators.copy_ops",
    "operators.edit_ops",
    "operators.selection_ops",
)

# 已注册的类（供 unregister 反序注销）
_registered_classes = []


def _collect_classes():
    """
    自动收集需要注册的类。

    规则:
        - 只收集「定义在该模块中」的 bpy 类型子类
          （通过 __module__ 判定，排除 import 进来的类）
        - 保留模块内的定义顺序（Python 3.7+ 类按定义顺序出现在模块 __dict__ 中）

    Returns:
        list: 按注册顺序排列的类列表
    """
    collected = []
    seen = set()

    for module_path in _CLASS_MODULE_ORDER:
        full_path = f"{__package__}.{module_path}"
        try:
            module = importlib.import_module(full_path)
        except Exception as e:
            log_error(f"加载模块 {full_path} 失败", exc=e)
            continue

        for _name, obj in vars(module).items():
            if not isinstance(obj, type):
                continue
            if getattr(obj, "__module__", None) != full_path:
                continue
            try:
                if not issubclass(obj, bpy.types.bpy_struct):
                    continue
            except TypeError:
                continue
            if obj in seen:
                continue
            seen.add(obj)
            collected.append(obj)

    return collected


def _get_settings_class():
    """取出 VertexColorToolSettings 类（延迟导入，避免模块顶层循环依赖）"""
    module = importlib.import_module(f"{__package__}.properties.settings")
    return module.VertexColorToolSettings


@bpy.app.handlers.persistent
def _on_load_post(_dummy):
    """
    加载新 .blend 后清空缓存（P1-5）。

    缓存键包含对象内存指针，理论上跨文件不会误命中，
    但清空可以释放内存并彻底杜绝跨会话的脏数据。
    """
    try:
        VertexColorCache.clear_cache()
    except Exception:
        pass


def register():
    """
    注册插件的所有类和属性

    注册流程:
        1. 检查 Blender 版本兼容性
        2. 自动收集并按顺序注册所有类（失败时回滚）
        3. 添加 Scene 属性以存储设置
        4. 注册缓存失效钩子
    """
    # 版本兼容性检查
    if not check_blender_compatibility():
        log_warning("当前 Blender 版本低于插件要求，插件可能无法正常工作")

    global _registered_classes
    _registered_classes = _collect_classes()

    # 逐个注册，失败时回滚，避免留下半注册状态
    registered = []
    try:
        for cls in _registered_classes:
            bpy.utils.register_class(cls)
            registered.append(cls)
    except Exception as e:
        log_error("注册插件类时出错，正在回滚", exc=e)
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
        _registered_classes = []
        raise

    # 添加 Scene 属性
    bpy.types.Scene.vertex_color_tool = bpy.props.PointerProperty(
        type=_get_settings_class()
    )

    # 缓存失效钩子
    if _on_load_post not in load_post:
        load_post.append(_on_load_post)

    version = bl_info["version"]
    print(
        f"顶点色复制工具 v{version[0]}.{version[1]}.{version[2]} 已加载"
        f"（共注册 {len(_registered_classes)} 个类）"
    )


def unregister():
    """
    注销插件的所有类和属性

    注销流程:
        1. 移除缓存失效钩子
        2. 清空缓存并释放原生库
        3. 删除 Scene 属性
        4. 反向注销所有类
    """
    # 移除钩子
    if _on_load_post in load_post:
        try:
            load_post.remove(_on_load_post)
        except Exception:
            pass

    # 清空缓存（释放仍存活的原生 KDTree 句柄）并卸载原生库。
    # 顺序不能反: NativeKDTree.close() 依赖模块级 _lib 仍在，先清缓存后 FreeLibrary。
    # Windows 上 DLL 不显式释放会锁定文件，「卸载插件」删除安装目录时会报「文件被占用」。
    try:
        from .core.cache import VertexColorCache
        VertexColorCache.clear_cache()
    except Exception:
        pass
    try:
        from .core import native_backend
        native_backend.unload()
    except Exception:
        pass

    # 删除 Scene 属性
    if hasattr(bpy.types.Scene, 'vertex_color_tool'):
        del bpy.types.Scene.vertex_color_tool

    # 反向注销所有类（单个失败不阻断其余注销）
    for cls in reversed(_registered_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as e:
            log_error(f"注销类 {getattr(cls, '__name__', cls)} 时出错", exc=e)

    print("顶点色复制工具已卸载")


if __name__ == "__main__":
    register()
