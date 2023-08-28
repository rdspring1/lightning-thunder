from typing import Any

import pytest
import torch
from torch.testing import assert_close

import thunder
from thunder import dtypes
from thunder.tests.framework import instantiate, requiresCUDA, ops, run_snippet, NOTHING, TorchExecutor
from thunder.tests.opinfos import OpInfo, get_opinfo
import thunder.core.devices as devices

from lightning_utilities.core.imports import package_available

CUDNN_AVAILABLE = package_available("cudnn")

cudnn: None | Any = None
if CUDNN_AVAILABLE:
    import cudnn
    from thunder.executors.cudnnex import deregister_cudnnex, register_cudnnex


# WARNING: cudnn executor is experimental. Tests that use cudnn might fail.\n
# Issue for tracking support: https://github.com/Lightning-AI/lightning-thunder/issues/880
# NOTE This test modifies the global executor map, so it technically should not
# be run in parallel with other tests
@requiresCUDA
def test_cudnn():
    if not CUDNN_AVAILABLE:
        pytest.skip("cudnn is not available")

    try:
        register_cudnnex()

        for dtype in (thunder.float16, thunder.bfloat16):
            b, h, s_q, s_kv, d_q, d_v = 8, 8, 256, 256, 64, 64
            shape_Q = (b, h, s_q, d_q)
            shape_K = (b, h, s_kv, d_q)
            shape_V = (b, h, s_q, d_v)

            query = 1 * (torch.randn(shape_Q, dtype=thunder.torch.to_torch_dtype(dtype), device="cuda") - 0.5)
            key = 2 * (torch.randn(shape_K, dtype=thunder.torch.to_torch_dtype(dtype), device="cuda") - 0.5)
            value = 3 * (torch.randn(shape_V, dtype=thunder.torch.to_torch_dtype(dtype), device="cuda") - 0.5)
            is_causal = False
            attn_mask = torch.randn(
                s_q, s_kv, requires_grad=False, device="cuda", dtype=thunder.torch.to_torch_dtype(dtype)
            )

            expected = torch.nn.functional.scaled_dot_product_attention(
                query, key, value, is_causal=is_causal, attn_mask=attn_mask
            )

            def test(query, key, value, is_causal=False, attn_mask=None):
                return thunder.torch.scaled_dot_product_attention(
                    query, key, value, is_causal=is_causal, attn_mask=attn_mask
                )

            ctest = thunder.compile(test, executors_list=["cudnn"])
            actual = ctest(query, key, value, is_causal=is_causal, attn_mask=attn_mask)
            torch.testing.assert_close(actual, expected, atol=1e-2, rtol=1e-2)
            last_trace = thunder.last_traces(ctest)[-1]
            assert any(bsym.sym.name == "cudnn_sdpa" for bsym in last_trace.bound_symbols)
    finally:
        deregister_cudnnex()


def snippet_torch_consistency(op, torch_op, sample):
    thunder_result = op(*sample.args, **sample.kwargs)
    torch_result = torch_op(*sample.args, **sample.kwargs)
    assert_close(thunder_result, torch_result, equal_nan=True, atol=1e-2, rtol=1e-2)

    last_trace = thunder.last_traces(op)[-1]
    assert any(bsym.sym.name == "cudnn_sdpa" for bsym in last_trace.bound_symbols)


# WARNING: cudnn executor is experimental. Tests that use cudnn might fail.\n
# Issue for tracking support: https://github.com/Lightning-AI/lightning-thunder/issues/880
# TODO Make it easier for executors to write tests like this, including writing them out-of-tree
# TODO The executor passed below is just a "dummy" that actually gets ignored -- we should provide
#   a way to use decorators like @ops without a particular executor
@ops(
    (get_opinfo("scaled_dot_product_attention"),),
    supported_devicetypes=(devices.DeviceType.CUDA,),
    supported_dtypes=(dtypes.float16, dtypes.bfloat16),
    supported_executors=(TorchExecutor,),
)
def test_cudnn_sdpa_vs_torch_consistency(op, device, dtype, *_):
    from thunder.executors.cudnnex import sdpa_checker

    if not CUDNN_AVAILABLE:
        pytest.skip("cudnn is not available")

    try:
        register_cudnnex()

        def fn(*args, **kwargs):
            return thunder.torch.scaled_dot_product_attention(*args, **kwargs)

        cfn = thunder.compile(fn, executors_list=["cudnn"])
        at_least_one_supported_input = False
        for sample in op.reference_inputs(device, dtype, requires_grad=False):
            if not sdpa_checker(*sample.args, **sample.kwargs):
                continue
            at_least_one_supported_input = True
            result = run_snippet(
                snippet_torch_consistency,
                op,
                device,
                dtype,
                cfn,
                op.torch_reference,
                sample,
            )
            if result is not None:
                return result
        if not at_least_one_supported_input:
            raise ValueError("No supported inputs were generated by the OpInfo")
    finally:
        deregister_cudnnex()
