"""Checks for simulator/actor interface compatibility."""

import tempfile
import unittest
from pathlib import Path

from run import check_actor_abi


class ActorAbiTest(unittest.TestCase):
    def test_actor_abi_requires_matching_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            source = checkout / "src" / "fabric_control_actor.v"
            source.parent.mkdir()
            source.write_text("// old actor\n")
            check_actor_abi(checkout, {"actor_abi": None})
            with self.assertRaisesRegex(RuntimeError, "requires protocol actor ABI 2"):
                check_actor_abi(checkout, {"actor_abi": 2})
            source.write_text("// SPATIAL_ACTOR_ABI: 2\n")
            check_actor_abi(checkout, {"actor_abi": 2})


if __name__ == "__main__":
    unittest.main()
