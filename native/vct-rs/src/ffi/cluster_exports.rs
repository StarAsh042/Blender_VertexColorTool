//! 目标物体聚类的 C ABI 导出。
//!
//! 嵌套数组无法用裸指针表达，故分组结果以「扁平成员数组 + 每簇起始偏移」
//! 两块缓冲给出，Python 侧据此还原嵌套列表。

use crate::cluster::{cluster_targets, ClusterParams};
use crate::error::VctError;
use crate::types::ObjectFeatures;

use super::as_i32_slice_mut;

/// 对 `count` 个目标物体做贪心聚类。
///
/// `out_members` 长度须 ≥ 物体总数；`out_offsets` 长度须 ≥ `count + 1`，
/// 其中 `out_offsets[n]` 是第 `n` 簇在 `out_members` 中的起始下标，
/// 末元素为成员总数。簇数写入 `out_cluster_count`。
///
/// 返回 `0` 表示成功，负值见 [`VctError::code`]。
///
/// # Safety
///
/// 调用方必须保证：`features` 指向至少 `count` 个连续可读的 [`ObjectFeatures`]；
/// `params` 指向一个可读的 [`ClusterParams`]；
/// 两个输出数组容量足够（见上）。
#[no_mangle]
pub unsafe extern "C" fn vct_cluster_targets(
    features: *const ObjectFeatures,
    count: i32,
    params: *const ClusterParams,
    out_members: *mut i32,
    out_offsets: *mut i32,
    out_cluster_count: *mut i32,
) -> i32 {
    if features.is_null() || params.is_null() || out_cluster_count.is_null() {
        return VctError::NullInput.code();
    }
    if count < 0 {
        return VctError::NullInput.code();
    }
    let n = count as usize;

    let fs = match unsafe { as_object_features(features, n) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };
    // SAFETY: 调用方承诺 params 指向可读的一个 ClusterParams。
    let p = unsafe { &*params };
    let clusters = cluster_targets(fs, p);

    let members = match unsafe { as_i32_slice_mut(out_members, n.max(1)) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };
    let offsets = match unsafe { as_i32_slice_mut(out_offsets, n + 1) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };

    let mut cursor = 0_usize;
    for (ci, cluster) in clusters.iter().enumerate() {
        offsets[ci] = cursor as i32;
        for &m in cluster {
            members[cursor] = m as i32;
            cursor += 1;
        }
    }
    offsets[clusters.len()] = cursor as i32;
    // SAFETY: 上方已判空。
    unsafe { *out_cluster_count = clusters.len() as i32 };
    0
}

/// 由调用方给出的结构体数组转成切片。
///
/// # Safety
///
/// `ptr` 必须指向至少 `len` 个连续、可读、正确对齐的 [`ObjectFeatures`]。
unsafe fn as_object_features<'a>(
    ptr: *const ObjectFeatures,
    len: usize,
) -> Result<&'a [ObjectFeatures], VctError> {
    if len == 0 {
        return Ok(&[]);
    }
    if ptr.is_null() {
        return Err(VctError::NullInput);
    }
    // SAFETY: 调用方已承诺指针有效且长度足够。
    Ok(unsafe { core::slice::from_raw_parts(ptr, len) })
}

#[cfg(test)]
mod tests {
    use super::vct_cluster_targets;
    use crate::cluster::ClusterParams;
    use crate::types::ObjectFeatures;

    fn obj(x: f64) -> ObjectFeatures {
        ObjectFeatures {
            location: [x, 0.0, 0.0],
            dimensions: [1.0, 1.0, 1.0],
            volume: 1.0,
            vertex_count: 10.0,
            bounding_radius: 1.0,
        }
    }

    #[test]
    fn cluster_roundtrip_via_offsets() {
        let fs = [obj(0.0), obj(0.2), obj(90.0)];
        let p = ClusterParams {
            distance_threshold: 10.0,
            clustering_threshold: 0.8,
        };
        let mut members = [0_i32; 3];
        let mut offsets = [0_i32; 4];
        let mut cluster_count = 0_i32;
        let rc = unsafe {
            vct_cluster_targets(
                fs.as_ptr(),
                3,
                &p,
                members.as_mut_ptr(),
                offsets.as_mut_ptr(),
                &mut cluster_count,
            )
        };
        assert_eq!(rc, 0);
        assert_eq!(cluster_count, 2, "应为两簇");
        // 两簇 -> offsets 长度为 3：第 0 簇起始、第 1 簇起始、成员总数
        assert_eq!(offsets[0], 0, "第 0 簇从 0 开始");
        assert_eq!(offsets[1], 2, "第 1 簇从 2 开始（第 0 簇有两个成员）");
        assert_eq!(offsets[2], 3, "末元素是成员总数");
        assert_eq!(&members[..2], &[0, 1]);
        assert_eq!(members[2], 2);
    }

    #[test]
    fn empty_input_is_accepted() {
        let p = ClusterParams {
            distance_threshold: 10.0,
            clustering_threshold: 0.8,
        };
        let mut members = [0_i32; 1];
        let mut offsets = [0_i32; 2];
        let mut cluster_count = -1_i32;
        // 真实调用方（numpy）即使数组为空也会传合法指针，故用空数组而非 null。
        let empty: [ObjectFeatures; 0] = [];
        let rc = unsafe {
            vct_cluster_targets(
                empty.as_ptr(),
                0,
                &p,
                members.as_mut_ptr(),
                offsets.as_mut_ptr(),
                &mut cluster_count,
            )
        };
        assert_eq!(rc, 0);
        assert_eq!(cluster_count, 0);
    }
}
