"""
Blender版本兼容性检查模块

说明（对应审计报告 P2-16）:
    代码大量使用 mesh.color_attributes / active_color 等 API，
    这些自 Blender 3.2 起才提供，因此最低支持版本为 3.2 而非 3.0。
"""

import bpy

BLENDER_VERSION = bpy.app.version

# 新式颜色属性 (mesh.color_attributes) 自 3.2 引入，故最低支持 3.2
MIN_BLENDER_VERSION = (3, 2, 0)


def check_blender_compatibility():
    """
    检查Blender版本兼容性

    Returns:
        bool: 版本是否兼容
    """
    if BLENDER_VERSION < MIN_BLENDER_VERSION:
        print(
            f"警告: 此插件需要 Blender {MIN_BLENDER_VERSION[0]}.{MIN_BLENDER_VERSION[1]} "
            f"或更高版本"
        )
        print(
            f"当前版本: {BLENDER_VERSION[0]}.{BLENDER_VERSION[1]}.{BLENDER_VERSION[2]}"
        )
        return False
    return True
