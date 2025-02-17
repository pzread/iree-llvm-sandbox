# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

from dataclasses import dataclass
from xdsl.ir import Operation, MLContext, Region, Block
from typing import List, Type, Optional
from xdsl.dialects.builtin import ArrayAttr, StringAttr, ModuleOp, IntegerAttr

from xdsl.pattern_rewriter import RewritePattern, GreedyRewritePatternApplier, PatternRewriteWalker, PatternRewriter, op_type_rewrite_pattern

import dialects.rel_alg as RelAlg
import dialects.rel_ssa as RelSSA

# This file contains the rewrite infrastructure to translate the relational
# algebra dialect to the relational SSA dialect. The current design has a parent
# class `RelAlgRewriter` that contains functions used for several `Rewriter`s.
# All other `Rewriter`s inherit from that class. To introduce SSA, the passes
# insert the children (and the their whole block), that should become operands,
# before the matched op, then use the `added_operations_before` functionality of
# the rewriter to link the operands to the new operation.


@dataclass
class RelAlgRewriter(RewritePattern):

  def convert_datatype(self, type_: RelAlg.DataType) -> RelSSA.DataType:
    if isinstance(type_, RelAlg.String):
      return RelSSA.String.get(type_.nullable)
    if isinstance(type_, RelAlg.Int32):
      return RelSSA.Int32()
    raise Exception(
        f"datatype conversion not yet implemented for {type(type_)}")

  def lookup_type_in_schema(self, name: str,
                            bag: RelSSA.Bag) -> Optional[RelSSA.DataType]:
    """
    Looks up the type of name in the schema of bag.
    """
    for s in bag.schema.data:
      if s.elt_name.data == name:
        return s.elt_type
    return None

  def find_type_in_parent_operator(self, name: str, op: Operation):
    """
    Crawls through all parent_ops until reaching either a ModuleOp, in which
    case the lookup failed or reaching an operator with an input bag, that the
    type can be looked up in.
    """
    parent_op = op.parent_op()
    if isinstance(parent_op, ModuleOp):
      raise Exception(f"element not found in parent schema: {name}")
    if isinstance(parent_op, RelSSA.Operator):
      type_ = self.lookup_type_in_schema(name, parent_op.results[0].typ)
      if type_:
        return type_
      raise Exception(f"element not found in parent schema: {name}")
    return self.find_type_in_parent_operator(name, parent_op)


#===------------------------------------------------------------------------===#
# Expressions
#===------------------------------------------------------------------------===#
"""
All expression rewriters implicitly assume that the last operation in a block is
the one to be yielded.
"""


@dataclass
class LiteralRewriter(RelAlgRewriter):

  @op_type_rewrite_pattern
  def match_and_rewrite(self, op: RelAlg.Literal, rewriter: PatternRewriter):
    new_op = RelSSA.Literal.get(op.val, self.convert_datatype(op.type))
    rewriter.replace_matched_op([new_op, RelSSA.Yield.get([new_op])])


@dataclass
class ColumnRewriter(RelAlgRewriter):

  @op_type_rewrite_pattern
  def match_and_rewrite(self, op: RelAlg.Column, rewriter: PatternRewriter):
    res_type = self.find_type_in_parent_operator(op.col_name.data, op)
    new_op = RelSSA.Column.get(op.col_name.data, res_type)
    rewriter.replace_matched_op([new_op, RelSSA.Yield.get([new_op])])


@dataclass
class CompareRewriter(RelAlgRewriter):

  @op_type_rewrite_pattern
  def match_and_rewrite(self, op: RelAlg.Compare, rewriter: PatternRewriter):
    # Remove the yield and inline the rest of the block.
    rewriter.erase_op(op.left.blocks[0].ops[-1])
    rewriter.inline_block_before_matched_op(op.left.blocks[0])
    left = rewriter.added_operations_before[-1]

    rewriter.erase_op(op.right.blocks[0].ops[-1])
    rewriter.inline_block_before_matched_op(op.right.blocks[0])
    right = rewriter.added_operations_before[-1]

    new_op = RelSSA.Compare.get(left, right, op.comparator)
    rewriter.replace_matched_op([new_op, RelSSA.Yield.get([new_op])])


#===------------------------------------------------------------------------===#
# Operators
#===------------------------------------------------------------------------===#


@dataclass
class SelectRewriter(RelAlgRewriter):

  @op_type_rewrite_pattern
  def match_and_rewrite(self, op: RelAlg.Select, rewriter: PatternRewriter):
    rewriter.inline_block_before_matched_op(op.input)
    predicates = rewriter.move_region_contents_to_new_regions(op.predicates)
    rewriter.insert_op_before_matched_op(
        RelSSA.Select.get(rewriter.added_operations_before[0], predicates))
    rewriter.erase_matched_op()


@dataclass
class PandasTableRewriter(RelAlgRewriter):

  @op_type_rewrite_pattern
  def match_and_rewrite(self, op: RelAlg.PandasTable,
                        rewriter: PatternRewriter):
    schema_names = [s.elt_name.data for s in op.schema.ops]
    schema_types = [self.convert_datatype(s.elt_type) for s in op.schema.ops]
    result_type = RelSSA.Bag.get(schema_types, schema_names)
    new_op = RelSSA.PandasTable.get(op.table_name.data, result_type)
    rewriter.insert_op_before_matched_op(new_op)
    rewriter.erase_matched_op()


@dataclass
class AggregateRewriter(RelAlgRewriter):

  @op_type_rewrite_pattern
  def match_and_rewrite(self, op: RelAlg.Aggregate, rewriter: PatternRewriter):
    rewriter.inline_block_before_matched_op(op.input.blocks[0])
    rewriter.insert_op_before_matched_op([
        RelSSA.Aggregate.get(rewriter.added_operations_before[0],
                             [c.data for c in op.col_names.data],
                             [f.data for f in op.functions.data])
    ])
    rewriter.erase_matched_op()


#===------------------------------------------------------------------------===#
# Conversion setup
#===------------------------------------------------------------------------===#

# This pass first rewrites operators and then expressions, since the schema of
# the encompassing operator needs to be known in order to know the type of an
# expression.


def alg_to_ssa(ctx: MLContext, query: ModuleOp):
  operator_walker = PatternRewriteWalker(GreedyRewritePatternApplier(
      [PandasTableRewriter(),
       SelectRewriter(),
       AggregateRewriter()]),
                                         walk_regions_first=True,
                                         apply_recursively=True,
                                         walk_reverse=False)
  operator_walker.rewrite_module(query)
  expression_walker = PatternRewriteWalker(GreedyRewritePatternApplier([
      LiteralRewriter(),
      ColumnRewriter(),
      CompareRewriter(),
  ]),
                                           walk_regions_first=True,
                                           apply_recursively=True,
                                           walk_reverse=False)
  expression_walker.rewrite_module(query)
