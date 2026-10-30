"""Print the Kev precision for this CPU: bf16 only where the CPU computes it natively
(AVX512-BF16 or AMX); elsewhere bf16 is emulated and several times slower than fp32.
torch.cpu has the checks in torch 2.8 and 2.13; torch._C._cpu lost them by 2.13 (the reason for the fallback order)."""
try:
    import torch
    c = torch.cpu if hasattr(torch.cpu, "_is_avx512_bf16_supported") else torch._C._cpu
    print("bf16" if c._is_avx512_bf16_supported() or c._is_amx_tile_supported() else "fp32")
except Exception:
    print("fp32")
