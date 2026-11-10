use jsonschema::{Draft, Retrieve, Uri, Validator};
use serde_json::Value;
use std::panic::{catch_unwind, AssertUnwindSafe};

struct NoExternalSchemas;

impl Retrieve for NoExternalSchemas {
    fn retrieve(&self, _: &Uri<String>) -> Result<Value, Box<dyn std::error::Error + Send + Sync>> {
        Err("External schemas are disabled".into())
    }
}

// C ABI status: 0 = success, 1 = invalid schema/instance, 2 = validator failure.
// The caller owns input bytes during a call and destroys a successful handle once.
// No Rust strings, collections, errors, or panics cross this boundary.
/// # Safety
/// `bytes` must cover `length` readable bytes; `result` must point to writable
/// handle storage. Destroy the returned non-null handle exactly once.
#[no_mangle]
pub unsafe extern "C" fn tinyagent_schema_create(
    bytes: *const u8,
    length: usize,
    result: *mut *mut Validator,
) -> i32 {
    if bytes.is_null() || result.is_null() {
        return 2;
    }
    unsafe { *result = std::ptr::null_mut() };
    catch_unwind(AssertUnwindSafe(|| {
        let bytes = unsafe { std::slice::from_raw_parts(bytes, length) };
        let Ok(schema) = serde_json::from_slice::<Value>(bytes) else {
            return 1;
        };
        if !schema.is_object() {
            return 1;
        }
        let Ok(validator) = jsonschema::options()
            .with_draft(Draft::Draft202012)
            .should_validate_formats(false)
            .with_retriever(NoExternalSchemas)
            .build(&schema)
        else {
            return 1;
        };
        unsafe { *result = Box::into_raw(Box::new(validator)) };
        0
    }))
    .unwrap_or(2)
}

/// # Safety
/// The validator must be a live handle returned by create. `bytes` must cover
/// `length` readable bytes. Validation may run concurrently on the same handle.
#[no_mangle]
pub unsafe extern "C" fn tinyagent_schema_validate(
    validator: *const Validator,
    bytes: *const u8,
    length: usize,
) -> i32 {
    if validator.is_null() || bytes.is_null() {
        return 2;
    }
    catch_unwind(AssertUnwindSafe(|| {
        let bytes = unsafe { std::slice::from_raw_parts(bytes, length) };
        let Ok(instance) = serde_json::from_slice::<Value>(bytes) else {
            return 1;
        };
        if instance.is_object() && unsafe { &*validator }.is_valid(&instance) {
            0
        } else {
            1
        }
    }))
    .unwrap_or(2)
}

/// # Safety
/// Pass a handle returned by create exactly once, after all validations finish.
#[no_mangle]
pub unsafe extern "C" fn tinyagent_schema_destroy(validator: *mut Validator) {
    if !validator.is_null() {
        drop(unsafe { Box::from_raw(validator) });
    }
}
