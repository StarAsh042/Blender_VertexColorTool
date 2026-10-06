"""
mathutils 的最小替身（仅供 scripts/ 下的基准脚本在无 Blender 环境使用）。

只实现聚类/匹配算法实际用到的运算：
    - 加减（逐分量）
    - length / length_squared
    - 索引访问 x/y/z
    - copy()

刻意保持与 mathutils.Vector 一致的语义（不可变运算、逐分量），
使基准结果与真实 Blender 环境可比。
"""


class Vector3:
    __slots__ = ('x', 'y', 'z')

    def __init__(self, x=0.0, y=0.0, z=0.0):
        # 兼容 mathutils.Vector 的两种构造方式：
        #   Vector(x, y, z) 与 Vector((x, y, z))
        # core/cache.py 的无 numpy 回退路径用的是后者。
        if isinstance(x, (tuple, list)):
            x, y, z = (x + (0.0, 0.0, 0.0))[:3]
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def copy(self):
        return Vector3(self.x, self.y, self.z)

    def __getitem__(self, i):
        return (self.x, self.y, self.z)[i]

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.z

    def __sub__(self, other):
        return Vector3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __add__(self, other):
        return Vector3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __len__(self):
        return 3

    @property
    def length_squared(self):
        return self.x * self.x + self.y * self.y + self.z * self.z

    @property
    def length(self):
        return (self.x * self.x + self.y * self.y + self.z * self.z) ** 0.5

    def dot(self, other):
        """点积（阶段 B 法线约束需要）"""
        return self.x * other.x + self.y * other.y + self.z * other.z

    def normalized(self):
        """单位化（阶段 B 法线约束需要）；零向量返回自身"""
        n = self.length
        if n == 0.0:
            return Vector3(0.0, 0.0, 0.0)
        return Vector3(self.x / n, self.y / n, self.z / n)

    def __repr__(self):
        return f"Vector3({self.x}, {self.y}, {self.z})"
