from dataclasses import dataclass
from typing import Optional

from chia.util.ints import uint64, uint16
from chia.util.streamable import Streamable, streamable
from chia.types.spend_bundle_conditions import SpendBundleConditions


@dataclass(frozen=True)
@streamable
class NPCResult(Streamable):
    error: Optional[uint16]
    conds: Optional[SpendBundleConditions]
    cost: uint64
