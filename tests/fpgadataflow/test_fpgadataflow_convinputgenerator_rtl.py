# Copyright (C) 2022, Advanced Micro Devices, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of FINN nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import pytest

from onnx import TensorProto, helper
from qonnx.core.datatype import DataType
from qonnx.core.modelwrapper import ModelWrapper
from qonnx.custom_op.general.im2col import compute_conv_output_dim
from qonnx.transformation.general import GiveUniqueNodeNames
from qonnx.util.basic import gen_finn_dt_tensor, qonnx_make_model

import finn.core.onnx_exec as oxe
from finn.transformation.fpgadataflow.prepare_ip import PrepareIP
from finn.transformation.fpgadataflow.prepare_rtlsim import PrepareRTLSim
from finn.transformation.fpgadataflow.set_exec_mode import SetExecMode


def make_single_im2col_modelwrapper(k, ifm_ch, ifm_dim, ofm_dim, stride, dilation, idt):
    k_h, k_w = k
    ifm_dim_h, ifm_dim_w = ifm_dim
    stride_h, stride_w = stride
    dilation_h, dilation_w = dilation
    ofm_dim_h, ofm_dim_w = ofm_dim

    odt = idt
    inp = helper.make_tensor_value_info(
        "inp", TensorProto.FLOAT, [1, ifm_dim_h, ifm_dim_w, ifm_ch]
    )
    outp = helper.make_tensor_value_info(
        "outp", TensorProto.FLOAT, [1, ofm_dim_h, ofm_dim_w, k_h * k_w * ifm_ch]
    )

    im2col_node = helper.make_node(
        "Im2Col",
        ["inp"],
        ["outp"],
        domain="finn.custom_op.general",
        stride=[stride_h, stride_w],
        kernel_size=[k_h, k_w],
        input_shape=str((1, ifm_dim_h, ifm_dim_w, ifm_ch)),
        dilations=[dilation_h, dilation_w],
        pad_amount=[0, 0, 0, 0],
        pad_value=0,
    )
    graph = helper.make_graph(
        nodes=[im2col_node], name="im2col_graph", inputs=[inp], outputs=[outp]
    )

    model = qonnx_make_model(graph, producer_name="im2col-model")
    model = ModelWrapper(model)

    model.set_tensor_datatype("inp", idt)
    model.set_tensor_datatype("outp", odt)

    return model


def make_single_slidingwindow_modelwrapper(
    k, ifm_ch, ifm_dim, ofm_dim, simd, m, parallel_window, stride, dilation, idt, dw=0
):
    k_h, k_w = k
    ifm_dim_h, ifm_dim_w = ifm_dim
    stride_h, stride_w = stride
    dilation_h, dilation_w = dilation
    ofm_dim_h, ofm_dim_w = ofm_dim

    odt = idt
    inp = helper.make_tensor_value_info(
        "inp", TensorProto.FLOAT, [1, ifm_dim_h, ifm_dim_w, ifm_ch]
    )
    outp = helper.make_tensor_value_info(
        "outp", TensorProto.FLOAT, [1, ofm_dim_h, ofm_dim_w, k_h * k_w * ifm_ch]
    )

    SlidingWindow_node = helper.make_node(
        "ConvolutionInputGenerator_rtl",
        ["inp"],
        ["outp"],
        domain="finn.custom_op.fpgadataflow",
        backend="fpgadataflow",
        ConvKernelDim=[k_h, k_w],
        IFMChannels=ifm_ch,
        IFMDim=[ifm_dim_h, ifm_dim_w],
        OFMDim=[ofm_dim_h, ofm_dim_w],
        SIMD=simd,
        M=m,
        parallel_window=parallel_window,
        Stride=[stride_h, stride_w],
        Dilation=[dilation_h, dilation_w],
        inputDataType=idt.name,
        outputDataType=odt.name,
        depthwise=dw,
    )
    graph = helper.make_graph(
        nodes=[SlidingWindow_node],
        name="slidingwindow_graph",
        inputs=[inp],
        outputs=[outp],
    )

    model = qonnx_make_model(graph, producer_name="slidingwindow-model")
    model = ModelWrapper(model)

    model.set_tensor_datatype("inp", idt)
    model.set_tensor_datatype("outp", odt)

    return model


def prepare_inputs(input_tensor):
    return {"inp": input_tensor}


# input datatype
@pytest.mark.parametrize("idt", [DataType["UINT4"]])
# kernel size
@pytest.mark.parametrize("k", [[3, 3], [1, 5]])
# input dimension
@pytest.mark.parametrize("ifm_dim", [[13, 13], [1, 21]])
# input channels
@pytest.mark.parametrize("ifm_ch", [6])
# Stride
@pytest.mark.parametrize("stride", [[1, 1], [2, 2]])
# Dilation
@pytest.mark.parametrize("dilation", [[1, 1], [2, 2]])
# depthwise
@pytest.mark.parametrize("dw", [0, 1])
# input channel parallelism ("SIMD")
@pytest.mark.parametrize("simd", [1, 3, 6])
# parallel_window enable (MMV_out = M*K)
@pytest.mark.parametrize("parallel_window", [0, 1])
# in/out MMV ("M")
@pytest.mark.parametrize("m", [1])
# Flip dimensions
@pytest.mark.parametrize("flip", [False])
@pytest.mark.slow
@pytest.mark.vivado
@pytest.mark.fpgadataflow
def test_fpgadataflow_slidingwindow_rtl(
    idt, k, ifm_dim, ifm_ch, stride, dilation, dw, simd, m, parallel_window, flip
):
    if flip:
        if (
            ifm_dim[0] == ifm_dim[1]
            and k[0] == k[1]
            and stride[0] == stride[1]
            and dilation[0] == dilation[1]
        ):
            pytest.skip("Dimension flip would have no effect")
        k = k[::-1]
        ifm_dim = ifm_dim[::-1]
        stride = stride[::-1]
        dilation = dilation[::-1]

    k_h, k_w = k
    ifm_dim_h, ifm_dim_w = ifm_dim
    stride_h, stride_w = stride
    dilation_h, dilation_w = dilation

    kernel_width = (k_w - 1) * dilation_w + 1  # incl. dilation
    kernel_height = (k_h - 1) * dilation_h + 1  # incl. dilation

    if simd > ifm_ch:
        pytest.skip("SIMD cannot be larger than number of input channels")
    if ifm_ch % simd != 0:
        pytest.skip("SIMD must divide number of input channels")
    if kernel_height > ifm_dim_h or stride_h > ifm_dim_h:
        pytest.skip(
            "Illegal convolution configuration: kernel or stride > FM dimension"
        )
    if kernel_width > ifm_dim_w or stride_w > ifm_dim_w:
        pytest.skip(
            "Illegal convolution configuration: kernel or stride > FM dimension"
        )
    if (k_h == 1 and (stride_h != 1 or dilation_h != 1)) or (
        k_w == 1 and (stride_w != 1 or dilation_w != 1)
    ):
        pytest.skip(
            """Illegal convolution configuration:
            stride or dilation defined for unitary kernel dim"""
        )
    if k_h == 1 and k_w == 1 and simd != ifm_ch:
        pytest.skip("1x1 Kernel only supported in parallel mode (SIMD=C)")
    if parallel_window and simd != ifm_ch:
        pytest.skip("Parallel window requires SIMD=C")

    ofm_dim_h = compute_conv_output_dim(ifm_dim_h, k_h, stride_h, 0, dilation_h)
    ofm_dim_w = compute_conv_output_dim(ifm_dim_w, k_w, stride_w, 0, dilation_w)
    ofm_dim = [ofm_dim_h, ofm_dim_w]

    x = gen_finn_dt_tensor(idt, (1, ifm_dim_h, ifm_dim_w, ifm_ch))
    model = make_single_slidingwindow_modelwrapper(
        k=k,
        ifm_ch=ifm_ch,
        ifm_dim=ifm_dim,
        ofm_dim=ofm_dim,
        simd=simd,
        m=m,
        parallel_window=parallel_window,
        stride=stride,
        dilation=dilation,
        idt=idt,
        dw=dw,
    )

    model = model.transform(SetExecMode("rtlsim"))
    model = model.transform(GiveUniqueNodeNames())
    model = model.transform(PrepareIP("xc7z020clg400-1", 5))
    model = model.transform(PrepareRTLSim())

    # prepare input data
    input_dict = prepare_inputs(x)
    # execute model
    y_produced = oxe.execute_onnx(model, input_dict)["outp"]
    golden = make_single_im2col_modelwrapper(
        k=k,
        ifm_ch=ifm_ch,
        ifm_dim=ifm_dim,
        ofm_dim=ofm_dim,
        stride=stride,
        dilation=dilation,
        idt=idt,
    )
    y_expected = oxe.execute_onnx(golden, input_dict)["outp"]

    if dw == 0:
        assert (y_produced == y_expected).all()
    else:
        y_expected = y_expected.reshape(
            1, ofm_dim_h, ofm_dim_w, k_h * k_w, ifm_ch // simd, simd
        )
        y_expected = y_expected.transpose(0, 1, 2, 4, 3, 5)
        y_expected = y_expected.reshape(1, ofm_dim_h, ofm_dim_w, ifm_ch * k_h * k_w)
        assert (y_produced == y_expected).all()
