//! `KDTree` 的 C ABI 导出。
//!
//! 句柄是 `Box<KdTree>` 擦除生命周期后的裸指针，由 [`vct_kdtree_free`] 释放，
//! 且必须**恰好一次**。Python 侧保证这一点：句柄释放后立即置空，`close()` 幂等。

use core::ffi::c_void;

use crate::error::VctError;
use crate::kdtree::KdTree;

use super::{as_f32_slice, as_f32_slice_mut, as_i32_slice_mut};

/// 构建一棵树并返回句柄；失败返回空指针。
///
/// # Safety
///
/// 调用方必须保证 `points` 指向至少 `count * 3` 个连续的 `f32`，
/// 且该内存在句柄存活期间不被修改或释放。
#[no_mangle]
pub unsafe extern "C" fn vct_kdtree_create(points: *const f32, count: i32) -> *mut c_void {
    if count <= 0 {
        return core::ptr::null_mut();
    }
    let Ok(slice) = (unsafe { as_f32_slice(points, count as usize * 3) }) else {
        return core::ptr::null_mut();
    };
    let Ok(tree) = KdTree::build(slice) else {
        return core::ptr::null_mut();
    };
    Box::into_raw(Box::new(tree)).cast::<c_void>()
}

/// 释放句柄。传入空指针是安全的空操作。
///
/// # Safety
///
/// `handle` 必须来自 [`vct_kdtree_create`]，且未被释放过。
#[no_mangle]
pub unsafe extern "C" fn vct_kdtree_free(handle: *mut c_void) {
    if handle.is_null() {
        return;
    }
    // SAFETY: 调用方承诺 handle 来自 Box::into_raw 且未重复释放。
    drop(unsafe { Box::from_raw(handle.cast::<KdTree>()) });
}

/// 批量最近邻查询：写出每个目标点的最近源点下标与距离平方。
///
/// 树为空时对应位置写下标 `-1`、距离 `0.0`。
/// 返回 `0` 表示成功，负值见 [`VctError::code`]。
///
/// # Safety
///
/// 调用方必须保证：句柄合法；`target_points` 指向至少 `count * 3` 个 `f32`；
/// `out_indices` 与 `out_dist_sq` 各可写至少 `count` 个元素。
#[no_mangle]
pub unsafe extern "C" fn vct_kdtree_query_indices(
    handle: *mut c_void,
    target_points: *const f32,
    count: i32,
    out_indices: *mut i32,
    out_dist_sq: *mut f32,
) -> i32 {
    if handle.is_null() || count < 0 {
        return VctError::NullInput.code();
    }
    if count == 0 {
        return 0;
    }
    // SAFETY: 调用方承诺句柄来自 vct_kdtree_create。
    let tree = unsafe { &*handle.cast::<KdTree>() };
    let queries = match unsafe { as_f32_slice(target_points, count as usize * 3) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };
    let idx_out = match unsafe { as_i32_slice_mut(out_indices, count as usize) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };
    let dist_out = match unsafe { as_f32_slice_mut(out_dist_sq, count as usize) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };

    for i in 0..count as usize {
        let q = &queries[3 * i..3 * i + 3];
        let Some((idx, d2)) = tree.nearest(q) else {
            idx_out[i] = -1;
            dist_out[i] = 0.0;
            continue;
        };
        idx_out[i] = idx as i32;
        dist_out[i] = d2;
    }
    0
}

#[cfg(test)]
mod tests {
    use super::{vct_kdtree_create, vct_kdtree_free, vct_kdtree_query_indices};

    #[test]
    fn create_query_free_roundtrip() {
        let points: [f32; 6] = [0.0, 0.0, 0.0, 4.0, 0.0, 0.0];
        let handle = unsafe { vct_kdtree_create(points.as_ptr(), 2) };
        assert!(!handle.is_null(), "建树应返回有效句柄");

        let target: [f32; 3] = [3.9, 0.0, 0.0];
        let mut idx: i32 = -1;
        let mut d2: f32 = -1.0;
        let rc = unsafe { vct_kdtree_query_indices(handle, target.as_ptr(), 1, &mut idx, &mut d2) };
        assert_eq!(rc, 0);
        assert_eq!(idx, 1, "应命中离 3.9 最近的那个点");
        assert!((d2 - 0.01).abs() < 1e-5, "距离平方应约为 0.01，实际 {d2}");

        unsafe { vct_kdtree_free(handle) };
    }

    #[test]
    fn null_handle_is_rejected() {
        let target: [f32; 3] = [0.0, 0.0, 0.0];
        let mut idx: i32 = 0;
        let mut d2: f32 = 0.0;
        let rc = unsafe {
            vct_kdtree_query_indices(core::ptr::null_mut(), target.as_ptr(), 1, &mut idx, &mut d2)
        };
        assert_ne!(rc, 0, "空句柄必须报错");
    }

    #[test]
    fn free_null_handle_is_noop() {
        unsafe { vct_kdtree_free(core::ptr::null_mut()) };
    }
}
