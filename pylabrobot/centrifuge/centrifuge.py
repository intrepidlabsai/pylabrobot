from typing import Optional

from pylabrobot.machines.machine import Machine

from .backend import CentrifugeBackend


class Centrifuge(Machine):
  """Frontend for a centrifuge."""

  def __init__(
    self,
    name: str,
    size_x: float,
    size_y: float,
    size_z: float,
    backend: CentrifugeBackend,
    category: str = "centrifuge",
    model: Optional[str] = None,
  ):
    super().__init__(
      name=name,
      size_x=size_x,
      size_y=size_y,
      size_z=size_z,
      backend=backend,
      category=category,
      model=model,
    )
    self.backend: CentrifugeBackend = backend  # fix type

  async def go_to_bucket1(self) -> None:
    await self.backend.go_to_bucket1()

  async def go_to_bucket2(self) -> None:
    await self.backend.go_to_bucket2()

  async def spin(self, g: float, duration: float, **backend_kwargs) -> None:
    """Start a spin cycle.

    Args:
      g: Relative centrifugal force in ×g.
      duration: Time at speed in seconds.
      **backend_kwargs: Backend-specific spin parameters (e.g. acceleration,
        deceleration as fractions of max).
    """
    await self.backend.spin(g=g, duration=duration, **backend_kwargs)
