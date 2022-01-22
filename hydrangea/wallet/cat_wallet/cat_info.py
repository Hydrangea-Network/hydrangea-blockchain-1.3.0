from dataclasses import dataclass
from typing import List, Optional, Tuple

from hydrangea.types.blockchain_format.program import Program
from hydrangea.types.blockchain_format.sized_bytes import bytes32
from hydrangea.wallet.lineage_proof import LineageProof
from hydrangea.util.streamable import Streamable, streamable


@dataclass(frozen=True)
@streamable
class CATInfo(Streamable):
    limitations_program_hash: bytes32
    my_tail: Optional[Program]  # this is the program
    lineage_proofs: List[Tuple[bytes32, Optional[LineageProof]]]  # {coin.name(): lineage_proof}
