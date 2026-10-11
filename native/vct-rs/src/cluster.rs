//! 目标物体的贪心聚类。
//!
//! # 与 `core/matching.py:cluster_target_objects` 的对齐要点
//!
//! 1. **贪心对遍历顺序敏感**。原实现的注释明确写着「必须按物体下标 j 升序，
//!    顺序不一致会导致分组结果不同」。本实现因此：
//!    - 空间索引用 [`BTreeMap`] 而非 `HashMap`（后者的迭代顺序每次运行都可能不同）；
//!    - **从不遍历索引**，只按键查找；
//!    - 候选一律按物体下标升序后再逐个判定。
//! 2. **空间索引只做候选枚举，不做判定**。格边长取查询半径，
//!    保证「距离 <= radius」的点必然落在相邻 27 格内，不会漏配对；
//!    超距候选仍会被枚举出来，由调用方做精确距离比较后剔除——
//!    这样浮点边界行为与原实现完全一致。
//! 3. **半径 <= 0 时退回线性扫描**，语义与原实现的退化路径相同。
//! 4. **聚类相似度是四维等权**，且位置项**不带衰减系数**
//!    （原实现的 `_target_similarity_from_distance` 用的是 `exp(-d/avg)`）。
//!
//! # 一处结构性差异（归类 iii）
//!
//! 原实现要处理「特征缺失的物体」：把它们标记 assigned 但不产出任何簇。
//! Rust 侧的特征是一个稠密数组，**不存在缺失**这一状态，故该分支没有对应物。

// 聚类要到 FFI 导出提交才被 crate 之外调用；此前 lib 目标视角下不可达。
// 该豁免在 FFI 落地后即可移除。
#![allow(dead_code)]

use std::collections::BTreeMap;

use crate::matching::py_max3;
use crate::types::ObjectFeatures;

/// 聚类参数。
#[derive(Debug, Clone, Copy, PartialEq)]
#[repr(C)]
pub struct ClusterParams {
    /// 与匹配同一套距离阈值；聚类半径取它的一半。
    pub distance_threshold: f64,
    /// 簇内相似度门槛。
    pub clustering_threshold: f64,
}

/// 均匀网格空间索引。格边长等于查询半径。
struct Grid {
    inv_cell: f64,
    cells: BTreeMap<(i32, i32, i32), Vec<u32>>,
}

impl Grid {
    /// 以 `radius` 为格边长建索引。
    fn new(fs: &[ObjectFeatures], radius: f64) -> Self {
        let inv_cell = 1.0 / radius;
        let mut cells: BTreeMap<(i32, i32, i32), Vec<u32>> = BTreeMap::new();
        for (i, f) in fs.iter().enumerate() {
            let key = Self::key(f.location, inv_cell);
            cells.entry(key).or_default().push(i as u32);
        }
        Self { inv_cell, cells }
    }

    fn key(loc: [f64; 3], inv: f64) -> (i32, i32, i32) {
        (
            (loc[0] * inv).floor() as i32,
            (loc[1] * inv).floor() as i32,
            (loc[2] * inv).floor() as i32,
        )
    }

    /// 枚举可能落在一个格边长内的候选下标（含超距的，由调用方精确比较）。
    fn query(&self, loc: [f64; 3]) -> Vec<u32> {
        let (cx, cy, cz) = Self::key(loc, self.inv_cell);
        let mut out = Vec::new();
        for dx in -1..=1 {
            for dy in -1..=1 {
                for dz in -1..=1 {
                    if let Some(bucket) = self.cells.get(&(cx + dx, cy + dy, cz + dz)) {
                        out.extend_from_slice(bucket);
                    }
                }
            }
        }
        out
    }
}

/// 两点的欧氏距离。
fn distance3(a: [f64; 3], b: [f64; 3]) -> f64 {
    let (dx, dy, dz) = (a[0] - b[0], a[1] - b[1], a[2] - b[2]);
    (dx * dx + dy * dy + dz * dz).sqrt()
}

/// 簇内相似度：四维**等权**平均，位置项不带衰减系数。
///
/// 与匹配用的 [`crate::matching::similarity`] 不同——那一处按用户权重加权、
/// 且位置项乘了衰减因子；聚类此处是固定等权、固定 `exp(-d/avg)`。
#[must_use]
pub fn target_similarity(a: &ObjectFeatures, b: &ObjectFeatures, dist: f64) -> f64 {
    let avg = a.bounding_radius + b.bounding_radius;
    let dist_score = if avg > 0.001 {
        (-(dist / avg)).exp()
    } else {
        1.0
    };

    let mut size_diff = 0.0;
    for i in 0..3 {
        let m = crate::matching::py_max(a.dimensions[i], b.dimensions[i]);
        if m > 0.001 {
            size_diff += (a.dimensions[i] - b.dimensions[i]).abs() / m;
        }
    }
    let size_score = 1.0 - size_diff / 3.0;

    let max_vol = py_max3(a.volume, b.volume, 0.001);
    let vol_score = 1.0 - (a.volume - b.volume).abs() / max_vol;

    let max_vert = py_max3(a.vertex_count, b.vertex_count, 1.0);
    let vert_score = 1.0 - (a.vertex_count - b.vertex_count).abs() / max_vert;

    ((dist_score + size_score + vol_score + vert_score) / 4.0).min(1.0)
}

/// 把目标物体贪心聚类。
///
/// 返回每个簇的成员下标；簇内与簇间均按物体下标升序。
#[must_use]
pub fn cluster_targets(fs: &[ObjectFeatures], p: &ClusterParams) -> Vec<Vec<u32>> {
    let mut clusters: Vec<Vec<u32>> = Vec::new();
    if fs.is_empty() {
        return clusters;
    }
    let radius = p.distance_threshold * 0.5;
    let grid = if radius > 0.0 {
        Some(Grid::new(fs, radius))
    } else {
        None
    };
    let mut assigned = vec![false; fs.len()];

    for i in 0..fs.len() {
        if assigned[i] {
            continue;
        }
        let mut cluster: Vec<u32> = vec![i as u32];
        assigned[i] = true;

        let mut candidates: Vec<u32> = match &grid {
            Some(g) => g.query(fs[i].location),
            None => ((i + 1)..fs.len()).map(|j| j as u32).collect(),
        };
        // 贪心顺序敏感：候选必须按下标升序处理。
        candidates.sort_unstable();
        candidates.retain(|&j| !assigned[j as usize] && (j as usize) > i);

        for j in candidates {
            let jj = j as usize;
            // 精确距离比较保留浮点边界行为：索引只做粗筛，判定以这里为准。
            if distance3(fs[i].location, fs[jj].location) > radius {
                continue;
            }
            let d = distance3(fs[i].location, fs[jj].location);
            if target_similarity(&fs[i], &fs[jj], d) >= p.clustering_threshold {
                cluster.push(j);
                assigned[jj] = true;
            }
        }
        clusters.push(cluster);
    }
    clusters
}
