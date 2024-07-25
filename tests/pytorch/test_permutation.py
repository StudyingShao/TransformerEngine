# Copyright (c) 2022-2024, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# See LICENSE for license information.

import torch
import pytest
from typing import Dict, List

from transformer_engine.pytorch import permute as te_permute, unpermute as te_unpermute
from transformer_engine.pytorch.utils import is_bf16_compatible
from transformer_engine.pytorch.fp8 import FP8GlobalStateManager
import transformer_engine_torch as tex

# Only run FP8 tests on H100.
fp8_available, reason_for_no_fp8 = FP8GlobalStateManager.is_fp8_available()

seed = 1234
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)

# TE tensor dtypes
_te_dtypes: List[tex.DType] = [tex.DType.kFloat32, tex.DType.kFloat16]
if is_bf16_compatible():
    _te_dtypes.append(tex.DType.kBFloat16)
if fp8_available:
    _te_dtypes.extend([tex.DType.kFloat8E4M3, tex.DType.kFloat8E5M2])


def pytorch_permute(tokens, indices, num_out_tokens: int = None):
    """
    Permute the tokens based on the indices. Token with the same index will be grouped together.
    The input indices shape is [tokens, top_k], it indicates which experts were selected by each token separately.

    Args:
        tokens: torch.Tensor
            The input token tensor.
        indices: torch.Tensor
            The token to expert indices tensor, should have a shape of [num_tokens] or [num_tokens, topk].
        num_out_tokens: int, optional
            The effective output token count, when enabling the capacity factor, should equal the number of tokens not dropped.
            By default, set to None, meaning no tokens are dropped.

    Returns:
        torch.Tensor:
            The permuted tensor.
        torch.Tensor:
            The sorted_indices corresponding permuted tensor.
    """
    if indices.dim() == 1:
        topk = 1
    else:
        topk = indices.size(1)
    flatten_indices = indices.view(-1)
    sorted_indices = torch.argsort(flatten_indices, stable=True)
    num_out_tokens = num_out_tokens if num_out_tokens is not None else flatten_indices.size(0)

    permuted_tokens = tokens.index_select(0, sorted_indices[:num_out_tokens] // topk)
    return permuted_tokens, sorted_indices


def pytorch_unpermute(
    permuted_tokens: torch.Tensor,
    sorted_indices: torch.Tensor,
    probs: torch.Tensor = None,
):
    """
    Unpermute a tensor of permuted tokens based on sorted indices, and optionally merge the tokens with their
    corresponding probabilities.

    Args:
        permuted_tokens: torch.Tensor
            The tensor of permuted tokens to be unpermuted.
        sorted_indices: torch.Tensor
            The tensor of sorted indices used to unpermute the tokens.
        probs: torch.Tensor, optional
            The tensor of probabilities corresponding to the permuted tokens. If provided, the unpermuted tokens will
            be merged with their respective probabilities.

    Returns:
        torch.Tensor:
            The unpermuted tokens, optionally merged with probabilities.
    """

    if probs is not None:
        # Unpermute and merge the tokens with their probabilities
        num_unpermuted_tokens = probs.numel()
        topk = probs.size(1)
    else:
        # Unpermute the tokens without merge
        num_unpermuted_tokens = sorted_indices.size(0)
        topk = 1
    unpermuted_tokens = torch.zeros(
        [num_unpermuted_tokens, permuted_tokens.shape[-1]],
        dtype=permuted_tokens.dtype,
        device=permuted_tokens.device,
    )

    unpermuted_tokens.index_copy_(0, sorted_indices[: permuted_tokens.size(0)], permuted_tokens)
    unpermuted_tokens = unpermuted_tokens.reshape(-1, topk, permuted_tokens.size(-1))
    if probs is not None:
        unpermuted_tokens = unpermuted_tokens * probs.unsqueeze(-1)
    unpermuted_tokens = unpermuted_tokens.sum(dim=1)
    return unpermuted_tokens


def dtype_tols(te_dtype: tex.DType) -> Dict[str, float]:
    """Estimated tolerances for a datatype

    Based on tolerances for torch.testing.assert_close.

    """
    if te_dtype == tex.DType.kFloat32:
        return dict(rtol=1.0e-6, atol=1.0e-6)
    if te_dtype == tex.DType.kFloat16:
        return dict(rtol=3.0e-3, atol=1.0e-5)
    if te_dtype == tex.DType.kBFloat16:
        return dict(rtol=2.0e-2, atol=1.0e-5)
    if te_dtype == tex.DType.kFloat8E5M2 or te_dtype == tex.DType.kFloat8E4M3:
        return dict(rtol=2.0e-1, atol=1.0e-1)
    raise ValueError(f"Unsuppored dtype ({te_dtype})")


def fp8_to_fp16(uint8_tensor, e4m3: bool = True):
    assert uint8_tensor.dtype == torch.uint8, "Input tensor must be uint8"

    float16_tensor = torch.zeros_like(uint8_tensor, dtype=torch.float16)

    sign = (uint8_tensor >> 7) & 1
    exponent_mask = 0xF if e4m3 else 0x1F
    if e4m3:
        exponent = (uint8_tensor >> 3) & exponent_mask
        mantissa = uint8_tensor & 0x7
    else:
        exponent = (uint8_tensor >> 2) & exponent_mask
        mantissa = uint8_tensor & 0x3

    exponent_bias = 7 if e4m3 else 15
    mantissa_max = 8.0 if e4m3 else 4.0

    normal_mask = (exponent != 0) & ~(exponent == exponent_mask)
    actual_exponent = exponent[normal_mask].to(torch.float16) - exponent_bias
    actual_mantissa = (mantissa[normal_mask].to(torch.float16) + mantissa_max) / mantissa_max
    float16_tensor[normal_mask] = (
        ((-1) ** sign[normal_mask].to(torch.float16)) * (2**actual_exponent) * actual_mantissa
    )

    subnormal_mask = (exponent == 0) & (mantissa != 0)
    subnormal_exponent = 1 - exponent_bias
    subnormal_mantissa = mantissa[subnormal_mask].to(torch.float16) / mantissa_max
    float16_tensor[subnormal_mask] = (
        ((-1) ** sign[subnormal_mask].to(torch.float16))
        * (2**subnormal_exponent)
        * subnormal_mantissa
    )

    return float16_tensor


@pytest.mark.parametrize("te_dtype", _te_dtypes)
@pytest.mark.parametrize("num_tokens", [4096])
@pytest.mark.parametrize("num_expert", [8, 16])
@pytest.mark.parametrize("hidden_size", [4096])
@pytest.mark.parametrize("topK", [1, 2, 5])
@pytest.mark.parametrize("num_out_tokens", [None, 4050])
@pytest.mark.parametrize("with_probs", [True, False])
def test_permutation(
    te_dtype,
    num_tokens,
    num_expert,
    hidden_size,
    topK,
    num_out_tokens,
    with_probs,
    BENCHMARK=False,
):
    if not with_probs and topK > 1:
        pytest.skip("Only permutations with topK=1 and without probabilities are supported.")

    if topK > num_expert:
        pytest.skip("topK should be smaller than the number of experts.")

    if num_out_tokens == None:
        num_out_tokens = num_tokens * topK

    print(
        f"token:{num_tokens} hidden_size:{hidden_size} expert:{num_expert} topK:{topK} {te_dtype}"
    )

    fp8 = False
    # Convert TE dtypes to PyTorch dtypes
    if te_dtype == tex.DType.kFloat32:
        dtype = torch.float32
    elif te_dtype == tex.DType.kFloat16:
        dtype = torch.float16
    elif te_dtype == tex.DType.kBFloat16:
        dtype = torch.bfloat16
    elif fp8_available and (te_dtype == tex.DType.kFloat8E5M2 or te_dtype == tex.DType.kFloat8E4M3):
        dtype = torch.uint8
        fp8 = True
    else:
        pytest.skip("Invalid dtype.")

    if fp8:
        N = 56 if te_dtype == tex.DType.kFloat8E4M3 else 60
        permute_fwd_input = torch.randint(
            low=0, high=N + 1, size=(num_tokens, hidden_size), dtype=torch.uint8
        ).cuda()
        permute_bwd_input = torch.randint(
            low=0, high=N + 1, size=(num_out_tokens, hidden_size), dtype=torch.uint8
        ).cuda()
        unpermute_bwd_input = torch.randint(
            low=0, high=N + 1, size=(num_tokens, hidden_size), dtype=torch.uint8
        ).cuda()
        pytorch_permute_fwd_input = fp8_to_fp16(
            permute_fwd_input, te_dtype == tex.DType.kFloat8E4M3
        )
        pytorch_permute_bwd_input = fp8_to_fp16(
            permute_bwd_input, te_dtype == tex.DType.kFloat8E4M3
        )
        pytorch_unpermute_bwd_input = fp8_to_fp16(
            unpermute_bwd_input, te_dtype == tex.DType.kFloat8E4M3
        )
    else:
        pytorch_permute_fwd_input = torch.rand((num_tokens, hidden_size), dtype=dtype).cuda()
        pytorch_permute_bwd_input = torch.rand((num_out_tokens, hidden_size), dtype=dtype).cuda()
        pytorch_unpermute_bwd_input = torch.rand((num_tokens, hidden_size), dtype=dtype).cuda()

    pytorch_permute_fwd_input.requires_grad_(True)

    if num_tokens > 0:
        indices = torch.stack([torch.randperm(num_expert)[:topK] for _ in range(num_tokens)])
    else:
        indices = torch.empty((num_tokens, topK))
    indices = indices.to(torch.int32).cuda()

    probs = None
    if with_probs:
        probs = torch.rand(num_tokens, topK).cuda()
        row_sums = probs.sum(dim=1, keepdim=True)
        probs = probs / row_sums
        probs.requires_grad_(True)

    ###################################################################################################################################
    #
    # PyTorch Permutation
    #
    ###################################################################################################################################
    pytorch_permute_output, sorted_indices = pytorch_permute(
        pytorch_permute_fwd_input, indices, num_out_tokens
    )
    pytorch_permute_output.backward(pytorch_permute_bwd_input, retain_graph=True)

    pytorch_unpermute_fwd_input = pytorch_permute_output.detach()
    pytorch_unpermute_fwd_input.requires_grad_(True)

    pytorch_unpermute_output = pytorch_unpermute(
        pytorch_unpermute_fwd_input, sorted_indices, probs=probs
    )
    pytorch_unpermute_output.backward(pytorch_unpermute_bwd_input, retain_graph=True)

    ###################################################################################################################################
    #
    # TE Permutation
    #
    ###################################################################################################################################
    te_permute_fwd_input = (
        permute_fwd_input.view(torch.float32) if fp8 else pytorch_permute_fwd_input.detach()
    )
    te_permute_fwd_input.requires_grad_(True)
    te_permute_bwd_input = (
        permute_bwd_input.view(torch.float32) if fp8 else pytorch_permute_bwd_input.detach()
    )

    te_permute_output, row_id_map = te_permute(
        te_permute_fwd_input, te_dtype, indices, num_out_tokens
    )
    te_permute_output.backward(te_permute_bwd_input, retain_graph=True)

    te_probs = None
    if with_probs:
        te_probs = probs.detach()
        te_probs.requires_grad_(True)
    te_unpermute_fwd_input = te_permute_output.detach()
    te_unpermute_fwd_input.requires_grad_(True)
    te_unpermute_bwd_input = (
        unpermute_bwd_input.view(torch.float32) if fp8 else pytorch_unpermute_bwd_input.detach()
    )

    te_unpermute_output = te_unpermute(te_unpermute_fwd_input, te_dtype, row_id_map, te_probs)
    te_unpermute_output.backward(te_unpermute_bwd_input, retain_graph=True)

    ###################################################################################################################################
    #
    # Results Check
    #
    ###################################################################################################################################
    tols = dtype_tols(te_dtype)

    if fp8:
        te_permute_output_ = fp8_to_fp16(
            te_permute_output.view(torch.uint8), te_dtype == tex.DType.kFloat8E4M3
        )
        te_permute_fwd_input_grad = fp8_to_fp16(
            te_permute_fwd_input.grad.view(torch.uint8), te_dtype == tex.DType.kFloat8E4M3
        )
        te_unpermute_output_ = fp8_to_fp16(
            te_unpermute_output.view(torch.uint8), te_dtype == tex.DType.kFloat8E4M3
        )
        te_unpermute_fwd_input_grad = fp8_to_fp16(
            te_unpermute_fwd_input.grad.view(torch.uint8), te_dtype == tex.DType.kFloat8E4M3
        )
    else:
        te_permute_output_ = te_permute_output
        te_permute_fwd_input_grad = te_permute_fwd_input.grad
        te_unpermute_output_ = te_unpermute_output
        te_unpermute_fwd_input_grad = te_unpermute_fwd_input.grad

    torch.testing.assert_close(
        pytorch_permute_output.float(),
        te_permute_output_.float(),
        msg=f"Mismatch in te_permute fwd",
    )
    torch.testing.assert_close(
        pytorch_permute_fwd_input.grad.float(),
        te_permute_fwd_input_grad.float(),
        msg=f"Mismatch in te_permute bwd",
        **tols,
    )
    torch.testing.assert_close(
        pytorch_unpermute_output.float(),
        te_unpermute_output_.float(),
        msg=f"Mismatch in te_unpermute fwd",
        **tols,
    )
    torch.testing.assert_close(
        pytorch_unpermute_fwd_input.grad.float(),
        te_unpermute_fwd_input_grad.float(),
        msg=f"Mismatch in te_unpermute bwd",
        **tols,
    )
    if with_probs:
        torch.testing.assert_close(
            probs.grad.float(), te_probs.grad.float(), msg=f"Mismatch in te_unpermute bwd", **tols
        )

    if not pytorch_permute_fwd_input.numel():
        print("Empty pytorch_permute_fwd_input activation test passed.")
        return

    ###################################################################################################################################
    #
    # Benchmark
    #
    ###################################################################################################################################
    def backward_wrapper(
        act, backward_input, forward_input=[], retain_graph=True, accumulate_grad=False
    ):
        # Set forward_input.grad to None to avoid grad accumulation.
        if accumulate_grad == False:
            for i in forward_input:
                i.grad = None
        return act.backward(backward_input, retain_graph=retain_graph)

    if BENCHMARK:
        t1 = perf_test_cuda_kernel(
            lambda: pytorch_permute(pytorch_permute_fwd_input, indices, num_out_tokens)
        )
        t2 = perf_test_cuda_kernel(
            lambda: te_permute(te_permute_fwd_input, te_dtype, indices, num_out_tokens)
        )
        print(f"permute\t\tfwd: pytorch: {t1:.3f} ms,  TE: {t2:.3f} ms")

        t1 = perf_test_cuda_kernel(
            lambda: backward_wrapper(
                pytorch_permute_output,
                pytorch_permute_bwd_input,
                forward_input=[pytorch_permute_fwd_input],
                retain_graph=True,
                accumulate_grad=False,
            )
        )
        t2 = perf_test_cuda_kernel(
            lambda: backward_wrapper(
                te_permute_output,
                te_permute_bwd_input,
                forward_input=[te_permute_fwd_input],
                retain_graph=True,
                accumulate_grad=False,
            )
        )
        print(f"permute\t\tbwd: pytorch: {t1:.3f} ms,  TE: {t2:.3f} ms")

        t1 = perf_test_cuda_kernel(
            lambda: pytorch_unpermute(pytorch_unpermute_fwd_input, sorted_indices, probs=probs)
        )
        t2 = perf_test_cuda_kernel(
            lambda: te_unpermute(te_unpermute_fwd_input, te_dtype, row_id_map, te_probs)
        )
        print(f"unpermute\tfwd: pytorch: {t1:.3f} ms,  TE: {t2:.3f} ms")

        t1 = perf_test_cuda_kernel(
            lambda: backward_wrapper(
                pytorch_unpermute_output,
                pytorch_unpermute_bwd_input,
                forward_input=(
                    [pytorch_unpermute_fwd_input, probs]
                    if with_probs
                    else [pytorch_unpermute_fwd_input]
                ),
                retain_graph=True,
                accumulate_grad=False,
            )
        )
        t2 = perf_test_cuda_kernel(
            lambda: backward_wrapper(
                te_unpermute_output,
                te_unpermute_bwd_input,
                forward_input=(
                    [te_unpermute_fwd_input, te_probs] if with_probs else [te_unpermute_fwd_input]
                ),
                retain_graph=True,
                accumulate_grad=False,
            )
        )
        print(f"unpermute\tbwd: pytorch: {t1:.3f} ms,  TE: {t2:.3f} ms")


def perf_test_cuda_kernel(cuda_kernel_fn):
    if torch.cuda.is_available():
        # create CUDA event
        start_event = torch.cuda.Event(enable_timing=True)
        end_event = torch.cuda.Event(enable_timing=True)

        # warmup
        for _ in range(50):
            cuda_kernel_fn()

        start_event.record()
        for _ in range(100):
            cuda_kernel_fn()
        end_event.record()
        torch.cuda.synchronize()

        elapsed_time_ms = start_event.elapsed_time(end_event)
        return elapsed_time_ms / 100
    else:
        pytest.skip("CUDA is not available.")


def test_permute_single_case():
    print("GPU:", torch.cuda.get_device_name(0))

    # te_dtype = tex.DType.kFloat32
    # te_dtype = tex.DType.kFloat16
    # te_dtype = tex.DType.kBFloat16
    te_dtype = tex.DType.kFloat8E5M2
    # te_dtype = tex.DType.kFloat8E4M3

    num_tokens = 10
    num_expert = 4
    hidden_size = 16
    topK = 2
    num_out_tokens = num_tokens * topK - 1
    with_probs = True
    Benchmark = True

    test_permutation(
        te_dtype=te_dtype,
        num_tokens=num_tokens,
        num_expert=num_expert,
        hidden_size=hidden_size,
        topK=topK,
        num_out_tokens=num_out_tokens,
        with_probs=with_probs,
        BENCHMARK=Benchmark,
    )


if __name__ == "__main__":
    test_permute_single_case()