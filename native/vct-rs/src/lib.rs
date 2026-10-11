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

mod error;

pub use error::VctError;

mod types;

pub use types::{ObjectFeatures, ABI_VERSION};

mod ffi;

mod kdtree;
