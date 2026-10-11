"""
统一日志与错误上报工具模块

设计目标（对应审计报告 P2-18）:
    - 消除全项目散落的 `except Exception: print(...)` + 静默 return None 模式
    - 让错误既进入控制台（开发者可见），也写入面板状态栏（美术用户可见）
    - 统一日志前缀，便于在 Blender 系统控制台中检索

P0-5 修复说明:
    全项目共31 处 `log_error(...)` 调用点，其中 30 处位于「逐物体循环内部」
    （core/vertex_color_ops.py、core/matching.py、core/cache.py、
    operators/copy_ops.py 等），属于最贴近用户的失败路径。
    这些调用点都未传 context，导致 `_write_panel_status` 被跳过，
    错误只进 Blender 控制台 —— 而美术用户不会打开控制台，
    于是表现为「操作失败但完全不知道原因」。

    本文件的修法（最小改动、一次性覆盖全部 31 处）:
        context 缺省时回退到 `bpy.context`。Blender 始终存在活动 scene，
        因此回退几乎必然成功，调用点无需逐个修改签名。

    刻意**不**给 `log_warning` 加同样的回退：批量复制时原生内核回退等
    良性告警会高频触发，若也写入 `last_operation`，会把真正的错误信息
    覆盖掉，反而降低面板状态栏的信噪比。

用法:
    from ..utils.logging_utils import log_warning, log_error, report_error

    try:
        ...
    except Exception as exc:
        report_error(self, context, f"复制 {obj.name} 失败", exc)
        return {'CANCELLED'}
"""

import traceback

import bpy

LOG_PREFIX = "[顶点色工具]"


def log_info(message):
    """输出普通信息到系统控制台"""
    print(f"{LOG_PREFIX} {message}")


def log_warning(message):
    """输出警告到系统控制台（刻意不写面板，见模块 docstring 的 P0-5 说明）"""
    print(f"{LOG_PREFIX} [警告] {message}")


def _resolve_context(context):
    """
    解析用于写面板的 context，缺省时回退到 `bpy.context`（P0-5）。

    Args:
        context: 调用方传入的 Blender context，可为 None

    Returns:
        可用的 context 对象，或None（此时调用方应跳过面板写入）
    """
    if context is not None:
        return context

    try:
        return bpy.context
    except Exception:
        # 极端情况（如 Blender 关闭过程中）取不到全局 context，
        # 此时退化为仅输出到控制台，绝不允许因此抛出异常。
        return None


def _write_panel_status(context, message):
    """
    把消息写入面板状态栏（last_operation）。

    说明:
        美术用户通常不会打开系统控制台，仅 print 会导致"操作失败但不知原因"。
        这里在可用时同步一份到面板，让失败对用户可见。
    """
    try:
        scene = getattr(context, "scene", None)
        if scene is None:
            return
        vc_tool = getattr(scene, "vertex_color_tool", None)
        if vc_tool is not None:
            vc_tool.last_operation = f"⚠ {message}"
    except Exception:
        # 上报逻辑本身绝不允许再抛异常
        pass


def log_error(message, exc=None, context=None):
    """
    输出错误到控制台，并同步到面板状态栏。

    Args:
        message: 人类可读的错误描述
        exc: 可选异常对象，会打印完整堆栈
        context: 可选 Blender context。**省略时自动回退到 `bpy.context`**
            （P0-5），使调用点无需逐个传递 context 即可让用户看到错误。
    """
    print(f"{LOG_PREFIX} [错误] {message}")
    if exc is not None:
        traceback.print_exception(type(exc), exc, exc.__traceback__)

    resolved = _resolve_context(context)
    if resolved is not None:
        _write_panel_status(resolved, message)


def report_error(operator, context, message, exc=None):
    """
    Operator 场景下的统一错误处理：控制台 + self.report + 面板状态栏。

    Args:
        operator: bpy.types.Operator 实例（用于 self.report）
        context: Blender context
        message: 错误描述
        exc: 可选异常对象
    """
    log_error(message, exc=exc, context=context)
    try:
        operator.report({'ERROR'}, message)
    except Exception:
        pass
