//! 带约束取色判定的 C ABI 导出。

use core::ffi::c_void;

use crate::error::VctError;
use crate::kdtree::KdTree;
use crate::pick::{pick_one, PickParams};

use super::{as_f32_slice, as_f64_slice, as_i32_slice_mut};

/// 为每个目标点选出源顶点下标。
///
/// 写出 `out_src_index`：命中写源下标，判定为「不写入」写 `-1`。
/// `angle_threshold_deg <= 0` 表示关闭法线约束；
/// `max_dist_sq < 0` 表示关闭距离上限——**0 是合法上限值**（恰好命中），
/// 因此不能用 0 表示「关闭」。
///
/// 返回 `0` 表示成功，负值见 [`VctError::code`]。
///
/// # Safety
///
/// 调用方必须保证：`handle` 来自 [`super::kdtree_exports::vct_kdtree_create`]；
/// `src_normals` 非空时指向至少 `3 * 点数` 个连续的 `f64`；
/// `target_points` 指向至少 `count * 3` 个 `f32`；
/// `target_normals` 非空时指向至少 `count * 3` 个连续的 `f64`；
/// `out_src_index` 可写至少 `count` 个 `i32`。
#[no_mangle]
pub unsafe extern "C" fn vct_kdtree_pick_constrained(
    handle: *mut c_void,
    src_normals: *const f64,
    target_points: *const f32,
    target_normals: *const f64,
    count: i32,
    angle_threshold_deg: f64,
    max_dist_sq: f64,
    out_src_index: *mut i32,
) -> i32 {
    if handle.is_null() || count < 0 {
        return VctError::NullInput.code();
    }
    if count == 0 {
        return 0;
    }
    // SAFETY: 调用方承诺 handle 来自 vct_kdtree_create。
    let tree = unsafe { &*handle.cast::<KdTree>() };
    let n = count as usize;

    let targets = match unsafe { as_f32_slice(target_points, n * 3) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };
    let idx_out = match unsafe { as_i32_slice_mut(out_src_index, n) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };
    let src = unsafe { as_f64_slice(src_normals, tree.len() * 3) }.ok();

    let params = PickParams {
        angle_threshold_deg,
        max_dist_sq: if max_dist_sq < 0.0 {
            None
        } else {
            Some(max_dist_sq)
        },
    };

    for i in 0..n {
        let q = &targets[3 * i..3 * i + 3];
        let tn = if target_normals.is_null() {
            None
        } else {
            unsafe { as_f64_slice(target_normals.add(3 * i), 3) }.ok()
        };
        idx_out[i] = pick_one(tree, src, q, tn, &params).map_or(-1, |v| v as i32);
    }
    0
}

#[cfg(test)]
mod tests {
    use super::vct_kdtree_pick_constrained;
    use crate::ffi::kdtree_exports::vct_kdtree_create;

    #[test]
    fn constrained_pick_roundtrip() {
        let points: [f32; 6] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.4];
        let handle = unsafe { vct_kdtree_create(points.as_ptr(), 2) };
        assert!(!handle.is_null());

        let normals: [f64; 6] = [0.0, 0.0, -1.0, 0.0, 0.0, 1.0];
        let target: [f32; 3] = [0.0, 0.0, 0.05];
        let tnorm: [f64; 3] = [0.0, 0.0, 1.0];
        let mut out: i32 = -9;
        let rc = unsafe {
            vct_kdtree_pick_constrained(
                handle,
                normals.as_ptr(),
                target.as_ptr(),
                tnorm.as_ptr(),
                1,
                75.0,
                -1.0,
                &mut out,
            )
        };
        assert_eq!(rc, 0);
        assert_eq!(out, 1, "薄壁应选中 +z 侧的那个点");
        unsafe { super::super::kdtree_exports::vct_kdtree_free(handle) };
    }

    #[test]
    fn null_handle_is_rejected() {
        let mut out: i32 = 0;
        let rc = unsafe {
            vct_kdtree_pick_constrained(
                core::ptr::null_mut(),
                core::ptr::null(),
                core::ptr::null(),
                core::ptr::null(),
                1,
                75.0,
                -1.0,
                &mut out,
            )
        };
        assert_ne!(rc, 0, "空句柄必须报错");
    }
}
