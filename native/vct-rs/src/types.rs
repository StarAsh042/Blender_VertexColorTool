//! 跨模块共享的数据形状。
//!
//! # 为什么是 `repr(C)`
//!
//! 这些结构体会被 Python 侧用 `ctypes` 直接解读。
//! `#[repr(C)]` 保证字段按声明顺序排列、无字段重排与填充优化，
//! 于是 Python 侧只需按同样的顺序声明 `ctypes.Structure._fields_` 即可，
//! 无需任何解析开销。

/// 本库的 ABI 版本号。
///
/// Python 侧加载后会校验该值，避免新旧不匹配导致的越界访问。
/// 该值是对外契约，只可递增。
pub const ABI_VERSION: i32 = 2;

/// 物体的几何特征，用于相似度匹配。
///
/// 全部字段为 `f64`（双精度）。这是本库的红线：
/// 评分公式里的阈值比较发生在双精度才成立的精度上，
/// 若在内部降为 `f32`，阈值边界上的判定会翻转，选中不同的物体。
#[derive(Debug, Clone, Copy, PartialEq)]
#[repr(C)]
pub struct ObjectFeatures {
    /// 世界坐标位置。
    pub location: [f64; 3],
    /// 包围盒三边尺寸。
    pub dimensions: [f64; 3],
    /// 体积。与 `dimensions` **不同源**——包围盒三边之积属于尺寸的重复计分。
    pub volume: f64,
    /// 顶点数。沿用 `f64` 是为与既有 ABI 的缓冲布局保持一致。
    pub vertex_count: f64,
    /// 包围球半径。用于按模型尺度归一化距离。
    pub bounding_radius: f64,
}

impl ObjectFeatures {
    /// 返回该结构体在 `repr(C)` 布局下的字节数。
    #[must_use]
    pub const fn size_in_bytes() -> usize {
        std::mem::size_of::<Self>()
    }
}

#[cfg(test)]
mod tests {
    use super::ObjectFeatures;

    #[test]
    fn object_features_layout_is_size() {
        // 3 + 3 + 1 + 1 + 1 个 f64。repr(C) 下无重排、无填充，
        // 字节数固定；Python 侧按同样的字段顺序声明 ctypes 结构才读得对。
        assert_eq!(ObjectFeatures::size_in_bytes(), 9 * 8);
    }
}
