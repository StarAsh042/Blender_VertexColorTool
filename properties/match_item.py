"""
匹配结果项数据模型
"""

import bpy


class VertexColorMatchItem(bpy.types.PropertyGroup):
    """
    匹配结果项 - 存储单个匹配关系的信息

    属性:
        source_name: 源物体名称
        target_name: 目标物体名称
        confidence: 匹配置信度 (0-100%)
        source_vcol_layer: 源顶点色层名称
        source_vcol_domain: 源顶点色域 (POINT/CORNER)
        cluster_id: 聚类ID，-1表示未使用聚类
    """
    source_name: bpy.props.StringProperty(
        name="源物体",
        description="源物体名称"
    )
    target_name: bpy.props.StringProperty(
        name="目标物体",
        description="目标物体名称"
    )
    confidence: bpy.props.FloatProperty(
        name="置信度",
        min=0,
        max=100,
        subtype='PERCENTAGE',
        description="匹配置信度百分比"
    )
    source_vcol_layer: bpy.props.StringProperty(
        name="源顶点色层",
        description="源物体的顶点色层名称"
    )
    source_vcol_domain: bpy.props.StringProperty(
        name="源顶点色域",
        description="顶点色数据域: POINT或CORNER"
    )
    cluster_id: bpy.props.IntProperty(
        name="聚类ID",
        default=-1,
        description="聚类标识符，-1表示未使用聚类"
    )
