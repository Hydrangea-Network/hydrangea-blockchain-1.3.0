from dataclasses import dataclass
from typing import List

from blspy import G2Element
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import SerializedProgram, INFINITE_COST
from chia.util.chain_utils import additions_for_solution, fee_for_solution
from chia.util.streamable import Streamable, streamable


@dataclass(frozen=True)
@streamable
class CoinSpend(Streamable):
    """
    This is a rather disparate data structure that validates coin transfers. It's generally populated
    with data from different sources, since burned coins are identified by name, so it is built up
    more often that it is streamed.
    """

    coin: Coin
    puzzle_reveal: SerializedProgram
    solution: SerializedProgram

    def additions(self) -> List[Coin]:
        return additions_for_solution(self.coin.name(), self.puzzle_reveal, self.solution, INFINITE_COST)

    def reserved_fee(self) -> int:
        return fee_for_solution(self.puzzle_reveal, self.solution, INFINITE_COST)

    def hints(self) -> List[bytes]:
        # import above was causing circular import issue
        from chia.full_node.mempool_check_conditions import get_name_puzzle_conditions
        from chia.consensus.default_constants import DEFAULT_CONSTANTS
        from chia.types.spend_bundle import SpendBundle
        from chia.full_node.bundle_tools import simple_solution_generator

        bundle = SpendBundle([self], G2Element())
        generator = simple_solution_generator(bundle)

        npc_result = get_name_puzzle_conditions(
            generator, INFINITE_COST, cost_per_byte=DEFAULT_CONSTANTS.COST_PER_BYTE, mempool_mode=False
        )
        assert npc_result is not None
        assert npc_result.conds is not None
        h_list = []
        for spend in npc_result.conds.spends:
            for _, _, hint in spend.create_coin:
                if hint != b"":
                    h_list.append(hint)

        return h_list
