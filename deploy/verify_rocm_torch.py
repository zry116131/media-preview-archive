import json
import time

import torch


def run_matmul(dtype: torch.dtype) -> dict[str, object]:
    torch.manual_seed(7)
    left = torch.randn((2048, 2048), device="cuda", dtype=dtype)
    right = torch.randn((2048, 2048), device="cuda", dtype=dtype)
    torch.cuda.synchronize()
    started = time.perf_counter()
    result = left @ right
    torch.cuda.synchronize()
    return {
        "dtype": str(dtype),
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "shape": list(result.shape),
        "finite": bool(torch.isfinite(result).all().item()),
        "mean": round(float(result.float().mean().item()), 6),
    }


report = {
    "torch": torch.__version__,
    "hip": torch.version.hip,
    "cuda_available": torch.cuda.is_available(),
    "device_count": torch.cuda.device_count(),
    "device": torch.cuda.get_device_name(0),
    "fp16": run_matmul(torch.float16),
    "bf16": run_matmul(torch.bfloat16),
}
print(json.dumps(report, ensure_ascii=False, indent=2))
