#!/usr/bin/env python3
# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

from xdsl.xdsl_opt_main import xDSLOptMain
from io import IOBase

from src.ibis_frontend import ibis_to_xdsl
from src.ibis_to_alg import ibis_to_alg
from src.alg_to_ssa import alg_to_ssa
from src.ssa_to_impl import ssa_to_impl
from dialects.ibis_dialect import Ibis
from dialects.rel_alg import RelationalAlg
from dialects.rel_ssa import RelSSA
from dialects.rel_impl import RelImpl


class RelOptMain(xDSLOptMain):

  def register_all_passes(self):
    self.available_passes['ibis-to-alg'] = ibis_to_alg
    self.available_passes['alg-to-ssa'] = alg_to_ssa
    self.available_passes['ssa-to-impl'] = ssa_to_impl

  def register_all_frontends(self):
    super().register_all_frontends()

    def parse_ibis(f: IOBase):
      import ibis
      import pandas as pd

      connection = ibis.pandas.connect({
          "t":
              pd.DataFrame({
                  "a": ["AS", "EU", "NA"],
                  "b": [2, 5, 4],
                  "c": [3, 1, 18]
              })
      })

      table = connection.table('t')
      query = f.read()
      res = eval(query)

      return ibis_to_xdsl(self.ctx, res)

    self.available_frontends['ibis'] = parse_ibis

  def register_all_dialects(self):
    super().register_all_dialects()
    """Register all dialects that can be used."""
    ibis = Ibis(self.ctx)
    rel_Alg = RelationalAlg(self.ctx)
    rel_ssa = RelSSA(self.ctx)
    rel_impl = RelImpl(self.ctx)


def __main__():
  rel_main = RelOptMain()
  try:
    module = rel_main.parse_input()
    rel_main.apply_passes(module)
  except Exception as e:
    print(e)
    exit(0)

  contents = rel_main.output_resulting_program(module)
  rel_main.print_to_output_stream(contents)


if __name__ == "__main__":
  __main__()
