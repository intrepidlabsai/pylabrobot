import copy
import sys
import unittest
from unittest.mock import patch

from pylabrobot.resources.opentrons_flex.deck import OTDeck
from pylabrobot.resources.opentrons_flex.load import ot_definition_to_resource


def _has_ot_api():
  return sys.version_info[:2] in ((3, 10), (3, 11))


def _tube_rack_definition() -> dict:
  wells = {
    f"{row}{col}": {
      "shape": "circular", "depth": 44.0, "diameter": 13.0, "totalLiquidVolume": 4000,
      "x": 18.88 + 18 * (col - 1), "y": 69.51 - 18 * "AB".index(row), "z": 2.0,
    }
    for col in (1, 2) for row in "AB"
  }
  return {
    "ordering": [["A1", "B1"], ["A2", "B2"]],
    "brand": {"brand": "test"},
    "metadata": {"displayName": "test rack", "displayCategory": "tubeRack",
                 "displayVolumeUnits": "mL", "tags": []},
    "dimensions": {"xDimension": 127.76, "yDimension": 85.02, "zDimension": 46.0},
    "wells": wells,
    "groups": [{"wells": list(wells), "metadata": {"wellBottomShape": "flat"}}],
    "parameters": {"format": "irregular", "quirks": [], "isTiprack": False,
                   "isMagneticModuleCompatible": False, "loadName": "test_rack"},
    "namespace": "custom_beta",
    "version": 1,
    "schemaVersion": 2,
    "cornerOffsetFromSlot": {"x": 0, "y": 0, "z": 0},
    "gripForce": 15,
    "gripHeightFromLabwareBottom": 11.2,
    "stackingOffsetWithLabware": {"opentrons_96_deep_well_adapter": {"x": 0, "y": 0, "z": 17.55}},
  }


@unittest.skipIf(not _has_ot_api(), "requires Python 3.10 or 3.11")
class OpentronsFlexSourceDefinitionTests(unittest.IsolatedAsyncioTestCase):
  """ Uploading the Opentrons definition a resource was loaded from. """

  async def _upload(self, upload_source_definitions: bool):
    from pylabrobot.liquid_handling.backends.opentrons_flex_backend import OpentronsFlexBackend

    defined = []

    def define(lw):
      defined.append(copy.deepcopy(lw))
      return {"data": {"definitionUri": f"{lw['namespace']}/{lw['parameters']['loadName']}/1"}}

    with patch("ot_api.labware.define", side_effect=define), patch("ot_api.labware.add"):
      backend = OpentronsFlexBackend(host="localhost",
                                     upload_source_definitions=upload_source_definitions)
      backend.ot_api_version = "8.0.0"
      deck = OTDeck()
      backend.set_deck(deck)
      rack = ot_definition_to_resource(_tube_rack_definition(), "rack")
      deck.assign_child_at_slot(rack, 5)
      await backend.assigned_resource_callback(rack)
    return backend, rack, defined[-1]

  async def test_uploads_source_unchanged_except_identity(self):
    _, _, lw = await self._upload(upload_source_definitions=True)
    source = _tube_rack_definition()
    self.assertEqual(lw["namespace"], "pylabrobot")
    self.assertEqual(lw["parameters"]["loadName"], "rack")
    self.assertEqual(lw["metadata"]["displayName"], "rack")
    for key in ("namespace", "parameters", "metadata"):
      lw.pop(key)
      source.pop(key)
    self.assertEqual(lw, source)

  async def test_well_names_map_to_source_wells(self):
    backend, rack, _ = await self._upload(upload_source_definitions=True)
    self.assertEqual(backend._ot_well_name(rack.get_item("B2")), "B2")

  async def test_source_wells_take_offsets_from_center(self):
    backend, rack, _ = await self._upload(upload_source_definitions=True)
    self.assertTrue(backend.offsets_from_center(rack.get_item("A1")))

  async def test_rebuilt_wells_get_center_from_liquid_handler(self):
    backend, rack, _ = await self._upload(upload_source_definitions=False)
    self.assertFalse(backend.offsets_from_center(rack.get_item("A1")))

  async def test_flag_off_rebuilds_definition(self):
    backend, rack, lw = await self._upload(upload_source_definitions=False)
    self.assertEqual(lw["parameters"]["loadName"], "rack")
    self.assertIn("rack_A1", lw["wells"])
    self.assertEqual(backend._ot_well_name(rack.get_item("A1")), "rack_A1")


if __name__ == "__main__":
  unittest.main()
