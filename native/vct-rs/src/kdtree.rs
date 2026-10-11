//! 三维 KD 树：构建与最近邻查询。
//!
//! # 为什么点集是拷贝而非借用
//!
//! 计划 D4 选定「拷贝点集」。这让本模块**全程安全代码**：
//! 建树与查询都用下标访问 `points`，不需要 `unsafe`。
//! unsafe 被收敛到 FFI 边界（把调用方的裸指针转成切片的那一层）。
//!
//! # 并列最近邻的决胜规则
//!
//! 距离完全相等时取**全局下标较小**的那个。这是本项目的确定性约定：
//! 分支定界的遍历顺序依赖树的形状，若用「先遇到者胜」，
//! 同样的输入在不同遍历路径下可能选中不同的点。

// 本模块的公开方法要到 FFI 导出提交才被 crate 之外调用。
// 在那之前 lib 目标视角下它们不可达（测试不计入可达性），会被判死代码。
// 该豁免在 FFI 导出落地后即可移除。
#![allow(dead_code)]

use crate::error::VctError;

/// 树中的一个节点。左右子树用下标表达而非引用，
/// 这样 `KdTree` 不必带生命周期参数，其类型保持简单可存。
#[derive(Debug, Clone, Copy)]
struct KdNode {
    /// 本节点持有的点在点集中的序号。
    point: u32,
    /// 分割所用坐标轴：`0`=x `1`=y `2`=z。
    axis: u8,
    /// 左子树根的下标，`-1` 表示空。
    left: i32,
    /// 右子树根的下标，`-1` 表示空。
    right: i32,
}

///
/// 点集在构建时被拷贝进树，因此本结构体不含任何生命周期参数。
#[derive(Debug)]
pub struct KdTree {
    /// 节点数组，用下标互相引用。
    nodes: Vec<KdNode>,
    /// 行主序的坐标拷贝，长度为 `3 * 点数`。
    points: Vec<f32>,
    /// 根节点下标，`-1` 表示空树。
    root: i32,
}

impl KdTree {
    /// 用 `points` 构建一棵树。
    ///
    /// `points` 是行主序的坐标数组，长度必须是 3 的倍数；
    /// 点 `i` 的坐标位于 `points[3*i .. 3*i+3]`。
    ///
    /// # Errors
    ///
    /// - 点集为空时返回 [`VctError::EmptyPointSet`]；
    /// - 长度不是 3 的倍数时返回 [`VctError::LengthMismatch`]。
    pub fn build(points: &[f32]) -> Result<Self, VctError> {
        if points.is_empty() {
            return Err(VctError::EmptyPointSet);
        }
        if points.len() % 3 != 0 {
            return Err(VctError::LengthMismatch {
                declared: (points.len() / 3) as i32,
                available: points.len(),
            });
        }

        let count = points.len() / 3;
        let mut order: Vec<u32> = (0..count as u32).collect();
        let mut tree = Self {
            nodes: Vec::with_capacity(count),
            points: points.to_vec(),
            root: -1,
        };
        tree.root = tree.build_range(&mut order, 0, count);
        Ok(tree)
    }

    /// 构建下标区间 `[lo, hi)`，返回根节点下标（空区间返回 `-1`）。
    fn build_range(&mut self, order: &mut [u32], lo: usize, hi: usize) -> i32 {
        if lo >= hi {
            return -1;
        }
        let axis = self.max_span_axis(order, lo, hi);
        let mid = lo + (hi - lo) / 2;

        // 对子区间做不稳定排序：整体有序后，中位数必然落在 mid。
        // 用 total_cmp 而非 <，是为 NaN 给出确定次序，否则排序结果不可复现。
        let coords = &self.points;
        let ax = axis as usize;
        order[lo..hi].sort_unstable_by(|a, b| {
            let va = coords[3 * *a as usize + ax];
            let vb = coords[3 * *b as usize + ax];
            va.total_cmp(&vb)
        });

        // 先占位再递归：递归可能让 nodes 扩容，故不能持有引用。
        let node = self.nodes.len() as i32;
        self.nodes.push(KdNode {
            point: order[mid],
            axis,
            left: -1,
            right: -1,
        });
        let left = self.build_range(order, lo, mid);
        let right = self.build_range(order, mid + 1, hi);
        self.nodes[node as usize].left = left;
        self.nodes[node as usize].right = right;
        node
    }

    /// 返回 `[lo, hi)` 中跨度最大的坐标轴；全部重合时返回 0。
    fn max_span_axis(&self, order: &[u32], lo: usize, hi: usize) -> u8 {
        let mut lowest = [f32::INFINITY; 3];
        let mut highest = [f32::NEG_INFINITY; 3];
        for &idx in &order[lo..hi] {
            let base = 3 * idx as usize;
            for (a, (low, high)) in lowest.iter_mut().zip(highest.iter_mut()).enumerate() {
                let v = self.points[base + a];
                if v < *low {
                    *low = v;
                }
                if v > *high {
                    *high = v;
                }
            }
        }
        let mut best = 0_usize;
        let mut best_span = f32::NEG_INFINITY;
        for a in 0..3 {
            let span = highest[a] - lowest[a];
            if span > best_span {
                best_span = span;
                best = a;
            }
        }
        best as u8
    }

    /// 返回离 `query` 最近的点的全局下标与距离平方。
    ///
    /// 距离完全相等时取下标较小者（见模块文档的决胜规则）。
    /// 树为空时返回 `None`。
    pub fn nearest(&self, query: &[f32]) -> Option<(u32, f32)> {
        if self.root < 0 {
            return None;
        }
        let mut best: Option<(u32, f32)> = None;
        self.search(self.root, query, &mut best);
        best
    }

    /// 分支定界搜索：只有当查询点到分割超平面的距离小于当前最优距离时才探远端。
    fn search(&self, node_index: i32, query: &[f32], best: &mut Option<(u32, f32)>) {
        let node = self.nodes[node_index as usize];
        let base = 3 * node.point as usize;
        let dist_sq = self.squared_distance(node.point as usize, query);

        let better = match *best {
            None => true,
            Some((idx, d)) => dist_sq < d || (dist_sq == d && node.point < idx),
        };
        if better {
            *best = Some((node.point, dist_sq));
        }

        let diff = query[node.axis as usize] - self.points[base + node.axis as usize];
        let (near, far) = if diff < 0.0 {
            (node.left, node.right)
        } else {
            (node.right, node.left)
        };
        if near >= 0 {
            self.search(near, query, best);
        }
        let cutoff = best.map_or(0.0, |(_, d)| d);
        if far >= 0 && diff * diff <= cutoff {
            self.search(far, query, best);
        }
    }

    /// 点 `index` 到查询点的距离平方。
    fn squared_distance(&self, index: usize, query: &[f32]) -> f32 {
        let base = index * 3;
        let (dx, dy, dz) = (
            self.points[base] - query[0],
            self.points[base + 1] - query[1],
            self.points[base + 2] - query[2],
        );
        dx * dx + dy * dy + dz * dz
    }
}
#[cfg(test)]
mod tests {
    use super::KdTree;
    use crate::error::VctError;
    #[test]
    fn finds_exact_hit() {
        let pts = [0.0, 0.0, 0.0, 5.0, 5.0, 5.0, 1.0, 1.0, 1.0];
        let tree = KdTree::build(&pts).expect("应可建树");
        let (idx, d2) = tree.nearest(&[5.0, 5.0, 5.0]).expect("应命中点 1");
        assert_eq!(idx, 1);
        assert_eq!(d2, 0.0);
    }
    #[test]
    fn builds_from_single_point() {
        let tree = KdTree::build(&[1.0, 2.0, 3.0]).expect("单点应可建树");
        assert_eq!(tree.nodes.len(), 1);
    }

    #[test]
    fn builds_from_eight_cube_corners() {
        let pts = [
            0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, // 0
            0.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0,
        ];
        let tree = KdTree::build(&pts).expect("8 点应可建树");
        assert_eq!(tree.nodes.len(), 8);
    }

    #[test]
    fn rejects_empty_point_set() {
        assert_eq!(KdTree::build(&[]).err(), Some(VctError::EmptyPointSet));
    }

    #[test]
    fn rejects_length_not_multiple_of_three() {
        let got = KdTree::build(&[0.0, 1.0]);
        assert!(matches!(got, Err(VctError::LengthMismatch { .. })));
    }

    #[test]
    fn handles_all_points_coincident() {
        let pts = [2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0];
        let tree = KdTree::build(&pts).expect("全部重合也应可建树");
        assert_eq!(tree.nodes.len(), 3);
    }
}
