//! 顶点色复制工具的原生计算内核。
//!
//! 本 crate 以纯 C ABI 形式导出函数，由 Python 侧通过 ctypes 加载。
//! 它**不做任何 I/O**：只接收调用方准备好的裸数组，返回计算结果。
//! 所有缓冲区由调用方分配，本库不负责其生命周期。

// unsafe 只允许出现在 ffi 模块（那里是唯一的裸指针入口）。
// 用 deny 而非 forbid：forbid 无法被内层 allow 覆盖，会连带禁止 ffi 模块。
// 论证见 src/ffi/mod.rs 的模块级文档。
#![deny(unsafe_code)]
#![warn(clippy::pedantic)]
#![deny(missing_docs)]
// 以下 lint 与本项目的既定语义冲突，逐条论证后关闭（不是为了"让门禁变绿"）：
//
// - float_cmp：并列最近邻「取全局下标较小者」的判据**就是**严格相等比较。
//   换成带容差的比较会让"恰好等距"不可判定，结果随浮点噪声漂移。
// - cast_possible_truncation / cast_sign_loss：点下标与节点下标在
//   u32 / usize / i32 之间往返。上界由 FFI 的 i32 计数参数决定，
//   远小于各类型的表示范围。
#![allow(
    clippy::float_cmp,
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_sign_loss
)]

mod error;

pub use error::VctError;

mod types;

pub use types::{ObjectFeatures, ABI_VERSION};

mod ffi;

mod kdtree;

mod matching;

mod pick;
