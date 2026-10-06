"""
统一日志与错误上报工具模块

设计目标（对应审计报告 P2-18）:
    - 消除全项目散落的 `except Exception: print(...)` + 静默 return None 模式
    - 让错误既进入控制台（开发者可见），也写入面板状态栏（美术用户可见）
    - 统一日志前缀，便于在 Blender 系统控制台中检索

用法:
    from ..utils.logging_utils import log_warning, log_error, report_error

    try:
        ...
    except Exception as exc:
        report_error(self, context, f"复制 {obj.name} 失败", exc)
        return {'CANCELLED'}
"""

import traceback

LOG_PREFIX = "[顶点色工具]"


def log_info(message):
    """输出普通信息到系统控制台"""
    print(f"{LOG_PREFIX} {message}")


def log_warning(message):
    """输出警告到系统控制台"""
    print(f"{LOG_PREFIX} [警告] {message}")


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
    输出错误到控制台，并（在可用时）同步到面板状态栏。

    Args:
        message: 人类可读的错误描述
        exc: 可选异常对象，会打印完整堆栈
        context: 可选 Blender context，用于写入面板状态
    """
    print(f"{LOG_PREFIX} [错误] {message}")
    if exc is not None:
        traceback.print_exception(type(exc), exc, exc.__traceback__)

    if context is not None:
        _write_panel_status(context, message)


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
