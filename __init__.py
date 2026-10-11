"""
Blender顶点色复制工具

插件入口模块，负责注册和注销所有类。

兼容性:
    - Blender 3.2+（使用 mesh.color_attributes，该 API 自 3.2 引入）
    - Python 3.7+

说明:
    本阶段仅建立可被 Blender 加载的空壳，业务模块随后按依赖拓扑加入。
"""

bl_info = {
    "name": "顶点色复制工具",
    "author": "StarAsh042",
    "version": (0, 0, 0),
    "blender": (3, 2, 0),
    "location": "3D View > Sidebar > 顶点色复制",
    "description": "根据位置接近和相似性在两组模型间复制顶点色",
    "category": "Mesh",
}


def register():
    """登记插件注册表。此时尚无业务类可注册。"""


def unregister():
    """注销插件注册表。此时尚无业务类可注销。"""
