import sys
import asyncio
from typing import Dict, Optional, List, cast, Union
import logging

from pylabrobot.liquid_handling.backends.backend import LiquidHandlerBackend
from pylabrobot.liquid_handling.errors import NoChannelError
from pylabrobot.liquid_handling.standard import (
  Pickup,
  PickupTipRack,
  Drop,
  DropTipRack,
  Aspiration,
  AspirationPlate,
  Dispense,
  DispensePlate,
  Move
)
from pylabrobot.resources import (
  Coordinate,
  ItemizedResource,
  Plate,
  Resource,
  TipRack,
  TipSpot
)
from pylabrobot.resources.opentrons_flex import OTDeck
from pylabrobot.temperature_controlling import OpentronsTemperatureModuleV2
from pylabrobot import utils

from pylabrobot.liquid_handling.liquid_handler import (
  DeckSlotMoveTo,
  StagingSlotMoveTo,
  ModuleMoveTo,
  AdapterMoveTo,
  TransferPlatformMoveTo,
  convert_move_to_types
)

from pylabrobot.resources.adapters import Adapter

logger = logging.getLogger("pylabrobot")

PYTHON_VERSION = sys.version_info[:2]

if PYTHON_VERSION in ((3, 10), (3, 11)):
  try:
    import ot_api
    USE_OT = True
  except ImportError:
    USE_OT = False
else:
  USE_OT = False

# https://github.com/Opentrons/opentrons/issues/14590
# https://forums.pylabrobot.org/t/connect-pylabrobot-to-ot2/2862/18
_OT_DECK_IS_ADDRESSABLE_AREA_VERSION = "7.1.0"


class OpentronsFlexBackend(LiquidHandlerBackend):
  """ Backend for the Opentrons Flex.
  """

  # trash container is fixed in deck slot 10
  fixed_trash_coords = {
    'x': 75.,
    'y': 390.,
    'z': 175.,
  }


  pipette_name2volume = {
    "p10_single": 10,
    "p10_multi": 10,
    "p20_single_gen2": 20,
    "p20_multi_gen2": 20,
    "p50_single": 50,
    "p50_multi": 50,
    "p300_single": 300,
    "p300_multi": 300,
    "p300_single_gen2": 300,
    "p300_multi_gen2": 300,
    "p1000_single": 1000,
    "p1000_single_gen2": 1000,
    "p300_single_gen3": 300,
    "p1000_single_gen3": 1000,
    "p1000_single_flex": 1000,
    "p1000_multi_flex": 1000
  }



  def __init__(self, host: str, port: int = 31950):
    super().__init__()

    if not USE_OT:
      raise RuntimeError("Opentrons is not installed. Please run pip install pylabrobot[opentrons]."
                         " Only supported on Python 3.10 and below.")

    self.host = host
    self.port = port

    ot_api.set_host(host)
    ot_api.set_port(port)

    self.defined_labware: Dict[str, str] = {}
    self.ot_api_version: Optional[str] = None
    self.left_pipette: Optional[Dict[str, str]] = None
    self.right_pipette: Optional[Dict[str, str]] = None

  def serialize(self) -> dict:
    return {
      **super().serialize(),
      "host": self.host,
      "port": self.port
    }

  async def setup(self):
    await super().setup()

    # create run
    run_id = await asyncio.to_thread(ot_api.runs.create)
    await asyncio.to_thread(ot_api.set_run, run_id)

    # get pipettes, then assign them
    self.left_pipette, self.right_pipette = await asyncio.to_thread(ot_api.lh.add_mounted_pipettes)

    self.left_pipette_has_tip = self.right_pipette_has_tip = False

    self.left_pipette_tip_max_vol = None
    self.right_pipette_tip_max_vol = None

    # get api version
    health = await asyncio.to_thread(ot_api.health.get)
    self.ot_api_version = health["api_version"]

  @property
  def num_channels(self) -> int:
    return len([p for p in [self.left_pipette, self.right_pipette] if p is not None])

  async def stop(self):
    self.defined_labware = {}
    await super().stop()

  def _tip_to_asp_disp_z_offset(self, max_vol: float) -> float:
    """ Get the z offset for aspirating and dispensing with a tip. """
    if max_vol == 1000.:
      return 0.0
    elif max_vol == 200.:
      return 37.0
    else:
      raise NotImplementedError(f"Unimplemented tip volume: {max_vol}")

  def _get_resource_slot(self, resource: Resource) -> str:
    """ Get the ultimate slot of a given resource. Some resources are assigned to another resource,
    such as a temperature controller, and we need to find the slot of the parent resource. Nesting
    may be deeper than one level, so we need to traverse the tree from the bottom up. """

    slot = None
    while resource.parent is not None:
      if isinstance(resource.parent, OTDeck):
        slot = cast(OTDeck, resource.parent).get_slot(resource)
        break
      resource = resource.parent
    if slot is None:
      raise ValueError("Resource not on the deck.")
    return slot



  async def assigned_resource_callback(self, resource: Resource):
    """ Called when a resource is assigned to a backend.

    Note that for Opentrons, all children to all resources on the deck are named "wells". They also
    have well-like attributes such as `displayVolumeUnits` and `totalLiquidVolume`. These seem to
    be ignored when they are not used for aspirating/dispensing.
    """

    await super().assigned_resource_callback(resource)

    if resource.name == "deck":
      return

    if cast(str, self.ot_api_version) >= _OT_DECK_IS_ADDRESSABLE_AREA_VERSION and \
      resource.name == "trash_container":
      return

    # if isinstance(resource, Adapter):
    #   await self.assigned_adapter_callback(resource)
    #   return

    if isinstance(resource, Adapter):
      slot = self.deck.get_adapter_slot(resource)
    else:
      slot = self._get_resource_slot(resource)

    # check if resource is actually a Module
    if isinstance(resource, OpentronsTemperatureModuleV2):
      await asyncio.to_thread(ot_api.modules.load_module,
        slot=slot,
        model="temperatureModuleV2",
        module_id=resource.backend.opentrons_id
      )
      # call self to assign the tube rack
      await self.assigned_resource_callback(resource.tube_rack)
      return

    well_names = [well.name for well in resource.children]
    if isinstance(resource, ItemizedResource):
      ordering = utils.reshape_2d(well_names, (resource.num_items_x, resource.num_items_y))
    else:
      ordering = [well_names]

    def _get_volume(well: Resource) -> float:
      """ Temporary hack to get the volume of the well (in ul), TODO: store in resource. """
      if isinstance(well, TipSpot):
        return well.make_tip().maximal_volume
      return well.get_size_x() * well.get_size_y() * well.get_size_z()

    # try to stick to opentrons' naming convention
    if isinstance(resource, Plate):
      display_category = "wellPlate"
    elif isinstance(resource, TipRack):
      display_category = "tipRack"
    else:
      display_category = "other"

    well_definitions = {
      child.name: {
        "depth": child.get_size_z(),
        "x": cast(Coordinate, child.location).x,
        "y": cast(Coordinate, child.location).y,
        "z": cast(Coordinate, child.location).z,
        "shape": "circular",

        # inscribed circle has diameter equal to the width of the well
        "diameter": child.get_size_x(),

        # Opentrons requires `totalLiquidVolume`, even for tip racks!
        "totalLiquidVolume": _get_volume(child),
      } for child in resource.children
    }

    format_ = "irregular" # Property to determine compatibility with multichannel pipette
    if isinstance(resource, ItemizedResource):
      if resource.num_items_x * resource.num_items_y == 96:
        format_ = "96Standard"
      elif resource.num_items_x * resource.num_items_y == 384:
        format_ = "384Standard"

    # Again, use default values and only set the real ones if applicable...
    tip_overlap: float = 0
    total_tip_length: float = 0
    if isinstance(resource, TipRack):
      tip_overlap = resource.get_tip("A1").fitting_depth
      total_tip_length = resource.get_tip("A1").total_tip_length

    lw = {
      "schemaVersion": 2,
      "version": 1,
      "namespace": "pylabrobot",
      "metadata":{
        "displayName": resource.name,
        "displayCategory": display_category,
        "displayVolumeUnits": "µL",
      },
      "brand":{
        "brand": "unknown",
      },
      "parameters":{
        "format": format_,
        "isTiprack": isinstance(resource, TipRack),
        # should we get the tip length from calibration on the robot? /calibration/tip_length
        "tipLength": total_tip_length,
        "tipOverlap": tip_overlap,
        "loadName": resource.name,
        "isMagneticModuleCompatible": False, # do we really care? If yes, store.
      },
      "ordering": ordering,
      "cornerOffsetFromSlot":{
        "x": resource.get_corner_offset_x(),
        "y": resource.get_corner_offset_y(),
        "z": resource.get_corner_offset_z(),
      },
      "dimensions":{
        "xDimension": resource.get_size_x(),
        "yDimension": resource.get_size_y(),
        "zDimension": resource.get_size_z(),
      },
      "wells": well_definitions,
      "groups": [
        {
          "wells": well_names,
          "metadata": {
            "displayName": "all wells",
            "displayCategory": display_category,
            "wellBottomShape": "flat" # TODO: get this from the resource
          },
        }
      ]
    }

    # add optional stacking labware with offset for well plates
    if isinstance(resource, Plate):
      lw['stackingOffsetWithLabware'] = {
        "opentrons_universal_flat_adapter": {
          "x": 0.0,
          "y": 0.0,
          "z": 12.0,
        },
        "opentrons_universal_flat_adapter_1": {
          "x": 0.0,
          "y": 0.0,
          "z": 12.0,
        },
        "opentrons_universal_flat_adapter_2": {
          "x": 0.0,
          "y": 0.0,
          "z": 12.0,
        },
        "opentrons_96_deep_well_adapter": {
          "x": 0.0,
          "y": 0.0,
          "z": 17.55,
        },
        "opentrons_96_deep_well_adapter_1": {
          "x": 0.0,
          "y": 0.0,
          "z": 17.55,
        },
        "opentrons_96_deep_well_adapter_2": {
            "x": 0.0,
            "y": 0.0,
            "z": 17.55,
          },
      }

    if isinstance(resource, Adapter):
      lw['allowedRoles'] = ['adapter']

    # optional labware parameters for opentrons flex gripper
    if resource.grip_force is not None:
      logger.info(f'Custom grip force found: {resource.grip_force} for resource {resource.name}')
      lw['gripForce'] = resource.grip_force
    if resource.grip_height_from_labware_bottom is not None:
      logger.info(f'Custom grip height found: {resource.grip_height_from_labware_bottom} for resource {resource.name}')
      lw['gripHeightFromLabwareBottom'] = resource.grip_height_from_labware_bottom

    data = await asyncio.to_thread(ot_api.labware.define, lw)
    namespace, definition, version = data["data"]["definitionUri"].split("/")

    # assign labware to robot
    labware_uuid = resource.name

    # handle the slot name for the opentrons api backend
    slot_obj = convert_move_to_types(slot)
    if isinstance(slot_obj, DeckSlotMoveTo):
      location = {'slotName': str(slot_obj.loc)}
    elif isinstance(slot_obj, StagingSlotMoveTo):
      location = {'addressableAreaName': slot_obj.matrix_loc}
      slot = slot_obj.matrix_loc
    elif isinstance(slot_obj, TransferPlatformMoveTo):
      location = {'slotName': str(slot_obj.loc)}
    else:
      raise ValueError(f"Unknown slot type: {slot}")

    if isinstance(resource, Adapter):
      # make sure that there is a heater shaker on the requested position
      modules = await asyncio.to_thread(ot_api.modules.list_connected_modules)
      avail_hs_modules_info = []
      for idx, mod_info in enumerate(modules):
        if mod_info.get("moduleModel") == "heaterShakerModuleV1":
          #avail_hs_modules_info.append(mod_info)
          deck_slot_matrix = mod_info.get("moduleOffset").get("slot")
          integer_deck_slot = self.convert_matrix_deck_slot_to_integer(deck_slot_matrix)
          if integer_deck_slot == slot:
            hs_id = mod_info.get("id")
            logger.info(f'Heater shaker found in slot {integer_deck_slot} with id: "{hs_id}". Assigning adapter to it.')

            await asyncio.to_thread(ot_api.labware.add,
              load_name=definition,
              namespace=namespace,
              location={'moduleId': hs_id},
              version=version,
              labware_id=labware_uuid,
              display_name=resource.name
            )
            self.deck.adapter_slots[slot-1] = resource
    else:
      try:
        integer_slot = self.convert_matrix_deck_slot_to_integer(slot)
      except TypeError:
        integer_slot = slot

      existing_slot_adapter = self.deck.adapter_slots[integer_slot-1]
      if isinstance(existing_slot_adapter, Adapter):
        # we have an adaper here, so we need to assign the labware to the adapter
        logger.info(f'Existing adapter found in slot {integer_slot}. Assigning labware {resource.name} to it.')
        await asyncio.to_thread(ot_api.labware.add,
          load_name=definition,
          namespace=namespace,
          location={'labwareId': existing_slot_adapter.name},
          labware_id=labware_uuid,
          version=version,
          display_name=resource.name
        )
      else:
        await asyncio.to_thread(ot_api.labware.add,
          load_name=definition,
          namespace=namespace,
          location=location,
          version=version,
          labware_id=labware_uuid,
          display_name=resource.name
        )

    self.defined_labware[resource.name] = labware_uuid


  @staticmethod
  def convert_matrix_deck_slot_to_integer(deck_slot_matrix: str) -> int:
    # staging area slots are treated differently than the rest of the deck
    if deck_slot_matrix in ['A4', 'B4', 'C4', 'D4']:
        return {'A4': 13, 'B4': 14, 'C4': 15, 'D4': 16}[deck_slot_matrix]
    row_mapping = {'A': 4, 'B': 3, 'C': 2, 'D': 1}
    row = deck_slot_matrix[0]
    column = int(deck_slot_matrix[1])
    return (row_mapping[row] - 1) * 3 + column



  async def unassigned_resource_callback(self, name: str):
    await super().unassigned_resource_callback(name)

    del self.defined_labware[name]

    # The OT-api does not support removing labware definitions
    # https://forums.pylabrobot.org/t/feature-request-support-unloading-labware-in-the-http-api/3098
    # instead, we move the labware off deck as a workaround
    await asyncio.to_thread(ot_api.labware.move_labware, labware_id=name, off_deck=True)

  def select_tip_pipette(self, tip_max_volume: float, with_tip: bool) -> Optional[str]:
    """ Select a pipette based on maximum tip volume for tip pick up or drop.

    The volume of the head must match the maximum tip volume. If both pipettes have the same
    maximum volume, the left pipette is selected.

    Args:
      tip_max_volume: The maximum volume of the tip.
      prefer_tip: If True, get a channel that has a tip.

    Returns:
      The id of the pipette, or None if no pipette is available.
    """

    if self.left_pipette is not None:
      left_volume_ = OpentronsFlexBackend.pipette_name2volume[self.left_pipette["name"]]
      left_vol = self.left_pipette_tip_max_vol if with_tip else tip_max_volume
      if left_vol == tip_max_volume and with_tip == self.left_pipette_has_tip:
        return cast(str, self.left_pipette["pipetteId"])

    if self.right_pipette is not None:
      right_volume_ = OpentronsFlexBackend.pipette_name2volume[self.right_pipette["name"]]
      right_vol = self.right_pipette_tip_max_vol if with_tip else tip_max_volume
      if right_vol == tip_max_volume and with_tip == self.right_pipette_has_tip:
        return cast(str, self.right_pipette["pipetteId"])

    return None


  async def pick_up_tips(self, ops: List[Pickup], use_channels: List[int], pipette=None):
    """ Pick up tips from the specified resource. """

    assert len(ops) == 1, "only one channel supported for now"
    assert use_channels == [0], "manual channel selection not supported on OT for now"
    op = ops[0] # for channel in channels
    # this feels wrong, why should backends check?
    assert op.resource.parent is not None, "must not be a floating resource"

    labware_id = self.defined_labware[op.resource.parent.name] # get name of tip rack
    tip_max_volume = op.tip.maximal_volume
    pipette_id = self.select_tip_pipette(tip_max_volume, with_tip=False)

    if pipette is not None:
      if pipette == "left":
        pipette_id= cast(str, self.left_pipette["pipetteId"])
      elif  pipette == "right":
        pipette_id = cast(str, self.right_pipette["pipetteId"])
      else:
        raise ValueError("pipette argument must be right or left")

    if not pipette_id:
      raise NoChannelError("No pipette channel of right type with no tip available.")

    if op.offset is not None:
      offset_x, offset_y, offset_z = op.offset.x, op.offset.y, op.offset.z
    else:
      offset_x = offset_y = offset_z = 0

    # ad-hoc offset adjustment that makes it smoother.
    logger.info('Using a flex z offset to pick up tips')
    offset_z+= 90

    await asyncio.to_thread(ot_api.lh.pick_up_tip, labware_id, well_name=op.resource.name, pipette_id=pipette_id,
      offset_x=offset_x, offset_y=offset_y, offset_z=offset_z)

    if pipette_id == self.left_pipette["pipetteId"]:
      self.left_pipette_has_tip = True
      self.left_pipette_tip_max_vol = op.resource.get_tip().maximal_volume
    else:
      self.right_pipette_has_tip = True
      self.right_pipette_tip_max_vol = op.resource.get_tip().maximal_volume



  async def drop_tips(self, ops: List[Drop], use_channels: List[int], pipette=None):
    """ Drop tips from the specified resource. """

    # right now we get the tip rack, and then identifier within that tip rack?
    # how do we do that with trash, assuming we don't want to have a child for the trash?

    assert len(ops) == 1 # only one channel supported for now
    assert use_channels == [0], "manual channel selection not supported on OT for now"
    op = ops[0] # for channel in channels
    # this feels wrong, why should backends check?
    assert op.resource.parent is not None, "must not be a floating resource"

    use_fixed_trash = op.resource.name == 'trash'
    if use_fixed_trash:
      labware_id = 'fixedTrash'
    else:
      labware_id = self.defined_labware[op.resource.parent.name] # get name of tip rack

    if pipette is not None:
      if pipette == "left":
        pipette_id= cast(str, self.left_pipette["pipetteId"])
      elif  pipette == "right":
        pipette_id = cast(str, self.right_pipette["pipetteId"])
      else:
        raise ValueError("pipette argument must be right or left")

    # use_fixed_trash = cast(str, self.ot_api_version) >= _OT_DECK_IS_ADDRESSABLE_AREA_VERSION and \
    #                     op.resource.name == "trash"

    tip_max_volume = op.tip.maximal_volume
    pipette_id = self.select_tip_pipette(tip_max_volume, with_tip=True)
    if not pipette_id:
      raise NoChannelError("No pipette channel of right type with tip available.")

    if op.offset is not None:
      offset_x, offset_y, offset_z = op.offset.x, op.offset.y, op.offset.z
    else:
      offset_x = offset_y = offset_z = 0

    # ad-hoc offset adjustment that makes it smoother.
    offset_z += 10

    if use_fixed_trash:
      await asyncio.to_thread(ot_api.lh.retract_pipette_z_axis, pipette_mount=pipette)
      await asyncio.to_thread(ot_api.lh.move_to_coords,
        x=self.fixed_trash_coords['x'],
        y=self.fixed_trash_coords['y'],
        z=self.fixed_trash_coords['z'],
        pipette_id=pipette_id,
      )
      await asyncio.to_thread(ot_api.lh.move_to_coords,
        x=self.fixed_trash_coords['x'],
        y=self.fixed_trash_coords['y'],
        z=self.fixed_trash_coords['z'] - 40.,
        pipette_id=pipette_id,
      )
      await asyncio.to_thread(ot_api.lh.drop_tip_in_place, pipette_id=pipette_id)
    else:
      await asyncio.to_thread(ot_api.lh.drop_tip, labware_id, well_name=op.resource.name, pipette_id=pipette_id,
        offset_x=offset_x, offset_y=offset_y, offset_z=offset_z)

    if self.left_pipette is not None and pipette_id == self.left_pipette["pipetteId"]:
      self.left_pipette_has_tip = False
      self.left_pipette_tip_max_vol = None
    else:
      self.right_pipette_has_tip = False
      self.right_pipette_tip_max_vol = None


  def select_liquid_pipette(self, volume: float) -> Optional[tuple[str, float]]:
    """ Select a pipette based on volume for an aspiration or dispense.

    The volume of the tip mounted on the head must be greater than the volume to aspirate or
    dispense. If both pipettes have the same maximum volume, the left pipette is selected.

    Only heads with a tip are considered.

    Args:
      volume: The volume to aspirate or dispense.

    Returns:
      The id of the pipette, or None if no pipette is available.
    """

    if self.left_pipette is not None:
      left_volume = OpentronsFlexBackend.pipette_name2volume[self.left_pipette["name"]]
      if left_volume >= volume and self.left_pipette_has_tip and self.left_pipette_tip_max_vol >= volume:
        return cast(str, self.left_pipette["pipetteId"]), self.left_pipette_tip_max_vol

    if self.right_pipette is not None:
      right_volume = OpentronsFlexBackend.pipette_name2volume[self.right_pipette["name"]]
      if right_volume >= volume and self.right_pipette_has_tip and self.right_pipette_tip_max_vol >= volume:
        return cast(str, self.right_pipette["pipetteId"]), self.right_pipette_tip_max_vol

    return None

  def get_pipette_name(self, pipette_id: str) -> str:
    """ Get the name of a pipette from its id. """

    if self.left_pipette is not None and pipette_id == self.left_pipette["pipetteId"]:
      return cast(str, self.left_pipette["name"])
    if self.right_pipette is not None and pipette_id == self.right_pipette["pipetteId"]:
      return cast(str, self.right_pipette["name"])
    raise ValueError(f"Unknown pipette id: {pipette_id}")

  def _get_default_aspiration_flow_rate(self, pipette_name: str) -> float:
    """ Get the default aspiration flow rate for the specified pipette.

    Data from https://archive.ph/ZUN9f

    Returns:
      The default flow rate in ul/s.
    """

    return {
      "p300_multi_gen2": 94,
      "p10_single": 5,
      "p10_multi": 5,
      "p50_single": 25,
      "p50_multi": 25,
      "p300_single": 150,
      "p300_multi": 150,
      "p1000_single": 500,

      "p20_single_gen2": 3.78,
      "p300_single_gen2": 46.43,
      "p1000_single_gen2": 137.35,
      "p20_multi_gen2": 7.6,
      "p1000_multi_flex": 500,
    }[pipette_name]

  async def aspirate(self, ops: List[Aspiration], use_channels: List[int]):
    """ Aspirate liquid from the specified resource using pip. """

    assert len(ops) == 1, "only one channel supported for now"
    assert use_channels == [0], "manual channel selection not supported on OT for now"
    op = ops[0]
    # this feels wrong, why should backends check?
    assert op.resource.parent is not None, "must not be a floating resource"


    volume = op.volume

    pipette_id, tip_max_vol = self.select_liquid_pipette(volume)
    if pipette_id is None:
      raise NoChannelError("No pipette channel of right type with tip available.")

    pipette_name = self.get_pipette_name(pipette_id)
    flow_rate = op.flow_rate or self._get_default_aspiration_flow_rate(pipette_name)

    labware_id = self.defined_labware[op.resource.parent.name]

    if op.offset is not None:
      offset_x, offset_y, offset_z = op.offset.x, op.offset.y, op.offset.z
    else:
      offset_x = offset_y = offset_z = 0

    # fixed z offset for aspirate and dispense commands with flex
    offset_z -= self._tip_to_asp_disp_z_offset(tip_max_vol)

    # Fix collisions after blowout?
    await asyncio.to_thread(ot_api.lh.move_to_well, labware_id, well_name=op.resource.name, pipette_id=pipette_id,
      offset_x=offset_x, offset_y=offset_y, offset_z=offset_z)

    await asyncio.to_thread(ot_api.lh.aspirate, labware_id, well_name=op.resource.name, pipette_id=pipette_id,
      volume=volume, flow_rate=flow_rate, offset_x=offset_x, offset_y=offset_y, offset_z=offset_z)

  def _get_default_dispense_flow_rate(self, pipette_name: str) -> float:
    """ Get the default dispense flow rate for the specified pipette.

    Data from https://archive.ph/ZUN9f

    Returns:
      The default flow rate in ul/s.
    """

    return {
      "p300_multi_gen2": 94,
      "p10_single": 10,
      "p10_multi": 10,
      "p50_single": 50,
      "p50_multi": 50,
      "p300_single": 300,
      "p300_multi": 300,
      "p1000_single": 1000,

      "p20_single_gen2": 7.56,
      "p300_single_gen2": 92.86,
      "p1000_single_gen2": 274.7,
      "p20_multi_gen2": 7.6,
      "p1000_multi_flex": 500,
    }[pipette_name]

  async def dispense(self, ops: List[Dispense], use_channels: List[int], push_outs: Optional[List[float]]=None):
    """ Dispense liquid from the specified resource using pip. """

    if push_outs is None:
        push_outs = [0.0] * len(ops)

    assert len(ops) == 1, "only one channel supported for now"
    assert len(push_outs) == len(ops), "number of push_outs must match number of ops"
    assert use_channels == [0], "manual channel selection not supported on OT for now"
    op = ops[0]
    push_out = push_outs[0]
    # this feels wrong, why should backends check?
    assert op.resource.parent is not None, "must not be a floating resource"

    volume = op.volume

    pipette_id, tip_max_vol = self.select_liquid_pipette(volume)
    if pipette_id is None:
      raise NoChannelError("No pipette channel of right type with tip available.")

    pipette_name = self.get_pipette_name(pipette_id)
    flow_rate = op.flow_rate or self._get_default_dispense_flow_rate(pipette_name)

    labware_id = self.defined_labware[op.resource.parent.name]

    if op.offset is not None:
      offset_x, offset_y, offset_z = op.offset.x, op.offset.y, op.offset.z
    else:
      offset_x = offset_y = offset_z = 0

    # fixed z dimension offsert for aspirate and dispense ops
    offset_z -= self._tip_to_asp_disp_z_offset(tip_max_vol)

    await asyncio.to_thread(ot_api.lh.dispense, labware_id, well_name=op.resource.name, pipette_id=pipette_id,
      volume=volume, flow_rate=flow_rate, offset_x=offset_x, offset_y=offset_y, offset_z=offset_z, push_out=push_out)

  async def home(self):
    """ Home the robot """
    await asyncio.to_thread(ot_api.health.home)

  async def pick_up_tips96(self, pickup: PickupTipRack):
    raise NotImplementedError("The Opentrons backend does not support the CoRe 96.")

  async def drop_tips96(self, drop: DropTipRack):
    raise NotImplementedError("The Opentrons backend does not support the CoRe 96.")

  async def aspirate96(self, aspiration: AspirationPlate):
    raise NotImplementedError("The Opentrons backend does not support the CoRe 96.")

  async def dispense96(self, dispense: DispensePlate):
    raise NotImplementedError("The Opentrons backend does not support the CoRe 96.")

  async def move_resource(self, move: Move):
    """ Move the specified lid within the robot. """
    raise NotImplementedError("Moving resources in Opentrons is not implemented yet.")


  async def move_labware(
      self,
      resource: Plate,
      to: Union[StagingSlotMoveTo, DeckSlotMoveTo, ModuleMoveTo, AdapterMoveTo, TransferPlatformMoveTo],
      pickup_offset_x: Optional[float]=0.,
      pickup_offset_y: Optional[float]=0.,
      pickup_offset_z: Optional[float]=0.,
      drop_offset_x: Optional[float]=0.,
      drop_offset_y: Optional[float]=0.,
      drop_offset_z: Optional[float]=0.,

    ) -> None:
    """ Move a labware to a specified location. """
    if isinstance(to, DeckSlotMoveTo):
      new_location = {'slotName': str(to.loc)}
    elif isinstance(to, StagingSlotMoveTo):
      new_location = {'addressableAreaName': to.matrix_loc}
    elif isinstance(to, ModuleMoveTo):
      new_location = {'moduleId': to.module_id}
    elif isinstance(to, AdapterMoveTo):
      labware_id = self.defined_labware[to.name]
      new_location = {'labwareId': labware_id}
    elif isinstance(to, TransferPlatformMoveTo):
      new_location = {'slotName': str(to.loc)}
      drop_offset_z += to.height
      drop_offset_z += 3.0 # extra z offset for buffer on transfer platform
    else:
      raise ValueError

    await asyncio.to_thread(ot_api.lh.home_gripper)

    # catch RuntimeErrors and handle them gracefully to prevent code from breaking
    try:
      # call to opentrons api to make the move
      await asyncio.to_thread(ot_api.lh.move_labware,
        labware_id=self.defined_labware[resource.name],
        new_location=new_location,
        pickup_offset_x=pickup_offset_x,
        pickup_offset_y=pickup_offset_y,
        pickup_offset_z=pickup_offset_z,
        drop_offset_x=drop_offset_x,
        drop_offset_y=drop_offset_y,
        drop_offset_z=drop_offset_z,
      )
    except RuntimeError as e:
      if str(e) == "Command timed out":
        logger.warning('Warning: moveLabware command timed out, but continuing execution.')
      else:
        raise


  async def list_connected_modules(self) -> List[dict]:
    """ List all connected temperature modules. """
    return cast(List[dict], await asyncio.to_thread(ot_api.modules.list_connected_modules))

  async def move_pipette_head(
    self,
    location: Coordinate,
    speed: Optional[float] = None,
    minimum_z_height: Optional[float] = None,
    pipette_id: Optional[str] = None,
    force_direct: bool = False
  ):
    """ Move the pipette head to the specified location. Whe a tip is mounted, the location refers
    to the bottom of the tip. If no tip is mounted, the location refers to the bottom of the
    pipette head.

    Args:
      location: The location to move to.
      speed: The speed to move at, in mm/s.
      minimum_z_height: The minimum z height to move to. Appears to be broken in the Opentrons API.
      pipette_id: The id of the pipette to move. If `"left"` or `"right"`, the left or right
        pipette is used.
      force_direct: If True, move the pipette head directly in all dimensions.
    """

    if self.left_pipette is not None and pipette_id == "left":
      pipette_id = self.left_pipette["pipetteId"]
    elif self.right_pipette is not None and pipette_id == "right":
      pipette_id = self.right_pipette["pipetteId"]

    if pipette_id is None:
      raise ValueError("No pipette id given or left/right pipette not available.")

    await asyncio.to_thread(ot_api.lh.move_arm,
      pipette_id=pipette_id,
      location_x=location.x,
      location_y=location.y,
      location_z=location.z,
      minimum_z_height=minimum_z_height,
      speed=speed,
      force_direct=force_direct
    )


  async def safe_move_gantry(self):
    """ Move the gantry to a safe position. """

    await asyncio.to_thread(ot_api.lh.move_to_coords,
      x=200.0,
      y=200.0,
      z=250.0,
      pipette_id=self.left_pipette['pipetteId'],
    )