//! 带约束的最近点取色判定。
//!
//! # 语义（与 `core/vertex_color_ops.py:_find_color_index` 逐条对齐）
//!
//! 1. **距离是硬边界，且必须早于法线精修**。最近点即距离的全局最小值，
//!    它超限就意味着任何源顶点都超限，故二次搜索在数学上不可能有解——
//!    直接拒绝，不给「法线搜索救回来」留后门。
//! 2. **法线是边界内的精修**：只在已通过距离校验的点之间找朝向兼容者。
//! 3. 扩张搜索的半径**被距离上限夹住**，否则「方向对但距离越界」的候选
//!    会把上限架空。
//! 4. 找不到兼容候选时**退回纯距离最近点**。该点已通过距离校验，
//!    故这条兜底天然不会突破上限。
//!
//! # 与 Python 的一处已知差异（归类 iii：表示差异）
//!
//! Python 的 `mathutils.kdtree.find()` 在距离相等时选哪一个点**依赖树形**，
//! 且与它自己的 `find_range()` 顺序并不一致（Blender 3.4.1 无头实测确认）。
//! 本实现改为确定性规则「距离相同则取下标较小者」，结果可能与 Python 不同，
//! 但永远可复现。
//!
//! # 精度
//!
//! 法线一律按 `f64` 参与判定：它归一化后直接与余弦阈值比较，
//! `f32` 的有效数字不足以在阈值边界保持判定稳定。

// 约束取色要到 FFI 导出提交才被 crate 之外调用；此前 lib 目标视角下不可达。
// 该豁免在 FFI 落地后即可移除。
#![allow(dead_code)]

use crate::kdtree::KdTree;

/// 取色参数。
#[derive(Debug, Clone, Copy, PartialEq)]
#[repr(C)]
pub struct PickParams {
    /// 法线夹角阈值（度）。`<= 0` 表示关闭法线约束。
    pub angle_threshold_deg: f64,
    /// 取色距离上限（**平方**）。`None` 表示不启用。
    pub max_dist_sq: Option<f64>,
}

/// 法线搜索的最大扩张轮数，每轮半径翻倍。
const MAX_ROUNDS: u32 = 8;

/// 首轮半径下限：最近距离恰为 0 时若从 0 开始翻倍，8 轮只覆盖 0.128 个单位。
const MIN_RADIUS: f64 = 1e-3;

/// 夹角阈值转余弦阈值。
fn cos_of_angle(deg: f64) -> f64 {
    (deg * std::f64::consts::PI / 180.0).cos()
}

/// 两个向量的点积。
fn dot3(a: &[f64], b: &[f64]) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

/// 源顶点 `idx` 的法线。
fn normal_at(src: &[f64], idx: u32) -> &[f64] {
    let base = 3 * idx as usize;
    &src[base..base + 3]
}

/// 为一个目标点选出源顶点下标；判定为「不写入」时返回 `None`。
#[must_use]
pub fn pick_one(
    tree: &KdTree,
    src_normals: Option<&[f64]>,
    target: &[f32],
    target_normal: Option<&[f64]>,
    p: &PickParams,
) -> Option<u32> {
    let (nearest, dist_sq) = tree.nearest(target)?;

    // 1. 距离硬边界：必须早于法线精修。
    if let Some(limit) = p.max_dist_sq {
        if f64::from(dist_sq) > limit {
            return None;
        }
    }

    // 2. 约束关闭 / 数据缺失时，直接用纯距离最近点（原行为）。
    if p.angle_threshold_deg <= 0.0 {
        return Some(nearest);
    }
    let (Some(src), Some(tn)) = (src_normals, target_normal) else {
        return Some(nearest);
    };
    if src.len() < 3 * tree.len() {
        return Some(nearest);
    }

    let cos_threshold = cos_of_angle(p.angle_threshold_deg);
    if dot3(normal_at(src, nearest), tn) >= cos_threshold {
        return Some(nearest);
    }

    // 3. 朝向不符：逐步扩大半径，在上限内找朝向兼容的最近候选。
    let mut radius: f64 = f64::from(dist_sq).sqrt().max(MIN_RADIUS);
    let mut clamped = false;
    for _ in 0..MAX_ROUNDS {
        radius *= 2.0;
        if let Some(limit) = p.max_dist_sq {
            if radius * radius > limit {
                radius = limit.sqrt();
                clamped = true;
            }
        }
        let r_sq = (radius * radius) as f32;

        // range_query 已按 (距离, 下标) 升序，故取第一个兼容者即可。
        let hit = tree
            .range_query(target, r_sq)
            .into_iter()
            .find(|(idx, _)| dot3(normal_at(src, *idx), tn) >= cos_threshold);
        if let Some((idx, _)) = hit {
            return Some(idx);
        }
        // 半径已顶到距离上限：再翻倍只会夹回同一半径。
        // 用标志位而非浮点比较——(sqrt(m))^2 的舍入可能略小于 m，导致判不中而空转满 8 轮。
        if clamped {
            break;
        }
    }

    // 4. 兜底：退回纯距离最近点（已通过距离校验）。
    Some(nearest)
}
