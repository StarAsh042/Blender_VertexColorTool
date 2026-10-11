//! 物体相似度评分。
//!
//! # 与 Python 版的对齐关系
//!
//! 本模块逐项复刻 `core/matching.py: calculate_similarity_score`，
//! 包含它的**早退顺序**与**NaN 语义**——这两处最容易在移植时悄悄漂移。
//!
//! # 三处必须显式处理的细节
//!
//! 1. **早退**：距离超阈值直接返回 0，且**在算任何其他维度之前**。
//! 2. **`max` 的 NaN 语义**：Python 的 `max(a, b)` 遇 NaN 返回 NaN，
//!    而 Rust 的 `f64::max` 会**忽略** NaN 返回另一个参数。
//!    故本模块一律使用 [`py_max`]，不用 `f64::max`。
//! 3. **归一化的阈值**：总权重 ≤ 0.001 时**不**归一化（沿用 Python 的 0.001）。

// 评分要到 FFI 导出提交才被 crate 之外调用；此前 lib 目标视角下不可达。
// 该豁免在 FFI 落地后即可移除。
#![allow(dead_code)]

use crate::types::ObjectFeatures;

/// 匹配参数。字段与 Python 侧 `vc_tool` 上的同名属性一一对应。
#[derive(Debug, Clone, Copy, PartialEq)]
#[repr(C)]
pub struct MatchParams {
    /// 最大匹配距离（比的是两个物体原点的距离，不是包围盒中心）。
    pub distance_threshold: f64,
    /// 位置衰减系数，越小则距离影响越缓和。
    pub position_decay_factor: f64,
    /// 位置维权重。
    pub w_distance: f64,
    /// 包围盒尺寸维权重。
    pub w_size: f64,
    /// 体积维权重。
    pub w_volume: f64,
    /// 顶点数维权重。
    pub w_vertex: f64,
    /// 相似度准入阈值，低于它直接淘汰。
    pub similarity_threshold: f64,
}

/// Python 语义的 `max(a, b)`。
///
/// 与 `f64::max` 的唯一区别在 NaN：Python 返回 NaN，本函数亦然；
/// `f64::max` 则会跳过 NaN 返回另一个参数。
#[must_use]
pub fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// Python 语义的 `max(a, b, c)`。
#[must_use]
pub fn py_max3(a: f64, b: f64, c: f64) -> f64 {
    py_max(py_max(a, b), c)
}

/// 计算两个物体的相似度，返回 `[0, 1]`。
///
/// 权重和 ≤ 0.001 时**不归一化**（与 Python 一致），此时四个权重均为 0
/// 会让结果恒为 0——这是 Python 的既有行为，此处原样保留，缺陷修复另走一批提交。
#[must_use]
pub fn similarity(source: &ObjectFeatures, target: &ObjectFeatures, p: &MatchParams) -> f64 {
    // 1. 距离早退：必须在任何其他维度之前，与 Python 的顺序一致。
    let dx = source.location[0] - target.location[0];
    let dy = source.location[1] - target.location[1];
    let dz = source.location[2] - target.location[2];
    let distance_sq = dx * dx + dy * dy + dz * dz;
    let max_distance_sq = p.distance_threshold * p.distance_threshold;
    if distance_sq > max_distance_sq {
        return 0.0;
    }
    let distance = distance_sq.sqrt();

    // 2. 位置分：按两物体半径之和归一化后做指数衰减。
    let avg_size = source.bounding_radius + target.bounding_radius;
    let distance_score = if avg_size > 0.001 {
        let normalized = distance / avg_size;
        (-normalized * p.position_decay_factor).exp()
    } else {
        1.0
    };

    // 3. 尺寸分：逐轴取相对差，跨度恒为 3。
    let mut size_diff = 0.0;
    for i in 0..3 {
        let s = source.dimensions[i];
        let t = target.dimensions[i];
        let m = py_max(s, t);
        if m > 0.001 {
            size_diff += (s - t).abs() / m;
        }
    }
    let size_score = 1.0 - size_diff / 3.0;

    // 4. 体积分：与尺寸不同源（真实体积，不是包围盒三边之积）。
    let max_vol = py_max3(source.volume, target.volume, 0.001);
    let volume_score = 1.0 - (source.volume - target.volume).abs() / max_vol;

    // 5. 顶点数分：分母下限取 1，与 Python 一致。
    let max_vert = py_max3(source.vertex_count, target.vertex_count, 1.0);
    let vertex_score = 1.0 - (source.vertex_count - target.vertex_count).abs() / max_vert;

    // 6. 加权：先求和再归一化，顺序与 Python 相同（浮点加法不满足结合律）。
    let total = distance_score * p.w_distance
        + size_score * p.w_size
        + volume_score * p.w_volume
        + vertex_score * p.w_vertex;
    let total_weight = p.w_distance + p.w_size + p.w_volume + p.w_vertex;
    let score = if total_weight > 0.001 {
        total / total_weight
    } else {
        total
    };

    score.clamp(0.0, 1.0)
}

/// 为一个目标物体找出得分最高的源物体。
///
/// # 与 Python 的一���之处
///
/// - `sim < similarity_threshold` 直接跳过（严格小于，恰好等于阈值会保留）。
/// - 更新用严格 `>`，故**并列时取源数组中下标最小者**。
/// - 得分恒为 0 时不会成为结果（因为要与初始的 0.0 比严格大于）。
#[must_use]
pub fn best_match(
    sources: &[ObjectFeatures],
    target: &ObjectFeatures,
    p: &MatchParams,
) -> Option<(u32, f64)> {
    let mut best: Option<(u32, f64)> = None;
    for (i, s) in sources.iter().enumerate() {
        let sim = similarity(s, target, p);
        if sim < p.similarity_threshold {
            continue;
        }
        let better = match best {
            None => true,
            Some((_, b)) => sim > b,
        };
        if better {
            best = Some((i as u32, sim));
        }
    }
    best
}

#[cfg(test)]
mod tests {
    use super::{best_match, py_max, py_max3, similarity, MatchParams};
    use crate::types::ObjectFeatures;

    fn obj(x: f64, y: f64, z: f64, size: f64, vol: f64, verts: f64) -> ObjectFeatures {
        ObjectFeatures {
            location: [x, y, z],
            dimensions: [size, size, size],
            volume: vol,
            vertex_count: verts,
            bounding_radius: size,
        }
    }

    fn params() -> MatchParams {
        MatchParams {
            distance_threshold: 50.0,
            position_decay_factor: 1.0,
            w_distance: 1.0,
            w_size: 1.0,
            w_volume: 1.0,
            w_vertex: 1.0,
            similarity_threshold: 0.0,
        }
    }

    fn zero_weights() -> MatchParams {
        MatchParams {
            w_distance: 0.0,
            w_size: 0.0,
            w_volume: 0.0,
            w_vertex: 0.0,
            ..params()
        }
    }

    #[test]
    fn identical_objects_score_one() {
        let a = obj(1.0, 2.0, 3.0, 2.0, 8.0, 100.0);
        let got = similarity(&a, &a, &params());
        assert!(
            (got - 1.0).abs() < 1e-12,
            "完全相同的物体应为 1.0，实际 {got}"
        );
    }

    #[test]
    fn distance_beyond_threshold_scores_zero() {
        let a = obj(0.0, 0.0, 0.0, 1.0, 1.0, 10.0);
        let b = obj(1000.0, 0.0, 0.0, 1.0, 1.0, 10.0);
        assert_eq!(similarity(&a, &b, &params()), 0.0, "超阈值必须早退为 0");
    }

    #[test]
    fn farther_object_scores_lower() {
        let a = obj(0.0, 0.0, 0.0, 1.0, 1.0, 10.0);
        let near = obj(1.0, 0.0, 0.0, 1.0, 1.0, 10.0);
        let far = obj(20.0, 0.0, 0.0, 1.0, 1.0, 10.0);
        let p = params();
        let s_near = similarity(&a, &near, &p);
        let s_far = similarity(&a, &far, &p);
        assert!(s_near > s_far, "近的应比远的分高：{s_near} vs {s_far}");
    }

    #[test]
    fn zero_weights_skip_normalisation_and_score_zero() {
        let a = obj(1.0, 1.0, 1.0, 2.0, 8.0, 100.0);
        assert_eq!(
            similarity(&a, &a, &zero_weights()),
            0.0,
            "权重全 0 时不得归一化，结果应恒为 0"
        );
    }

    #[test]
    fn py_max_propagates_nan_like_python() {
        let nan = f64::NAN;
        assert!(py_max(nan, 1.0).is_nan(), "py_max(NaN, 1.0) 必须返回 NaN");
        assert!(py_max3(nan, 1.0, 2.0).is_nan(), "三参 max 同样必须传播 NaN");
        // 对照：f64::max 会忽略 NaN，这正是本模块不用它的原因。
        assert_eq!(
            f64::max(nan, 1.0),
            1.0,
            "f64::max 忽略 NaN，是本模块绕开它的理由"
        );
    }

    #[test]
    fn non_finite_input_stays_in_range() {
        let inf = f64::INFINITY;
        let a = obj(inf, 0.0, 0.0, inf, inf, inf);
        let b = obj(0.0, 0.0, 0.0, 1.0, 1.0, 1.0);
        let got = similarity(&a, &b, &params());
        assert!((0.0..=1.0).contains(&got), "结果必须仍在 [0,1]，实际 {got}");
    }

    #[test]
    fn tie_keeps_lower_source_index() {
        let t = obj(1.0, 0.0, 0.0, 1.0, 1.0, 10.0);
        let left = obj(0.0, 0.0, 0.0, 1.0, 1.0, 10.0);
        let right = obj(2.0, 0.0, 0.0, 1.0, 1.0, 10.0);
        let (idx, _) = best_match(&[left, right], &t, &params()).expect("应能找到匹配");
        assert_eq!(idx, 0, "并列时必须取源数组中下标最小者");
    }

    #[test]
    fn below_threshold_candidate_is_rejected() {
        let t = obj(1000.0, 0.0, 0.0, 1.0, 1.0, 10.0);
        let far = obj(0.0, 0.0, 0.0, 1.0, 1.0, 10.0);
        let mut p = params();
        p.similarity_threshold = 0.7;
        assert_eq!(best_match(&[far], &t, &p), None, "低于阈值必须淘汰");
    }
}
