# Copyright (C) 2024, Advanced Micro Devices, Inc.
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
import torch
from onnx import helper
import finn.core.onnx_exec as oxe
from brevitas.export import export_qonnx
from qonnx.util.cleanup import cleanup as qonnx_cleanup
from onnx import TensorProto, helper
from qonnx.core.datatype import DataType
from qonnx.core.modelwrapper import ModelWrapper
from qonnx.custom_op.registry import getCustomOp
from qonnx.transformation.infer_shapes import InferShapes
from qonnx.util.basic import gen_finn_dt_tensor, qonnx_make_model
from qonnx.transformation.infer_datatypes import InferDataTypes
import finn.transformation.fpgadataflow.convert_to_hw_layers as to_hw
from finn.transformation.fpgadataflow.compile_cppsim import CompileCppSim
from finn.transformation.fpgadataflow.hlssynth_ip import HLSSynthIP
from finn.transformation.fpgadataflow.prepare_cppsim import PrepareCppSim
from finn.transformation.fpgadataflow.prepare_ip import PrepareIP
from finn.transformation.fpgadataflow.prepare_rtlsim import PrepareRTLSim
from finn.transformation.fpgadataflow.set_exec_mode import SetExecMode
from finn.transformation.fpgadataflow.specialize_layers import SpecializeLayers
from finn.transformation.qonnx.convert_qonnx_to_finn import ConvertQONNXtoFINN
from finn.transformation.fpgadataflow.create_stitched_ip import CreateStitchedIP
from finn.transformation.fpgadataflow.create_dataflow_partition import (
    CreateDataflowPartition,
)
from qonnx.transformation.general import (
    ApplyConfig,
    GiveUniqueNodeNames,
)
import finn.transformation.streamline.absorb as absorb
from onnx import helper
import torch
import torch.nn as nn
import brevitas.nn as qnn
import numpy as np
test_fpga_part = "xczu3eg-sbva484-1-e"
target_clk_ns = 5
export_onnx_path = "pytest_quantsoftmax_dut.onnx"

class QuantSoftMaxSimple(nn.Module):
    def __init__(self, bit_width=8, signed=True):
        super(QuantSoftMaxSimple, self).__init__()
        self.output_identity = qnn.QuantIdentity(bit_width=bit_width, scaling_per_tensor=True, bias=False, signed = signed)
        self.softmax = nn.Softmax(dim=-1) # softmax along the last dimension

    def get_quant_scale(self):
        return self.output_identity.quant_act_scale()

    def forward(self, x):
        x = self.softmax(x)
        x = self.output_identity(x)
        return x

def create_model(io_shape=(1, 12, 128, 128), idt=DataType["INT8"], odt=DataType["INT8"]):
    '''
    Create a quantized softmax model.
    Input and output are quantized to Int8ActPerTensorFloat, this is to make sure
    that the softmax layer is followed by a Quant node.
    '''
    dut = QuantSoftMaxSimple(odt.bitwidth(), odt.signed())
    input = torch.rand(io_shape)
    export_qonnx(dut, input, export_onnx_path, opset_version=11)
    qonnx_cleanup(export_onnx_path, out_file=export_onnx_path)
    # set the model input to UINT8
    model = ModelWrapper(export_onnx_path)
    model.set_tensor_datatype(model.graph.input[0].name, idt)
    return model, dut.get_quant_scale()

def make_single_quantsoftmax_modelwrapper(impl_style="hls", simd=1, idt=DataType["UINT8"], odt=DataType["UINT8"], ifm_dim=(128, 128)):
    '''
    Create a single quantized softmax node with variable parameters.
    this is before SpecializeLayers() transformation.
    '''
    inp = helper.make_tensor_value_info("global_in", TensorProto.FLOAT, list(ifm_dim))
    outp = helper.make_tensor_value_info("global_out", TensorProto.FLOAT, list(ifm_dim))
    new_node = helper.make_node(
        "QuantSoftmax",
        ["global_in"],
        ["global_out"],
        domain="finn.custom_op.fpgadataflow",
        backend="fpgadataflow",
        ifm_dim=list(ifm_dim),
        input_data_type = idt.name,
        output_data_type = odt.name,
        simd=simd,
        preferred_impl_style=impl_style,
    )
    graph = helper.make_graph(
        [new_node],
        "softmax_graph",
        inputs=[inp],
        outputs=[outp]
    )
    model = qonnx_make_model(graph)
    model = ModelWrapper(model)

    model.set_tensor_datatype("global_in", idt)
    model.set_tensor_datatype("global_out", idt)

    return model

@pytest.mark.parametrize("exec_mode", ["cppsim", "rtlsim", "stitched_ip"])
@pytest.mark.parametrize("simd", ["simd1", "simd2", "simd3", "simd4"])
@pytest.mark.fpgadataflow
def test_convert_to_hw_softmax_layer(exec_mode, simd):
    '''
    This test checks that the softmax layer can be converted to a HW layer.
    '''
    if (exec_mode == "stitched_ip" or exec_mode == "rtlsim") and simd != "simd1":
        pytest.skip("Skipping this test to avoid long test times")
    # Create the qonnx model
    io_shape = (1, 12, 128, 128)
    # input = torch.randn(io_shape)
    input = gen_finn_dt_tensor(DataType["UINT8"], io_shape)
    input_t = {"global_in": input}

    model, _ = create_model(io_shape)

    simd = int(simd[-1])
    folding_config = {
        "Defaults": {},
        "QuantSoftmax_0": {
            "simd": simd,
            "preferred_impl_style": "hls"
        }
    }
    try:
        model = model.transform(ConvertQONNXtoFINN())
        model = model.transform(InferShapes())
        model = model.transform(InferDataTypes())
        model = model.transform(to_hw.InferQuantSoftmax())
        model = model.transform(GiveUniqueNodeNames())
        # isolate fpga dataflow layers
        parent_model = model.transform(CreateDataflowPartition())
        sdp_node = parent_model.get_nodes_by_op_type("StreamingDataflowPartition")[0]
        sdp_node_path = getCustomOp(sdp_node).get_nodeattr("model")
        model = ModelWrapper(sdp_node_path)
        model = model.transform(ApplyConfig(folding_config))
        model = model.transform(SpecializeLayers(test_fpga_part))
        model = model.transform(GiveUniqueNodeNames())
        if exec_mode == "cppsim":
            model = model.transform(SetExecMode("cppsim"))
            model = model.transform(PrepareCppSim())
            model = model.transform(CompileCppSim())
        elif exec_mode == "rtlsim":
            model = model.transform(SetExecMode("rtlsim"))
            model = model.transform(PrepareIP(test_fpga_part, target_clk_ns))
            model = model.transform(HLSSynthIP())
            try:
                model = model.transform(PrepareRTLSim())
                pytest.fail("PrepareRTLSim should have failed")
            except Exception as e:
                # expected to fail because this node do not support rtlsim
                pass
        elif exec_mode == "stitched_ip":
            model = model.transform(PrepareIP(test_fpga_part, target_clk_ns))
            model = model.transform(HLSSynthIP())
            model = model.transform(CreateStitchedIP(test_fpga_part, target_clk_ns))
    except Exception as e:
        pytest.fail(f"Failed to transform the model: {str(e)}")


@pytest.mark.parametrize("impl_style", ["hls"])
@pytest.mark.parametrize("simd", ["simd1", "simd2", "simd3", "simd4"])
@pytest.mark.parametrize("idt", ["INT8", "INT9", "INT16"])
@pytest.mark.parametrize("odt", ["INT8", "INT16"])
@pytest.mark.parametrize("ifm_dim", [(1, 128, 384), (1, 12, 12, 128)])
@pytest.mark.fpgadataflow
def test_fpga_dataflow_quantsoftmax(impl_style, simd, idt, odt, ifm_dim):
    idt = DataType[idt]
    odt = DataType[odt]
    simd = int(simd[-1])
    io_shape = ifm_dim
    tollerance = 2
    model = make_single_quantsoftmax_modelwrapper(impl_style=impl_style, simd=simd, idt=idt, odt=odt, ifm_dim=ifm_dim)

    if(ifm_dim[-1] % simd != 0):
        pytest.skip(f"Skipping this test because the inner dimension is not a multiple of {simd}")

    input = gen_finn_dt_tensor(idt, io_shape)
    in_name = model.graph.input[0].name
    out_name = model.graph.output[0].name
    input_t = {in_name: input}

    # Create reference values using the qonnx model
    ref_model, scale = create_model(io_shape, odt)
    y_ref = oxe.execute_onnx(ref_model, input_t)[out_name]
    y_ref = y_ref / scale
    y_ref = y_ref.numpy()

    y_out = oxe.execute_onnx(model, input_t)[out_name]
    assert np.allclose(y_ref, y_out, atol=tollerance), "Model output does not match expected output"

    try:
        model = model.transform(SpecializeLayers(test_fpga_part))
        model = model.transform(GiveUniqueNodeNames())
        model = model.transform(SetExecMode("rtlsim"))
        model = model.transform(PrepareCppSim())
        model = model.transform(CompileCppSim())
    except Exception as e:
        pytest.fail(f"Failed to transform the model: {str(e)}")

    # run the model
    y_hw = oxe.execute_onnx(model, input_t)[out_name]

    y_hw_flat = y_hw.flatten()
    y_ref_flat = y_ref.flatten()
    for i in range(len(y_hw_flat)):
        if np.allclose(y_hw_flat[i], y_ref_flat[i], atol=tollerance) == False:
            print(f"Index: {i}, Expected: {y_ref_flat[i]}, Got: {y_hw_flat[i]}")

    assert np.allclose(y_ref, y_hw, atol=tollerance), "Model output does not match expected output"
