"""Print the Kev precision for this CPU: bf16 only where the CPU computes it natively
(AVX512-BF16 or AMX); elsewhere bf16 is emulated and several times slower than fp32."""
try:
    import torch
    c = torch._C._cpu
    print("bf16" if c._is_avx512_bf16_supported() or c._is_amx_tile_supported() else "fp32")
except Exception:
    print("fp32")
