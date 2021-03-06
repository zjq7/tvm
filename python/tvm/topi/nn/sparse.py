# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Sparse operators"""
from __future__ import absolute_import
import tvm
from tvm import te

from ..utils import get_const_tuple


def sparse_dense_sp_rhs(data, weight_data, weight_indices, weight_indptr):
    """
    Computes sparse-dense matrix multiplication of `data` and
    `(weight_data, weight_indices, weight_indptr).T`

    Parameters
    ----------
    data : tvm.te.Tensor
        2-D with shape [M, K], float32

    weight_data : tvm.te.Tensor
        1-D with shape [nnz] (CSR) or
        3-D with shape [num_blocks, bs_r, bs_c] (BSR)

    weight_indices : tvm.te.Tensor
        1-D with shape [nnz] (CSR) or
        1-D with shape [num_blocks] (BSR)

    weight_indptr : tvm.te.Tensor
        1-D with shape [N + 1] (CSR) or
        1-D with shape [(N + 1) // bs_r] (BSR)

    Returns
    -------
    output : tvm.te.Tensor
        2-D with shape [M, N]
    """
    assert len(weight_data.shape) in (1, 3)
    if len(weight_data.shape) == 1:
        func = _sparse_dense_sp_rhs_csrmm
    if len(weight_data.shape) == 3:
        func = _sparse_dense_sp_rhs_bsrmm
    return func(data, weight_data, weight_indices, weight_indptr)


def sparse_dense_sp_lhs(data_data, data_indices, data_indptr, weight):
    """
    Computes sparse-dense matrix multiplication of
    `(data_data, data_indices, data_indptr)` and `weight.T`

    Parameters
    ----------
    data_data:
        1-D with shape [nnz] (CSR) or
        3-D with shape [num_blocks, bs_r, bs_c] (BSR)

    data_indices:
        1-D with shape [nnz] (CSR) or
        1-D with shape [num_blocks] (BSR)

    data_indptr:
        1-D with shape [M + 1] (CSR) or
        1-D with shape [(M + 1) // bs_r] (BSR)

    weight:
        2-D with shape [N, K], float32

    Returns
    -------
    output : tvm.te.Tensor
        2-D with shape [M, N]
    """
    assert len(data_data.shape) in (1, 3)
    if len(data_data.shape) == 1:
        func = _sparse_dense_sp_lhs_csrmm
    if len(data_data.shape) == 3:
        func = _sparse_dense_sp_lhs_bsrmm
    return func(data_data, data_indices, data_indptr, weight)


# pylint: disable=no-else-return,inconsistent-return-statements
def sparse_dense(dense_data, sparse_data, sparse_indices, sparse_indptr, sparse_lhs=False):
    """
    Computes sparse-dense matrix multiplication of `data` and
    `(weight_data, weight_indices, weight_indptr).T`, if sparse_lhs=False
    or
    Computes sparse-dense matrix multiplication of
    `(data_data, data_indices, data_indptr)` and `weight.T`, if sparse_lhs=True

    Parameters
    ----------
    dense_data : tvm.te.Tensor
        2-D with shape [M, K], float32

    sparse_data : tvm.te.Tensor
        1-D with shape [nnz] (CSR) or
        3-D with shape [num_blocks, bs_r, bs_c] (BSR)

    sparse_indices : tvm.te.Tensor
        1-D with shape [nnz] (CSR) or
        1-D with shape [num_blocks] (BSR)

    sparse_indptr : tvm.te.Tensor
        1-D with shape [N + 1] (CSR) or
        1-D with shape [(N + 1) // bs_r] (BSR)

    sparse_lhs : bool, optional
        Indicates whether lhs or rhs matrix is sparse. Default value is False.

    Returns
    -------
    output : tvm.te.Tensor
        2-D with shape [M, N]
    """
    if sparse_lhs:
        return sparse_dense_sp_lhs(sparse_data, sparse_indices, sparse_indptr, dense_data)
    else:
        return sparse_dense_sp_rhs(dense_data, sparse_data, sparse_indices, sparse_indptr)


def _sparse_dense_sp_lhs_csrmm(data_data, data_indices, data_indptr, weight):
    oshape = (get_const_tuple(data_indptr.shape)[0] - 1, get_const_tuple(weight.shape)[0])

    def f(row, i):
        row_start = data_indptr[row]
        row_end = data_indptr[row + 1]
        row_elems = row_end - row_start
        elem_idx = te.reduce_axis((0, row_elems), name="elem_idx")
        elem = row_start + elem_idx
        a_val = data_data[elem]
        weight_val = weight[i, data_indices[elem]]
        return te.sum(a_val * weight_val, axis=elem_idx)

    return te.compute(oshape, f, tag="sparse_dense_sp_lhs_csrmm")


def _sparse_dense_sp_rhs_csrmm(data, weight_data, weight_indices, weight_indptr):
    oshape = (get_const_tuple(data.shape)[0], get_const_tuple(weight_indptr.shape)[0] - 1)

    def f(i, row):
        row_start = weight_indptr[row]
        row_end = weight_indptr[row + 1]
        row_elems = row_end - row_start
        elem_idx = te.reduce_axis((0, row_elems), name="elem_idx")
        elem = row_start + elem_idx
        a_val = weight_data[elem]
        weight_val = data[i, weight_indices[elem]]
        return te.sum(a_val * weight_val, axis=elem_idx)

    return te.compute(oshape, f, tag="sparse_dense_sp_rhs_csrmm")


def _sparse_dense_sp_lhs_bsrmm(data_data, data_indices, data_indptr, weight):
    (m, _) = get_const_tuple(weight.shape)
    (_, bs_r, bs_c) = get_const_tuple(data_data.shape)
    (num_blocks_plus_1,) = get_const_tuple(data_indptr.shape)
    num_blocks = num_blocks_plus_1 - 1

    def _compute_block(nb_j, j, i):
        row_start = data_indptr[nb_j]
        row_end = data_indptr[nb_j + 1]
        row_elems = row_end - row_start
        elem_idx = te.reduce_axis((0, row_elems), name="elem_idx")
        block_offset = row_start + elem_idx
        c = te.reduce_axis((0, bs_c), name="c")
        block_j = data_indices[block_offset]
        block_ij_val = data_data[block_offset][j][c]
        x_val = weight[i, bs_c * block_j + c]
        return te.sum(block_ij_val * x_val, axis=[elem_idx, c])

    idxd = tvm.tir.indexdiv
    idxm = tvm.tir.indexmod

    bsrmm_block = te.compute(
        (num_blocks, bs_r, m), _compute_block, tag="sparse_dense_sp_lhs_bsrmm_block"
    )
    return te.compute(
        (num_blocks * bs_r, m),
        lambda m, n: bsrmm_block[idxd(m, bs_r), idxm(m, bs_r), n],
        tag="sparse_dense_sp_lhs_bsrmm",
    )


def _sparse_dense_sp_rhs_bsrmm(data, weight_data, weight_indices, weight_indptr):
    (m, _) = get_const_tuple(data.shape)
    (_, bs_r, bs_c) = get_const_tuple(weight_data.shape)
    (num_blocks_plus_1,) = get_const_tuple(weight_indptr.shape)
    num_blocks = num_blocks_plus_1 - 1

    def _compute_block(i, nb_j, j):
        row_start = weight_indptr[nb_j]
        row_end = weight_indptr[nb_j + 1]
        row_elems = row_end - row_start
        elem_idx = te.reduce_axis((0, row_elems), name="elem_idx")
        block_offset = row_start + elem_idx
        c = te.reduce_axis((0, bs_c), name="c")
        block_j = weight_indices[block_offset]
        block_ij_val = weight_data[block_offset][j][c]
        x_val = data[i, bs_c * block_j + c]
        return te.sum(block_ij_val * x_val, axis=[elem_idx, c])

    idxd = tvm.tir.indexdiv
    idxm = tvm.tir.indexmod

    bsrmm_block = te.compute(
        (m, num_blocks, bs_r), _compute_block, tag="sparse_dense_sp_rhs_bsrmm_block"
    )
    return te.compute(
        (m, num_blocks * bs_r),
        lambda m, n: bsrmm_block[m, idxd(n, bs_r), idxm(n, bs_r)],
        tag="sparse_dense_sp_rhs_bsrmm",
    )


def sparse_transpose(sparse_data, sparse_indices, sparse_indptr):
    """
    Transpose a square sparse matrix,
    `A` is an n-by-n sparse matrix in the CSR format.
    ** Currently only support Square Matrices **

    Parameters
    ----------
    sparse_data : tvm.te.Tensor
        1-D with shape [nonzeros], dtype of 'float32'

    sparse_indices : tvm.te.Tensor
        1-D with shape [nonzeros], dtype of 'int32'

    sparse_indptr : tvm.te.Tensor
        1-D with shape [n+1], dtype of 'int32'

    Returns
    -------
    out_data : tvm.te.Tensor
        1-D with shape [nonzeros], dtype of 'float32'

    out_indices : tvm.te.Tensor
        1-D with shape [nonzeros], dtype of 'int32'

    out_indptr : tvm.te.Tensor
        1-D with shape [n+1], dtype of 'int32'
    """
    assert len(sparse_data.shape) == 1, "error in data dimension"
    assert len(sparse_indices.shape) == 1, "error in indices dimension"
    assert len(sparse_indptr.shape) == 1, "error in indptr dimension"

    nnz = get_const_tuple(sparse_data.shape)[0]
    n = get_const_tuple(sparse_indptr.shape)[0] - 1
    output_shape = [(nnz,), (nnz,), (n + 1,)]

    # TODO: Add BSR transpose support

    output_data, output_indices, output_indptr = te.extern(
        shape=output_shape,
        inputs=[sparse_data, sparse_indices, sparse_indptr],
        fcompute=lambda ins, outs: _csr_transpose_ir(
            ins[0], ins[1], ins[2], outs[0], outs[1], outs[2]
        ),
        tag="sparse_transpose_csr",
        dtype=["float32", "int32", "int32"],
        name="out",
    )

    return [output_data, output_indices, output_indptr]


def _csr_transpose_ir(data, indices, indptr, out_data, out_indices, out_indptr):
    """define ir for csr_transpose"""
    irb = tvm.tir.ir_builder.create()

    data_ptr = irb.buffer_ptr(data)
    indices_ptr = irb.buffer_ptr(indices)
    indptr_ptr = irb.buffer_ptr(indptr)

    out_data_ptr = irb.buffer_ptr(out_data)
    out_indices_ptr = irb.buffer_ptr(out_indices)
    out_indptr_ptr = irb.buffer_ptr(out_indptr)

    n = get_const_tuple(indptr.shape)[0] - 1
    nnz = get_const_tuple(data.shape)[0]

    with irb.for_range(0, n, kind="parallel", name="col") as col:
        out_indptr_ptr[col] = 0

    with irb.for_range(0, nnz, kind="serial", name="nz_idx") as nz_idx:
        out_indptr_ptr[indices_ptr[nz_idx]] += 1

    cumsum = irb.allocate("int32", (1,), name="cumsum", scope="local")
    temp = irb.allocate("int32", (1,), name="temp", scope="local")
    cumsum[0] = 0
    with irb.for_range(0, n, kind="serial", name="col") as col:
        temp[0] = out_indptr_ptr[col]
        out_indptr_ptr[col] = cumsum[0]
        cumsum[0] += temp[0]

    out_indptr_ptr[n] = nnz

    with irb.for_range(0, n, kind="serial", name="row") as row:
        offset = indptr_ptr[row]
        diff = indptr_ptr[row + 1] - indptr_ptr[row]
        with irb.for_range(0, diff, kind="serial", name="idx") as idx:
            real_idx = offset + idx
            col = indices_ptr[real_idx]
            dest = out_indptr_ptr[col]

            out_indices_ptr[dest] = row
            out_data_ptr[dest] = data_ptr[real_idx]
            out_indptr_ptr[col] += 1

    last = irb.allocate("int32", (1,), name="last", scope="local")
    temp2 = irb.allocate("int32", (1,), name="temp2", scope="local")
    last[0] = 0
    with irb.for_range(0, n, kind="serial", name="col") as col:
        temp2[0] = out_indptr_ptr[col]
        out_indptr_ptr[col] = last[0]
        last[0] = temp2[0]

    return irb.get()


@tvm.target.generic_func
def sparse_dense_alter_layout(_attrs, _inputs, _tinfos, _out_type):
    """Change Sparse Dense layout.

    This is used for modifying the inputs weights so they are more amenable for
    the target.

    Parameters
    ----------
    attrs : tvm.ir.Attrs
        Attributes of current convolution
    inputs : tvm.relay.Expr
        Grouped input symbols
    tinfos : list
        Input shape and dtype
    out_type: type
        The output type

    Note
    ----
    Unlike other TOPI functions, this function operates on both graph level and operator level.
    """
    return None
